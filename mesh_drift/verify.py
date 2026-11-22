"""Independent replay and integrity verification of a ledger database."""

from __future__ import annotations

from typing import Any

from .canon import canonical_text, json_digest
from .engine import analyze_observations
from .store import LEDGER_FIELDS, Database


def _expected_stub(replay: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "snapshot_id": replay["expected_snapshot_id"],
        "logical_clock_ms": replay["expected_logical_clock_ms"],
        "services": [replay["service"]],
    }


def _recompute_input_digest(replay: dict[str, Any]) -> str:
    return json_digest(
        {
            "expected": {
                "schema_version": 1,
                "snapshot_id": replay["expected_snapshot_id"],
                "logical_clock_ms": replay["expected_logical_clock_ms"],
                "service": replay["service"],
            },
            "policy": replay["policy"],
            "observations": replay["observations"],
            "abandoned": replay["abandoned"],
            "clock_ms": replay["logical_clock_ms"],
        }
    )


def verify_database(database: Database) -> dict[str, Any]:
    errors: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    rounds = database.list_rounds()
    entries = database.list_ledger_entries()
    rounds_by_id = {item["round_id"]: item for item in rounds}

    for round_record in rounds:
        round_id = round_record["round_id"]
        replay = round_record["replay_inputs"]
        expected = _expected_stub(replay)
        recomputed = analyze_observations(
            expected,
            replay["service"],
            replay["policy"],
            replay["observations"],
            replay["logical_clock_ms"],
            replay["ledger_context"],
        )
        stored_analysis = round_record["computed"].get("analysis")
        if canonical_text(recomputed) != canonical_text(stored_analysis):
            errors.append(
                {
                    "code": "E_VERIFY_REPLAY_MISMATCH",
                    "round_id": round_id,
                    "message": "recomputed drift/decision analysis differs from committed round",
                }
            )
        recomputed_input_digest = _recompute_input_digest(replay)
        if recomputed_input_digest != round_record["input_snapshot_digest"]:
            errors.append(
                {
                    "code": "E_VERIFY_INPUT_DIGEST",
                    "round_id": round_id,
                    "expected": round_record["input_snapshot_digest"],
                    "actual": recomputed_input_digest,
                }
            )
        if json_digest(replay["service"]) != round_record["expected_snapshot_digest"]:
            errors.append(
                {
                    "code": "E_VERIFY_EXPECTED_DIGEST",
                    "round_id": round_id,
                }
            )
        if json_digest(replay["policy"]) != round_record["policy_digest"]:
            errors.append(
                {
                    "code": "E_VERIFY_POLICY_DIGEST",
                    "round_id": round_id,
                }
            )
        summary_without_digest = {
            key: value
            for key, value in round_record["computed"].items()
            if key != "summary_digest"
        }
        expected_summary_digest = json_digest(
            {
                "input_snapshot_digest": summary_without_digest["input_snapshot_digest"],
                "decision_stats": summary_without_digest["decision_stats"],
                "drift_type_stats": summary_without_digest["drift_type_stats"],
                "actions": summary_without_digest["actions"],
                "analysis": summary_without_digest["analysis"],
            }
        )
        if expected_summary_digest != round_record["computed"].get("summary_digest"):
            errors.append(
                {
                    "code": "E_VERIFY_SUMMARY_DIGEST",
                    "round_id": round_id,
                }
            )
        for action in round_record["computed"].get("actions", []):
            if action["entry_id"] not in {entry["entry_id"] for entry in entries}:
                errors.append(
                    {
                        "code": "E_VERIFY_ACTION_ENTRY",
                        "round_id": round_id,
                        "entry_id": action["entry_id"],
                    }
                )
        participating = set(round_record["participating"])
        started = set(round_record["started"])
        abandoned = {item["instance_id"] for item in round_record["abandoned"]}
        if not started.issubset(participating):
            errors.append(
                {"code": "E_VERIFY_PARTICIPATION", "round_id": round_id}
            )
        if not abandoned.issubset(participating):
            errors.append(
                {"code": "E_VERIFY_ABANDONED", "round_id": round_id}
            )
        if started & abandoned:
            errors.append(
                {"code": "E_VERIFY_STARTED_ABANDONED_OVERLAP", "round_id": round_id}
            )

    for entry in entries:
        entry_id = entry["entry_id"]
        revisions = database.ledger_revisions(entry_id)
        events = database.ledger_events(entry_id)
        evidence = database.ledger_evidence(entry_id)
        if not revisions:
            errors.append({"code": "E_VERIFY_MISSING_REVISION", "entry_id": entry_id})
            continue
        versions = [item["entry_version"] for item in revisions]
        if versions != list(range(1, len(versions) + 1)):
            errors.append(
                {
                    "code": "E_VERIFY_REVISION_SEQUENCE",
                    "entry_id": entry_id,
                    "versions": versions,
                }
            )
        if revisions[-1]["entry_version"] != entry["entry_version"]:
            errors.append(
                {
                    "code": "E_VERIFY_ENTRY_VERSION",
                    "entry_id": entry_id,
                    "entry": entry["entry_version"],
                    "revision": revisions[-1]["entry_version"],
                }
            )
        entry_payload = {field: entry[field] for field in LEDGER_FIELDS}
        if canonical_text(revisions[-1]["payload"]) != canonical_text(entry_payload):
            errors.append(
                {
                    "code": "E_VERIFY_FINAL_REVISION_PAYLOAD",
                    "entry_id": entry_id,
                }
            )
        for revision in revisions:
            if revision["canonical_digest"] != json_digest(revision["payload"]):
                errors.append(
                    {
                        "code": "E_VERIFY_REVISION_DIGEST",
                        "entry_id": entry_id,
                        "entry_version": revision["entry_version"],
                    }
                )
        event_versions = [item["entry_version"] for item in events]
        if event_versions != versions:
            errors.append(
                {
                    "code": "E_VERIFY_EVENT_SEQUENCE",
                    "entry_id": entry_id,
                    "events": event_versions,
                    "revisions": versions,
                }
            )
        for event in events:
            expected_event_digest = json_digest(
                {
                    "entry_id": event["entry_id"],
                    "entry_version": event["entry_version"],
                    "round_id": event["round_id"],
                    "clock_ms": event["clock_ms"],
                    "event_type": event["event_type"],
                    "details": event["details"],
                }
            )
            if event["canonical_digest"] != expected_event_digest:
                errors.append(
                    {
                        "code": "E_VERIFY_EVENT_DIGEST",
                        "entry_id": entry_id,
                        "entry_version": event["entry_version"],
                    }
                )
            if event["round_id"] not in rounds_by_id:
                errors.append(
                    {
                        "code": "E_VERIFY_EVENT_ROUND",
                        "entry_id": entry_id,
                        "round_id": event["round_id"],
                    }
                )
        evidence_rounds = {item["round_id"] for item in evidence}
        revision_rounds = {item["round_id"] for item in revisions}
        for item in evidence:
            expected_evidence_digest = json_digest(
                {
                    "entry_id": item["entry_id"],
                    "entry_version": item["entry_version"],
                    "round_id": item["round_id"],
                    "clock_ms": item["clock_ms"],
                    "evidence": item["evidence"],
                }
            )
            if item["canonical_digest"] != expected_evidence_digest:
                errors.append(
                    {
                        "code": "E_VERIFY_EVIDENCE_DIGEST",
                        "entry_id": entry_id,
                        "round_id": item["round_id"],
                    }
                )
        if not revision_rounds.issubset(evidence_rounds):
            errors.append(
                {
                    "code": "E_VERIFY_EVIDENCE_CHAIN",
                    "entry_id": entry_id,
                    "missing_rounds": sorted(revision_rounds - evidence_rounds),
                }
            )
        occurrence_evidence = [
            item for item in evidence if item["evidence"].get("kind") != "CLOSE"
        ]
        if entry["occurrences"] != len(occurrence_evidence):
            errors.append(
                {
                    "code": "E_VERIFY_OCCURRENCES",
                    "entry_id": entry_id,
                    "occurrences": entry["occurrences"],
                    "evidence_rows": len(occurrence_evidence),
                }
            )
        if entry["canonical_digest"] != revisions[-1]["canonical_digest"]:
            errors.append(
                {"code": "E_VERIFY_ENTRY_DIGEST", "entry_id": entry_id}
            )
        if entry["closed_clock_ms"] is not None and entry["status"] != "CLOSED":
            errors.append(
                {
                    "code": "E_VERIFY_CLOSED_CLOCK",
                    "entry_id": entry_id,
                    "status": entry["status"],
                }
            )
        if entry["status"] == "CLOSED" and entry["closed_clock_ms"] is None:
            errors.append(
                {"code": "E_VERIFY_CLOSED_STATUS", "entry_id": entry_id}
            )

    service_states = {item["service"]: item for item in database.service_states()}
    rounds_by_service: dict[str, list[dict[str, Any]]] = {}
    for round_record in rounds:
        rounds_by_service.setdefault(round_record["service"], []).append(round_record)
    for service_name, service_rounds in rounds_by_service.items():
        service_rounds.sort(key=lambda item: (item["logical_clock_ms"], item["round_id"]))
        state = service_states.get(service_name)
        if state is None:
            errors.append(
                {"code": "E_VERIFY_SERVICE_STATE_MISSING", "service": service_name}
            )
            continue
        latest = service_rounds[-1]
        if state["last_committed_round_id"] != latest["round_id"]:
            errors.append(
                {
                    "code": "E_VERIFY_SERVICE_WATERMARK",
                    "service": service_name,
                    "expected_round": latest["round_id"],
                    "actual_round": state["last_committed_round_id"],
                }
            )
        if state["watermark_clock_ms"] != latest["logical_clock_ms"]:
            errors.append(
                {
                    "code": "E_VERIFY_CLOCK_WATERMARK",
                    "service": service_name,
                    "expected": latest["logical_clock_ms"],
                    "actual": state["watermark_clock_ms"],
                }
            )
        if state["round_count"] != len(service_rounds):
            errors.append(
                {
                    "code": "E_VERIFY_ROUND_COUNT",
                    "service": service_name,
                    "expected": len(service_rounds),
                    "actual": state["round_count"],
                }
            )

    unfinished = database.list_unfinished_runs()
    if unfinished:
        errors.append(
            {
                "code": "E_VERIFY_UNFINISHED_ROUNDS",
                "round_ids": sorted(item["round_id"] for item in unfinished),
            }
        )
    if not rounds:
        warnings.append({"code": "W_VERIFY_EMPTY_LEDGER", "message": "no rounds committed"})

    return {
        "ok": not errors,
        "errors": errors,
        "warnings": warnings,
        "round_count": len(rounds),
        "entry_count": len(entries),
        "service_count": len(service_states),
        "database_digest": database.database_digest(),
    }
