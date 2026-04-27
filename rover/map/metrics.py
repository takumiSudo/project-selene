"""
Three-layer metrics computation for the colony map.

Layer A — Structural (networkx)
    Graph topology, transitive blast radius, cascade simulation with timing.

Layer B — Operational (metadata + logs)
    Implicit dependencies from metadata fields, historically-dissolved edges
    from log keyword scanning, per-pod composite risk scores.

Layer C — LLM Enrichment (Anthropic API, optional)
    Structured dependency signals extracted from comms and logs via a tool-use
    call that enforces a JSON schema.  Returns LLMDerivedSignal objects, never
    freeform prose.  Skipped gracefully if no API key is provided.

Public entry point:
    await compute_all_metrics(pods, edges, timeline, reconciliation_issues, api_key)
"""

from __future__ import annotations

import json
import logging
import re
from collections import deque
from datetime import datetime

import networkx as nx

from .models import (
    BlastRadiusEntry,
    CascadeSimulation,
    CascadeStep,
    CascadeSurvivor,
    ChainedCascade,
    ChainedCascadeEvent,
    CommEntry,
    DependencyEdge,
    EdgeState,
    ExtendedMetrics,
    HistoricalEdge,
    LLMDerivedSignal,
    LLMRelationshipType,
    LogEntry,
    MetadataSignal,
    MetadataSignalType,
    PodNode,
    ReconciliationIssue,
    RiskScore,
)

logger = logging.getLogger(__name__)

# ── Constants ────────────────────────────────────────────────────────────────

CRITICALITY_WEIGHT: dict[str, int] = {
    "critical": 4,
    "high": 3,
    "medium": 2,
    "low": 1,
    "unknown": 1,
}

# Resources whose loss can cause immediate life-safety consequences.
LIFE_CRITICAL_RESOURCES: set[str] = {
    "electrical_power",
    "medical_oxygen",
    "atmospheric_regulation",
    "co2_balance",
    "pharmaceuticals",
    "sterilization_water",
}

# Metadata field → hours multiplier for survival window calculation.
# e.g. backup_power_hours: 4  → 4 * 1.0 = 4 hours
#      independent_power_days: 30 → 30 * 24 = 720 hours
TIMING_METADATA: dict[str, float] = {
    "backup_power_hours":      1.0,
    "independent_power_days":  24.0,
    "oxygen_reserve_hours":    1.0,
    "pharmacy_stock_days":     24.0,
}

# Which metadata timing fields apply to which lost resource.
RESOURCE_TIMING_FIELDS: dict[str, list[str]] = {
    "electrical_power":       ["backup_power_hours", "independent_power_days"],
    "medical_oxygen":         ["oxygen_reserve_hours"],
    "pharmaceuticals":        ["pharmacy_stock_days"],
    "atmospheric_regulation": ["backup_power_hours"],
    "co2_balance":            ["backup_power_hours"],
}

# Log event detail keywords that signal a relationship was dissolved.
DISSOLUTION_KEYWORDS: list[str] = [
    "decommissioned", "rerouted", "retired", "sealed",
    "transferred", "removed", "consolidated", "simplified",
    "discontinued", "shut down",
]

# Metadata field name patterns implying a dependency on another pod.
# Each tuple: (field_regex, value_regex, resource_type)
METADATA_DEP_PATTERNS: list[tuple[str, str, str]] = [
    (r".*_loop$",    r"(aquifer|helios|nexus|zephyr|forge|terminus)",   "loop_dependency"),
    (r".*_source$",  r"(aquifer|helios|nexus|zephyr|forge|terminus)",   "source_dependency"),
    (r".*_feed.*",   r"(aquifer|helios|nexus|zephyr|forge|terminus)",   "feed_dependency"),
    (r"coolant_.*",  r"(aquifer)",                                        "coolant_water"),
]

# Metadata field patterns indicating resilience (independent capability).
RESILIENCE_PATTERNS: list[str] = [
    r"independent_.*",
    r".*_reserve_.*",
    r"ice_harvest.*",
]

# Metadata backup fields — zero value means no redundancy.
BACKUP_ZERO_PATTERNS: list[str] = [
    r"backup_systems",
    r"backup_.*_count",
    r"redundant_.*",
]


# ── Graph construction ────────────────────────────────────────────────────────

def _build_graph(
    pods: dict[str, PodNode],
    edges: list[DependencyEdge],
) -> nx.DiGraph:
    """Build a weighted DiGraph.  Edge direction: source → target (dependent → supplier)."""
    G = nx.DiGraph()
    for pod_id in pods:
        G.add_node(pod_id, name=pods[pod_id].display_name)
    for e in edges:
        G.add_edge(
            e.source, e.target,
            resource=e.resource,
            criticality=e.criticality.value,
            weight=CRITICALITY_WEIGHT.get(e.criticality.value, 1),
        )
    return G


# ─────────────────────────────────────────────────────────────────────────────
# LAYER A — Structural
# ─────────────────────────────────────────────────────────────────────────────

def _compute_structural(
    pods: dict[str, PodNode],
    edges: list[DependencyEdge],
) -> dict:
    """Run networkx structural analysis.  Returns a dict of field values."""
    G = _build_graph(pods, edges)
    R = G.reverse(copy=True)   # R: supplier → dependent (for blast-radius BFS)

    # ── Basic degree ─────────────────────────────────────────────────────
    in_deg  = {n: G.in_degree(n)  for n in G.nodes()}   # pods that depend ON n
    out_deg = {n: G.out_degree(n) for n in G.nodes()}   # pods n depends ON

    # ── Centrality ───────────────────────────────────────────────────────
    betweenness  = nx.betweenness_centrality(G, normalized=True, weight="weight")
    in_centrality = nx.in_degree_centrality(G)

    # ── Articulation points (undirected view) ────────────────────────────
    # A pod whose removal disconnects the undirected graph.
    uG = G.to_undirected()
    art_points = list(nx.articulation_points(uG))

    # ── Strongly connected components ────────────────────────────────────
    sccs = [list(c) for c in nx.strongly_connected_components(G) if len(c) > 1]

    # ── Longest path ─────────────────────────────────────────────────────
    try:
        longest = nx.dag_longest_path(G)
    except nx.NetworkXUnfeasible:
        longest = []   # graph has cycles — SCC detected above

    # ── Transitive blast radius ──────────────────────────────────────────
    blast_radius: dict[str, BlastRadiusEntry] = {}
    for pod_id in pods:
        # BFS in the reverse graph from pod_id → reaches all transitive dependents
        affected = set(nx.bfs_tree(R, pod_id).nodes()) - {pod_id}
        subgraph_nodes = affected | {pod_id}
        sub = G.subgraph(subgraph_nodes)
        weighted = float(sum(
            CRITICALITY_WEIGHT.get(data.get("criticality", "unknown"), 1)
            for _, _, data in sub.edges(data=True)
        ))
        blast_radius[pod_id] = BlastRadiusEntry(
            pod_id=pod_id,
            blast_radius_pods=sorted(affected),
            blast_radius_count=len(affected),
            blast_radius_weighted=weighted,
        )

    # ── Rankings ─────────────────────────────────────────────────────────
    highest_blast = sorted(
        blast_radius, key=lambda p: blast_radius[p].blast_radius_count, reverse=True
    )

    return dict(
        in_degree=in_deg,
        out_degree=out_deg,
        betweenness_centrality={k: round(v, 4) for k, v in betweenness.items()},
        in_degree_centrality={k: round(v, 4) for k, v in in_centrality.items()},
        articulation_points=art_points,
        strongly_connected_components=sccs,
        longest_path=longest,
        blast_radius=blast_radius,
        highest_blast_radius=highest_blast,
        G=G,   # passed to later layers, removed before building ExtendedMetrics
    )


