from __future__ import annotations

import json

from chaos_budget.bundle import load_bundle
from chaos_budget.demo import run_manifest_workflow
from chaos_budget.engine import Engine
from chaos_budget.storage import Database
from chaos_budget.utils import canonical_json
from generate_samples import generate
import main as cli


def test_manifest_workflow_conservation_states_and_determinism(tmp_path):
    sample_dir = tmp_path / "sample_data"
    generate(sample_dir)
    bundle = load_bundle(sample_dir)

    first = run_manifest_workflow(bundle)
    second = run_manifest_workflow(bundle)

    assert first["verify"]["ok"] is True
    assert second["verify"]["ok"] is True
    assert canonical_json(first["status"]) == canonical_json(second["status"])

    states = {e["experiment_id"]: e["state"] for e in first["status"]["experiments"]}
    assert states["EXP-001"] == "completed"
    assert states["EXP-006"] == "failed"
    assert states["EXP-007"] == "blocked_waiting"
    assert states["EXP-009"] == "recovered"
    assert states["EXP-010"] == "running"
    assert states["EXP-011"] == "pending_admission"
    assert states["EXP-003"] == "abandoned"
    assert states["EXP-008"] == "abandoned"

    outcomes = first["status"]["outcome_counts"]
    assert outcomes["failed_after_start"] >= 1
    assert outcomes["voluntarily_abandoned"] == 3
    assert outcomes["within_budget_not_started"] == 1

    summary = first["verify"]["summary"]
    assert summary["total_balance"] == (
        summary["total_initial_balance"]
        - summary["total_debited"]
        + summary["total_credited"]
    )


def test_duplicate_observation_is_idempotent_and_single_charge(tmp_path):
    sample_dir = tmp_path / "sample_data"
    generate(sample_dir)
    bundle = load_bundle(sample_dir)
    db = Database.initialize(":memory:", bundle, reset=True)
    engine = Engine(db, bundle)

    engine.try_admit("EXP-001", 1000)
    blocked_event = next(
        event
        for event in bundle["observations"]
        if event["experiment_id"] == "EXP-001" and event["sequence"] == 1
    )

    first = engine.submit_observation(blocked_event)
    second = engine.submit_observation(blocked_event)

    assert first["decision_seq"] == second["decision_seq"]
    assert second["idempotent"] is True

    phase_count = db.conn.execute(
        """
        SELECT COUNT(*) AS count FROM budget_ledger
         WHERE account_id=? AND reason='phase_progression_charge'
        """,
        ("experiment:EXP-001:points",),
    ).fetchone()["count"]
    assert phase_count == 1

    processed = db.conn.execute(
        "SELECT COUNT(*) AS count FROM observation_inbox WHERE experiment_id='EXP-001' AND sequence=1"
    ).fetchone()["count"]
    assert processed == 1


def test_recovery_generation_mismatch_is_rejected_without_state_change(tmp_path):
    sample_dir = tmp_path / "sample_data"
    generate(sample_dir)
    bundle = load_bundle(sample_dir)
    db = Database.initialize(":memory:", bundle, reset=True)
    engine = Engine(db, bundle)

    engine.try_admit("EXP-002", 1000)
    failure = next(
        e for e in bundle["observations"] if e["experiment_id"] == "EXP-002" and e["sequence"] == 1
    )
    engine.submit_observation(failure)
    bad_recovery = dict(
        next(e for e in bundle["observations"] if e["experiment_id"] == "EXP-002" and e["sequence"] == 2)
    )
    bad_recovery["generation"] = 99

    try:
        engine.submit_observation(bad_recovery)
    except Exception as exc:
        assert exc.code == "GENERATION_MISMATCH"
    else:
        raise AssertionError("generation mismatch must fail")

    state = db.conn.execute(
        "SELECT state FROM experiments WHERE experiment_id='EXP-002'"
    ).fetchone()["state"]
    assert state == "failed"
    inbox_status = db.conn.execute(
        "SELECT status FROM observation_inbox WHERE experiment_id='EXP-002' AND sequence=2"
    ).fetchone()["status"]
    assert inbox_status == "rejected"


def test_cli_load_status_verify_persisted(tmp_path, capsys):
    sample_dir = tmp_path / "sample_data"
    generate(sample_dir)
    db_path = tmp_path / "runtime" / "state.sqlite3"

    assert cli.main(["--sample-dir", str(sample_dir), "--db", str(db_path), "load", "--reset"]) == 0
    assert db_path.exists()
    capsys.readouterr()
    assert cli.main(["--sample-dir", str(sample_dir), "--db", str(db_path), "status"]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["persisted"] is True

    assert cli.main(["--sample-dir", str(sample_dir), "--db", str(db_path), "verify", "--strict"]) == 0
    verify = json.loads(capsys.readouterr().out)
    assert verify["ok"] is True
    assert verify["mode"] == "persisted_sqlite"


def test_cli_bare_observe_runs_idempotent_sample_workflow(tmp_path, capsys):
    sample_dir = tmp_path / "sample_data"
    generate(sample_dir)
    db_path = tmp_path / "runtime" / "bare.sqlite3"

    assert cli.main(["--sample-dir", str(sample_dir), "--db", str(db_path), "load", "--reset"]) == 0
    capsys.readouterr()
    assert cli.main(["--sample-dir", str(sample_dir), "--db", str(db_path), "observe"]) == 0
    observed = json.loads(capsys.readouterr().out)
    assert observed["ok"] is True
    assert observed["mode"] == "idempotent_manifest_workflow"

    assert cli.main(["--sample-dir", str(sample_dir), "--db", str(db_path), "observe"]) == 0
    second = json.loads(capsys.readouterr().out)
    assert second["ok"] is True

    assert cli.main(["--sample-dir", str(sample_dir), "--db", str(db_path), "verify", "--strict"]) == 0
    verified = json.loads(capsys.readouterr().out)
    assert verified["verification"]["ok"] is True
