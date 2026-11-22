from __future__ import annotations

import json
from pathlib import Path

import pytest

from conftest import ROOT, cli_json
from stream_reopt.engine import Engine
from stream_reopt.errors import EngineError
from stream_reopt.sample import load_sample


@pytest.fixture(scope="module")
def main_run(tmp_path_factory: pytest.TempPathFactory) -> tuple[dict, dict, dict]:
    root = tmp_path_factory.mktemp("main-run")
    database = root / "runtime.db"
    code, payload, completed = cli_json(
        [
            "run",
            "--sample",
            "samples/reopt_demo.json",
            "--db",
            str(database),
            "--batches",
            "9",
        ],
        cwd=ROOT,
    )
    assert code == 0, completed.stderr
    verify_code, verify, verify_completed = cli_json(
        [
            "verify",
            "--sample",
            "samples/reopt_demo.json",
            "--db",
            str(database),
        ],
        cwd=ROOT,
    )
    assert verify_code == 0, verify_completed.stderr
    status_code, status_payload, status_completed = cli_json(
        [
            "status",
            "--sample",
            "samples/reopt_demo.json",
            "--db",
            str(database),
        ],
        cwd=ROOT,
    )
    assert status_code == 0, status_completed.stderr
    return payload, verify, status_payload


def test_main_run_observations_and_switches(main_run: tuple[dict, dict, dict]) -> None:
    payload, _, status = main_run
    assert payload["ok"] is True
    assert payload["processed"] == list(range(1, 10))
    observations = status["observations"]
    assert len(observations) == 9
    assert [item["arrival"] for item in observations] == list(range(1, 10))
    assert [item["input_rows"] for item in observations] == sorted(
        item["input_rows"] for item in observations
    )
    assert [item["output_rows"] for item in observations] == sorted(
        item["output_rows"] for item in observations
    )
    drift_records = status["drift_records"]
    assert drift_records[0]["seq"] == 2
    assert drift_records[0]["decision"] == "cooldown_suppressed"
    assert drift_records[0]["rejected"][0]["reason"] == "cooldown"
    assert drift_records[1]["seq"] == 3
    assert drift_records[1]["trigger"] is True
    assert drift_records[1]["sources"][0]["source"]["table"]
    assert drift_records[1]["sources"][0]["source"]["join_key"]
    assert drift_records[1]["sources"][0]["batch_interval"]
    reopt_records = status["reopt_records"]
    assert reopt_records[0]["outcome"] == "switched"
    assert reopt_records[0]["version_changed"] is True
    assert reopt_records[0]["from_version"] == 1
    assert reopt_records[0]["to_version"] == 2
    assert reopt_records[0]["rejected_candidates"]
    assert status["shape_version"] >= 2
    assert status["precision_sensitive"] is True


def test_main_run_row_by_row_truth(main_run: tuple[dict, dict, dict]) -> None:
    _, verify, _ = main_run
    assert verify["ok"] is True
    assert verify["row_by_row_match"] is True
    assert verify["first_mismatch_index"] is None
    assert verify["persisted_digest_match"] is True
    assert verify["batch_digest_checks"] == 9
    assert verify["truth"]["row_count"] == verify["current_shape"]["row_count"]
    assert verify["truth"]["digest"] == verify["current_shape"]["digest"]


def test_runs_are_byte_stable_across_databases(
    tmp_path: Path,
    main_run: tuple[dict, dict, dict],
) -> None:
    _, _, status = main_run
    sample = load_sample(ROOT / "samples" / "reopt_demo.json")
    second_database = tmp_path / "second.db"
    with Engine(sample, second_database) as engine:
        engine.run()
        second_status = engine.status()
    assert second_status == status


def test_budget_exhaustion_keeps_original_shape(tmp_path: Path) -> None:
    database = tmp_path / "budget.db"
    code, payload, completed = cli_json(
        [
            "run",
            "--sample",
            "samples/budget_exhausted.json",
            "--db",
            str(database),
            "--batches",
            "9",
        ],
        cwd=ROOT,
    )
    assert code == 3, completed.stderr
    assert payload["error"]["code"] == "E_SWITCH_BUDGET"
    assert payload["processed"] == [1, 2, 3]
    assert payload["status"]["watermark"]["committed_batches"] == 3
    assert payload["status"]["shape_version"] == 1
    reopt = next(item for item in payload["status"]["reopt_records"] if item["outcome"] == "kept_original_budget")
    assert reopt["error_code"] == "E_SWITCH_BUDGET"
    assert reopt["from_version"] == reopt["to_version"] == 1
    assert all(candidate["switch_cost"] > 0.5 for candidate in reopt["rejected_candidates"])


def test_precision_sensitive_error_chain(tmp_path: Path) -> None:
    database = tmp_path / "precision.db"
    code, payload, completed = cli_json(
        [
            "run",
            "--sample",
            "samples/precision_sensitive.json",
            "--db",
            str(database),
            "--batches",
            "9",
        ],
        cwd=ROOT,
    )
    assert code == 0, completed.stderr
    assert payload["status"]["precision_sensitive"] is True
    flagged = [
        record for record in payload["status"]["reopt_records"] if record["precision_sensitive"]
    ]
    assert flagged
    chain = flagged[0]["error_chain"]
    assert chain["top_two_cost_difference"] <= chain["float_tolerance"]
    assert chain["participating_tables"]
    assert chain["batch_interval"]
    assert chain["steps"]
    assert chain["top_two_error_bounds"]


def test_idempotent_duplicate_commit_and_order_guard(tmp_path: Path) -> None:
    sample = load_sample(ROOT / "samples" / "reopt_demo.json")
    database = tmp_path / "idempotent.db"
    with Engine(sample, database) as engine:
        with pytest.raises(EngineError) as out_of_order:
            engine.commit_arrival(3)
        assert out_of_order.value.code == "E_OUT_OF_ORDER"
        first = engine.commit_arrival(1)
        second = engine.commit_arrival(1)
        assert first.committed is True and first.idempotent is False
        assert second.committed is False and second.idempotent is True
        assert len(engine.storage.observations()) == 1
        assert engine.storage.state().committed_count == 1