# ─────────────────────────────────────────────────────────────────────────────
# CASCADE SIMULATION — runs after Layers A+B so it can use risk + metadata + logs
# ─────────────────────────────────────────────────────────────────────────────

# Pods marked as survivors must show buffer >= this many hours past the cascade
# front-line failure window.  One week ≈ "the colony is in crisis anyway."
_SURVIVOR_BUFFER_HOURS = 7 * 24

# Maximum cascade triggers to simulate per run (avoid combinatorial output).
_MAX_TRIGGERS = 4


def _compute_cascades(
    pods: dict[str, PodNode],
    edges: list[DependencyEdge],
    structural: dict,
    metadata_signals: list[MetadataSignal],
    historical_edges: list[HistoricalEdge],
    risk_scores: dict[str, RiskScore],
) -> list[CascadeSimulation]:
    """Multi-layer cascade simulation rooted at corroborated SPOFs.

    Differs from the old articulation-point-only approach in four ways:
      1. Triggers are picked from corroborated risk_scores, not just APs.
      2. Only operational edges (RECONCILED + DEP_ONLY) traverse the graph.
         SUPPLY_ONLY admin oversight is filtered out.
      3. SCC membership flags compound failures — a trigger that depends on
         one of its own dependents (mutual destruction loop, e.g. aquifer↔helios).
      4. Survivors are classified by metadata resilience markers, not just
         graph reachability.
    """
    triggers = _select_cascade_triggers(structural, risk_scores)
    if not triggers:
        return []

    G_ops = _build_operational_graph(pods, edges)
    R_ops = G_ops.reverse(copy=True)
    sccs = list(nx.strongly_connected_components(G_ops))

    simulations: list[CascadeSimulation] = []
    for trigger in triggers:
        if trigger not in G_ops:
            continue
        sim = _simulate_one_cascade(
            trigger=trigger,
            pods=pods,
            edges=edges,
            G_ops=G_ops,
            R_ops=R_ops,
            sccs=sccs,
            structural=structural,
            metadata_signals=metadata_signals,
            historical_edges=historical_edges,
            risk_scores=risk_scores,
        )
        # Skip cascades that affect nothing operationally — these triggers were
        # ranked highly by formal blast_radius (which counts admin oversight)
        # but have no operational dependents.  Reporting them is just noise.
        if sim.total_pods_affected == 0 and not sim.compound_dependents:
            logger.debug("Cascade [%s] skipped — no operational dependents", trigger)
            continue
        simulations.append(sim)

    return simulations


def _select_cascade_triggers(
    structural: dict,
    risk_scores: dict[str, RiskScore],
) -> list[str]:
    """Pick cascade roots: APs first, then corroborated SPOFs, then top-1 by risk."""
    triggers: list[str] = list(dict.fromkeys(structural.get("articulation_points", [])))

    by_risk = sorted(
        risk_scores.values(), key=lambda r: r.overall_risk, reverse=True
    )

    # Pods with >= 2 independent SPOF signals
    for rs in by_risk:
        if rs.spof_corroboration_count >= 2 and rs.pod_id not in triggers:
            triggers.append(rs.pod_id)

    # Always include the top-ranked pod even if corroboration is weak
    if by_risk and by_risk[0].pod_id not in triggers:
        triggers.insert(0, by_risk[0].pod_id)

    # Include any pod with blast >= 0.7 if we still have room (catches obvious SPOFs
    # that don't quite hit corroboration=2 because they have no metadata flag)
    for rs in by_risk:
        if rs.blast_radius_score >= 0.7 and rs.pod_id not in triggers:
            triggers.append(rs.pod_id)
        if len(triggers) >= _MAX_TRIGGERS:
            break

    return triggers[:_MAX_TRIGGERS]


def _build_operational_graph(
    pods: dict[str, PodNode],
    edges: list[DependencyEdge],
) -> nx.DiGraph:
    """Like _build_graph but keeps only operational edges (RECONCILED + DEP_ONLY).

    SUPPLY_ONLY edges are administrative oversight (e.g. Artemis claims to supply
    `administrative_oversight` to all pods) and inflate the blast radius without
    representing an actual operational dependency.  Filtering them gives the
    cascade a faithful view of resource flow.
    """
    G = nx.DiGraph()
    for pod_id in pods:
        G.add_node(pod_id, name=pods[pod_id].display_name)
    for e in edges:
        if e.state == EdgeState.SUPPLY_ONLY:
            continue
        G.add_edge(
            e.source, e.target,
            resource=e.resource,
            criticality=e.criticality.value,
            weight=CRITICALITY_WEIGHT.get(e.criticality.value, 1),
        )
    return G


