"""
Mapping phase orchestrator — five cleanly separated phases.

Each phase function accepts only typed inputs and returns typed outputs so
individual phases can be imported and exercised in isolation by test suites
without running the full pipeline.

  Phase 0  discover    Gateway BFS → pod_registry
  Phase 1  crawl       pod_registry → pods (all 6 endpoints per pod)
  Phase 2  reconcile   pods → edges + reconciliation_issues
  Phase 3  timeline    pods → sorted merged event stream
  Phase 4  metrics     pods + edges + timeline + issues → ExtendedMetrics
  Phase 5  assemble    all → ColonyMap → map.json

Intermediate phase artefacts are written to OUTPUT_DIR/phases/ so each phase
can be debugged independently and test fixtures can stub any phase boundary
by pre-writing the expected artefact.

Sanity-check output:
  phases/phase_2_reconciliation.json   — machine-readable edge audit
  phases/reconciliation_audit.txt      — human-readable discrepancy report
  phases/phase_4_metrics_summary.json  — key risk signals at a glance
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from .crawler import assemble_timeline, crawl_all_pods, reconcile_edges
from .discovery import discover_colony
from .metrics import compute_all_metrics
from .models import (
    ColonyMap,
    CommEntry,
    DependencyEdge,
    EdgeState,
    ExtendedMetrics,
    LogEntry,
    PodNode,
    ReconciliationIssue,
)

# ---------------------------------------------------------------------------
# Configuration (all overridable — tested code passes these as params)
# ---------------------------------------------------------------------------

DEFAULT_GATEWAY_URL    = os.environ.get("GATEWAY_URL",  "http://gateway:3000")
DEFAULT_OUTPUT_DIR     = Path(os.environ.get("OUTPUT_DIR", "/rover/output"))
DEFAULT_LLM_API_KEY    = os.environ.get("LLM_API_KEY")
DEFAULT_DELIVERABLE_DIR = Path(os.environ.get("DELIVERABLE_DIR", "")) if os.environ.get("DELIVERABLE_DIR") else None


def _inference_params_from_env() -> dict[str, float]:
    """Read overridable cascade-chaining inference parameters from env vars."""
    params: dict[str, float] = {}
    raw = os.environ.get("HELIOS_COOLANT_DEGRADATION_HOURS")
    if raw:
        try:
            params["helios_coolant_degradation_hours"] = float(raw)
        except ValueError:
            logger.warning("Ignoring non-numeric HELIOS_COOLANT_DEGRADATION_HOURS=%r", raw)
    return params

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)


def _phase_header(n: int, name: str) -> None:
    logger.info("")
    logger.info("━" * 60)
    logger.info("  PHASE %d — %s", n, name.upper())
    logger.info("━" * 60)


# ---------------------------------------------------------------------------
# Phase 0 — Discovery
# ---------------------------------------------------------------------------

async def phase_0_discover(
    gateway_url: str,
) -> tuple[dict[str, tuple[str, int]], list[str], list[str]]:
    """BFS from gateway to build a complete pod_registry.

    Returns:
        pod_registry    dict[pod_id → (hostname, port)]
        discovery_order list[pod_id] in BFS visit order
        unreachable     pod_ids referenced in graph but not responding
    """
    _phase_header(0, "Discovery")
    logger.info("Gateway: %s", gateway_url)

    pod_registry, discovery_order, unreachable = await discover_colony(gateway_url)

    logger.info("Found %d pods — BFS order: %s", len(pod_registry), " → ".join(discovery_order))
    if unreachable:
        logger.warning("Unreachable pods referenced in graph: %s", unreachable)

    return pod_registry, discovery_order, unreachable


def _write_phase_0(
    pod_registry: dict[str, tuple[str, int]],
    discovery_order: list[str],
    unreachable: list[str],
    phases_dir: Path,
) -> None:
    out = {
        "pod_count": len(pod_registry),
        "discovery_order": discovery_order,
        "unreachable": unreachable,
        "registry": {pid: {"hostname": h, "port": p} for pid, (h, p) in pod_registry.items()},
    }
    (phases_dir / "phase_0_discovery.json").write_text(json.dumps(out, indent=2))
    logger.info("Phase 0 artefact → phases/phase_0_discovery.json")


# ---------------------------------------------------------------------------
# Phase 1 — Crawl
# ---------------------------------------------------------------------------

async def phase_1_crawl(
    pod_registry: dict[str, tuple[str, int]],
) -> dict[str, PodNode]:
    """Fetch all 6 endpoints for every discovered pod concurrently."""
    _phase_header(1, "Crawl")
    logger.info("Crawling %d pods (6 endpoints each)...", len(pod_registry))

    pods = await crawl_all_pods(pod_registry)

    total_logs  = sum(len(p.logs)           for p in pods.values())
    total_comms = sum(len(p.comms or [])    for p in pods.values())
    comms_pods  = [pid for pid, p in pods.items() if p.has_comms]
    error_pods  = [pid for pid, p in pods.items() if p.crawl_errors]

    logger.info(
        "Crawl complete — %d pods, %d log entries, %d comms messages",
        len(pods), total_logs, total_comms,
    )
    logger.info("Comms-enabled pods (%d): %s", len(comms_pods), comms_pods)
    if error_pods:
        logger.warning("Pods with crawl errors: %s", error_pods)

    return pods


def _write_phase_1(pods: dict[str, PodNode], phases_dir: Path) -> None:
    summary = {
        "pod_count": len(pods),
        "pods": {
            pid: {
                "name":          p.display_name,
                "role":          p.info.role if p.info else None,
                "deps_declared": len(p.raw_dependencies),
                "sups_declared": len(p.raw_supplies),
                "log_entries":   len(p.logs),
                "has_comms":     p.has_comms,
                "comms_count":   len(p.comms) if p.comms else 0,
                "crawl_errors":  p.crawl_errors,
            }
            for pid, p in pods.items()
        },
    }
    (phases_dir / "phase_1_crawl_summary.json").write_text(json.dumps(summary, indent=2))
    logger.info("Phase 1 artefact → phases/phase_1_crawl_summary.json")


# ---------------------------------------------------------------------------
# Phase 2 — Reconcile
# ---------------------------------------------------------------------------

def phase_2_reconcile(
    pods: dict[str, PodNode],
) -> tuple[list[DependencyEdge], list[ReconciliationIssue]]:
    """Cross-reference /dependencies vs /supplies to produce the reconciled edge list.

    This is the primary sanity-check phase: any edge where both sides do not
    agree represents either a stale declaration, an informal dependency, or a
    documentation gap.  The counts are logged prominently.
    """
    _phase_header(2, "Reconcile")

    edges, issues = reconcile_edges(pods)

    reconciled   = [e for e in edges if e.reconciled]
    dep_only     = [e for e in edges if e.state == EdgeState.DEP_ONLY]
    supply_only  = [e for e in edges if e.state == EdgeState.SUPPLY_ONLY]
    total        = len(edges)

    pct = lambda n: f"{100 * n / total:.1f}%" if total else "0%"

    logger.info("Edges total:      %d", total)
    logger.info("Reconciled:       %d  (%s)  — both sides agree", len(reconciled), pct(len(reconciled)))
    logger.info("Unreconciled:     %d  (%s)", len(issues), pct(len(issues)))
    logger.info("  DEP_ONLY:       %d  — source declares dep; target /supplies omits source", len(dep_only))
    logger.info("  SUPPLY_ONLY:    %d  — target declares supply; source /deps omits target", len(supply_only))

    if dep_only:
        logger.info("DEP_ONLY edges (likely stale or undocumented):")
        for e in dep_only:
            src_name = pods[e.source].display_name if e.source in pods else e.source
            tgt_name = pods[e.target].display_name if e.target in pods else e.target
            logger.info("  [%s]  %s → %s  [%s]", e.criticality.value.upper(), src_name, tgt_name, e.resource)

    if supply_only:
        logger.info("SUPPLY_ONLY edges (informal/administrative supplies):")
        for e in supply_only:
            src_name = pods[e.source].display_name if e.source in pods else e.source
            tgt_name = pods[e.target].display_name if e.target in pods else e.target
            logger.info("  %s → %s  [%s]", src_name, tgt_name, e.resource)

    return edges, issues


def _write_phase_2(
    edges: list[DependencyEdge],
    issues: list[ReconciliationIssue],
    phases_dir: Path,
) -> None:
    out = {
        "summary": {
            "total_edges":        len(edges),
            "reconciled":         sum(1 for e in edges if e.reconciled),
            "dep_only":           sum(1 for e in edges if e.state == EdgeState.DEP_ONLY),
            "supply_only":        sum(1 for e in edges if e.state == EdgeState.SUPPLY_ONLY),
        },
        "edges": [e.model_dump() for e in edges],
        "issues": [i.model_dump() for i in issues],
    }
    path = phases_dir / "phase_2_reconciliation.json"
    path.write_text(json.dumps(out, indent=2, default=str))
    logger.info("Phase 2 artefact → phases/phase_2_reconciliation.json")


# ---------------------------------------------------------------------------
# Phase 3 — Timeline
# ---------------------------------------------------------------------------

def phase_3_timeline(
    pods: dict[str, PodNode],
) -> list[LogEntry | CommEntry]:
    """Merge all pod logs and comms into a single timestamp-sorted timeline."""
    _phase_header(3, "Timeline")

    timeline = assemble_timeline(pods)

    logs  = [e for e in timeline if isinstance(e, LogEntry)]
    comms = [e for e in timeline if isinstance(e, CommEntry)]
    span  = (
        f"{timeline[0].timestamp.date()} → {timeline[-1].timestamp.date()}"
        if timeline else "empty"
    )

    logger.info(
        "Timeline: %d events (%d logs, %d comms)  span: %s",
        len(timeline), len(logs), len(comms), span,
    )

    return timeline


def _write_phase_3(
    timeline: list[LogEntry | CommEntry],
    phases_dir: Path,
) -> None:
    event_types: dict[str, int] = {}
    for e in timeline:
        if isinstance(e, LogEntry):
            event_types[e.event] = event_types.get(e.event, 0) + 1
        else:
            event_types["comm"] = event_types.get("comm", 0) + 1

    summary = {
        "total_events": len(timeline),
        "log_count":    sum(1 for e in timeline if isinstance(e, LogEntry)),
        "comm_count":   sum(1 for e in timeline if isinstance(e, CommEntry)),
        "event_type_counts": dict(sorted(event_types.items(), key=lambda x: -x[1])),
        "date_range": {
            "first": timeline[0].timestamp.isoformat() if timeline else None,
            "last":  timeline[-1].timestamp.isoformat() if timeline else None,
        },
    }
    (phases_dir / "phase_3_timeline_summary.json").write_text(json.dumps(summary, indent=2))
    logger.info("Phase 3 artefact → phases/phase_3_timeline_summary.json")


# ---------------------------------------------------------------------------
# Phase 4 — Metrics
# ---------------------------------------------------------------------------

async def phase_4_metrics(
    pods: dict[str, PodNode],
    edges: list[DependencyEdge],
    timeline: list[LogEntry | CommEntry],
    issues: list[ReconciliationIssue],
    api_key: str | None = None,
    inference_parameters: dict[str, float] | None = None,
) -> ExtendedMetrics:
    """Three-layer metrics: structural (networkx), operational (metadata+logs), LLM enrichment."""
    _phase_header(4, "Metrics")

    metrics = await compute_all_metrics(
        pods, edges, timeline, issues, api_key,
        inference_parameters=inference_parameters,
    )

    # ── Layer A summary ──────────────────────────────────────────────────
    art = metrics.articulation_points
    logger.info("Articulation points (true SPOFs): %s", art or "none")

    for pod_id in metrics.highest_blast_radius[:3]:
        br = metrics.blast_radius.get(pod_id)
        if br:
            logger.info(
                "  Blast radius: %s → %d pods affected (weighted %.1f)",
                pods[pod_id].display_name if pod_id in pods else pod_id,
                br.blast_radius_count,
                br.blast_radius_weighted,
            )

    for sim in metrics.cascade_simulations:
        t_lc  = sim.time_to_life_critical_hours
        t_cw  = sim.time_to_colony_wide_hours
        logger.info(
            "  Cascade [%s]: %d pods affected, %d survivors, life-critical=%s colony-wide=%s",
            sim.trigger_pod,
            sim.total_pods_affected,
            len(sim.survivors),
            f"{t_lc:.0f}h" if t_lc is not None else "unknown",
            f"{t_cw:.0f}h" if t_cw is not None else "unknown",
        )
        for sig in sim.corroboration_signals:
            logger.info("      ⚑ %s", sig)
        if sim.compound_dependents:
            logger.info(
                "      ↻ compound (mutual destruction): %s",
                ", ".join(sim.compound_dependents),
            )
        for s in sim.survivors:
            logger.info("      ✔ survivor %s — %s", s.pod_id, s.reason)

    # ── Chained cascades (pessimistic colony-wide timeline) ──────────────
    for cc in metrics.chained_cascades:
        logger.info(
            "  Chained [%s] secondary=%s life_critical=%s colony_wide=%s",
            cc.primary_trigger,
            cc.secondary_triggers,
            f"{cc.time_to_life_critical_hours:.0f}h" if cc.time_to_life_critical_hours is not None else "?",
            f"{cc.time_to_colony_wide_hours:.0f}h" if cc.time_to_colony_wide_hours is not None else "?",
        )
        for evt in cc.events:
            t = f"T+{evt.cumulative_hours:.0f}h" if evt.cumulative_hours is not None else "T+?"
            via = f" via {evt.via_secondary_trigger}" if evt.via_secondary_trigger else ""
            conf = f" [{evt.inference_confidence}]"
            logger.info(
                "      %-7s %-12s loses '%s' from %s%s%s",
                t, evt.pod_id, evt.lost_resource, evt.immediate_supplier, via, conf,
            )

    # ── Layer B summary ──────────────────────────────────────────────────
    no_backup = [s for s in metrics.metadata_signals if s.signal_type.value == "no_backup"]
    stale_ref = [s for s in metrics.metadata_signals if s.signal_type.value == "stale_reference"]
    dissolved = metrics.historical_edges
    still_dec = [h for h in dissolved if h.still_declared]

    logger.info("Metadata signals: %d total (%d no_backup, %d stale_references)",
                len(metrics.metadata_signals), len(no_backup), len(stale_ref))
    logger.info("Historical dissolved edges: %d (%d still formally declared — stale)",
                len(dissolved), len(still_dec))

    # ── Layer C summary ──────────────────────────────────────────────────
    llm = metrics.llm_derived_signals
    if llm:
        implicit   = [s for s in llm if s.relationship_type.value == "implicit_dependency"]
        stale_deps = [s for s in llm if s.relationship_type.value == "stale_dependency"]
        concerns   = [s for s in llm if s.relationship_type.value == "reliability_concern"]
        cap_risks  = [s for s in llm if s.relationship_type.value == "capacity_risk"]
        logger.info(
            "LLM signals: %d total — %d implicit_deps, %d stale_deps, %d reliability_concerns, %d capacity_risks",
            len(llm), len(implicit), len(stale_deps), len(concerns), len(cap_risks),
        )
        for s in llm[:5]:
            logger.info(
                "  [%s] %s → %s (conf=%.2f): %s",
                s.relationship_type.value, s.source_pod, s.target_pod,
                s.confidence, s.evidence_quote[:70],
            )
    else:
        logger.info("LLM signals: none (API key not set or no signals above confidence threshold)")

    # ── Risk ranking ─────────────────────────────────────────────────────
    logger.info("Top 3 by overall risk:")
    for pod_id in metrics.highest_overall_risk[:3]:
        rs = metrics.risk_scores.get(pod_id)
        if rs:
            logger.info(
                "  %s — risk=%.3f  blast=%.2f  AP=%s  corroboration=%d",
                pods[pod_id].display_name if pod_id in pods else pod_id,
                rs.overall_risk,
                rs.blast_radius_score,
                rs.is_articulation_point,
                rs.spof_corroboration_count,
            )

    return metrics


def _write_phase_4(
    metrics: ExtendedMetrics,
    pods: dict[str, PodNode],
    phases_dir: Path,
) -> None:
    summary = {
        "articulation_points": metrics.articulation_points,
        "top_blast_radius": [
            {
                "pod_id": pid,
                "name":   pods[pid].display_name if pid in pods else pid,
                "count":  metrics.blast_radius[pid].blast_radius_count,
                "pods":   metrics.blast_radius[pid].blast_radius_pods,
                "weighted": metrics.blast_radius[pid].blast_radius_weighted,
            }
            for pid in metrics.highest_blast_radius
        ],
        "cascade_simulations": [
            {
                "trigger":              sim.trigger_pod,
                "trigger_reason":       sim.trigger_reason,
                "corroboration":        sim.corroboration_signals,
                "direct_dependents":    sim.direct_dependents,
                "compound_dependents":  sim.compound_dependents,
                "pods_affected":        sim.total_pods_affected,
                "life_critical_hours":  sim.time_to_life_critical_hours,
                "colony_wide_hours":    sim.time_to_colony_wide_hours,
                "steps": [
                    {
                        "pod_id":            s.pod_id,
                        "failure_mode":      s.failure_mode,
                        "hop":               s.hop,
                        "window_hours":      s.estimated_window_hours,
                        "cumulative_hours":  s.cumulative_hours,
                        "lost_resource":     s.lost_resource,
                        "immediate_supplier": s.immediate_supplier,
                        "is_life_critical":  s.is_life_critical,
                        "is_compound":       s.is_compound,
                        "evidence_source":   s.evidence_source,
                    }
                    for s in sim.steps
                ],
                "survivors": [
                    {
                        "pod_id":   sv.pod_id,
                        "reason":   sv.reason,
                        "evidence": sv.evidence,
                    }
                    for sv in sim.survivors
                ],
            }
            for sim in metrics.cascade_simulations
        ],
        "chained_cascades": [
            {
                "primary_trigger":          cc.primary_trigger,
                "secondary_triggers":       cc.secondary_triggers,
                "inference_parameters":     cc.inference_parameters,
                "time_to_life_critical_hours": cc.time_to_life_critical_hours,
                "time_to_colony_wide_hours":   cc.time_to_colony_wide_hours,
                "events": [
                    {
                        "pod_id":            evt.pod_id,
                        "cumulative_hours":  evt.cumulative_hours,
                        "lost_resource":     evt.lost_resource,
                        "immediate_supplier": evt.immediate_supplier,
                        "failure_mode":      evt.failure_mode,
                        "via_primary":       evt.via_primary,
                        "via_secondary_trigger": evt.via_secondary_trigger,
                        "secondary_offset_hours": evt.secondary_offset_hours,
                        "secondary_offset_evidence": evt.secondary_offset_evidence,
                        "inference_confidence": evt.inference_confidence,
                        "is_life_critical":  evt.is_life_critical,
                        "is_compound":       evt.is_compound,
                    }
                    for evt in cc.events
                ],
                "survivors": [sv.pod_id for sv in cc.survivors],
            }
            for cc in metrics.chained_cascades
        ],
        "metadata_signals": {
            "no_backup":          [s.model_dump() for s in metrics.metadata_signals if s.signal_type.value == "no_backup"],
            "stale_references":   [s.model_dump() for s in metrics.metadata_signals if s.signal_type.value == "stale_reference"],
            "implied_deps":       [s.model_dump() for s in metrics.metadata_signals if s.signal_type.value == "implied_dependency"],
            "resilience_markers": [s.model_dump() for s in metrics.metadata_signals if s.signal_type.value == "resilience_marker"],
        },
        "historical_edges": [h.model_dump(mode="json") for h in metrics.historical_edges],
        "risk_ranking": [
            {
                "pod_id":               rs.pod_id,
                "name":                 pods[rs.pod_id].display_name if rs.pod_id in pods else rs.pod_id,
                "overall_risk":         rs.overall_risk,
                "blast_radius_score":   rs.blast_radius_score,
                "vulnerability_score":  rs.vulnerability_score,
                "is_articulation_point": rs.is_articulation_point,
                "is_metadata_spof":     rs.is_metadata_spof,
                "corroboration":        rs.spof_corroboration_count,
                "dissolved_edges":      rs.historical_dissolution_count,
            }
            for rs in sorted(metrics.risk_scores.values(), key=lambda r: r.overall_risk, reverse=True)
        ],
        "llm_derived_signals": [s.model_dump(mode="json") for s in metrics.llm_derived_signals],
    }
    path = phases_dir / "phase_4_metrics_summary.json"
    path.write_text(json.dumps(summary, indent=2, default=str))
    logger.info("Phase 4 artefact → phases/phase_4_metrics_summary.json")


# ---------------------------------------------------------------------------
# Reconciliation audit (written after Phase 4 so we have historical context)
# ---------------------------------------------------------------------------

def _write_reconciliation_audit(
    pods: dict[str, PodNode],
    edges: list[DependencyEdge],
    issues: list[ReconciliationIssue],
    metrics: ExtendedMetrics,
    phases_dir: Path,
    crawled_at: datetime,
) -> None:
    """Human-readable reconciliation report cross-referenced with historical and LLM evidence."""

    def pod_name(pid: str) -> str:
        return pods[pid].display_name if pid in pods else pid

    dep_only    = [e for e in edges if e.state == EdgeState.DEP_ONLY]
    supply_only = [e for e in edges if e.state == EdgeState.SUPPLY_ONLY]
    total       = len(edges)
    pct         = lambda n: f"{100*n/total:.1f}%" if total else "0%"

    lines: list[str] = []
    sep = "─" * 78

    lines += [
        "RECONCILIATION AUDIT — Selene Colony Map",
        f"Generated:  {crawled_at.strftime('%Y-%m-%dT%H:%M:%SZ')}",
        "=" * 78,
        "",
        "SUMMARY",
        f"  Total edges:     {total}",
        f"  Reconciled:      {len(edges) - len(issues)}  ({pct(len(edges) - len(issues))})  — /dependencies and /supplies agree",
        f"  Unreconciled:    {len(issues)}  ({pct(len(issues))})",
        f"    DEP_ONLY:      {len(dep_only)}  — source declares dep; target /supplies omits source",
        f"    SUPPLY_ONLY:   {len(supply_only)}  — target declares supply; source /deps omits target",
        "",
    ]

    # Index for cross-referencing
    hist_by_pair: dict[tuple[str, str], list] = {}
    for h in metrics.historical_edges:
        hist_by_pair.setdefault((h.source, h.target), []).append(h)
        hist_by_pair.setdefault((h.target, h.source), []).append(h)

    llm_by_pair: dict[tuple[str, str], list] = {}
    for s in metrics.llm_derived_signals:
        llm_by_pair.setdefault((s.source_pod, s.target_pod), []).append(s)

    meta_stale: dict[str, list] = {}
    for s in metrics.metadata_signals:
        if s.signal_type.value == "stale_reference":
            meta_stale.setdefault(s.pod_id, []).append(s)

    # ── DEP_ONLY ─────────────────────────────────────────────────────────
    if dep_only:
        lines += [
            "DEP_ONLY — Ghost Dependencies",
            "  Source declares dep; target's /supplies does not list source.",
            "  These are the highest-priority findings: the formal graph believes",
            "  a dependency exists that the supplier is unaware of.",
            sep,
        ]
        for e in sorted(dep_only, key=lambda x: x.criticality.value):
            lines.append(
                f"  [{e.criticality.value.upper():8s}]  {pod_name(e.source)} → {pod_name(e.target)}  [{e.resource}]"
            )

            # Historical context
            for h in hist_by_pair.get((e.source, e.target), []):
                lines.append(f"    ⚑ HISTORICAL [{h.dissolved_at.date()}]: {h.evidence_text[:100]}...")
                if h.still_declared:
                    lines.append(f"      → Formal /dependencies STILL lists this (stale declaration)")

            # Metadata stale reference
            for ms in meta_stale.get(e.source, []):
                if ms.implied_dep_target == e.target:
                    lines.append(f"    ⚑ METADATA: field '{ms.field}={ms.value}' still references {e.target}")

            # LLM context
            for sig in llm_by_pair.get((e.source, e.target), []):
                lines.append(
                    f"    ⚑ LLM [{sig.relationship_type.value}, conf={sig.confidence:.2f}]: "
                    f'"{sig.evidence_quote[:80]}..."'
                )

            lines.append("")

    # ── SUPPLY_ONLY ───────────────────────────────────────────────────────
    if supply_only:
        lines += [
            "SUPPLY_ONLY — Undeclared or Administrative Supplies",
            "  Target's /supplies lists source; source's /dependencies omits target.",
            "  Often administrative/oversight relationships (Artemis → pods).",
            "  Flag if the supplied resource is operational rather than administrative.",
            sep,
        ]
        for e in supply_only:
            lines.append(
                f"  {pod_name(e.source)} → {pod_name(e.target)}  [{e.resource}]"
            )
            for sig in llm_by_pair.get((e.target, e.source), []):
                lines.append(
                    f"    ⚑ LLM [{sig.relationship_type.value}, conf={sig.confidence:.2f}]: "
                    f'"{sig.evidence_quote[:80]}..."'
                )
            lines.append("")

    # ── Hidden transitive dependencies ────────────────────────────────────
    implicit_llm = [
        s for s in metrics.llm_derived_signals
        if s.relationship_type.value == "implicit_dependency" and not s.is_formally_declared
    ]
    if implicit_llm:
        lines += [
            "IMPLICIT DEPENDENCIES (LLM-derived, not in formal graph)",
            "  These relationships exist operationally but are invisible to the",
            "  reconciler because neither pod has declared them.",
            sep,
        ]
        for sig in sorted(implicit_llm, key=lambda s: s.confidence, reverse=True):
            lines.append(
                f"  {pod_name(sig.source_pod)} → {pod_name(sig.target_pod)}  "
                f"[{sig.resource or 'unknown'}]  conf={sig.confidence:.2f}"
            )
            lines.append(f'    Evidence ({sig.evidence_pod}): "{sig.evidence_quote[:100]}..."')
            lines.append("")

    text = "\n".join(lines)
    (phases_dir / "reconciliation_audit.txt").write_text(text)
    logger.info("Reconciliation audit → phases/reconciliation_audit.txt")


# ---------------------------------------------------------------------------
# Phase 5 — Assemble
# ---------------------------------------------------------------------------

def phase_5_assemble(
    gateway_url: str,
    pods: dict[str, PodNode],
    edges: list[DependencyEdge],
    timeline: list[LogEntry | CommEntry],
    issues: list[ReconciliationIssue],
    metrics: ExtendedMetrics,
    discovery_order: list[str],
    unreachable: list[str],
    output_dir: Path,
) -> ColonyMap:
    """Assemble the ColonyMap and write map.json."""
    _phase_header(5, "Assemble")

    colony_map = ColonyMap(
        crawled_at=datetime.now(tz=timezone.utc),
        gateway_url=gateway_url,
        pods=pods,
        edges=edges,
        timeline=timeline,
        reconciliation_issues=issues,
        metrics=metrics,
        discovery_order=discovery_order,
        unreachable_pods=unreachable,
    )

    out_path = output_dir / "map.json"
    out_path.write_text(colony_map.model_dump_json(indent=2))

    size_kb = out_path.stat().st_size / 1024
    logger.info(
        "map.json written — %d pods, %d edges, %d timeline events, %.1f KB",
        colony_map.pod_count(),
        len(colony_map.edges),
        len(colony_map.timeline),
        size_kb,
    )

    if DEFAULT_DELIVERABLE_DIR and DEFAULT_DELIVERABLE_DIR.is_dir():
        import shutil
        shutil.copy2(out_path, DEFAULT_DELIVERABLE_DIR / "map.json")
        logger.info("map.json duplicated → %s/map.json", DEFAULT_DELIVERABLE_DIR)

    return colony_map


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

async def run(
    gateway_url: str = DEFAULT_GATEWAY_URL,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    api_key: str | None = DEFAULT_LLM_API_KEY,
    inference_parameters: dict[str, float] | None = None,
) -> ColonyMap:
    """Run all five phases and return the completed ColonyMap.

    Parameters are explicit so test code can inject alternatives without
    touching environment variables.
    """
    if inference_parameters is None:
        inference_parameters = _inference_params_from_env()

    output_dir.mkdir(parents=True, exist_ok=True)
    phases_dir = output_dir / "phases"
    phases_dir.mkdir(exist_ok=True)

    started = datetime.now(tz=timezone.utc)
    logger.info("═" * 60)
    logger.info("  SELENE MAPPING AGENT")
    logger.info("  Gateway:  %s", gateway_url)
    logger.info("  Output:   %s", output_dir)
    logger.info("  LLM key:  %s", "configured" if api_key else "not set — Layer C will be skipped")
    if inference_parameters:
        logger.info("  Inference overrides: %s", inference_parameters)
    logger.info("  Started:  %s", started.strftime("%H:%M:%S UTC"))
    logger.info("═" * 60)

    # ── Phase 0: Discovery ────────────────────────────────────────────────
    pod_registry, discovery_order, unreachable = await phase_0_discover(gateway_url)
    _write_phase_0(pod_registry, discovery_order, unreachable, phases_dir)

    if not pod_registry:
        logger.error("No pods discovered — aborting")
        sys.exit(1)

    # ── Phase 1: Crawl ────────────────────────────────────────────────────
    pods = await phase_1_crawl(pod_registry)
    _write_phase_1(pods, phases_dir)

    # ── Phase 2: Reconcile ────────────────────────────────────────────────
    edges, issues = phase_2_reconcile(pods)
    _write_phase_2(edges, issues, phases_dir)

    # ── Phase 3: Timeline ─────────────────────────────────────────────────
    timeline = phase_3_timeline(pods)
    _write_phase_3(timeline, phases_dir)

    # ── Phase 4: Metrics ──────────────────────────────────────────────────
    metrics = await phase_4_metrics(
        pods, edges, timeline, issues, api_key,
        inference_parameters=inference_parameters,
    )
    _write_phase_4(metrics, pods, phases_dir)

    # Reconciliation audit (needs Phase 4 context for LLM + historical cross-ref)
    _write_reconciliation_audit(pods, edges, issues, metrics, phases_dir, started)

    # ── Phase 5: Assemble ─────────────────────────────────────────────────
    colony_map = phase_5_assemble(
        gateway_url=gateway_url,
        pods=pods,
        edges=edges,
        timeline=timeline,
        issues=issues,
        metrics=metrics,
        discovery_order=discovery_order,
        unreachable=unreachable,
        output_dir=output_dir,
    )

    elapsed = (datetime.now(tz=timezone.utc) - started).total_seconds()
    logger.info("")
    logger.info("═" * 60)
    logger.info("  MAPPING COMPLETE — %.1fs", elapsed)
    logger.info("  Output: %s/map.json", output_dir)
    logger.info("  Pods: %d  |  Edges: %d  |  Unreconciled: %d",
                colony_map.pod_count(),
                len(colony_map.edges),
                colony_map.unreconciled_edge_count())
    logger.info("═" * 60)

    return colony_map


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    asyncio.run(run())
