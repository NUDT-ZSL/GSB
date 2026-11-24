#!/usr/bin/env python3
"""Compare component output against truth.json through the sync marker."""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from certpath.canon import load_json
from certpath.engine import CertPathEngine


def _compare(sample_dir: Path) -> dict[str, Any]:
    truth = load_json(sample_dir / "truth.json")
    marker = load_json(sample_dir / "sync.marker.json")
    with tempfile.TemporaryDirectory(prefix="certpath-validate-") as workspace:
        report = CertPathEngine(workspace).adjudicate(sample_dir)
    mismatches: list[str] = []
    if truth.get("sync_marker") != marker.get("sync_marker"):
        mismatches.append("truth/marker sync value differs")
    if report.get("sync_marker") != marker.get("sync_marker"):
        mismatches.append("component did not read the declared sync marker")
    if report.get("sync_marker_verified") is not True:
        mismatches.append("component sync marker verification failed")

    actual_chains = {item["chain_id"]: item for item in report["chains"]}
    for chain_id, expected in truth["chains"].items():
        actual = actual_chains.get(chain_id)
        if actual is None:
            mismatches.append(f"{chain_id}: missing chain result")
            continue
        if actual["conclusion"] != expected["conclusion"]:
            mismatches.append(
                f"{chain_id}: conclusion {actual['conclusion']} != {expected['conclusion']}"
            )
        actual_paths = [
            item["fingerprint_sequence"] for item in actual["candidates"]
        ]
        if actual_paths != expected["candidate_paths"]:
            mismatches.append(f"{chain_id}: candidate paths differ")
        expected_selected = expected.get("selected")
        actual_selected = actual.get("selected_fingerprint_sequence")
        if actual_selected != expected_selected:
            mismatches.append(f"{chain_id}: selected path differs")
        if "trust_generation" in expected:
            if actual.get("trust_generation") != expected["trust_generation"]:
                mismatches.append(f"{chain_id}: trust generation differs")
        for key in (
            "truncated",
            "truncated_count",
            "truncated_has_valid",
            "candidates_total",
        ):
            if key in expected and actual.get(
                "enumeration_truncated" if key == "truncated" else key
            ) != expected[key]:
                mismatches.append(f"{chain_id}: {key} differs")
        for code in expected.get("required_codes", []):
            if code not in actual["business_codes"]:
                mismatches.append(f"{chain_id}: missing code {code}")

    conflict_fields = {
        (item["fingerprint"], item["field"])
        for item in report["snapshot"]["conflicts"]
    }
    for expected in truth.get("expected_conflicts", []):
        key = (expected["fingerprint"], expected["field"])
        if key not in conflict_fields:
            mismatches.append(
                f"missing trust conflict {expected['fingerprint']}/{expected['field']}"
            )
    return {
        "format": "certpath.validation-result",
        "version": 1,
        "sync_marker": marker.get("sync_marker"),
        "chains_checked": len(truth["chains"]),
        "summary": report["summary"],
        "mismatches": mismatches,
        "ok": not mismatches,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--samples", default="samples")
    args = parser.parse_args()
    result = _compare(Path(args.samples))
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