def _simulate_one_cascade(
    *,
    trigger: str,
    pods: dict[str, PodNode],
    edges: list[DependencyEdge],
    G_ops: nx.DiGraph,
    R_ops: nx.DiGraph,
    sccs: list[set[str]],
    structural: dict,
    metadata_signals: list[MetadataSignal],
    historical_edges: list[HistoricalEdge],
    risk_scores: dict[str, RiskScore],
) -> CascadeSimulation:
    """Build one CascadeSimulation rooted at `trigger`."""
    # ── Determine trigger context ────────────────────────────────────────
    trigger_scc = next((c for c in sccs if trigger in c and len(c) > 1), set())
    direct_deps = sorted(R_ops.successors(trigger))

    # ── Reverse-BFS to find affected pods + hop ──────────────────────────
    hop_of: dict[str, int] = {trigger: 0}
    queue: deque[str] = deque([trigger])
    while queue:
        current = queue.popleft()
        for dep in R_ops.successors(current):
            if dep not in hop_of:
                hop_of[dep] = hop_of[current] + 1
                queue.append(dep)

    affected = {p for p in hop_of if p != trigger}

    # ── Cumulative time per pod (fixed-point relaxation) ─────────────────
    cumulative, via = _compute_cumulative_times(trigger, pods, G_ops, R_ops, affected)

    # ── Classify survivors before building steps ─────────────────────────
    survivors = _classify_survivors(
        trigger=trigger,
        affected=affected,
        all_pods=pods,
        edges=edges,
        metadata_signals=metadata_signals,
        cumulative=cumulative,
    )
    survivor_ids = {s.pod_id for s in survivors}

    # ── Build cascade steps for affected non-survivors ───────────────────
    steps: list[CascadeStep] = []
    for pod_id in affected:
        if pod_id in survivor_ids:
            continue
        # Prefer the path that produced the cumulative time — this keeps
        # failure_mode + lost_resource consistent with cumulative_hours.
        # Fall back to graph shortest path when timing is unknown.
        if via.get(pod_id):
            supplier, resource = via[pod_id]
        else:
            resource, supplier = _resource_lost_via(G_ops, pod_id, trigger)
        window = _survival_window(pods.get(pod_id), resource)
        evidence = _timing_evidence(pods.get(pod_id), resource)
        steps.append(CascadeStep(
            pod_id=pod_id,
            failure_mode=f"loses '{resource}' from {supplier}",
            hop=hop_of.get(pod_id, 1),
            estimated_window_hours=window,
            cumulative_hours=cumulative.get(pod_id),
            evidence_source=evidence,
            is_life_critical=resource in LIFE_CRITICAL_RESOURCES,
            is_compound=pod_id in trigger_scc,
            immediate_supplier=supplier,
            lost_resource=resource,
        ))

    steps.sort(key=lambda s: (s.hop, s.cumulative_hours if s.cumulative_hours is not None else 1e9, s.pod_id))

    # ── Aggregate timings ────────────────────────────────────────────────
    life_critical_cums = [
        s.cumulative_hours for s in steps
        if s.is_life_critical and s.cumulative_hours is not None
    ]
    all_cums = [s.cumulative_hours for s in steps if s.cumulative_hours is not None]
    time_to_life_critical = min(life_critical_cums) if life_critical_cums else None
    time_to_colony_wide = max(all_cums) if all_cums else None

    # ── Corroboration signals ────────────────────────────────────────────
    corroboration = _trigger_corroboration(
        trigger=trigger,
        pods=pods,
        structural=structural,
        metadata_signals=metadata_signals,
        historical_edges=historical_edges,
        risk_scores=risk_scores,
        affected_count=len(steps),
    )

    compound_pods = sorted(p for p in affected if p in trigger_scc and p not in survivor_ids)

    return CascadeSimulation(
        trigger_pod=trigger,
        trigger_reason=_trigger_reason(trigger, structural, risk_scores),
        corroboration_signals=corroboration,
        direct_dependents=direct_deps,
        compound_dependents=compound_pods,
        steps=steps,
        survivors=survivors,
        total_pods_affected=len(steps),
        time_to_life_critical_hours=time_to_life_critical,
        time_to_colony_wide_hours=time_to_colony_wide,
    )


def _compute_cumulative_times(
    trigger: str,
    pods: dict[str, PodNode],
    G_ops: nx.DiGraph,
    R_ops: nx.DiGraph,
    affected: set[str],
) -> tuple[dict[str, float | None], dict[str, tuple[str, str] | None]]:
    """Fixed-point relaxation of cumulative survival time from T=0.

    For each affected pod, the cumulative time = min over all parent paths of
    (parent.cumulative + this_pod.window_for_lost_resource).  None means
    the cumulative is unknown along every path (no metadata buffer).

    Returns (cumulative, via) where via[pod_id] = (supplier, resource) for the
    path that produced the minimum cumulative — used to label failure_mode
    consistently with the timing.
    """
    cumulative: dict[str, float | None] = {trigger: 0.0}
    via: dict[str, tuple[str, str] | None] = {trigger: None}
    for pod_id in affected:
        cumulative[pod_id] = None
        via[pod_id] = None

    # Relax until no changes (bounded by node count for a connected component)
    for _ in range(len(affected) + 2):
        changed = False
        for pod_id in affected:
            best: float | None = cumulative[pod_id]
            best_via: tuple[str, str] | None = via[pod_id]
            # For each upstream parent (in G_ops, this pod points TO its supplier)
            for supplier in G_ops.successors(pod_id):
                if supplier not in cumulative:
                    continue
                parent_cum = cumulative[supplier]
                if parent_cum is None:
                    continue
                resource = G_ops[pod_id][supplier].get("resource", "unknown")
                window = _survival_window(pods.get(pod_id), resource)
                if window is None:
                    continue
                candidate = parent_cum + window
                if best is None or candidate < best:
                    best = candidate
                    best_via = (supplier, resource)
            if best != cumulative[pod_id]:
                cumulative[pod_id] = best
                via[pod_id] = best_via
                changed = True
        if not changed:
            break

    return cumulative, via


def _classify_survivors(
    *,
    trigger: str,
    affected: set[str],
    all_pods: dict[str, PodNode],
    edges: list[DependencyEdge],
    metadata_signals: list[MetadataSignal],
    cumulative: dict[str, float | None],
) -> list[CascadeSurvivor]:
    """Pods that escape the cascade — either no path from trigger OR enough buffer.

    Type 1 — Independent: no operational edge path from the trigger reaches them.
    Type 2 — Resilient: reachable but only via low/unknown-criticality edges and
             holds a RESILIENCE_MARKER buffer >= _SURVIVOR_BUFFER_HOURS.
    """
    resilience_by_pod: dict[str, list[MetadataSignal]] = {}
    for sig in metadata_signals:
        if sig.signal_type == MetadataSignalType.RESILIENCE_MARKER:
            resilience_by_pod.setdefault(sig.pod_id, []).append(sig)

    survivors: list[CascadeSurvivor] = []

    for pod_id in all_pods:
        if pod_id == trigger:
            continue

        if pod_id not in affected:
            evidence = [f"no operational dependency path from {trigger}"]
            for sig in resilience_by_pod.get(pod_id, []):
                evidence.append(f"metadata:{sig.field}={sig.value}")
            survivors.append(CascadeSurvivor(
                pod_id=pod_id,
                reason="independent — no operational path from trigger",
                evidence=evidence,
            ))
            continue

        # Type 2: reachable, but resilient enough to survive the cascade window
        resilience = resilience_by_pod.get(pod_id)
        if not resilience:
            continue

        out_edges = [
            e for e in edges
            if e.source == pod_id and e.state != EdgeState.SUPPLY_ONLY
            and (e.target == trigger or e.target in affected)
        ]
        if not out_edges:
            continue

        all_low = all(e.criticality.value in ("low", "unknown") for e in out_edges)
        cum = cumulative.get(pod_id)
        survives_long = cum is None or cum >= _SURVIVOR_BUFFER_HOURS
        if not (all_low and survives_long):
            continue

        targets = ",".join(sorted({e.target for e in out_edges}))
        evidence = [f"only low-criticality dep into cascade ({targets})"]
        for sig in resilience:
            evidence.append(f"metadata:{sig.field}={sig.value}")
        if cum is not None:
            evidence.append(f"cumulative_survival={cum:.0f}h")
        survivors.append(CascadeSurvivor(
            pod_id=pod_id,
            reason="resilient — buffer exceeds cascade window",
            evidence=evidence,
        ))

    return survivors


