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
    CommEntry,
    DependencyEdge,
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

    # ── Cascade simulations (one per articulation point) ─────────────────
    cascade_sims = _compute_cascades(pods, G, art_points)

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
        cascade_simulations=cascade_sims,
        highest_blast_radius=highest_blast,
        G=G,   # passed to later layers, removed before building ExtendedMetrics
    )


def _compute_cascades(
    pods: dict[str, PodNode],
    G: nx.DiGraph,
    art_points: list[str],
) -> list[CascadeSimulation]:
    simulations: list[CascadeSimulation] = []

    for trigger in art_points:
        steps = _cascade_steps(pods, G, trigger)
        life_critical_times = [
            s.estimated_window_hours
            for s in steps
            if s.is_life_critical and s.estimated_window_hours is not None
        ]
        simulations.append(CascadeSimulation(
            trigger_pod=trigger,
            trigger_reason=f"Articulation point — removing {trigger} disconnects the colony graph",
            steps=steps,
            total_pods_affected=len(steps),
            time_to_life_critical_hours=min(life_critical_times) if life_critical_times else None,
        ))

    return simulations


def _cascade_steps(
    pods: dict[str, PodNode],
    G: nx.DiGraph,
    trigger: str,
) -> list[CascadeStep]:
    """BFS in the reverse graph from `trigger`, building ordered cascade steps."""
    R = G.reverse(copy=False)
    steps: list[CascadeStep] = []
    visited = {trigger}
    queue: deque[tuple[str, int]] = deque()

    # Seed queue with direct dependents of trigger
    for dependent in R.successors(trigger):   # in R, successor = original dependent
        if dependent not in visited:
            visited.add(dependent)
            queue.append((dependent, 1))

    while queue:
        pod_id, hop = queue.popleft()
        resource, lost_from = _resource_lost(G, pod_id, trigger, hop)
        timing = _survival_window(pods.get(pod_id), resource)
        evidence = _timing_evidence(pods.get(pod_id), resource)

        steps.append(CascadeStep(
            pod_id=pod_id,
            failure_mode=f"loses '{resource}' from {lost_from}",
            hop=hop,
            estimated_window_hours=timing,
            evidence_source=evidence,
            is_life_critical=resource in LIFE_CRITICAL_RESOURCES,
        ))

        for next_dep in R.successors(pod_id):
            if next_dep not in visited:
                visited.add(next_dep)
                queue.append((next_dep, hop + 1))

    return sorted(steps, key=lambda s: (s.hop, s.pod_id))


def _resource_lost(
    G: nx.DiGraph, pod_id: str, trigger: str, hop: int
) -> tuple[str, str]:
    """Return (resource, immediate_supplier) for a pod in the cascade."""
    if hop == 1 and G.has_edge(pod_id, trigger):
        data = G[pod_id][trigger]
        return data.get("resource", "unknown"), trigger

    # Multi-hop: walk the shortest path from pod_id toward trigger
    try:
        path = nx.shortest_path(G, pod_id, trigger)
        if len(path) >= 2:
            next_hop = path[1]
            data = G[pod_id].get(next_hop, {})
            return data.get("resource", "unknown"), next_hop
    except (nx.NetworkXNoPath, nx.NodeNotFound):
        pass
    return "unknown", trigger


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
) -> ExtendedMetrics:
    """Compute all three metric layers and return a fully populated ExtendedMetrics."""

    logger.info("=== Metrics Layer A: structural ===")
    structural = _compute_structural(pods, edges)

    logger.info("=== Metrics Layer B: operational ===")
    operational = _compute_operational(pods, edges, timeline, structural)

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
        cascade_simulations=structural["cascade_simulations"],
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
