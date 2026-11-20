from __future__ import annotations

import copy
import json
import math
import sqlite3
from pathlib import Path
from typing import Any, Callable

import pytest

from tailsample.errors import BusinessError
from tailsample.model import parse_event_bundle, parse_topology
from tailsample.policy import parse_policy_bundle
from tailsample.util import load_json


ROOT = Path(__file__).resolve().parents[1]
SAMPLES = ROOT / "samples"


def event_bundle() -> dict[str, Any]:
    return load_json(SAMPLES / "events.json")


def topology_data() -> dict[str, Any]:
    return load_json(SAMPLES / "topology.json")


def services() -> set[str]:
    return set(parse_topology(topology_data()).services)


def capture_code(callable_object: Callable[[], Any]) -> str:
    with pytest.raises(BusinessError) as caught:
        callable_object()
    return caught.value.code


def test_duplicate_service_is_bad_topology() -> None:
    data = topology_data()
    data["services"].append(copy.deepcopy(data["services"][0]))
    assert capture_code(lambda: parse_topology(data)) == "E_BAD_TOPOLOGY"


def test_unknown_service_is_isolated_error_code() -> None:
    data = event_bundle()
    data["events"][0]["span"]["service"] = "catalog"
    assert (
        capture_code(lambda: parse_event_bundle(data, services()))
        == "E_UNKNOWN_SERVICE"
    )


def test_time_inversion_has_dedicated_error_code() -> None:
    data = event_bundle()
    data["events"][0]["span"]["end_ms"] = 1
    assert (
        capture_code(lambda: parse_event_bundle(data, services()))
        == "E_TIME_INVERSION"
    )


def test_error_flag_must_be_boolean() -> None:
    data = event_bundle()
    data["events"][0]["span"]["is_error"] = 1
    assert (
        capture_code(lambda: parse_event_bundle(data, services())) == "E_BAD_SPAN"
    )


def test_non_finite_attribute_is_bad_span() -> None:
    data = event_bundle()
    data["events"][0]["span"]["attributes"]["latency"] = math.nan
    assert (
        capture_code(lambda: parse_event_bundle(data, services())) == "E_BAD_SPAN"
    )


def test_missing_parent_has_dedicated_error_code() -> None:
    data = event_bundle()
    root = copy.deepcopy(data["events"][0]["span"])
    child = copy.deepcopy(data["events"][1]["span"])
    root.update(
        {
            "trace_id": "trace-graph",
            "span_id": "root",
            "parent_span_id": None,
            "attributes": {},
        }
    )
    child.update(
        {
            "trace_id": "trace-graph",
            "span_id": "child",
            "parent_span_id": "missing",
            "attributes": {},
        }
    )
    bundle = {
        "events": [
            {"event_id": "g1", "arrival_ms": 10, "span": root},
            {"event_id": "g2", "arrival_ms": 20, "span": child},
        ]
    }
    assert (
        capture_code(lambda: parse_event_bundle(bundle, services()))
        == "E_MISSING_PARENT"
    )


def test_parent_cycle_is_bad_topology() -> None:
    data = event_bundle()
    first = copy.deepcopy(data["events"][0]["span"])
    second = copy.deepcopy(data["events"][1]["span"])
    first.update(
        {
            "trace_id": "trace-cycle",
            "span_id": "a",
            "parent_span_id": "b",
            "attributes": {},
            "root_end": False,
        }
    )
    second.update(
        {
            "trace_id": "trace-cycle",
            "span_id": "b",
            "parent_span_id": "a",
            "attributes": {},
            "root_end": False,
        }
    )
    bundle = {
        "events": [
            {"event_id": "c1", "arrival_ms": 10, "span": first},
            {"event_id": "c2", "arrival_ms": 20, "span": second},
        ]
    }
    assert (
        capture_code(lambda: parse_event_bundle(bundle, services()))
        == "E_BAD_TOPOLOGY"
    )


def test_duplicate_typed_attribute_key_has_dedicated_error_code(
    tmp_path: Path,
) -> None:
    path = tmp_path / "duplicate.json"
    path.write_text(
        '{"attributes":{"http.status_code":200,"http.status_code":"200"}}',
        encoding="utf-8",
    )
    assert capture_code(lambda: load_json(path)) == "E_ATTRIBUTE_TYPE_CONFLICT"