# ─────────────────────────────────────────────────────────────────────────────
# CHAINED CASCADES — primary trigger + secondary trigger chaining (pessimistic)
# ─────────────────────────────────────────────────────────────────────────────

# Inference parameters used when metadata cannot supply a survival window.
# Keep this list short and well-documented — every entry is a synthetic timing
# that the reporter must cite explicitly.
_DEFAULT_INFERENCE_PARAMS: dict[str, float] = {
    # Helios battery thermal regulation depends on Aquifer coolant. There is no
    # battery_thermal_hours field on Helios; 48h is the operational rule of
    # thumb backed by helios.coolant_loop="aquifer-primary" and the
    # 2094-02-14 backup-coolant decommission log.
    "helios_coolant_degradation_hours": 48.0,
}

# Map (resource_lost, secondary_trigger_pod) → inference parameter key.
# Used only when the primary cascade has no metadata-based timing for that
# secondary trigger pod.
_INFERENCE_RULES: list[tuple[str, str, str]] = [
    ("coolant_water", "helios", "helios_coolant_degradation_hours"),
]


def _compute_chained_cascades(
    pods: dict[str, PodNode],
    cascade_simulations: list[CascadeSimulation],
    inference_params: dict[str, float],
) -> list[ChainedCascade]:
    """Chain primary cascades with their secondary triggers' cascades.

    For each primary cascade, identify any pod in its affected set that is
    itself a cascade trigger. Compute the secondary trigger's failure time
    (from primary cascade metadata if available, otherwise from inference
    rules), then graft the secondary's cascade events into the primary
    timeline using the **pessimistic chaining model**:

        chained.cumulative_hours = secondary_offset + pod.own_buffer

    Production is assumed to halt the moment the supplier fails — a downstream
    pod does NOT get an extension equal to its supplier's own backup window.
    See Decision 26 for the rationale.
    """
    sim_by_trigger = {s.trigger_pod: s for s in cascade_simulations}
    chained: list[ChainedCascade] = []

    for primary in cascade_simulations:
        # Find secondary triggers in primary's affected set
        secondary_triggers: list[str] = [
            step.pod_id for step in primary.steps
            if step.pod_id in sim_by_trigger and step.pod_id != primary.trigger_pod
        ]

        events: list[ChainedCascadeEvent] = []
        events_by_pod: dict[str, ChainedCascadeEvent] = {}

        # ── Step 1: copy primary cascade events with their own cumulative_hours
        for step in primary.steps:
            evt = ChainedCascadeEvent(
                pod_id=step.pod_id,
                cumulative_hours=step.cumulative_hours,
                lost_resource=step.lost_resource,
                immediate_supplier=step.immediate_supplier,
                failure_mode=step.failure_mode,
                via_primary=True,
                inference_confidence=("metadata" if step.cumulative_hours is not None else "unknown"),
                is_life_critical=step.is_life_critical,
                is_compound=step.is_compound,
            )
            events.append(evt)
            events_by_pod[step.pod_id] = evt

        # ── Step 2: for each secondary trigger, compute offset and graft
        for sec_trigger in secondary_triggers:
            sec_sim = sim_by_trigger[sec_trigger]
            offset, offset_evidence = _secondary_trigger_offset(
                sec_trigger, primary, inference_params,
            )

            # Update the secondary trigger's own event with the offset (if better)
            sec_event = events_by_pod.get(sec_trigger)
            if sec_event and offset is not None:
                if sec_event.cumulative_hours is None or offset < sec_event.cumulative_hours:
                    sec_event.cumulative_hours = offset
                    sec_event.via_secondary_trigger = sec_trigger
                    sec_event.secondary_offset_hours = offset
                    sec_event.secondary_offset_evidence = offset_evidence
                    sec_event.inference_confidence = (
                        "inferred" if offset_evidence.startswith("inferred:") else "metadata"
                    )

            # Graft each secondary cascade step using pessimistic chaining
            for sec_step in sec_sim.steps:
                if sec_step.pod_id == primary.trigger_pod:
                    continue   # don't chain back into the primary trigger

                own_buffer = _survival_window(pods.get(sec_step.pod_id), sec_step.lost_resource)
                if offset is None or own_buffer is None:
                    chained_cum: float | None = None
                else:
                    chained_cum = offset + own_buffer

                existing = events_by_pod.get(sec_step.pod_id)
                if existing is None:
                    new_evt = ChainedCascadeEvent(
                        pod_id=sec_step.pod_id,
                        cumulative_hours=chained_cum,
                        lost_resource=sec_step.lost_resource,
                        immediate_supplier=sec_step.immediate_supplier,
                        failure_mode=sec_step.failure_mode,
                        via_primary=False,
                        via_secondary_trigger=sec_trigger,
                        secondary_offset_hours=offset,
                        secondary_offset_evidence=offset_evidence,
                        inference_confidence=_inference_confidence(chained_cum, offset_evidence),
                        is_life_critical=sec_step.is_life_critical,
                        is_compound=sec_step.is_compound,
                    )
                    events.append(new_evt)
                    events_by_pod[sec_step.pod_id] = new_evt
                else:
                    # Take the earlier failure time (and its supporting attribution)
                    if chained_cum is not None and (
                        existing.cumulative_hours is None or chained_cum < existing.cumulative_hours
                    ):
                        existing.cumulative_hours = chained_cum
                        existing.lost_resource = sec_step.lost_resource
                        existing.immediate_supplier = sec_step.immediate_supplier
                        existing.failure_mode = sec_step.failure_mode
                        existing.via_secondary_trigger = sec_trigger
                        existing.secondary_offset_hours = offset
                        existing.secondary_offset_evidence = offset_evidence
                        existing.inference_confidence = _inference_confidence(chained_cum, offset_evidence)
                        existing.is_life_critical = existing.is_life_critical or sec_step.is_life_critical

        # Sort: timed events first by time, untimed last by pod_id
        events.sort(key=lambda e: (
            e.cumulative_hours if e.cumulative_hours is not None else 1e9,
            e.pod_id,
        ))

        timed = [e.cumulative_hours for e in events if e.cumulative_hours is not None]
        life_critical_timed = [
            e.cumulative_hours for e in events
            if e.is_life_critical and e.cumulative_hours is not None
        ]

        chained.append(ChainedCascade(
            primary_trigger=primary.trigger_pod,
            secondary_triggers=sorted(secondary_triggers),
            inference_parameters=dict(inference_params),
            events=events,
            survivors=primary.survivors,
            time_to_life_critical_hours=min(life_critical_timed) if life_critical_timed else None,
            time_to_colony_wide_hours=max(timed) if timed else None,
        ))

    return chained


