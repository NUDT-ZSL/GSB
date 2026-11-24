#!/usr/bin/env python3
"""Validate component output using only truth sync markers for record lookup."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tsaudit.canon import sha256_obj
from tsaudit.engine import AuditEngine


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def verify_marker_file(samples: Path) -> list[str]:
    marker_path = samples / "sync.marker.json"
    marker = _load_json(marker_path)
    errors: list[str] = []
    for relative, expected in marker.get("files", {}).items():
        actual = hashlib.sha256((samples / relative).read_bytes()).hexdigest()
        if actual != expected:
            errors.append(f"file marker mismatch: {relative}")
    total = sha256_obj({
        "files": marker.get("files", {}),
        "records": sorted(marker.get("record_markers", [])),
    })
    if total != marker.get("total_marker"):
        errors.append("total sync marker mismatch")
    return errors


def validate(samples: Path, workspace: Path | None = None) -> dict[str, Any]:
    truth = _load_json(samples / "truth.json")
    marker_errors = verify_marker_file(samples)
    temporary: tempfile.TemporaryDirectory[str] | None = None
    if workspace is None:
        temporary = tempfile.TemporaryDirectory(prefix="tsa-validate-")
        workspace = Path(temporary.name)

    try:
        engine = AuditEngine(workspace)
        report = engine.adjudicate_sample(samples)
        artifact_by_marker = {
            item.get("sync_marker"): item for item in report["artifacts"] if item.get("sync_marker")
        }
        token_by_marker = {
            item.get("sync_marker"): item for item in report["tokens"] if item.get("sync_marker")
        }
        rejection_by_marker = {
            item.get("sync_marker"): item
            for item in report.get("rejections", []) if item.get("sync_marker")
        }
        mismatches: list[dict[str, Any]] = []
        checked = 0

        for case in truth.get("sync_checks", []):
            marker = case["sync_marker"]
            expected = case.get("expected", {})
            checked += 1
            if expected.get("rejected"):
                rejection = rejection_by_marker.get(marker)
                if rejection is None:
                    mismatches.append({"sync_marker": marker, "error": "rejection output missing"})
                elif expected.get("code") and rejection.get("code") != expected["code"]:
                    mismatches.append({
                        "sync_marker": marker, "field": "rejection_code",
                        "expected": expected["code"], "actual": rejection.get("code"),
                    })
                continue
            if case["kind"] == "signature":
                actual = artifact_by_marker.get(marker)
                if actual is None:
                    mismatches.append({"sync_marker": marker, "error": "artifact output missing"})
                    continue
                if "status" in expected and actual["status"] != expected["status"]:
                    mismatches.append({"sync_marker": marker, "field": "status",
                                      "expected": expected["status"], "actual": actual["status"]})
                if "primary_token_id" in expected and actual.get("primary_token_id") != expected["primary_token_id"]:
                    mismatches.append({"sync_marker": marker, "field": "primary_token_id",
                                      "expected": expected["primary_token_id"],
                                      "actual": actual.get("primary_token_id")})
                for code in expected.get("codes", []):
                    if code not in actual.get("business_codes", []):
                        mismatches.append({"sync_marker": marker, "field": "codes",
                                          "missing_code": code,
                                          "actual": actual.get("business_codes")})
                for code in expected.get("codes_include", []):
                    if code not in actual.get("business_codes", []):
                        mismatches.append({"sync_marker": marker, "field": "codes_include",
                                          "missing_code": code,
                                          "actual": actual.get("business_codes")})
                if "alternative_token_ids" in expected and \
                        actual.get("alternative_token_ids") != expected["alternative_token_ids"]:
                    mismatches.append({"sync_marker": marker, "field": "alternative_token_ids",
                                      "expected": expected["alternative_token_ids"],
                                      "actual": actual.get("alternative_token_ids")})
                if "basis_generation" in expected and \
                        actual.get("basis_generation") != expected["basis_generation"]:
                    mismatches.append({"sync_marker": marker, "field": "basis_generation",
                                      "expected": expected["basis_generation"],
                                      "actual": actual.get("basis_generation")})
            elif case["kind"] == "token":
                actual_token = token_by_marker.get(marker)
                if actual_token is None:
                    mismatches.append({"sync_marker": marker, "error": "token output missing"})
                    continue
                if actual_token.get("issued_at_ms") != expected.get("issued_at_ms"):
                    mismatches.append({"sync_marker": marker, "field": "issued_at_ms"})
                if actual_token.get("token_generation") != expected.get("token_generation"):
                    mismatches.append({"sync_marker": marker, "field": "token_generation"})

        update_info = truth.get("revocation_update")
        if update_info:
            update_report = engine.import_update(samples / update_info["file"])
            updated_by_marker = {
                item.get("sync_marker"): item
                for item in update_report["artifacts"] if item.get("sync_marker")
            }
            expected_after_by_marker = update_info.get("expected_after_by_marker", {})
            for marker, expected_after in expected_after_by_marker.items():
                actual = updated_by_marker[marker]
                if actual["status"] != expected_after["status"]:
                    mismatches.append({"sync_marker": marker, "stage": "after_revocation",
                                      "field": "status", "expected": expected_after["status"],
                                      "actual": actual["status"]})
                if actual.get("primary_token_id") != expected_after.get("primary_token_id"):
                    mismatches.append({"sync_marker": marker, "stage": "after_revocation",
                                      "field": "primary_token_id",
                                      "expected": expected_after.get("primary_token_id"),
                                      "actual": actual.get("primary_token_id")})

        return {
            "ok": not mismatches and not marker_errors,
            "checked_sync_cases": checked,
            "mismatches": mismatches,
            "marker_errors": marker_errors,
            "summary": report["summary"],
        }
    finally:
        if temporary is not None:
            temporary.cleanup()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--samples", default="samples")
    parser.add_argument("--workspace")
    args = parser.parse_args()
    result = validate(Path(args.samples), Path(args.workspace) if args.workspace else None)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
