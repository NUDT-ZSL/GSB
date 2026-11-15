from __future__ import annotations

import json
from typing import Any, Mapping

from .canonical import sha256_value
from .config import RulesConfig
from .parser import ParsedEventStream
from .scheduler import SchedulingResult

REPORT_SCHEMA_VERSION = "1.0"


def _violation_counts(
    scheduling: SchedulingResult,
    parsed: ParsedEventStream,
) -> dict[str, int]:
    counts: dict[str, int] = {}
    for result in scheduling.matches:
        for violation in result.errors:
            code = violation.code.value
            counts[code] = counts.get(code, 0) + 1
    for violation in parsed.stream_errors:
        code = violation.code.value
        counts[code] = counts.get(code, 0) + 1
    return {code: counts[code] for code in sorted(counts)}


def build_report(
    *,
    config: RulesConfig,
    parsed: ParsedEventStream,
    scheduling: SchedulingResult,
    scheduler_budget_echo: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    matches = [result.to_dict() for result in scheduling.matches]
    passed = sum(1 for result in scheduling.matches if result.status == "ok")
    failed = len(scheduling.matches) - passed
    violation_counts = _violation_counts(scheduling, parsed)

    payload: dict[str, Any] = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "tool": {
            "name": "card-replay-validator",
            "version": "0.1.0",
            "rng_algorithm": "sha256->PCG64",
            "hash_algorithm": "sha256",
        },
        "inputs": {
            "events": parsed.to_input_summary(),
            "rules": config.to_dict(),
            "rules_sha256": sha256_value(config.to_dict()),
            "budgets": dict(scheduler_budget_echo or {}),
        },
        "scheduler": scheduling.scheduler,
        "summary": {
            "matches_total": len(matches),
            "matches_passed": passed,
            "matches_with_violations": failed,
            "event_lines": parsed.event_lines,
            "stream_error_count": len(parsed.stream_errors),
            "all_matches_have_results": scheduling.scheduler["completion"]["missing"] == 0,
            "violations_by_code": violation_counts,
        },
        "stream_errors": [violation.to_dict() for violation in parsed.stream_errors],
        "matches": matches,
    }
    payload["content_hash"] = report_content_hash(payload)
    return payload


def report_content_hash(report: Mapping[str, Any]) -> str:
    payload = dict(report)
    payload.pop("content_hash", None)
    return sha256_value(payload)


def finalize_report(report: dict[str, Any]) -> dict[str, Any]:
    finalized = dict(report)
    finalized.pop("content_hash", None)
    finalized["content_hash"] = report_content_hash(finalized)
    return finalized


def report_to_bytes(report: Mapping[str, Any], *, pretty: bool = True) -> bytes:
    if "content_hash" not in report:
        report = finalize_report(dict(report))
    text = json.dumps(
        report,
        ensure_ascii=False,
        sort_keys=True,
        indent=2 if pretty else None,
        separators=None if pretty else (",", ":"),
        allow_nan=False,
    )
    return (text + "\n").encode("utf-8")


def write_report(path: str, report: Mapping[str, Any]) -> None:
    with open(path, "wb") as handle:
        handle.write(report_to_bytes(report, pretty=True))


def verify_report_hash(report: Mapping[str, Any]) -> bool:
    stored = report.get("content_hash")
    return isinstance(stored, str) and stored == report_content_hash(report)