def _secondary_trigger_offset(
    secondary_trigger: str,
    primary_sim: CascadeSimulation,
    inference_params: dict[str, float],
) -> tuple[float | None, str]:
    """When does the secondary trigger fail in the primary cascade timeline?

    Priority:
      1. Primary cascade has cumulative_hours for this pod from metadata → use it
      2. Resource-specific inference rule applies → use that
      3. Return None
    """
    primary_step = next(
        (s for s in primary_sim.steps if s.pod_id == secondary_trigger), None,
    )
    if primary_step is None:
        return None, ""

    # Case 1: metadata-derived
    if primary_step.cumulative_hours is not None:
        evidence = (
            f"metadata:{primary_step.evidence_source}"
            if primary_step.evidence_source else "metadata"
        )
        return primary_step.cumulative_hours, evidence

    # Case 2: inference rule
    for resource, sec_pod, param_key in _INFERENCE_RULES:
        if primary_step.lost_resource == resource and secondary_trigger == sec_pod:
            hours = inference_params.get(param_key)
            if hours is not None:
                return float(hours), f"inferred:{param_key}={hours}"

    return None, ""


def _inference_confidence(value: float | None, evidence: str) -> str:
    if value is None:
        return "unknown"
    if evidence.startswith("inferred:"):
        return "inferred"
    return "metadata"


def _resource_lost_via(
    G_ops: nx.DiGraph, pod_id: str, trigger: str,
) -> tuple[str, str]:
    """Return (resource, immediate_supplier) for a pod in the cascade.

    For direct dependents, return the edge resource and the trigger as supplier.
    For multi-hop dependents, walk the shortest path toward the trigger and
    return the resource on the first hop (the resource this pod *immediately* loses).
    """
    if G_ops.has_edge(pod_id, trigger):
        data = G_ops[pod_id][trigger]
        return data.get("resource", "unknown"), trigger
    try:
        path = nx.shortest_path(G_ops, pod_id, trigger)
        if len(path) >= 2:
            supplier = path[1]
            data = G_ops[pod_id].get(supplier, {})
            return data.get("resource", "unknown"), supplier
    except (nx.NetworkXNoPath, nx.NodeNotFound):
        pass
    return "unknown", trigger


def _trigger_reason(
    trigger: str, structural: dict, risk_scores: dict[str, RiskScore],
) -> str:
    if trigger in structural.get("articulation_points", []):
        return f"Articulation point — removing {trigger} disconnects the colony graph"
    rs = risk_scores.get(trigger)
    if rs:
        return (
            f"Highest corroborated SPOF — overall_risk={rs.overall_risk:.2f}, "
            f"corroboration={rs.spof_corroboration_count}/4 independent signals"
        )
    return f"Selected by blast radius: {trigger}"


def _trigger_corroboration(
    *,
    trigger: str,
    pods: dict[str, PodNode],
    structural: dict,
    metadata_signals: list[MetadataSignal],
    historical_edges: list[HistoricalEdge],
    risk_scores: dict[str, RiskScore],
    affected_count: int,
) -> list[str]:
    """Human-readable list of independent signals that flag `trigger` as critical."""
    signals: list[str] = []

    if trigger in structural.get("articulation_points", []):
        signals.append("articulation_point (graph disconnects without it)")

    no_backup = [
        s for s in metadata_signals
        if s.pod_id == trigger and s.signal_type == MetadataSignalType.NO_BACKUP
    ]
    for sig in no_backup:
        signals.append(f"no_backup metadata: {sig.field}={sig.value}")

    hist = [
        h for h in historical_edges
        if h.source == trigger or h.target == trigger
    ]
    if hist:
        signals.append(f"{len(hist)} historical dissolutions (redundancy stripped over time)")

    rs = risk_scores.get(trigger)
    if rs and rs.blast_radius_score > 0:
        total_pods = max(len(pods) - 1, 1)
        signals.append(
            f"blast_radius={rs.blast_radius_score:.0%} "
            f"(operational cascade reaches {affected_count}/{total_pods})"
        )

    return signals


def _survival_window(pod: PodNode | None, resource: str) -> float | None:
    """Return estimated hours a pod can survive losing `resource`, from metadata."""
    if not pod or not pod.info:
        return None
    meta = pod.info.metadata
    for field in RESOURCE_TIMING_FIELDS.get(resource, []):
        val = meta.get(field)
        if isinstance(val, (int, float)) and val > 0:
            return float(val) * TIMING_METADATA.get(field, 1.0)
    return None


def _timing_evidence(pod: PodNode | None, resource: str) -> str:
    if not pod or not pod.info:
        return ""
    meta = pod.info.metadata
    parts = []
    for field in RESOURCE_TIMING_FIELDS.get(resource, []):
        if field in meta:
            parts.append(f"metadata:{field}={meta[field]}")
    return ", ".join(parts) if parts else "no timing data in metadata"


# ─────────────────────────────────────────────────────────────────────────────
# LAYER B — Operational
# ─────────────────────────────────────────────────────────────────────────────

def _compute_operational(
    pods: dict[str, PodNode],
    edges: list[DependencyEdge],
    timeline: list[LogEntry | CommEntry],
    structural: dict,
) -> dict:
    metadata_signals = _extract_metadata_signals(pods, edges)
    historical_edges = _detect_historical_dissolutions(timeline, pods, edges)
    risk_scores = _compute_risk_scores(pods, structural, metadata_signals, historical_edges)

    most_vulnerable = sorted(
        risk_scores,
        key=lambda p: risk_scores[p].vulnerability_score,
        reverse=True,
    )
    highest_risk = sorted(
        risk_scores,
        key=lambda p: risk_scores[p].overall_risk,
        reverse=True,
    )

    return dict(
        metadata_signals=metadata_signals,
        historical_edges=historical_edges,
        risk_scores=risk_scores,
        most_vulnerable=most_vulnerable,
        highest_overall_risk=highest_risk,
    )


