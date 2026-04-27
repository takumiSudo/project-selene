"""
Citation grounding audit for the reporter.

Parses the rendered report text for citations and validates each one against
the actual `map.json` data.  Used as a pre-publish sanity check so the report
cannot ship a hallucinated reference.

Citation grammar:
    [pod:logs:POD:ISO_TIMESTAMP]
    [pod:comms:POD:ISO_TIMESTAMP]
    [edge:SOURCE->TARGET:RESOURCE]
    [directive:DIRECTIVE_ID]
    [pod:metadata:POD:FIELD]

Returns a list of unresolved citations (empty list = clean report).
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from map.models import ColonyMap

logger = logging.getLogger(__name__)

# One pattern that captures the entire citation, then dispatch on prefix.
_CITATION_RE = re.compile(
    r"\[("
    r"pod:logs:[^\]]+|"
    r"pod:comms:[^\]]+|"
    r"edge:[^\]]+|"
    r"directive:[^\]]+|"
    r"pod:metadata:[^\]]+"
    r")\]"
)

# Match a "Directive YYYY-NNN" anywhere in a log detail.
_DIRECTIVE_RE = re.compile(r"directive\s+(\d{4}-\d+)", re.IGNORECASE)


def _normalise_ts(ts: datetime) -> str:
    """Stable ISO-8601 with Z suffix; no microseconds."""
    return ts.strftime("%Y-%m-%dT%H:%M:%SZ")


def build_citation_index(colony_map: "ColonyMap") -> dict[str, set[str]]:
    """Pre-compute the set of valid citation IDs for fast lookup."""
    log_ids: set[str] = set()
    comm_ids: set[str] = set()
    for entry in colony_map.timeline:
        ts_str = _normalise_ts(entry.timestamp)
        if entry.kind == "log":
            log_ids.add(f"{entry.pod_id}:{ts_str}")
        elif entry.kind == "comm":
            comm_ids.add(f"{entry.pod_id}:{ts_str}")

    edge_ids = {f"{e.source}->{e.target}:{e.resource}" for e in colony_map.edges}

    directive_ids: set[str] = set()
    for entry in colony_map.timeline:
        if entry.kind == "log":
            for m in _DIRECTIVE_RE.finditer(entry.detail):
                directive_ids.add(m.group(1))

    metadata_ids: set[str] = set()
    for pid, pod in colony_map.pods.items():
        if pod.info:
            for field in pod.info.metadata:
                metadata_ids.add(f"{pid}:{field}")

    return {
        "logs":      log_ids,
        "comms":     comm_ids,
        "edges":     edge_ids,
        "directive": directive_ids,
        "metadata":  metadata_ids,
    }


def audit_report(report_text: str, colony_map: "ColonyMap") -> list[str]:
    """Find every unresolved citation in `report_text`.

    Returns a list of unresolved citation strings (empty list on success).
    """
    index = build_citation_index(colony_map)
    unresolved: list[str] = []

    for match in _CITATION_RE.finditer(report_text):
        body = match.group(1)

        if body.startswith("pod:logs:"):
            cid = body[len("pod:logs:"):]
            if cid not in index["logs"]:
                unresolved.append(f"pod:logs:{cid}")
        elif body.startswith("pod:comms:"):
            cid = body[len("pod:comms:"):]
            if cid not in index["comms"]:
                unresolved.append(f"pod:comms:{cid}")
        elif body.startswith("pod:metadata:"):
            cid = body[len("pod:metadata:"):]
            if cid not in index["metadata"]:
                unresolved.append(f"pod:metadata:{cid}")
        elif body.startswith("edge:"):
            cid = body[len("edge:"):]
            if cid not in index["edges"]:
                unresolved.append(f"edge:{cid}")
        elif body.startswith("directive:"):
            cid = body[len("directive:"):]
            if cid not in index["directive"]:
                unresolved.append(f"directive:{cid}")

    return unresolved


def summarise_audit(report_text: str, colony_map: "ColonyMap") -> dict:
    """Return a dict with citation counts + unresolved list."""
    index = build_citation_index(colony_map)
    matches = list(_CITATION_RE.finditer(report_text))

    by_kind: dict[str, int] = {"logs": 0, "comms": 0, "edges": 0, "directive": 0, "metadata": 0}
    for m in matches:
        body = m.group(1)
        if body.startswith("pod:logs:"):       by_kind["logs"] += 1
        elif body.startswith("pod:comms:"):    by_kind["comms"] += 1
        elif body.startswith("pod:metadata:"): by_kind["metadata"] += 1
        elif body.startswith("edge:"):         by_kind["edges"] += 1
        elif body.startswith("directive:"):    by_kind["directive"] += 1

    unresolved = audit_report(report_text, colony_map)
    return {
        "total_citations": len(matches),
        "by_kind":         by_kind,
        "available_ids": {
            "logs":      len(index["logs"]),
            "comms":     len(index["comms"]),
            "edges":     len(index["edges"]),
            "directive": len(index["directive"]),
            "metadata":  len(index["metadata"]),
        },
        "unresolved_count": len(unresolved),
        "unresolved":       unresolved,
    }
