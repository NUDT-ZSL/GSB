from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from audit_ledger import (
    AuditEngine,
    AuditError,
    AuditStore,
    FaultPlan,
    InputBundle,
    canonical_json,
    load_bundle,
    parse_events,
    parse_policy,
    parse_quotas,
    retry_delay_ms,
    sha256_digest,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SAMPLES = PROJECT_ROOT / "samples"


def policy_raw(**overrides: Any) -> dict[str, Any]:
    value: dict[str, Any] = {
        "policy_version": 1,
        "allowed_categories": ["auth", "admin", "data", "export"],
        "max_events_per_round": 20,
        "logical_time_budget_ms": 100,
        "clock_step_ms": 1,
        "retry_base_ms": 5,
        "retry_cap_ms": 20,
        "max_attempts": 3,
        "max_clock_regression_ms": 0,
        "window_ms": 86400000,
    }
    value.update(overrides)
    return value


def quotas_raw(
    declarations: list[dict[str, Any]], version: int = 1
) -> dict[str, Any]:
    return {"quota_version": version, "declarations": declarations}


def declaration(
    tenant: str,
    quota: int,
    max_event: int,
    action: str,
) -> dict[str, Any]:
    return {
        "tenant": tenant,
        "daily_byte_quota": quota,
        "max_event_bytes": max_event,
        "quota_action": action,
    }


def event_raw(
    seq: int,
    tenant: str,
    payload: Any,
    logical_ts_ms: int,
    idempotency_key: str,
    category: str = "auth",
) -> dict[str, Any]:
    return {
        "event_seq": seq,
        "event_version": 1,
        "tenant": tenant,
        "category": category,
        "payload": payload,
        "payload_digest": sha256_digest(payload),
        "logical_ts_ms": logical_ts_ms,
        "idempotency_key": idempotency_key,
    }


def make_bundle(
    events: list[dict[str, Any]],
    declarations: list[dict[str, Any]],
    quota_version: int = 1,
    **policy_overrides: Any,
) -> InputBundle:
    quotas = parse_quotas(quotas_raw(declarations, version=quota_version))
    policy = parse_policy(policy_raw(**policy_overrides))
    parsed = parse_events(
        {"stream_version": 1, "events": events},
        quotas,
        policy,
    )
    return InputBundle(events=parsed, quotas=quotas, policy=policy)


def all_outcomes(report: dict[str, Any]) -> list[dict[str, Any]]:
    outcomes: list[dict[str, Any]] = []
    for round_report in report["rounds"]:
        outcomes.extend(round_report["outcomes"])
    outcomes.extend(report["active_abandoned"])
    return outcomes


def sample_bundle() -> InputBundle:
    return load_bundle(
        SAMPLES / "events.json",
        SAMPLES / "quotas.json",
        SAMPLES / "policy.json",
    )


def test_sample_flow_replay_and_expected_verify(tmp_path: Path) -> None:
    db = tmp_path / "audit.db"
    store = AuditStore(db)
    bundle = sample_bundle()
    assert store.load_bundle(bundle)["queued"] == 11

    report = AuditEngine(store).write()
    assert report["summary"] == {
        "archived": 1,
        "budget_unwritten": 0,
        "deferred_observations": 15,
        "rejected": 2,
        "written": 8,
    }
    status = store.status()
    assert status["event_watermark"] == 11
    assert status["logical_clock_watermark"] == 1704153600000
    assert status["manual_pending_count"] == 0
    windows = {
        (tenant["tenant"], window["window_id"]): window
        for tenant in status["tenants"]
        for window in tenant["windows"]
    }
    assert windows[("tenant-alpha", "day-19723")]["used_bytes"] == 132
    assert windows[("tenant-alpha", "day-19723")]["remaining_bytes"] == 58
    assert windows[("tenant-beta", "day-19723")]["archived_bytes"] == 63
    assert windows[("tenant-gamma", "day-19723")]["overflow_count"] == 2

    expected = json.loads((SAMPLES / "expected_summary.json").read_text("utf-8"))
    before = store.verify(expected)
    assert before["ok"] is True
    digest_before = before["ledger"]

    replay_bundle = sample_bundle()
    assert store.load_bundle(replay_bundle, source="replay")[
        "already_processed"
    ] == 11
    replay = AuditEngine(store).write()
    assert replay["summary"]["duplicate"] == 11
    after = store.verify(expected)
    assert after["ok"] is True
    assert after["ledger"] == digest_before
    assert after["checked"]["audit_event_count"] == 8
    assert after["checked"]["archive_event_count"] == 1


def test_quota_verdicts_and_decision_determinism(tmp_path: Path) -> None:
    payload_a = {"index": 1, "pad": "x" * 20}
    payload_b = {"index": 2, "pad": "y" * 20}
    payload_c = {"index": 3, "pad": "z" * 20}
    events = [
        event_raw(1, "a", payload_a, 1000, "a-1"),
        event_raw(2, "a", payload_b, 1001, "a-2"),
        event_raw(3, "b", payload_a, 1002, "b-1"),
        event_raw(4, "b", payload_b, 1003, "b-2"),
        event_raw(5, "c", payload_a, 1004, "c-1"),
        event_raw(6, "c", payload_b, 1005, "c-2"),
        event_raw(7, "d", payload_c, 1006, "d-1"),
    ]
    declarations = [
        declaration("a", 50, 100, "reject"),
        declaration("b", 50, 100, "archive"),
        declaration("c", 50, 100, "allow_overflow"),
        declaration("d", 100, 10, "reject"),
    ]
    bundle = make_bundle(events, declarations)
    outcomes_by_run: list[list[dict[str, Any]]] = []
    for run in range(2):
        store = AuditStore(tmp_path / f"quota-{run}.db")
        store.load_bundle(bundle)
        report = AuditEngine(store).write()
        outcomes_by_run.append(all_outcomes(report))
        assert store.verify()["ok"] is True

    outcomes = outcomes_by_run[0]
    assert [item["status"] for item in outcomes] == [
        "written",
        "rejected",
        "written",
        "archived",
        "written",
        "written",
        "rejected",
    ]
    assert outcomes[1]["error_code"] == "AUD-QUOTA-EXCEEDED"
    assert outcomes[1]["decision"]["first_constraint"] == "WINDOW_QUOTA"
    assert outcomes[1]["decision"]["remaining_bytes"] >= 0
    assert outcomes[3]["error_code"] == "AUD-QUOTA-EXCEEDED"
    assert outcomes[3]["decision"]["first_constraint"] == "WINDOW_QUOTA"
    assert outcomes[5]["verdict"] == "overflow_allowed"
    assert outcomes[6]["error_code"] == "AUD-EVENT-TOO-LARGE"
    assert outcomes[6]["decision"]["first_constraint"] == "MAX_EVENT_BYTES"
    assert canonical_json(outcomes_by_run[0]) == canonical_json(outcomes_by_run[1])

    ledger = {
        (item["tenant"], item["window_id"]): item
        for item in store.verify()["ledger"]
    }
    assert ledger[("a", "day-0")]["used_bytes"] == len(
        canonical_json(payload_a).encode("utf-8")
    )
    assert ledger[("b", "day-0")]["archived_bytes"] > 0
    assert ledger[("c", "day-0")]["overflow_count"] == 1
    assert ("d", "day-0") not in ledger


def test_retry_success_and_manual_exhaustion(tmp_path: Path) -> None:
    payload = {"action": "login", "actor": "retry"}
    bundle = make_bundle(
        [event_raw(1, "a", payload, 1000, "retry-key")],
        [declaration("a", 1000, 1000, "reject")],
        retry_base_ms=5,
        retry_cap_ms=20,
        max_attempts=3,
    )
    store = AuditStore(tmp_path / "retry-ok.db")
    store.load_bundle(bundle)
    plan = FaultPlan(fail_counts={"retry-key": 1})
    report = AuditEngine(store, failure_hook=plan.hook).write()
    assert report["summary"]["write_failed"] == 1
    assert report["summary"]["written"] == 1
    assert store.verify()["ok"] is True
    with store.connection() as connection:
        row = connection.execute(
            "SELECT state, attempts FROM work_queue WHERE event_seq = 1"
        ).fetchone()
        assert row["state"] == "written"
        assert row["attempts"] == 1

    exhausted_store = AuditStore(tmp_path / "retry-manual.db")
    exhausted_store.load_bundle(bundle)
    exhausted = AuditEngine(
        exhausted_store,
        failure_hook=FaultPlan(fail_counts={"retry-key": 99}).hook,
    ).write()
    assert exhausted["summary"]["manual"] == 1
    assert exhausted["summary"]["write_failed"] == 2
    assert exhausted["manual_pending_count"] == 1
    status = exhausted_store.status()
    assert status["manual_pending_count"] == 1
    assert status["manual_queue"][0]["reason_code"] == "AUD-RETRY-EXHAUSTED"
    assert status["tenants"][0]["windows"][0]["manual_pending_count"] == 1
    verified = exhausted_store.verify()
    assert verified["ok"] is True
    assert verified["checked"]["audit_event_count"] == 0


def test_budget_classification_and_active_abandon(tmp_path: Path) -> None:
    events = [
        event_raw(
            index,
            "a",
            {"index": index, "pad": "x" * 20},
            1000 + index,
            f"budget-{index}",
        )
        for index in range(1, 6)
    ]
    bundle = make_bundle(
        events,
        [declaration("a", 10000, 500, "reject")],
        max_events_per_round=4,
        logical_time_budget_ms=25,
        clock_step_ms=10,
    )
    store = AuditStore(tmp_path / "budget-time.db")
    store.load_bundle(bundle)
    report = AuditEngine(store).write(max_rounds=1)
    assert [item["event_seq"] for item in report["rounds"][0]["outcomes"]] == [1, 2]
    deferred = report["rounds"][0]["budget_unwritten"]
    assert [item["event_seq"] for item in deferred] == [3, 4, 5]
    assert all(item["reason"] == "LOGICAL_TIME_BUDGET" for item in deferred)
    assert report["summary"]["budget_unwritten"] == 3
    assert report["summary"]["written"] == 2
    assert store.status()["queue_by_state"]["pending"] == 3

    max_store = AuditStore(tmp_path / "budget-count.db")
    max_bundle = make_bundle(
        events,
        [declaration("a", 10000, 500, "reject")],
        max_events_per_round=2,
        logical_time_budget_ms=100,
        clock_step_ms=1,
    )
    max_store.load_bundle(max_bundle)
    max_report = AuditEngine(max_store).write(max_rounds=1)
    assert [
        item["reason"] for item in max_report["rounds"][0]["budget_unwritten"]
    ] == ["MAX_EVENTS_PER_ROUND"] * 3

    abandon_store = AuditStore(tmp_path / "abandon.db")
    abandon_store.load_bundle(bundle)
    abandon_report = AuditEngine(abandon_store).write(abandon_sequences=(3,))
    assert abandon_report["active_abandoned"][0]["status"] == "abandoned"
    assert abandon_report["summary"]["abandoned"] == 1
    assert abandon_store.verify()["ok"] is True
    assert abandon_store.status()["queue_by_state"]["abandoned"] == 1

    zero_store = AuditStore(tmp_path / "budget-below-step.db")
    zero_bundle = make_bundle(
        events,
        [declaration("a", 10000, 500, "reject")],
        max_events_per_round=4,
        logical_time_budget_ms=5,
        clock_step_ms=10,
    )
    zero_store.load_bundle(zero_bundle)
    zero_report = AuditEngine(zero_store).write(max_rounds=1)
    assert zero_report["rounds"][0]["outcomes"] == []
    assert len(zero_report["rounds"][0]["budget_unwritten"]) == 5
    assert zero_report["summary"]["budget_unwritten"] == 5


def test_ledger_rebuild_consistency_and_tamper_detection(tmp_path: Path) -> None:
    db = tmp_path / "tamper.db"
    store = AuditStore(db)
    store.load_bundle(sample_bundle())
    AuditEngine(store).write()
    assert store.verify()["ok"] is True

    with sqlite3.connect(db) as connection:
        connection.execute(
            """
            UPDATE tenant_ledger SET used_bytes = used_bytes + 1
            WHERE tenant = 'tenant-alpha' AND window_id = 'day-19723'
              AND ledger_version = (SELECT value FROM meta WHERE key = 'current_ledger_version')
            """
        )
        connection.commit()
    verified = store.verify()
    assert verified["ok"] is False
    assert any(
        item["code"] == "AUD-LEDGER-DIGEST-MISMATCH"
        for item in verified["mismatches"]
    )
    with pytest.raises(AuditError) as exc_info:
        AuditEngine(store).write()
    assert exc_info.value.code == "AUD-LEDGER-DIGEST-MISMATCH"
    assert exc_info.value.tenant == "tenant-alpha"
    assert exc_info.value.window_id == "day-19723"


def test_forced_termination_recovery_is_exactly_once(tmp_path: Path) -> None:
    db = tmp_path / "crash.db"
    store = AuditStore(db)
    store.load_bundle(sample_bundle())
    worker = """
import os
import sys
from audit_ledger import AuditEngine, AuditStore

store = AuditStore(sys.argv[1])
def crash(intent_id, next_index, ledger_version):
    os._exit(77)

AuditEngine(store, after_group_commit=crash).write()
"""
    completed = subprocess.run(
        [sys.executable, "-c", worker, str(db)],
        cwd=PROJECT_ROOT,
        text=True,
        capture_output=True,
        timeout=30,
    )
    assert completed.returncode == 77
    with store.connection() as connection:
        prepared = connection.execute(
            "SELECT COUNT(*) AS count FROM write_intents WHERE state = 'prepared'"
        ).fetchone()["count"]
        assert prepared == 1
        committed_events = connection.execute(
            "SELECT COUNT(*) AS count FROM processed_events"
        ).fetchone()["count"]
        assert committed_events == 3

    resumed = AuditEngine(store).resume(advance_clock_ms=50)
    assert resumed["recovered_intents"]
    with store.connection() as connection:
        duplicate_audit = connection.execute(
            """
            SELECT COUNT(*) AS count FROM (
              SELECT event_seq, event_version, COUNT(*) AS n
              FROM audit_events GROUP BY event_seq, event_version HAVING n > 1
            )
            """
        ).fetchone()["count"]
        assert duplicate_audit == 0
        assert connection.execute(
            "SELECT COUNT(*) AS count FROM processed_events"
        ).fetchone()["count"] == 11
        assert connection.execute(
            "SELECT COUNT(*) AS count FROM audit_events"
        ).fetchone()["count"] == 8
    expected = json.loads((SAMPLES / "expected_summary.json").read_text("utf-8"))
    assert store.verify(expected)["ok"] is True


def test_multiprocess_tenant_writers_and_idempotent_race(tmp_path: Path) -> None:
    db = tmp_path / "concurrent.db"
    store = AuditStore(db)
    store.load_bundle(sample_bundle())
    processes = [
        subprocess.Popen(
            [
                sys.executable,
                "-m",
                "audit_ledger",
                "write",
                "--db",
                str(db),
                "--tenant",
                tenant,
            ],
            cwd=PROJECT_ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        for tenant in ("tenant-alpha", "tenant-beta", "tenant-gamma")
    ]
    for process in processes:
        stdout, stderr = process.communicate(timeout=30)
        assert process.returncode == 0, stdout + stderr
    expected = json.loads((SAMPLES / "expected_summary.json").read_text("utf-8"))
    assert store.verify(expected)["ok"] is True
    with store.connection() as connection:
        assert connection.execute(
            "SELECT COUNT(*) AS count FROM processed_events"
        ).fetchone()["count"] == 11
        assert connection.execute(
            "SELECT COUNT(*) AS count FROM audit_events"
        ).fetchone()["count"] == 8

    race_db = tmp_path / "idempotent-race.db"
    race_store = AuditStore(race_db)
    race_bundle = make_bundle(
        [event_raw(1, "a", {"action": "race"}, 5000, "race-key")],
        [declaration("a", 1000, 500, "reject")],
    )
    race_store.load_bundle(race_bundle)
    racers = [
        subprocess.Popen(
            [
                sys.executable,
                "-m",
                "audit_ledger",
                "write",
                "--db",
                str(race_db),
            ],
            cwd=PROJECT_ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        for _ in range(2)
    ]
    for process in racers:
        stdout, stderr = process.communicate(timeout=30)
        assert process.returncode == 0, stdout + stderr
    with race_store.connection() as connection:
        assert connection.execute(
            "SELECT COUNT(*) AS count FROM audit_events"
        ).fetchone()["count"] == 1
        assert connection.execute(
            "SELECT COUNT(*) AS count FROM processed_events"
        ).fetchone()["count"] == 1
    assert race_store.verify()["ok"] is True


def test_config_clock_and_idempotency_error_codes(tmp_path: Path) -> None:
    payload = {"action": "login"}
    store = AuditStore(tmp_path / "errors.db")
    version_two = make_bundle(
        [event_raw(1, "a", payload, 1000, "errors-1")],
        [declaration("a", 1000, 500, "reject")],
        quota_version=2,
    )
    store.load_bundle(version_two)
    version_one_raw = quotas_raw(
        [declaration("a", 1000, 500, "reject")], version=1
    )
    version_one_quotas = parse_quotas(version_one_raw)
    version_one_policy = parse_policy(policy_raw(max_clock_regression_ms=0))
    version_one_events = parse_events(
        {
            "stream_version": 1,
            "events": [event_raw(1, "a", payload, 1000, "errors-1")],
        },
        version_one_quotas,
        version_one_policy,
    )
    with pytest.raises(AuditError) as regression:
        store.load_bundle(
            InputBundle(
                events=version_one_events,
                quotas=version_one_quotas,
                policy=version_one_policy,
            )
        )
    assert regression.value.code == "AUD-QUOTA-VERSION-REGRESSION"

    clock_store = AuditStore(tmp_path / "clock.db")
    clock_bundle = make_bundle(
        [
            event_raw(1, "a", payload, 1000, "clock-1"),
            event_raw(2, "a", payload, 500, "clock-2"),
        ],
        [declaration("a", 1000, 500, "reject")],
        max_clock_regression_ms=0,
    )
    clock_store.load_bundle(clock_bundle)
    with pytest.raises(AuditError) as clock_error:
        AuditEngine(clock_store).write()
    assert clock_error.value.code == "AUD-CLOCK-REGRESSION"
    assert clock_error.value.tenant == "a"
    assert clock_error.value.window_id == "day-0"

    idem_store = AuditStore(tmp_path / "idem.db")
    first = make_bundle(
        [event_raw(1, "a", payload, 1000, "same-key")],
        [declaration("a", 1000, 500, "reject")],
    )
    idem_store.load_bundle(first)
    AuditEngine(idem_store).write()
    conflict = make_bundle(
        [event_raw(2, "a", {"action": "different"}, 1001, "same-key")],
        [declaration("a", 1000, 500, "reject")],
    )
    with pytest.raises(AuditError) as idem_error:
        idem_store.load_bundle(conflict)
    assert idem_error.value.code == "AUD-IDEMPOTENCY-CONFLICT"
    assert idem_error.value.tenant == "a"
    assert idem_error.value.window_id == "day-0"
    with idem_store.connection() as connection:
        assert connection.execute(
            "SELECT COUNT(*) AS count FROM submissions"
        ).fetchone()["count"] == 1


def test_invalid_input_returns_code_without_creating_database(
    tmp_path: Path,
) -> None:
    raw_events = json.loads((SAMPLES / "events.json").read_text("utf-8"))
    raw_events["events"][0]["payload_digest"] = "sha256:" + "0" * 64
    invalid_path = tmp_path / "invalid-events.json"
    invalid_path.write_text(
        json.dumps(raw_events), encoding="utf-8"
    )
    db = tmp_path / "must-not-exist.db"
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "audit_ledger",
            "load",
            "--db",
            str(db),
            "--events",
            str(invalid_path),
            "--quotas",
            str(SAMPLES / "quotas.json"),
            "--policy",
            str(SAMPLES / "policy.json"),
        ],
        cwd=PROJECT_ROOT,
        text=True,
        capture_output=True,
        timeout=30,
    )
    assert completed.returncode == 2
    payload = json.loads(completed.stdout)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "AUD-EVENT-DIGEST-MISMATCH"
    assert not db.exists()


def test_validation_codes_and_retry_delay_bounds() -> None:
    quotas = parse_quotas(
        quotas_raw([declaration("a", 1000, 500, "reject")])
    )
    policy = parse_policy(policy_raw())
    base = {"action": "validate"}

    with pytest.raises(AuditError) as sequence_error:
        parse_events(
            {
                "events": [
                    event_raw(2, "a", base, 100, "v-2"),
                    event_raw(1, "a", base, 101, "v-1"),
                ]
            },
            quotas,
            policy,
        )
    assert sequence_error.value.code == "AUD-EVENT-SEQ-NOT-INCREASING"

    with pytest.raises(AuditError) as tenant_error:
        parse_events(
            {"events": [event_raw(1, "unknown", base, 100, "v-tenant")]},
            quotas,
            policy,
        )
    assert tenant_error.value.code == "AUD-EVENT-TENANT-UNKNOWN"

    with pytest.raises(AuditError) as category_error:
        parse_events(
            {
                "events": [
                    event_raw(1, "a", base, 100, "v-category", category="other")
                ]
            },
            quotas,
            policy,
        )
    assert category_error.value.code == "AUD-EVENT-CATEGORY-UNKNOWN"

    with pytest.raises(AuditError) as clock_error:
        parse_events(
            {"events": [event_raw(1, "a", base, -1, "v-clock")]},
            quotas,
            policy,
        )
    assert clock_error.value.code == "AUD-EVENT-CLOCK-INVALID"

    with pytest.raises(AuditError) as quota_error:
        parse_quotas(quotas_raw([declaration("a", 0, 500, "reject")]))
    assert quota_error.value.code == "AUD-QUOTA-INVALID"

    with pytest.raises(AuditError) as policy_error:
        parse_policy(policy_raw(retry_base_ms=30, retry_cap_ms=20))
    assert policy_error.value.code == "AUD-POLICY-INVALID"

    first = retry_delay_ms("retry-key", 3, 5, 20)
    assert first == retry_delay_ms("retry-key", 3, 5, 20)
    assert 5 <= first <= 20
