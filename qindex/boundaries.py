"""Manual boundary samples and their executable assertions."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .loader import load_workload
from .solver import solve_workload


def _flatten_actions(report: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        action
        for step in report["steps"]
        for action in step["actions"]
    ]


def _query_index(report: dict[str, Any]) -> dict[str, dict[str, Any]]:
    queries: dict[str, dict[str, Any]] = {}
    for step in report["steps"]:
        for query in step["queries"]:
            queries[str(query["query_id"])] = query
    return queries


def _check(
    name: str,
    actual: Any,
    expected: Any,
    relation: str = "eq",
) -> dict[str, Any]:
    if relation == "eq":
        passed = actual == expected
    elif relation == "truthy":
        passed = bool(actual)
    elif relation == "falsy":
        passed = not bool(actual)
    elif relation == "positive":
        passed = isinstance(actual, (int, float)) and actual > 0
    else:
        raise ValueError(f"未知校验关系: {relation}")
    return {
        "check": name,
        "passed": passed,
        "actual": actual,
        "expected": expected,
        "relation": relation,
    }


def _evaluate_case(
    case_name: str,
    workload: dict[str, Any],
    report: dict[str, Any],
) -> list[dict[str, Any]]:
    actions = _flatten_actions(report)
    queries = _query_index(report)
    checks: list[dict[str, Any]] = [
        _check("solver_all_ok", report["summary"]["all_ok"], True),
        _check(
            "all_queries_match_reference",
            report["summary"]["all_queries_match_reference"],
            True,
        ),
        _check(
            "all_partition_accounting_ok",
            report["summary"]["all_partition_accounting_ok"],
            True,
        ),
    ]
    flush_actions = [
        action for action in actions if action["action"] == "flush"
    ]
    compact_actions = [
        action for action in actions if action["action"] == "compact"
    ]
    final_segments = report["final_segment_manifest"]

    if case_name == "exact_flush":
        checks.append(
            _check("flush_action_count", len(flush_actions), 1)
        )
        checks.append(
            _check(
                "flush_entry_count",
                flush_actions[0]["entry_count"] if flush_actions else None,
                3,
            )
        )
        checks.append(
            _check(
                "live_segment_entry_count",
                sum(segment["entry_count"] for segment in final_segments),
                3,
            )
        )
        query = queries.get("Q-EXACT-001", {})
        checks.append(
            _check(
                "query_hit_count",
                len(query.get("hits", [])),
                3,
            )
        )
        checks.append(
            _check(
                "same_timestamp_not_split",
                len({hit["source"]["segment_id"] for hit in query.get("hits", [])}),
                1,
            )
        )

    elif case_name == "invalid_ratio_exact":
        exact_ratio = False
        for action in compact_actions:
            ratio_condition = action["conditions"].get("invalid_ratio") or {}
            for item in ratio_condition.get("trigger_segments", []):
                if (
                    item["invalid_ratio"] == 0.5
                    and item["threshold"] == 0.5
                    and item["comparison"] == ">="
                ):
                    exact_ratio = True
        checks.append(_check("exact_0_5_ratio_triggered", exact_ratio, True))
        checks.append(
            _check(
                "compaction_discarded_two",
                [
                    action["discarded_invalid_entry_count"]
                    for action in compact_actions
                ],
                [2],
            )
        )
        checks.append(
            _check(
                "archived_query_hit_count",
                len(queries["Q-RATIO-ARCHIVED"]["hits"]),
                0,
            )
        )
        checks.append(
            _check(
                "surviving_query_hit_count",
                len(queries["Q-RATIO-SURVIVING"]["hits"]),
                2,
            )
        )
        checks.append(
            _check(
                "surviving_order",
                [
                    hit["event_id"]
                    for hit in queries["Q-RATIO-SURVIVING"]["hits"]
                ],
                ["RATIO-003", "RATIO-004"],
            )
        )
        checks.append(
            _check(
                "ledger_all_balanced",
                all(action["ledger"]["ok"] for action in compact_actions),
                True,
            )
        )

    elif case_name == "archived_query":
        checks.append(
            _check("compaction_count", len(compact_actions), 0)
        )
        old_segment_still_live = any(
            any(
                entry["event_id"] in ("ARCH-001", "ARCH-002")
                for entry in segment["entries"]
            )
            for segment in final_segments
        )
        checks.append(
            _check(
                "old_segment_still_live",
                old_segment_still_live,
                True,
            )
        )
        archived_query = queries["Q-ARCH-AFTER"]
        checks.append(
            _check(
                "archived_query_hit_count",
                len(archived_query["hits"]),
                0,
            )
        )
        checks.append(
            _check(
                "invalid_filtered_count",
                archived_query["invalid_filtered_entries"],
                2,
            )
        )
        invalidation = report["steps"][1]["invalidations"][0]
        checks.append(
            _check(
                "stale_hit",
                invalidation["diagnostic"]["first_inconsistency"],
                None,
            )
        )
        checks.append(
            _check(
                "unrelated_batch_hit_count",
                len(queries["Q-ARCH-KEEP"]["hits"]),
                1,
            )
        )

    elif case_name == "capacity_only":
        capacity_actions = [
            action
            for action in compact_actions
            if action["trigger"] in ("partition_capacity", "level_capacity")
        ]
        checks.append(
            _check(
                "capacity_compaction_count",
                len(capacity_actions),
                1,
            )
        )
        if capacity_actions:
            action = capacity_actions[0]
            checks.append(
                _check(
                    "discarded_invalid_entry_count",
                    action["discarded_invalid_entry_count"],
                    0,
                )
            )
            checks.append(
                _check(
                    "reclaimed_bytes_positive",
                    action["reclaimed_bytes"],
                    "> 0",
                    "positive",
                )
            )
            checks.append(
                _check(
                    "valid_sequence_preserved",
                    action["valid_sequence_preserved"],
                    True,
                )
            )
        checks.append(
            _check(
                "query_hit_count",
                len(_query_index(report)["Q-CAP-001"]["hits"]),
                4,
            )
        )

    elif case_name == "ratio_and_capacity":
        combined = [
            action
            for action in compact_actions
            if action["trigger"] == "invalid_ratio+capacity"
        ]
        checks.append(
            _check("combined_trigger_count", len(combined), 1)
        )
        if combined:
            action = combined[0]
            checks.append(
                _check(
                    "priority_decision",
                    action["conditions"]["priority_decision"],
                    "invalid_ratio_before_capacity",
                )
            )
            checks.append(
                _check(
                    "discarded_invalid_entry_count",
                    action["discarded_invalid_entry_count"],
                    2,
                )
            )
            checks.append(
                _check(
                    "ledger_balanced",
                    action["ledger"]["ok"],
                    True,
                )
            )
        checks.append(
            _check(
                "archived_query_hit_count",
                len(queries["Q-BOTH-OLD"]["hits"]),
                0,
            )
        )
        checks.append(
            _check(
                "surviving_query_hit_count",
                len(queries["Q-BOTH-KEEP"]["hits"]),
                2,
            )
        )

    else:
        checks.append(
            {
                "check": "known_case",
                "passed": False,
                "actual": case_name,
                "expected": "known boundary case",
                "relation": "eq",
            }
        )
    return checks


def run_boundary_samples(samples_dir: str | Path) -> dict[str, Any]:
    samples_dir = Path(samples_dir)
    results: list[dict[str, Any]] = []
    for sample_path in sorted(samples_dir.glob("*.json")):
        workload = load_workload(sample_path)
        case_name = (
            workload.get("sample_expectations", {}).get("case")
            or sample_path.stem
        )
        try:
            report = solve_workload(workload)
            checks = _evaluate_case(case_name, workload, report)
            passed = all(check["passed"] for check in checks)
            results.append(
                {
                    "file": sample_path.name,
                    "case": case_name,
                    "passed": passed,
                    "checks": checks,
                    "report_summary": report["summary"],
                    "compactions": report["reclamation_actions"],
                    "queries": [
                        {
                            "query_id": query["query_id"],
                            "hit_event_ids": [
                                hit["event_id"] for hit in query["hits"]
                            ],
                            "invalid_filtered_entries": query[
                                "invalid_filtered_entries"
                            ],
                            "reference_match": query["verification"][
                                "match"
                            ],
                        }
                        for step in report["steps"]
                        for query in step["queries"]
                    ],
                }
            )
        except Exception as exc:  # Keep all samples runnable for diagnostics.
            results.append(
                {
                    "file": sample_path.name,
                    "case": case_name,
                    "passed": False,
                    "checks": [
                        {
                            "check": "solve_without_exception",
                            "passed": False,
                            "actual": f"{type(exc).__name__}: {exc}",
                            "expected": None,
                            "relation": "eq",
                        }
                    ],
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
    return {
        "format": "quality-index-boundary-report/v1",
        "all_passed": all(result["passed"] for result in results),
        "sample_count": len(results),
        "results": results,
    }


def dumps_boundary_report(report: dict[str, Any]) -> str:
    return json.dumps(report, ensure_ascii=False, indent=2) + "\n"