def _extract_metadata_signals(
    pods: dict[str, PodNode],
    edges: list[DependencyEdge],
) -> list[MetadataSignal]:
    """Scan each pod's metadata for implicit deps, backup gaps, and resilience markers."""
    signals: list[MetadataSignal] = []
    known_pod_ids = set(pods.keys())
    dissolved_targets = {(e.source, e.target) for e in edges if not e.reconciled}

    for pod in pods.values():
        if not pod.info:
            continue
        meta = pod.info.metadata

        for field, value in meta.items():
            field_lower = field.lower()
            value_str = str(value).lower()

            # ── Pattern 1: backup field with zero value ───────────────────
            if any(re.match(pat, field_lower) for pat in BACKUP_ZERO_PATTERNS):
                if value in (0, 0.0, False, "0", "false", "none", ""):
                    signals.append(MetadataSignal(
                        pod_id=pod.id,
                        field=field,
                        value=value,
                        signal_type=MetadataSignalType.NO_BACKUP,
                        description=(
                            f"{pod.display_name}: {field}={value} — "
                            f"no redundancy for this resource category"
                        ),
                    ))

            # ── Pattern 2: *_source / *_loop / *_feed → implied dep ───────
            for pat, val_pat, resource in METADATA_DEP_PATTERNS:
                if re.match(pat, field_lower) and re.search(val_pat, value_str):
                    # Extract target pod name from value (e.g. "aquifer-primary" → "aquifer")
                    target = _pod_name_from_value(value_str, known_pod_ids)
                    if target and target != pod.id:
                        # Is this already in the formal graph?
                        formally_declared = any(
                            e.source == pod.id and e.target == target
                            for e in edges
                        )
                        # Is this potentially stale (a dissolved edge exists)?
                        stale = (pod.id, target) in dissolved_targets
                        signals.append(MetadataSignal(
                            pod_id=pod.id,
                            field=field,
                            value=value,
                            signal_type=(
                                MetadataSignalType.STALE_REFERENCE
                                if stale else MetadataSignalType.IMPLIED_DEPENDENCY
                            ),
                            implied_dep_target=target,
                            implied_dep_resource=resource,
                            description=(
                                f"{pod.display_name}: metadata field '{field}={value}' "
                                f"implies dependency on {target} for {resource}"
                                + (" [STALE — dissolved in logs]" if stale else "")
                                + ("" if formally_declared else " [NOT in formal /dependencies]")
                            ),
                        ))

            # ── Pattern 3: resilience markers ────────────────────────────
            if any(re.match(pat, field_lower) for pat in RESILIENCE_PATTERNS):
                if isinstance(value, (int, float)) and value > 0:
                    signals.append(MetadataSignal(
                        pod_id=pod.id,
                        field=field,
                        value=value,
                        signal_type=MetadataSignalType.RESILIENCE_MARKER,
                        description=(
                            f"{pod.display_name}: {field}={value} — "
                            f"pod has independent capability reducing upstream risk"
                        ),
                    ))

    logger.info("Metadata signals: %d extracted across %d pods", len(signals), len(pods))
    return signals


def _pod_name_from_value(value_str: str, known_ids: set[str]) -> str | None:
    """Extract a pod_id from a metadata value string like 'aquifer-primary'."""
    for pod_id in known_ids:
        if pod_id in value_str:
            return pod_id
    return None


def _detect_historical_dissolutions(
    timeline: list[LogEntry | CommEntry],
    pods: dict[str, PodNode],
    edges: list[DependencyEdge],
) -> list[HistoricalEdge]:
    """Scan the timeline for log entries describing dissolved relationships."""
    historical: list[HistoricalEdge] = []
    pod_ids = set(pods.keys())
    pod_name_to_id = {
        p.display_name.lower(): p.id for p in pods.values() if p.info
    }

    for entry in timeline:
        if not isinstance(entry, LogEntry):
            continue
        detail_lower = entry.detail.lower()

        # Must contain a dissolution keyword
        if not any(kw in detail_lower for kw in DISSOLUTION_KEYWORDS):
            continue

        # Find all other pods mentioned in the log text
        mentioned: set[str] = set()
        for pid in pod_ids:
            if pid != entry.pod_id and pid in detail_lower:
                mentioned.add(pid)
        for name_lower, pid in pod_name_to_id.items():
            if pid != entry.pod_id and name_lower in detail_lower:
                mentioned.add(pid)

        if not mentioned:
            continue

        for other_pod in mentioned:
            resource = _infer_resource_from_text(detail_lower)
            # Determine direction: who was the supplier?
            # Heuristic: the pod writing the log is usually the one that HAD the dep.
            source, target = entry.pod_id, other_pod

            # Check if this relationship still exists formally (= stale declaration)
            still_declared = any(
                (e.source == source and e.target == target) or
                (e.source == target and e.target == source)
                for e in edges
            )

            # Avoid duplicates
            duplicate = any(
                h.source == source and h.target == target
                and h.resource == resource
                and abs((h.dissolved_at - entry.timestamp).total_seconds()) < 86400
                for h in historical
            )
            if not duplicate:
                historical.append(HistoricalEdge(
                    source=source,
                    target=target,
                    resource=resource,
                    dissolved_at=entry.timestamp,
                    log_pod=entry.pod_id,
                    evidence_text=entry.detail,
                    still_declared=still_declared,
                ))

    logger.info(
        "Historical edges: %d dissolved relationships found (%d still formally declared)",
        len(historical),
        sum(1 for h in historical if h.still_declared),
    )
    return historical


def _infer_resource_from_text(text: str) -> str:
    """Best-effort resource extraction from a log detail string."""
    patterns = [
        (r"water|coolant|slurry|humidity|irrigation", "water"),
        (r"power|electrical|electricity|solar|grid",  "electrical_power"),
        (r"oxygen|o2|atmospheric|air",                "atmospheric"),
        (r"pharmaceutical|medication|drug",           "pharmaceuticals"),
        (r"silicon|feedstock|mineral|regolith",       "raw_materials"),
        (r"comms|relay|communication|data",           "comms_relay"),
    ]
    for pattern, resource in patterns:
        if re.search(pattern, text):
            return resource
    return "unknown"


