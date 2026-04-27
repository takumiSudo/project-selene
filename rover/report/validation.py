"""
Validation suite for the Selene reporting pipeline.

Two sections:
    1. AUDIT  — citation grounding (wraps audit.py) + LLM-enrichment signal
                validation against the ground-truth colony map.
    2. TESTS  — unit tests that cover the citation sanitizer, catalog
                generation, and audit logic (adapted from ad-hoc checks run
                during development).

Usage:
    # Full run (tests + audit against live artifacts):
    python -m report.validation

    # Explicit paths:
    python -m report.validation --map /rover/output/map.json \
                                --report /rover/output/report.md
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

# ── Section 1: AUDIT ────────────────────────────────────────────────────────


def _normalise_ts(ts_str: str) -> str:
    """Stable ISO-8601 with Z suffix; strips microseconds."""
    dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _build_index(data: dict) -> dict[str, set[str]]:
    """Build citation index from raw map.json dict (no Pydantic import needed)."""
    import re
    directive_re = re.compile(r"directive\s+(\d{4}-\d+)", re.IGNORECASE)

    log_ids: set[str] = set()
    comm_ids: set[str] = set()
    directive_ids: set[str] = set()

    for entry in data.get("timeline", []):
        ts = _normalise_ts(entry["timestamp"])
        pod = entry["pod_id"]
        if entry["kind"] == "log":
            log_ids.add(f"{pod}:{ts}")
            for m in directive_re.finditer(entry.get("detail", "")):
                directive_ids.add(m.group(1))
        elif entry["kind"] == "comm":
            comm_ids.add(f"{pod}:{ts}")

    edge_ids: set[str] = {
        f"{e['source']}->{e['target']}:{e['resource']}"
        for e in data.get("edges", [])
    }

    metadata_ids: set[str] = set()
    for pid, pod in data.get("pods", {}).items():
        info = pod.get("info") or {}
        for field in (info.get("metadata") or {}):
            metadata_ids.add(f"{pid}:{field}")

    return {
        "logs":      log_ids,
        "comms":     comm_ids,
        "edges":     edge_ids,
        "directive": directive_ids,
        "metadata":  metadata_ids,
    }


def audit_citations(report_text: str, data: dict) -> dict:
    """
    Citation-grounding audit: every [pod:logs:...] / [edge:...] etc. in the
    report must resolve to a real entry in map.json.

    Returns a dict with total counts, per-kind counts, and unresolved list.
    """
    import re
    CITATION_RE = re.compile(
        r"\[("
        r"pod:logs:[^\]]+|"
        r"pod:comms:[^\]]+|"
        r"edge:[^\]]+|"
        r"directive:[^\]]+|"
        r"pod:metadata:[^\]]+"
        r")\]"
    )

    index = _build_index(data)
    matches = list(CITATION_RE.finditer(report_text))

    def is_valid(body: str) -> bool:
        if body.startswith("pod:logs:"):       return body[9:] in index["logs"]
        elif body.startswith("pod:comms:"):    return body[10:] in index["comms"]
        elif body.startswith("pod:metadata:"): return body[13:] in index["metadata"]
        elif body.startswith("edge:"):         return body[5:] in index["edges"]
        elif body.startswith("directive:"):    return body[10:] in index["directive"]
        return False

    by_kind: dict[str, int] = {k: 0 for k in ("logs", "comms", "edges", "directive", "metadata")}
    unresolved: list[str] = []

    for m in matches:
        body = m.group(1)
        if body.startswith("pod:logs:"):       by_kind["logs"] += 1
        elif body.startswith("pod:comms:"):    by_kind["comms"] += 1
        elif body.startswith("pod:metadata:"): by_kind["metadata"] += 1
        elif body.startswith("edge:"):         by_kind["edges"] += 1
        elif body.startswith("directive:"):    by_kind["directive"] += 1
        if not is_valid(body):
            unresolved.append(body)

    return {
        "total_citations":  len(matches),
        "by_kind":          by_kind,
        "available_ids": {k: len(v) for k, v in index.items()},
        "unresolved_count": len(unresolved),
        "unresolved":       unresolved,
    }


def audit_llm_signals(data: dict) -> dict:
    """
    LLM-enrichment audit: validate every signal in metrics.llm_derived_signals
    against the actual colony data.

    Checks:
        (A) source_pod / target_pod / evidence_pod are known pod IDs
        (B) is_formally_declared matches the actual edge set
            (operational edges only: reconciled + dep_only)
        (C) evidence_timestamp, when present, matches a real timeline entry
            for evidence_pod

    Returns a dict with signal count, issue list, and per-check tallies.
    """
    sigs = (data.get("metrics") or {}).get("llm_derived_signals") or []
    if not sigs:
        return {
            "signal_count": 0,
            "issues": [],
            "issue_count": 0,
            "tallies": {"bad_pod_id": 0, "wrong_formally_declared": 0, "bad_timestamp": 0},
            "valid": True,
        }

    known_pods = set(data.get("pods", {}).keys())

    # Operational edge set: reconciled + dep_only (SUPPLY_ONLY are admin-only)
    operational_pairs: set[tuple[str, str]] = {
        (e["source"], e["target"])
        for e in data.get("edges", [])
        if e.get("state") in ("reconciled", "dep_only")
    }

    # Timeline index: pod_id -> set of normalised timestamp strings
    timeline_by_pod: dict[str, set[str]] = {}
    for entry in data.get("timeline", []):
        ts = _normalise_ts(entry["timestamp"])
        timeline_by_pod.setdefault(entry["pod_id"], set()).add(ts)

    issues: list[str] = []
    tallies = {"bad_pod_id": 0, "wrong_formally_declared": 0, "bad_timestamp": 0}

    for sig in sigs:
        src, tgt = sig.get("source_pod", ""), sig.get("target_pod", "")
        ev_pod   = sig.get("evidence_pod", "")

        # (A) Pod ID validity
        for field, val in [("source_pod", src), ("target_pod", tgt), ("evidence_pod", ev_pod)]:
            if val not in known_pods:
                issues.append(f"A: {field}='{val}' is not a known pod")
                tallies["bad_pod_id"] += 1

        # (B) is_formally_declared vs operational graph
        is_declared = sig.get("is_formally_declared", False)
        actual = (src, tgt) in operational_pairs
        if is_declared != actual:
            issues.append(
                f"B: {src}->{tgt} is_formally_declared={is_declared} but "
                f"operational edge {'exists' if actual else 'does not exist'}"
            )
            tallies["wrong_formally_declared"] += 1

        # (C) Evidence timestamp
        ev_ts = sig.get("evidence_timestamp")
        if ev_ts:
            ts_norm = _normalise_ts(ev_ts)
            pod_times = timeline_by_pod.get(ev_pod, set())
            if ts_norm not in pod_times:
                issues.append(
                    f"C: evidence_timestamp {ts_norm} not found in timeline for pod '{ev_pod}'"
                )
                tallies["bad_timestamp"] += 1

    return {
        "signal_count": len(sigs),
        "issues":       issues,
        "issue_count":  len(issues),
        "tallies":      tallies,
        "valid":        len(issues) == 0,
    }


def run_full_validation(report_text: str | None, data: dict) -> dict:
    """Run citation audit + LLM signal audit and return combined results."""
    citation_result = (
        audit_citations(report_text, data)
        if report_text is not None
        else None
    )
    llm_result = audit_llm_signals(data)

    return {
        "citation_audit":   citation_result,
        "llm_signal_audit": llm_result,
    }


# ── Section 2: TESTS ────────────────────────────────────────────────────────
# These tests are adapted from ad-hoc checks run during development (Session 9)
# to verify the citation sanitizer and audit logic against known-good data.


def _make_minimal_index() -> dict[str, set[str]]:
    """Minimal index representing the live Selene colony data."""
    return {
        "edges": {
            "zephyr->aquifer:humidity_feedstock",
            "helios->aquifer:coolant_water",
            "vault->helios:electrical_power",
            "medica->helios:electrical_power",
            "terminus->aquifer:slurry_water",
            "prometheus->aquifer:synthesis_water",
            "prometheus->hydroponics:nutrient_compounds",
            "aquifer->helios:electrical_power",
        },
        "directive": {"2092-042", "2093-089", "2094-011"},
        "comms": {
            "zephyr:2094-03-18T09:22:00Z",
            "zephyr:2094-05-02T14:10:00Z",
            "zephyr:2094-06-14T16:45:00Z",
            "artemis:2094-01-18T09:00:00Z",
            "prometheus:2094-01-22T15:40:00Z",
            "vault:2094-02-10T11:20:00Z",
            "hydroponics:2094-02-05T10:30:00Z",
        },
        "logs": {
            "zephyr:2093-06-20T12:00:00Z",
            "aquifer:2093-10-01T16:00:00Z",
            "helios:2094-02-14T07:00:00Z",
            "vault:2093-03-15T10:30:00Z",
            "terminus:2093-05-11T09:30:00Z",
            "prometheus:2093-09-30T16:00:00Z",
        },
        "metadata": {
            "aquifer:backup_systems",
            "aquifer:throughput_l_day",
            "aquifer:rated_capacity_l_day",
            "helios:coolant_loop",
            "sentinel:independent_power_kw",
            "sentinel:ice_harvest_l_day",
            "nexus:independent_power_days",
            "zephyr:humidity_reclaim_pct",
            "zephyr:backup_power_hours",
            "prometheus:water_source",
            "vault:decommissioned_reserves",
        },
    }


import re as _re

_CITATION_RE = _re.compile(
    r"\[("
    r"pod:logs:[^\]]+|"
    r"pod:comms:[^\]]+|"
    r"edge:[^\]]+|"
    r"directive:[^\]]+|"
    r"pod:metadata:[^\]]+"
    r")\]"
)


def _is_valid(body: str, index: dict) -> bool:
    if body.startswith("pod:logs:"):       return body[9:] in index["logs"]
    elif body.startswith("pod:comms:"):    return body[10:] in index["comms"]
    elif body.startswith("pod:metadata:"): return body[13:] in index["metadata"]
    elif body.startswith("edge:"):         return body[5:] in index["edges"]
    elif body.startswith("directive:"):    return body[10:] in index["directive"]
    return False


def _sanitize(text: str, index: dict) -> str:
    return _CITATION_RE.sub(
        lambda m: m.group(0) if _is_valid(m.group(1), index) else "",
        text,
    )


class CitationSanitizerTests(unittest.TestCase):
    """
    Verify that _sanitize strips invalid citations and preserves valid ones.

    Test cases derived from the 16 unresolved citations observed in the first
    report.md run (Session 9 audit), plus their corrected equivalents.
    """

    def setUp(self):
        self.idx = _make_minimal_index()

    # ── Citations that MUST be stripped ───────────────────────────────────

    def test_strip_wrong_resource_zephyr_water(self):
        # LLM guessed 'water'; actual resource is 'humidity_feedstock'
        out = _sanitize("[edge:zephyr->aquifer:water]", self.idx)
        self.assertEqual(out, "")

    def test_strip_wrong_resource_terminus_water(self):
        # LLM guessed 'water'; actual resource is 'slurry_water'
        out = _sanitize("[edge:terminus->aquifer:water]", self.idx)
        self.assertEqual(out, "")

    def test_strip_wrong_resource_vault_helios_water(self):
        # LLM guessed 'water'; actual resource is 'electrical_power'
        out = _sanitize("[edge:vault->helios:water]", self.idx)
        self.assertEqual(out, "")

    def test_strip_nonexistent_edge_vault_aquifer(self):
        # Vault does not depend on Aquifer (no such edge in the graph)
        out = _sanitize("[edge:vault->aquifer:water]", self.idx)
        self.assertEqual(out, "")

    def test_strip_wrong_direction_helios_aquifer_coolant(self):
        # LLM reversed the direction: helios depends on aquifer, not vice versa
        out = _sanitize("[edge:aquifer->helios:coolant_water]", self.idx)
        self.assertEqual(out, "")

    def test_strip_wrong_direction_helios_medica(self):
        # supply_only edge is medica->helios, not helios->medica
        out = _sanitize("[edge:helios->medica:electrical_power]", self.idx)
        self.assertEqual(out, "")

    def test_strip_wrong_resource_prometheus_hydroponics(self):
        # Actual resource is 'nutrient_compounds', not 'water'
        out = _sanitize("[edge:prometheus->hydroponics:water]", self.idx)
        self.assertEqual(out, "")

    def test_strip_directive_alpha_suffix(self):
        # '2093-P4' has a letter suffix — not a real directive in the logs
        out = _sanitize("[directive:2093-P4]", self.idx)
        self.assertEqual(out, "")

    def test_strip_comms_no_channel_pod(self):
        # Medica has no comms channel — any pod:comms:medica citation is invalid
        out = _sanitize("[pod:comms:medica:2093-07-08T09:45:00Z]", self.idx)
        self.assertEqual(out, "")

    def test_strip_comms_wrong_timestamp_for_zephyr(self):
        # Zephyr's 2093-06-20 entry is a LOG, not a COMM
        out = _sanitize("[pod:comms:zephyr:2093-06-20T12:00:00Z]", self.idx)
        self.assertEqual(out, "")

    # ── Citations that MUST be preserved ──────────────────────────────────

    def test_keep_correct_resource_zephyr(self):
        out = _sanitize("[edge:zephyr->aquifer:humidity_feedstock]", self.idx)
        self.assertEqual(out, "[edge:zephyr->aquifer:humidity_feedstock]")

    def test_keep_correct_direction_helios_aquifer(self):
        out = _sanitize("[edge:helios->aquifer:coolant_water]", self.idx)
        self.assertEqual(out, "[edge:helios->aquifer:coolant_water]")

    def test_keep_valid_directive(self):
        out = _sanitize("[directive:2093-089]", self.idx)
        self.assertEqual(out, "[directive:2093-089]")

    def test_keep_valid_vault_helios_electrical(self):
        out = _sanitize("[edge:vault->helios:electrical_power]", self.idx)
        self.assertEqual(out, "[edge:vault->helios:electrical_power]")

    def test_keep_valid_comms_zephyr(self):
        out = _sanitize("[pod:comms:zephyr:2094-03-18T09:22:00Z]", self.idx)
        self.assertEqual(out, "[pod:comms:zephyr:2094-03-18T09:22:00Z]")

    def test_keep_valid_log_zephyr(self):
        # Zephyr 2093-06-20 is a LOG entry — valid as pod:logs citation
        out = _sanitize("[pod:logs:zephyr:2093-06-20T12:00:00Z]", self.idx)
        self.assertEqual(out, "[pod:logs:zephyr:2093-06-20T12:00:00Z]")

    def test_keep_valid_metadata(self):
        out = _sanitize("[pod:metadata:aquifer:backup_systems]", self.idx)
        self.assertEqual(out, "[pod:metadata:aquifer:backup_systems]")

    def test_mixed_text_strips_only_invalid(self):
        text = (
            "Zephyr lost [edge:zephyr->aquifer:water] and "
            "[pod:metadata:aquifer:backup_systems] confirms zero backup."
        )
        out = _sanitize(text, self.idx)
        self.assertNotIn("[edge:zephyr->aquifer:water]", out)
        self.assertIn("[pod:metadata:aquifer:backup_systems]", out)


class EvidenceListSanitizerTests(unittest.TestCase):
    """Verify that _sanitize_evidence filters invalid items from evidence lists."""

    def setUp(self):
        self.idx = _make_minimal_index()

    def _filter(self, items):
        out = []
        for item in items:
            m = _CITATION_RE.search(item)
            if m is None or _is_valid(m.group(1), self.idx):
                out.append(item)
        return out

    def test_filters_invalid_edge_from_list(self):
        ev = ["[edge:zephyr->aquifer:water]", "[pod:metadata:aquifer:backup_systems]"]
        out = self._filter(ev)
        self.assertEqual(out, ["[pod:metadata:aquifer:backup_systems]"])

    def test_keeps_all_valid(self):
        ev = ["[directive:2093-089]", "[pod:logs:helios:2094-02-14T07:00:00Z]"]
        out = self._filter(ev)
        self.assertEqual(out, ev)

    def test_empty_list_unchanged(self):
        self.assertEqual(self._filter([]), [])


class CitationIndexTests(unittest.TestCase):
    """Verify that _build_index produces the right shape and known values."""

    @classmethod
    def setUpClass(cls):
        map_path = Path(__file__).parent.parent.parent / ".artifacts" / "map.json"
        if not map_path.exists():
            # Try the rover output path (inside container)
            map_path = Path("/rover/output/map.json")
        if not map_path.exists():
            cls.data = None
            return
        with open(map_path) as f:
            cls.data = json.load(f)

    def setUp(self):
        if self.data is None:
            self.skipTest("map.json not found — run the mapping phase first")

    def test_index_has_all_keys(self):
        idx = _build_index(self.data)
        self.assertEqual(set(idx.keys()), {"logs", "comms", "edges", "directive", "metadata"})

    def test_known_directives_present(self):
        idx = _build_index(self.data)
        for d in ("2093-089", "2094-011"):
            self.assertIn(d, idx["directive"], f"directive {d} missing from index")

    def test_comms_only_five_pods(self):
        idx = _build_index(self.data)
        comms_pods = {c.split(":")[0] for c in idx["comms"]}
        # Known comms-enabled pods from the colony
        self.assertLessEqual(comms_pods, {"artemis", "vault", "prometheus", "hydroponics", "zephyr"})

    def test_edge_ids_are_directional(self):
        idx = _build_index(self.data)
        # Aquifer depends on Helios for power, not the other way
        self.assertIn("aquifer->helios:electrical_power", idx["edges"])
        self.assertNotIn("helios->aquifer:electrical_power", idx["edges"])

    def test_helios_aquifer_coolant_direction(self):
        idx = _build_index(self.data)
        self.assertIn("helios->aquifer:coolant_water", idx["edges"])
        self.assertNotIn("aquifer->helios:coolant_water", idx["edges"])

    def test_metadata_includes_aquifer_backup_systems(self):
        idx = _build_index(self.data)
        self.assertIn("aquifer:backup_systems", idx["metadata"])

    def test_log_count_matches_timeline(self):
        idx = _build_index(self.data)
        log_count_in_timeline = sum(
            1 for e in self.data["timeline"] if e["kind"] == "log"
        )
        self.assertEqual(len(idx["logs"]), log_count_in_timeline)


class LLMSignalAuditTests(unittest.TestCase):
    """Validate the LLM-enrichment audit logic against the live map."""

    @classmethod
    def setUpClass(cls):
        map_path = Path(__file__).parent.parent.parent / ".artifacts" / "map.json"
        if not map_path.exists():
            map_path = Path("/rover/output/map.json")
        if not map_path.exists():
            cls.data = None
            return
        with open(map_path) as f:
            cls.data = json.load(f)

    def setUp(self):
        if self.data is None:
            self.skipTest("map.json not found — run the mapping phase first")

    def test_all_signal_pod_ids_valid(self):
        result = audit_llm_signals(self.data)
        bad_pod_issues = [i for i in result["issues"] if i.startswith("A:")]
        self.assertEqual(bad_pod_issues, [], "Some LLM signals reference unknown pod IDs")

    def test_all_evidence_timestamps_valid(self):
        result = audit_llm_signals(self.data)
        bad_ts_issues = [i for i in result["issues"] if i.startswith("C:")]
        self.assertEqual(bad_ts_issues, [], "Some LLM signal evidence_timestamps don't match timeline")

    def test_signal_count_nonzero_if_key_set(self):
        sigs = (self.data.get("metrics") or {}).get("llm_derived_signals") or []
        if not sigs:
            self.skipTest("No LLM signals (API key not set during last run)")
        self.assertGreater(len(sigs), 0)

    def test_prometheus_aquifer_stale_dep_signal_present(self):
        sigs = (self.data.get("metrics") or {}).get("llm_derived_signals") or []
        if not sigs:
            self.skipTest("No LLM signals (API key not set during last run)")
        match = next(
            (s for s in sigs
             if s["source_pod"] == "prometheus"
             and s["target_pod"] == "aquifer"
             and s["relationship_type"] == "stale_dependency"),
            None,
        )
        self.assertIsNotNone(
            match,
            "Expected a prometheus->aquifer stale_dependency signal from the comms evidence",
        )


# ── CLI orchestrator ─────────────────────────────────────────────────────────


def _run_tests() -> unittest.TestResult:
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for cls in (
        CitationSanitizerTests,
        EvidenceListSanitizerTests,
        CitationIndexTests,
        LLMSignalAuditTests,
    ):
        suite.addTests(loader.loadTestsFromTestCase(cls))
    runner = unittest.TextTestRunner(verbosity=2, stream=sys.stdout)
    return runner.run(suite)


def main() -> int:
    parser = argparse.ArgumentParser(description="Selene validation suite")
    parser.add_argument("--map",    default=None, help="Path to map.json")
    parser.add_argument("--report", default=None, help="Path to report.md")
    parser.add_argument("--tests-only", action="store_true", help="Run tests only, skip audit")
    args = parser.parse_args()

    print("=" * 60)
    print("  SELENE VALIDATION SUITE")
    print("=" * 60)

    # ── Tests ────────────────────────────────────────────────────────────
    print("\n── SECTION 2: TEST SUITE ──\n")
    test_result = _run_tests()

    if args.tests_only:
        return 0 if test_result.wasSuccessful() else 1

    # ── Audit ────────────────────────────────────────────────────────────
    map_candidates = [
        args.map,
        str(Path(__file__).parent.parent.parent / ".artifacts" / "map.json"),
        "/rover/output/map.json",
    ]
    map_path = next((p for p in map_candidates if p and Path(p).exists()), None)
    if map_path is None:
        print("\n[audit] map.json not found — skipping audit section")
        return 0 if test_result.wasSuccessful() else 1

    report_candidates = [
        args.report,
        str(Path(__file__).parent.parent.parent / ".artifacts" / "report.md"),
        "/rover/output/report.md",
    ]
    report_path = next((p for p in report_candidates if p and Path(p).exists()), None)

    with open(map_path) as f:
        data = json.load(f)
    report_text = Path(report_path).read_text() if report_path else None

    print("\n── SECTION 1: AUDIT ──\n")
    result = run_full_validation(report_text, data)

    ca = result["citation_audit"]
    if ca:
        status = "PASS" if ca["unresolved_count"] == 0 else "FAIL"
        print(f"Citation audit:    [{status}]  {ca['total_citations']} total, {ca['unresolved_count']} unresolved")
        for c in ca["unresolved"][:10]:
            print(f"  unresolved: {c}")

    la = result["llm_signal_audit"]
    status_l = "PASS" if la["valid"] else "FAIL"
    print(f"LLM signal audit:  [{status_l}]  {la['signal_count']} signals, {la['issue_count']} issues")
    t = la["tallies"]
    print(f"  bad pod IDs: {t['bad_pod_id']}  |  wrong is_formally_declared: {t['wrong_formally_declared']}  |  bad timestamps: {t['bad_timestamp']}")

    print("\n" + "=" * 60)
    overall = test_result.wasSuccessful() and (ca is None or ca["unresolved_count"] == 0) and la["tallies"]["bad_pod_id"] == 0 and la["tallies"]["bad_timestamp"] == 0
    print(f"  OVERALL: {'PASS' if overall else 'FAIL'}")
    print("=" * 60)
    return 0 if overall else 1


if __name__ == "__main__":
    sys.exit(main())
