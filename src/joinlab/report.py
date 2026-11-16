"""Stable structured reports derived from session records."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .errors import SchemaError
from .typesys import json_dumps_stable


REPORT_FORMAT = "joinlab.report.v1"
SESSION_RECORD_FORMAT = "joinlab.session-record.v1"


def build_report(session_record: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(session_record, dict):
        raise SchemaError("session record root must be an object", "$")
    if session_record.get("format") != SESSION_RECORD_FORMAT:
        raise SchemaError(
            f"unsupported session record format; expected {SESSION_RECORD_FORMAT}",
            "$.format",
        )
    raw_queries = session_record.get("queries")
    if not isinstance(raw_queries, list):
        raise SchemaError("session record queries must be an array", "$.queries")
    query_summaries: list[dict[str, Any]] = []
    for index, item in enumerate(raw_queries):
        path = f"$.queries[{index}]"
        if not isinstance(item, dict):
            raise SchemaError("query summary must be an object", path)
        consistency = item.get("cross_path") if isinstance(item.get("cross_path"), dict) else {}
        versions = item.get("versions") if isinstance(item.get("versions"), dict) else {}
        selected = item.get("selected_plan") if isinstance(item.get("selected_plan"), dict) else {}
        query_summaries.append(
            {
                "name": item.get("name"),
                "query_id": item.get("query_id"),
                "canonical_sql": item.get("canonical_sql"),
                "selected_plan": {
                    "plan_id": selected.get("plan_id"),
                    "plan_signature": selected.get("plan_signature"),
                    "relation_order": selected.get("relation_order"),
                    "operators": selected.get("operators"),
                    "quantized_cost": selected.get("quantized_cost"),
                },
                "candidate_count": item.get("candidate_count"),
                "result": {
                    "row_count": item.get("row_count"),
                    "result_digest": item.get("result_digest"),
                    "stats_fingerprint": item.get("stats_fingerprint"),
                },
                "cross_path": {
                    "consistent": consistency.get("consistent"),
                    "selected_digest": consistency.get("selected_digest"),
                    "fallback_digest": consistency.get("fallback_digest"),
                    "distinct_plans": consistency.get("distinct_plans"),
                    "first_difference": consistency.get("first_difference"),
                },
                "versions": {
                    "schema_version": versions.get("schema_version"),
                    "data_version": versions.get("data_version"),
                    "param_version": versions.get("param_version"),
                },
            }
        )

    verification = session_record.get("verification") if isinstance(session_record.get("verification"), dict) else {}
    anomalies = [
        {
            "kind": item.get("kind"),
            "expected_code": item.get("expected_code"),
            "detected_code": item.get("detected_code"),
            "path": item.get("detected_path"),
            "input_only": item.get("input_only"),
        }
        for item in session_record.get("anomalies", [])
        if isinstance(item, dict)
    ]
    all_consistent = all(item["cross_path"]["consistent"] is True for item in query_summaries)
    all_codes_ok = all(item["expected_code"] == item["detected_code"] for item in anomalies)
    return {
        "format": REPORT_FORMAT,
        "summary": {
            "query_count": len(query_summaries),
            "all_cross_path_consistent": all_consistent,
            "all_anomaly_codes_as_expected": all_codes_ok,
            "selected_plan_ids": [item["selected_plan"]["plan_id"] for item in query_summaries],
            "versions": session_record.get("versions"),
            "snapshot_restored": verification.get("snapshot_restored"),
            "refresh_converged": verification.get("refresh_converged"),
            "final_param_version": verification.get("final_param_version"),
            "result_bytes_are_compared": True,
            "timings_are_observational": True,
        },
        "queries": query_summaries,
        "refresh": verification.get("refresh"),
        "snapshot": verification.get("snapshot"),
        "anomalies": anomalies,
    }


def load_session_record(path: str | Path) -> dict[str, Any]:
    source = Path(path).expanduser().resolve()
    try:
        with source.open("r", encoding="utf-8") as handle:
            document = json.load(handle)
    except FileNotFoundError as exc:
        raise SchemaError("session record does not exist", str(source)) from exc
    except json.JSONDecodeError as exc:
        raise SchemaError(
            f"invalid session record JSON: {exc.msg}",
            f"{source}@{exc.lineno}:{exc.colno}",
        ) from exc
    except OSError as exc:
        raise SchemaError(f"cannot read session record: {exc}", str(source)) from exc
    if not isinstance(document, dict):
        raise SchemaError("session record root must be an object", "$")
    return document


def write_report(report: dict[str, Any], path: str | Path) -> None:
    target = Path(path).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json_dumps_stable(report, pretty=True), encoding="utf-8")