def _compute_risk_scores(
    pods: dict[str, PodNode],
    structural: dict,
    metadata_signals: list[MetadataSignal],
    historical_edges: list[HistoricalEdge],
) -> dict[str, RiskScore]:
    blast_radius: dict[str, BlastRadiusEntry] = structural["blast_radius"]
    art_points: set[str] = set(structural["articulation_points"])
    out_deg: dict[str, int] = structural["out_degree"]

    max_blast = max((v.blast_radius_count for v in blast_radius.values()), default=1)
    max_out   = max(out_deg.values(), default=1)

    scores: dict[str, RiskScore] = {}

    for pod_id in pods:
        br = blast_radius.get(pod_id)
        blast_score = (br.blast_radius_count / max_blast) if br else 0.0
        vuln_score  = (out_deg.get(pod_id, 0) / max_out)

        is_ap = pod_id in art_points

        pod_meta = [
            s for s in metadata_signals
            if s.pod_id == pod_id and s.signal_type == MetadataSignalType.NO_BACKUP
        ]
        is_meta_spof = len(pod_meta) > 0

        hist_count = sum(
            1 for h in historical_edges
            if h.source == pod_id or h.target == pod_id
        )

        # Corroboration: how many independent signals confirm critical role
        corroboration = sum([
            is_ap,
            is_meta_spof,
            hist_count > 2,           # multiple dissolutions involving this pod
            blast_score > 0.5,        # affects >50% of colony if it fails
        ])

        # Composite risk (weights tuned toward blast radius as primary signal)
        overall = round(
            0.40 * blast_score
            + 0.20 * vuln_score
            + 0.20 * (1.0 if is_ap else 0.0)
            + 0.10 * min(hist_count / 5.0, 1.0)
            + 0.10 * (1.0 if is_meta_spof else 0.0),
            4,
        )

        scores[pod_id] = RiskScore(
            pod_id=pod_id,
            blast_radius_score=round(blast_score, 4),
            vulnerability_score=round(vuln_score, 4),
            spof_corroboration_count=corroboration,
            is_articulation_point=is_ap,
            is_metadata_spof=is_meta_spof,
            historical_dissolution_count=hist_count,
            overall_risk=overall,
        )

    return scores


# ─────────────────────────────────────────────────────────────────────────────
# LAYER C — LLM Enrichment
# ─────────────────────────────────────────────────────────────────────────────

_LLM_TOOL_SCHEMA = {
    "name": "report_dependency_signals",
    "description": (
        "Report all dependency signals found in the inter-pod communications "
        "and operational logs. Each signal represents a directed relationship "
        "source_pod → target_pod with a typed relationship and verbatim evidence."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "signals": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "source_pod": {
                            "type": "string",
                            "description": "Pod ID that has or is concerned about the relationship",
                        },
                        "target_pod": {
                            "type": "string",
                            "description": "Pod ID being depended upon or supplied",
                        },
                        "relationship_type": {
                            "type": "string",
                            "enum": [t.value for t in LLMRelationshipType],
                        },
                        "resource": {
                            "type": ["string", "null"],
                            "description": "Resource or service involved (null if not identifiable)",
                        },
                        "is_formally_declared": {
                            "type": "boolean",
                            "description": "True if this relationship already exists in the formal graph",
                        },
                        "confidence": {
                            "type": "number",
                            "minimum": 0.0,
                            "maximum": 1.0,
                        },
                        "evidence_quote": {
                            "type": "string",
                            "description": "Verbatim excerpt from the comms or log message",
                        },
                        "evidence_pod": {
                            "type": "string",
                            "description": "Pod ID whose endpoint produced this message",
                        },
                        "evidence_timestamp": {
                            "type": ["string", "null"],
                            "description": "ISO 8601 timestamp of the message (null if unknown)",
                        },
                    },
                    "required": [
                        "source_pod", "target_pod", "relationship_type",
                        "confidence", "evidence_quote", "evidence_pod",
                        "is_formally_declared",
                    ],
                },
            }
        },
        "required": ["signals"],
    },
}


def _build_llm_prompt(
    pods: dict[str, PodNode],
    edges: list[DependencyEdge],
    timeline: list[LogEntry | CommEntry],
    reconciliation_issues: list[ReconciliationIssue],
) -> str:
    pod_ids = sorted(pods.keys())

    # Compact formal graph
    formal_graph = [
        {
            "source": e.source,
            "target": e.target,
            "resource": e.resource,
            "state": e.state.value,
            "criticality": e.criticality.value,
        }
        for e in edges
    ]

    # All comms — highest signal density
    comms = [
        {
            "pod": entry.pod_id,
            "sender": entry.sender,
            "recipient": entry.recipient,
            "content": entry.content,
            "timestamp": entry.timestamp.isoformat(),
        }
        for entry in timeline
        if isinstance(entry, CommEntry)
    ]

    # Only dissolution/rerouting logs — keeps prompt focused
    dissolution_logs = [
        {
            "pod": entry.pod_id,
            "timestamp": entry.timestamp.isoformat(),
            "event": entry.event,
            "detail": entry.detail,
        }
        for entry in timeline
        if isinstance(entry, LogEntry)
        and any(kw in entry.detail.lower() for kw in DISSOLUTION_KEYWORDS)
    ]

    # Unreconciled edges — these are the starting point for stale dep detection
    unreconciled = [
        {
            "source": ri.edge_source,
            "target": ri.edge_target,
            "resource": ri.resource,
            "state": ri.state.value,
            "description": ri.description,
        }
        for ri in reconciliation_issues
    ]

    # Compact formal graph as simple strings (less noise than full JSON)
    formal_edges_compact = [
        f"{e['source']} → {e['target']} [{e['resource']}, {e['state']}]"
        for e in formal_graph
    ]

    return f"""You are an infrastructure dependency analyst for a 12-pod lunar colony. Report ALL dependency signals you find by calling the report_dependency_signals tool. You MUST call the tool — do not respond with text.

VALID POD IDs: {json.dumps(pod_ids)}

FORMAL EDGES (source → target [resource, state]):
{chr(10).join(formal_edges_compact)}

ANOMALIES — edges declared by only one side (investigate these especially):
{json.dumps(unreconciled, indent=2)}

INTER-POD COMMUNICATIONS — highest signal density, read carefully:
{json.dumps(comms, indent=2)}

DISSOLUTION / REROUTING LOGS — relationships that changed operationally:
{json.dumps(dissolution_logs, indent=2)}

SIGNAL TYPES to find (report ALL you find with confidence >= 0.7):
1. implicit_dependency — A uses B's resource but it is NOT listed in the formal edges above
2. stale_dependency    — formal edge exists but logs/comms show the relationship was rerouted/decommissioned
3. reliability_concern — a pod expressed worry about another pod's reliability, capacity, or backup
4. undeclared_supply   — a pod provides something to another pod not shown in formal edges
5. capacity_risk       — a pod is near its operational limit in a way that threatens its dependents

For each signal: use a VERBATIM QUOTE from the data above as evidence_quote. Set is_formally_declared=true only if the exact source→target edge with same resource already appears in FORMAL EDGES."""


