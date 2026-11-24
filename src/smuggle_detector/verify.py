"""Truth-marker-only sample verification."""

from __future__ import annotations

from typing import Any


def _project_messages(messages: list[dict]) -> list[tuple]:
    return sorted(
        (
            item["direction"],
            item.get("sync_id"),
            item["start_offset"],
            item["end_offset"],
            item["complete"],
        )
        for item in messages
    )


def _project_hits(hits: list[dict]) -> list[tuple]:
    return sorted(
        (
            item["code"],
            item["direction"],
            item["byte_offset"],
            item.get("sync_id"),
        )
        for item in hits
    )


def verify_truth(truth: dict[str, Any], report: dict[str, Any]) -> dict[str, Any]:
    mismatches: list[dict] = []
    report_connections = {
        item["connection_id"]: item for item in report.get("connections", [])
    }
    truth_connections = truth.get("connections", {})

    for connection_id in sorted(truth_connections):
        expected = truth_connections[connection_id]
        actual = report_connections.get(connection_id)
        if actual is None:
            mismatches.append(
                {
                    "connection_id": connection_id,
                    "kind": "missing_connection",
                }
            )
            continue

        expected_status = expected.get("expected_status", "ok")
        if actual.get("status") != expected_status:
            mismatches.append(
                {
                    "connection_id": connection_id,
                    "kind": "status",
                    "expected": expected_status,
                    "actual": actual.get("status"),
                }
            )

        actual_codes = sorted(
            {
                item["code"]
                for item in actual.get("hits", []) + actual.get("errors", [])
            }
        )
        if actual_codes != expected.get("expected_codes", []):
            mismatches.append(
                {
                    "connection_id": connection_id,
                    "kind": "business_codes",
                    "expected": expected.get("expected_codes", []),
                    "actual": actual_codes,
                }
            )

        if expected_status == "timeout":
            continue

        expected_generation = expected.get("expected_rule_generation")
        actual_generation = actual.get("rule_view", {}).get("generation")
        if actual_generation != expected_generation:
            mismatches.append(
                {
                    "connection_id": connection_id,
                    "kind": "rule_generation",
                    "expected": expected_generation,
                    "actual": actual_generation,
                }
            )

        expected_messages = sorted(
            (
                item["direction"],
                item["sync_id"],
                item["start_offset"],
                item["end_offset"],
                item["complete"],
            )
            for item in expected.get("expected_boundaries", [])
        )
        actual_messages = _project_messages(actual.get("messages", []))
        if actual_messages != expected_messages:
            mismatches.append(
                {
                    "connection_id": connection_id,
                    "kind": "message_boundaries",
                    "expected": [list(item) for item in expected_messages],
                    "actual": [list(item) for item in actual_messages],
                }
            )

        expected_hits = sorted(
            (
                item["code"],
                item["direction"],
                item["byte_offset"],
                item.get("sync_id"),
            )
            for item in expected.get("expected_hits", [])
        )
        actual_hits = _project_hits(actual.get("hits", []))
        if actual_hits != expected_hits:
            mismatches.append(
                {
                    "connection_id": connection_id,
                    "kind": "hits",
                    "expected": [list(item) for item in expected_hits],
                    "actual": [list(item) for item in actual_hits],
                }
            )

        expected_error_codes = sorted(
            item["code"] for item in expected.get("expected_errors", [])
        )
        actual_error_codes = sorted(
            item["code"] for item in actual.get("errors", [])
        )
        if actual_error_codes != expected_error_codes:
            mismatches.append(
                {
                    "connection_id": connection_id,
                    "kind": "errors",
                    "expected": expected_error_codes,
                    "actual": actual_error_codes,
                }
            )

        for expected_error in expected.get("expected_errors", []):
            if expected_error["code"] != "E_MSG_GAP":
                continue
            actual_gaps = sorted(
                item.get("missing_ranges", [])
                for item in actual.get("errors", [])
                if item["code"] == "E_MSG_GAP"
            )
            if actual_gaps != [sorted(expected_error.get("missing_ranges", []))]:
                mismatches.append(
                    {
                        "connection_id": connection_id,
                        "kind": "missing_ranges",
                        "expected": expected_error.get("missing_ranges", []),
                        "actual": actual_gaps,
                    }
                )

        if (
            actual.get("duplicate_count", 0)
            != expected.get("expected_duplicate_count", 0)
        ):
            mismatches.append(
                {
                    "connection_id": connection_id,
                    "kind": "duplicate_count",
                    "expected": expected.get("expected_duplicate_count", 0),
                    "actual": actual.get("duplicate_count", 0),
                }
            )

    extra_connections = sorted(set(report_connections) - set(truth_connections))
    if extra_connections:
        mismatches.append(
            {
                "kind": "extra_connections",
                "connections": extra_connections,
            }
        )

    return {
        "ok": not mismatches,
        "checked_connections": len(truth_connections),
        "mismatch_count": len(mismatches),
        "mismatches": mismatches,
    }

