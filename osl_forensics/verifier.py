"""Marker-driven injection verification."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .parser import parse_osl
from .policy import LITERAL_RESERVED16_LAYOUT, Policy


def policy_for_marker(marker: dict[str, Any]) -> Policy:
    overrides = dict(marker.get("policy_overrides", {}))
    if overrides.get("header_layout") == "literal42":
        overrides["header_layout"] = LITERAL_RESERVED16_LAYOUT
    return Policy(**overrides)


def verify_marker_file(marker_path: str | Path) -> dict[str, Any]:
    path = Path(marker_path)
    marker = json.loads(path.read_text(encoding="utf-8"))
    sample_path = path.parent / marker["sample_file"]
    sample_bytes = sample_path.read_bytes()
    policy = policy_for_marker(marker)
    parser_config = marker.get("parser_config", {})
    result = parse_osl(
        sample_bytes,
        file_id=marker.get("case_id", sample_path.stem),
        policy=policy,
        timeout_seconds=parser_config.get("timeout_seconds"),
    )
    actual_code = result.primary_code
    actual_offset = result.error_offset
    passed = (
        actual_code == marker["expect_code"]
        and actual_offset == marker.get("expect_offset")
    )
    sync_token = marker.get("binary_sync_marker", "").split("@", 1)[0].encode("ascii")
    sync_token_present = not sync_token or sync_token in sample_bytes
    return {
        "case_id": marker.get("case_id"),
        "marker_path": str(path),
        "passed": passed,
        "expected_code": marker["expect_code"],
        "expected_offset": marker.get("expect_offset"),
        "actual_code": actual_code,
        "actual_offset": actual_offset,
        "sync_token_present": sync_token_present,
        "diagnostics": [d.to_dict() for d in result.diagnostics],
    }


def verify_injection_tree(root: str | Path) -> dict[str, Any]:
    marker_paths = sorted(Path(root).rglob("sync_marker.json"))
    cases = [verify_marker_file(path) for path in marker_paths]
    passed = sum(1 for case in cases if case["passed"])
    return {
        "kind": "osl-injection-verification",
        "root": str(root),
        "total": len(cases),
        "passed": passed,
        "failed": len(cases) - passed,
        "ok": passed == len(cases) and bool(cases),
        "cases": cases,
    }