async def _run_llm_enrichment(
    pods: dict[str, PodNode],
    edges: list[DependencyEdge],
    timeline: list[LogEntry | CommEntry],
    reconciliation_issues: list[ReconciliationIssue],
    api_key: str,
) -> list[LLMDerivedSignal]:
    """Call the Anthropic API with a tool-use constraint to get structured signals."""
    try:
        import anthropic
    except ImportError:
        logger.warning("anthropic package not installed — skipping LLM enrichment")
        return []

    prompt = _build_llm_prompt(pods, edges, timeline, reconciliation_issues)
    client = anthropic.AsyncAnthropic(api_key=api_key)

    raw_signals: list[dict] = []
    for attempt in range(1, 3):
        logger.info("Layer C: calling LLM (attempt %d/2)", attempt)
        try:
            response = await client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=8192,
                tools=[_LLM_TOOL_SCHEMA],
                tool_choice={"type": "tool", "name": "report_dependency_signals"},
                messages=[{"role": "user", "content": prompt}],
            )
        except Exception as exc:
            logger.error("LLM enrichment attempt %d failed: %s", attempt, exc)
            continue

        for block in response.content:
            if block.type == "tool_use" and block.name == "report_dependency_signals":
                raw_signals = block.input.get("signals", [])
                break

        logger.info("Layer C: attempt %d returned %d raw signals", attempt, len(raw_signals))
        if raw_signals:
            break

    signals: list[LLMDerivedSignal] = []
    valid_pod_ids = set(pods.keys())
    seen: set[tuple] = set()

    for raw in raw_signals:
        src = raw.get("source_pod", "")
        tgt = raw.get("target_pod", "")

        if src not in valid_pod_ids or tgt not in valid_pod_ids:
            logger.warning("LLM signal references unknown pod: %s → %s", src, tgt)
            continue

        try:
            rel = LLMRelationshipType(raw.get("relationship_type", ""))
        except ValueError:
            logger.warning("LLM signal has unknown relationship_type: %s", raw)
            continue

        dedup_key = (src, tgt, rel.value)
        if dedup_key in seen:
            continue
        seen.add(dedup_key)

        ts_raw = raw.get("evidence_timestamp")
        ts = None
        if ts_raw:
            try:
                ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
            except ValueError:
                pass

        signals.append(LLMDerivedSignal(
            source_pod=src,
            target_pod=tgt,
            relationship_type=rel,
            resource=raw.get("resource"),
            is_formally_declared=bool(raw.get("is_formally_declared", False)),
            confidence=float(raw.get("confidence", 0.0)),
            evidence_quote=raw.get("evidence_quote", ""),
            evidence_pod=raw.get("evidence_pod", src),
            evidence_timestamp=ts,
        ))

    logger.info("Layer C: %d deduplicated signals parsed", len(signals))
    return signals


# ─────────────────────────────────────────────────────────────────────────────
# Public entry point
# ─────────────────────────────────────────────────────────────────────────────

async def compute_all_metrics(
    pods: dict[str, PodNode],
    edges: list[DependencyEdge],
    timeline: list[LogEntry | CommEntry],
    reconciliation_issues: list[ReconciliationIssue],
    api_key: str | None = None,
    inference_parameters: dict[str, float] | None = None,
) -> ExtendedMetrics:
    """Compute all three metric layers and return a fully populated ExtendedMetrics."""

    # Always start from the documented defaults and let the caller override.
    merged_params = dict(_DEFAULT_INFERENCE_PARAMS)
    if inference_parameters:
        merged_params.update(inference_parameters)
    inference_parameters = merged_params

    logger.info("=== Metrics Layer A: structural ===")
    structural = _compute_structural(pods, edges)

    logger.info("=== Metrics Layer B: operational ===")
    operational = _compute_operational(pods, edges, timeline, structural)

    logger.info("=== Metrics: cascade simulation (deterministic) ===")
    cascade_simulations = _compute_cascades(
        pods=pods,
        edges=edges,
        structural=structural,
        metadata_signals=operational["metadata_signals"],
        historical_edges=operational["historical_edges"],
        risk_scores=operational["risk_scores"],
    )
    logger.info(
        "Cascades: %d simulated (triggers: %s)",
        len(cascade_simulations),
        [s.trigger_pod for s in cascade_simulations],
    )

    logger.info("=== Metrics: chained cascades (pessimistic chaining) ===")
    chained_cascades = _compute_chained_cascades(
        pods=pods,
        cascade_simulations=cascade_simulations,
        inference_params=inference_parameters,
    )
    for cc in chained_cascades:
        logger.info(
            "  Chain [%s] secondary=%s life_critical=%s colony_wide=%s",
            cc.primary_trigger,
            cc.secondary_triggers,
            f"{cc.time_to_life_critical_hours:.0f}h" if cc.time_to_life_critical_hours is not None else "?",
            f"{cc.time_to_colony_wide_hours:.0f}h" if cc.time_to_colony_wide_hours is not None else "?",
        )

    logger.info("=== Metrics Layer C: LLM enrichment ===")
    llm_signals: list[LLMDerivedSignal] = []
    if api_key:
        llm_signals = await _run_llm_enrichment(
            pods, edges, timeline, reconciliation_issues, api_key
        )
    else:
        logger.info("Layer C skipped — no API key provided")

    # Remove internal graph object (not serialisable) before building the model
    structural.pop("G")

    return ExtendedMetrics(
        # Layer A
        in_degree=structural["in_degree"],
        out_degree=structural["out_degree"],
        betweenness_centrality=structural["betweenness_centrality"],
        in_degree_centrality=structural["in_degree_centrality"],
        articulation_points=structural["articulation_points"],
        strongly_connected_components=structural["strongly_connected_components"],
        longest_path=structural["longest_path"],
        blast_radius=structural["blast_radius"],
        cascade_simulations=cascade_simulations,
        chained_cascades=chained_cascades,
        highest_blast_radius=structural["highest_blast_radius"],
        # Layer B
        metadata_signals=operational["metadata_signals"],
        historical_edges=operational["historical_edges"],
        risk_scores=operational["risk_scores"],
        most_vulnerable=operational["most_vulnerable"],
        highest_overall_risk=operational["highest_overall_risk"],
        # Layer C
        llm_derived_signals=llm_signals,
    )
