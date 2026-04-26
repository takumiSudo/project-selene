"""
Full endpoint crawl — Phase 3 + edge reconciliation — Phase 4.

Given a pod_registry from discovery.py, this module:
  1. Fetches all 6 endpoints per pod concurrently (parallelised across pods
     and across endpoints within each pod).
  2. Parses each response into the typed models from models.py.
  3. Builds the merged, timestamp-sorted timeline (Decision 5).
  4. Runs the edge reconciliation pass (Decision 4), producing DependencyEdge
     objects with declared_by_source / declared_by_target flags and EdgeState.

A key subtlety from reading the actual pod configs:
  - /comms returns 404 (not an empty list) when a pod has no comms channel.
    comms=None in PodNode means 404; comms=[] means the channel exists but
    has no messages (in practice this doesn't happen, but it's modelled).
  - /supplies entries have no criticality or notes — defaults are used.
  - The "from"/"to" fields in comms messages are role identifiers
    (e.g. "artemis_ops"), not pod_ids. pod_id is set to the pod whose
    /comms endpoint we were crawling.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

import httpx

from .models import (
    CommEntry,
    Criticality,
    DependencyEdge,
    EdgeState,
    LogEntry,
    PodInfo,
    PodNode,
    PodStatus,
    ReconciliationIssue,
    ResourceLink,
)

logger = logging.getLogger(__name__)

FETCH_TIMEOUT = 10.0
# Max simultaneous open connections (12 pods × 6 endpoints = 72 max in-flight).
CONCURRENCY_LIMIT = 24


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------


async def _get(
    client: httpx.AsyncClient,
    url: str,
    sem: asyncio.Semaphore,
) -> tuple[int, dict | None]:
    """Return (status_code, json_body) for a GET request.

    Returns (0, None) on network / timeout errors.
    The status code is preserved so callers can distinguish 404 (expected for
    /comms on silent pods) from genuine failures.
    """
    async with sem:
        try:
            r = await client.get(url, timeout=FETCH_TIMEOUT)
            try:
                body = r.json()
            except Exception:
                body = None
            return r.status_code, body
        except Exception as exc:
            logger.debug("GET %s failed: %s", url, exc)
            return 0, None


# ---------------------------------------------------------------------------
# Single-pod crawl
# ---------------------------------------------------------------------------


async def _crawl_pod(
    client: httpx.AsyncClient,
    pod_id: str,
    hostname: str,
    port: int,
    sem: asyncio.Semaphore,
) -> PodNode:
    """Fetch all 6 endpoints for one pod concurrently and return a PodNode."""
    base = f"http://{hostname}:{port}"

    (info_status, info_data), \
    (status_status, status_data), \
    (dep_status, dep_data), \
    (sup_status, sup_data), \
    (log_status, log_data), \
    (comm_status, comm_data) = await asyncio.gather(
        _get(client, f"{base}/info",         sem),
        _get(client, f"{base}/status",       sem),
        _get(client, f"{base}/dependencies", sem),
        _get(client, f"{base}/supplies",     sem),
        _get(client, f"{base}/logs",         sem),
        _get(client, f"{base}/comms",        sem),
    )

    node = PodNode(id=pod_id, hostname=hostname, port=port)

    # ── /info ────────────────────────────────────────────────────────────
    if info_status == 200 and info_data:
        try:
            node.info = PodInfo(
                id=info_data.get("id", pod_id),
                name=info_data.get("name", pod_id),
                role=info_data.get("role", ""),
                population=info_data.get("population", 0),
                status=info_data.get("status", ""),
                uptime_days=info_data.get("uptime_days", 0),
                metadata=info_data.get("metadata", {}),
            )
        except Exception as exc:
            node.crawl_errors["/info"] = str(exc)
    elif info_status != 200:
        node.crawl_errors["/info"] = f"HTTP {info_status}"

    pod_name = node.info.name if node.info else pod_id

    # ── /status ──────────────────────────────────────────────────────────
    if status_status == 200 and status_data:
        try:
            node.status = PodStatus(
                status=status_data.get("status", "unknown"),
                alerts=status_data.get("alerts", []),
                last_incident=status_data.get("last_incident"),
            )
        except Exception as exc:
            node.crawl_errors["/status"] = str(exc)
    elif status_status != 200:
        node.crawl_errors["/status"] = f"HTTP {status_status}"

    # ── /dependencies ────────────────────────────────────────────────────
    if dep_status == 200 and dep_data:
        try:
            node.raw_dependencies = [
                ResourceLink(
                    pod_id=d["pod_id"],
                    resource=d.get("resource", ""),
                    criticality=_parse_criticality(d.get("criticality", "")),
                    notes=d.get("notes", ""),
                )
                for d in dep_data.get("dependencies", [])
                if "pod_id" in d
            ]
        except Exception as exc:
            node.crawl_errors["/dependencies"] = str(exc)
    elif dep_status != 200:
        node.crawl_errors["/dependencies"] = f"HTTP {dep_status}"

    # ── /supplies ────────────────────────────────────────────────────────
    if sup_status == 200 and sup_data:
        try:
            node.raw_supplies = [
                ResourceLink(
                    pod_id=s["pod_id"],
                    resource=s.get("resource", ""),
                    # /supplies entries don't carry criticality — left as UNKNOWN
                )
                for s in sup_data.get("supplies", [])
                if "pod_id" in s
            ]
        except Exception as exc:
            node.crawl_errors["/supplies"] = str(exc)
    elif sup_status != 200:
        node.crawl_errors["/supplies"] = f"HTTP {sup_status}"

    # ── /logs ────────────────────────────────────────────────────────────
    if log_status == 200 and log_data:
        try:
            node.logs = [
                LogEntry(
                    timestamp=entry["timestamp"],
                    pod_id=pod_id,
                    pod_name=pod_name,
                    event=entry.get("event", "unknown"),
                    detail=entry.get("detail", ""),
                )
                for entry in log_data.get("logs", [])
                if "timestamp" in entry
            ]
        except Exception as exc:
            node.crawl_errors["/logs"] = str(exc)
    elif log_status != 200:
        node.crawl_errors["/logs"] = f"HTTP {log_status}"

    # ── /comms ───────────────────────────────────────────────────────────
    # 404 → pod has no comms channel → comms stays None
    # 200 → comms channel exists → parse messages (may be empty list)
    if comm_status == 200 and comm_data:
        try:
            node.comms = [
                CommEntry(
                    timestamp=msg["timestamp"],
                    pod_id=pod_id,
                    pod_name=pod_name,
                    sender=msg.get("from", ""),
                    recipient=msg.get("to", ""),
                    content=msg.get("content", ""),
                )
                for msg in comm_data.get("messages", [])
                if "timestamp" in msg
            ]
        except Exception as exc:
            node.crawl_errors["/comms"] = str(exc)
            node.comms = None
    elif comm_status == 404:
        node.comms = None   # expected — no channel configured
    elif comm_status != 0:
        node.crawl_errors["/comms"] = f"HTTP {comm_status}"

    logger.info(
        "Crawled %s: deps=%d supplies=%d logs=%d comms=%s errors=%s",
        pod_name,
        len(node.raw_dependencies),
        len(node.raw_supplies),
        len(node.logs),
        "yes" if node.has_comms else "no",
        list(node.crawl_errors.keys()) or "none",
    )
    return node


# ---------------------------------------------------------------------------
# Batch crawl
# ---------------------------------------------------------------------------


async def crawl_all_pods(
    pod_registry: dict[str, tuple[str, int]],
) -> dict[str, PodNode]:
    """Crawl all discovered pods concurrently.

    Returns a dict of pod_id → PodNode with all available endpoint data filled in.
    """
    sem = asyncio.Semaphore(CONCURRENCY_LIMIT)

    async with httpx.AsyncClient() as client:
        tasks = [
            _crawl_pod(client, pod_id, hostname, port, sem)
            for pod_id, (hostname, port) in pod_registry.items()
        ]
        nodes = await asyncio.gather(*tasks, return_exceptions=False)

    return {node.id: node for node in nodes}


# ---------------------------------------------------------------------------
# Edge reconciliation (Decision 4)
# ---------------------------------------------------------------------------


def reconcile_edges(
    pods: dict[str, PodNode],
) -> tuple[list[DependencyEdge], list[ReconciliationIssue]]:
    """Build the reconciled dependency graph from the crawled pod data.

    For every (source, target, resource) triple that appears in either pod's
    /dependencies or /supplies, a DependencyEdge is created and both boolean
    flags are set independently.  After both passes the edge is finalised:

        RECONCILED   — both pods declared the relationship
        DEP_ONLY     — source claims dependency; target's /supplies omits it
        SUPPLY_ONLY  — target claims it supplies source; source's /deps omits it

    The EdgeState encodes the reconciliation outcome for the reporting phase.
    """
    # edge_map key: (source_pod_id, target_pod_id, resource)
    edge_map: dict[tuple[str, str, str], DependencyEdge] = {}

    def _get_or_create(src: str, tgt: str, resource: str, crit: Criticality) -> DependencyEdge:
        key = (src, tgt, resource)
        if key not in edge_map:
            edge_map[key] = DependencyEdge(
                source=src,
                target=tgt,
                resource=resource,
                criticality=crit,
            )
        else:
            # Upgrade criticality if the dep side carries it (supplies don't)
            if crit != Criticality.UNKNOWN:
                edge_map[key].criticality = crit
        return edge_map[key]

    # Pass 1 — walk every pod's /dependencies (source → target)
    for pod in pods.values():
        for dep in pod.raw_dependencies:
            edge = _get_or_create(pod.id, dep.pod_id, dep.resource, dep.criticality)
            edge.declared_by_source = True

    # Pass 2 — walk every pod's /supplies (target → source, inverted)
    for pod in pods.values():
        for sup in pod.raw_supplies:
            edge = _get_or_create(sup.pod_id, pod.id, sup.resource, Criticality.UNKNOWN)
            edge.declared_by_target = True

    # Finalise all edges (compute reconciled + state)
    edges = [e.finalise() for e in edge_map.values()]

    # Build human-readable reconciliation issues for the report
    issues: list[ReconciliationIssue] = []
    for edge in edges:
        if not edge.reconciled:
            src_name = pods[edge.source].display_name if edge.source in pods else edge.source
            tgt_name = pods[edge.target].display_name if edge.target in pods else edge.target
            if edge.state == EdgeState.DEP_ONLY:
                desc = (
                    f"{src_name} declares a dependency on {tgt_name} for "
                    f"'{edge.resource}', but {tgt_name}'s /supplies does not list {src_name}."
                )
            else:
                desc = (
                    f"{tgt_name} claims to supply '{edge.resource}' to {src_name}, "
                    f"but {src_name}'s /dependencies does not list {tgt_name}."
                )
            issues.append(ReconciliationIssue(
                edge_source=edge.source,
                edge_target=edge.target,
                resource=edge.resource,
                state=edge.state,
                description=desc,
            ))

    logger.info(
        "Reconciliation: %d edges total — %d reconciled, %d unreconciled (%d dep_only, %d supply_only)",
        len(edges),
        sum(1 for e in edges if e.reconciled),
        sum(1 for e in edges if not e.reconciled),
        sum(1 for e in edges if e.state == EdgeState.DEP_ONLY),
        sum(1 for e in edges if e.state == EdgeState.SUPPLY_ONLY),
    )
    return edges, issues


# ---------------------------------------------------------------------------
# Timeline assembly (Decision 5)
# ---------------------------------------------------------------------------


def assemble_timeline(pods: dict[str, PodNode]) -> list[LogEntry | CommEntry]:
    """Merge all pod logs and comms into a single timestamp-sorted timeline.

    Every entry carries pod_id and pod_name so the timeline is self-contained
    for the reporting phase.
    """
    events: list[LogEntry | CommEntry] = []

    for pod in pods.values():
        events.extend(pod.logs)
        if pod.comms:
            events.extend(pod.comms)

    events.sort(key=lambda e: e.timestamp)
    logger.info("Timeline: %d events across %d pods", len(events), len(pods))
    return events


# ---------------------------------------------------------------------------
# Convenience: parse criticality string safely
# ---------------------------------------------------------------------------


def _parse_criticality(value: str) -> Criticality:
    try:
        return Criticality(value.lower())
    except (ValueError, AttributeError):
        return Criticality.UNKNOWN