def test_policy_rollback_in_bundle_has_dedicated_error_code() -> None:
    data = load_json(SAMPLES / "policies.json")
    data["policies"][0]["activated_at_ms"] = 500
    data["policies"][1]["activated_at_ms"] = 100
    assert (
        capture_code(lambda: parse_policy_bundle(data)) == "E_POLICY_ROLLBACK"
    )


def test_invalid_load_does_not_change_waiting_or_persistent_state(
    loaded_db: Path,
    run_cli: Callable[..., Any],
) -> None:
    before = run_cli(["status", "--db", str(loaded_db)])
    assert before.returncode == 0
    failed = run_cli(
        [
            "load",
            "--db",
            str(loaded_db),
            "--topology",
            str(SAMPLES / "topology.json"),
            "--policies",
            str(SAMPLES / "policies.json"),
            "--events",
            str(SAMPLES / "invalid_span.json"),
        ]
    )
    assert failed.returncode == 14
    payload = json.loads(failed.stderr)
    assert payload["error"]["code"] == "E_TIME_INVERSION"
    after = run_cli(["status", "--db", str(loaded_db)])
    assert after.returncode == 0
    assert before.stdout == after.stdout


def test_quarantined_trace_does_not_block_other_trace_closure(
    tmp_path: Path,
    run_cli: Callable[..., Any],
) -> None:
    events = {
        "events": [
            {
                "event_id": "orphan-child",
                "arrival_ms": 100,
                "span": {
                    "trace_id": "trace-missing-parent",
                    "span_id": "orphan",
                    "parent_span_id": "not-arrived",
                    "service": "checkout",
                    "operation": "authorize",
                    "start_ms": 90,
                    "end_ms": 150,
                    "status_code": 200,
                    "is_error": False,
                    "attributes": {},
                    "root_end": False,
                },
            },
            {
                "event_id": "good-root",
                "arrival_ms": 1100,
                "span": {
                    "trace_id": "trace-good",
                    "span_id": "good-root",
                    "parent_span_id": None,
                    "service": "gateway",
                    "operation": "GET /good",
                    "start_ms": 1000,
                    "end_ms": 1050,
                    "status_code": 200,
                    "is_error": False,
                    "attributes": {},
                },
            },
        ]
    }
    events_path = tmp_path / "mixed-events.json"
    events_path.write_text(json.dumps(events), encoding="utf-8")
    db = tmp_path / "quarantine.db"
    loaded = run_cli(
        [
            "load",
            "--db",
            str(db),
            "--topology",
            str(SAMPLES / "topology.json"),
            "--policies",
            str(SAMPLES / "policies.json"),
            "--events",
            str(events_path),
            "--window-ms",
            "900",
            "--quota-per-second",
            "2",
            "--clock-tolerance-ms",
            "10",
            "--reset",
        ]
    )
    assert loaded.returncode == 0, loaded.stderr
    sampled = run_cli(["sample", "--db", str(db), "--advance-to", "2000"])
    assert sampled.returncode == 0, sampled.stderr
    payload = json.loads(sampled.stdout)
    assert len(payload["quarantined"]) == 1
    assert payload["quarantined"][0]["error"]["code"] == "E_MISSING_PARENT"
    bad = json.loads(
        run_cli(
            ["query", "--db", str(db), "--trace-id", "trace-missing-parent"]
        ).stdout
    )
    good = json.loads(
        run_cli(["query", "--db", str(db), "--trace-id", "trace-good"]).stdout
    )
    assert bad["status"] == "quarantined"
    assert bad["quarantine"]["code"] == "E_MISSING_PARENT"
    assert good["status"] == "decided"
    assert good["decision"]["outcome"] in {"KEEP", "DROP"}


def test_verify_maps_corrupted_quota_ledger_to_quota_error(
    decided_db: Path,
    run_cli: Callable[..., Any],
) -> None:
    connection = sqlite3.connect(decided_db)
    try:
        connection.execute(
            "UPDATE quota_ledger SET used = 0 WHERE logical_second = 0"
        )
        connection.commit()
    finally:
        connection.close()
    failed = run_cli(["verify", "--db", str(decided_db)])
    assert failed.returncode == 1
    payload = json.loads(failed.stdout)
    assert payload["ok"] is False
    assert payload["error_code"] == "E_QUOTA_MISMATCH"

