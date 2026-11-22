from __future__ import annotations

from pathlib import Path

from conftest import SAMPLES, remove_db, run_cli_json


def _setup(tmp_path: Path) -> tuple[Path, dict]:
    db = tmp_path / "invalidation.sqlite"
    remove_db(db)
    run_cli_json(["load", str(SAMPLES / "sample.json"), "--db", str(db)])
    run_cli_json(["run", "--db", str(db)])
    _, status = run_cli_json(["status", "--db", str(db)])
    return db, status


def test_weight_change_reuses_all_ledgers_and_only_recomputes_aggregates(
    tmp_path: Path,
) -> None:
    db, before = _setup(tmp_path)
    code, loaded = run_cli_json(
        [
            "load",
            str(SAMPLES / "params_v2_weight.json"),
            "--db",
            str(db),
            "--version",
            "2",
        ]
    )
    assert code == 0
    invalidation = loaded["imported"]["invalidation"]
    assert invalidation["batch_version"] == 2
    assert len(invalidation["changed"]) == 1
    changed = invalidation["changed"][0]
    assert (changed["iface"], changed["queue"], changed["trigger"]) == (
        "eth0",
        0,
        "weight",
    )
    actions = {
        item["component"]: item["action"] for item in changed["components"]
    }
    assert actions["interrupt_count"] == "reuse"
    assert actions["conversion_residue"] == "reuse"
    assert actions["throughput"] == "reuse"
    assert actions["throughput_aggregate"] == "recompute"
    assert actions["attribution"] == "recompute"
    assert changed["detail"]["affected_record_interval"] == {
        "first": 1000,
        "last": 1730,
    }
    assert changed["detail"]["reused_ledger_rows"] == 30
    _, after = run_cli_json(["status", "--db", str(db)])
    for old, new in zip(before["queues"], after["queues"]):
        assert old["ledger_digest"] == new["ledger_digest"]
        assert old["param_version"] == new["param_version"]
        assert old["fixed_interrupts"] == new["fixed_interrupts"]
    assert after["queues"][0]["weight"] == 5
    assert after["weighted_throughput_bps"] != before["weighted_throughput_bps"]
    _, rerun = run_cli_json(["run", "--db", str(db)])
    assert rerun["idempotent"] == 75
    code, verified = run_cli_json(["verify", "--db", str(db)])
    assert code == 0
    assert verified["ok"] is True


def test_coalescing_change_resets_only_affected_queue(tmp_path: Path) -> None:
    db, before = _setup(tmp_path)
    code, loaded = run_cli_json(
        [
            "load",
            str(SAMPLES / "params_v2_coalesce.json"),
            "--db",
            str(db),
            "--version",
            "2",
        ]
    )
    assert code == 0
    changed = loaded["imported"]["invalidation"]["changed"]
    assert len(changed) == 1
    assert changed[0]["queue"] == 0
    assert changed[0]["trigger"] == "coalescing"
    _, after = run_cli_json(["status", "--db", str(db)])
    assert after["queues"][0]["record_watermark"] is None
    assert after["queues"][0]["fixed_interrupts"] == 0
    assert after["queues"][0]["param_version"] != before["queues"][0]["param_version"]
    for old, new in zip(before["queues"][1:], after["queues"][1:]):
        assert old["ledger_digest"] == new["ledger_digest"]
        assert old["record_watermark"] == new["record_watermark"]
        assert old["fixed_interrupts"] == new["fixed_interrupts"]
    _, rerun = run_cli_json(["run", "--db", str(db), "--queue", "eth0:0"])
    assert rerun["committed"] == 30
    _, final = run_cli_json(["status", "--db", str(db)])
    assert final["queues"][0]["fixed_interrupts"] == 7
    code, verified = run_cli_json(["verify", "--db", str(db)])
    assert code == 0
    assert verified["ok"] is True


def test_version_rollback_and_version_conflict_are_rejected(tmp_path: Path) -> None:
    db = tmp_path / "versions.sqlite"
    remove_db(db)
    run_cli_json(["load", str(SAMPLES / "sample.json"), "--db", str(db)])
    run_cli_json(
        [
            "load",
            str(SAMPLES / "params_v2_coalesce.json"),
            "--db",
            str(db),
            "--version",
            "2",
        ]
    )
    code, rollback = run_cli_json(
        [
            "load",
            str(SAMPLES / "sample.json"),
            "--db",
            str(db),
            "--version",
            "1",
        ]
    )
    assert code == 2
    assert rollback["error"]["code"] == "E_PARAM_VERSION_ROLLBACK"

    code, conflict = run_cli_json(
        [
            "load",
            str(SAMPLES / "params_v2_weight.json"),
            "--db",
            str(db),
            "--version",
            "2",
        ]
    )
    assert code == 2
    assert conflict["error"]["code"] == "E_PARAM_VERSION_CONFLICT"

