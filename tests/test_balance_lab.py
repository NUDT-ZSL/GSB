from __future__ import annotations

import json
import os
import signal
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

from balance_lab.canonical import write_json
from balance_lab.cli import main as cli_main
from balance_lab.errors import BalanceLabError, ReportVersionError
from balance_lab.evolution import run_evolution
from balance_lab.fixedpoint import residual_summary
from balance_lab.ledger import Ledger
from balance_lab.model import load_model
from balance_lab.pipeline import run_pipeline
from balance_lab.report import atomic_write_report
from balance_lab.sample import build_sample_bundle, generate_sample_files

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def sample_dir(tmp_path: Path) -> Path:
    path = tmp_path / "samples"
    generate_sample_files(path)
    return path


def _write_bundle(directory: Path, bundle: dict) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    for filename, value in bundle.items():
        write_json(directory / filename, value)
    return directory


def test_sample_validates_and_has_thirteen_chunks(sample_dir: Path) -> None:
    model = load_model(sample_dir)
    assert [battle.chunk_count for battle in model.battle_list()] == [3, 4, 6]
    assert model.input_version_id.startswith("in_")


@pytest.mark.parametrize(
    "mutate,expected_code",
    [
        (
            lambda bundle: bundle["balance_numbers.json"]["units"].append(
                dict(bundle["balance_numbers.json"]["units"][0])
            ),
            "E_DUPLICATE_ID",
        ),
        (
            lambda bundle: bundle["battle_scripts.json"]["battles"][0]["rounds"][0][
                "actions"
            ][0].update({"logic_time": -2}),
            "E_BAD_LOGIC_TIME",
        ),
        (
            lambda bundle: bundle["battle_scripts.json"]["battles"][0]["rounds"][0][
                "actions"
            ][0].update({"target_id": "u_missing"}),
            "E_TARGET_NOT_FOUND",
        ),
        (
            lambda bundle: bundle["balance_numbers.json"]["units"][0].update(
                {"crit_rate": 0.123456789}
            ),
            "E_FIXED_NOT_QUANTIZABLE",
        ),
        (
            lambda bundle: bundle["levels.json"]["criteria"]["cr_peak"].update(
                {"min_team_hp_ratio": 1.5}
            ),
            "E_THRESHOLD_DOMAIN",
        ),
    ],
)
def test_invalid_input_returns_business_error_without_ledger(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], mutate, expected_code: str
) -> None:
    bundle = build_sample_bundle()
    mutate(bundle)
    bad_input = _write_bundle(tmp_path / "bad-input", bundle)
    state_dir = tmp_path / "state"
    exit_code = cli_main(
        [
            "run",
            "--input",
            str(bad_input),
            "--state-dir",
            str(state_dir),
            "--reports-dir",
            str(tmp_path / "reports"),
        ]
    )
    output = json.loads(capsys.readouterr().out)
    assert exit_code == 2
    assert output["ok"] is False
    assert output["error"]["code"] == expected_code
    assert not (state_dir / "balance.db").exists()


def test_residual_summary_marks_first_breach() -> None:
    records = [
        {
            "round": 1,
            "unit_id": "u_a",
            "skill_id": "sk_a",
            "fixed_raw": 1_000_000,
            "float_value": 1.0,
        },
        {
            "round": 2,
            "unit_id": "u_b",
            "skill_id": "sk_b",
            "fixed_raw": 2_000_000,
            "float_value": 2.01,
        },
    ]
    summary = residual_summary(
        records, scale=1_000_000, abs_tolerance=1e-6, relative_tolerance=0.0
    )
    assert summary.breach_count == 1
    assert summary.first_breach is not None
    assert summary.first_breach["round"] == 2
    assert summary.first_breach["unit_id"] == "u_b"


def test_same_logic_time_uses_declaration_order(
    sample_dir: Path, tmp_path: Path
) -> None:
    state_dir = tmp_path / "order-state"
    result = run_pipeline(sample_dir, state_dir)
    assert result["status"] == "completed"
    for battle in result["aggregates"]:
        with Ledger(state_dir / "balance.db") as ledger:
            chunks = ledger.load_chunk_results(battle["battle_id"])
        for chunk in chunks:
            for round_record in chunk["rounds"]:
                orders = round_record["action_order"]
                keys = [
                    (item["logic_time"], item["declaration_index"]) for item in orders
                ]
                assert keys == sorted(keys)
                ties = [key for key in keys if key[0] == 0]
                if len(ties) > 1:
                    declarations = [key[1] for key in ties]
                    assert declarations == sorted(declarations)


def test_balance_checks_detect_defeat_timeout_and_reward_imbalance(
    tmp_path: Path,
) -> None:
    bundle = build_sample_bundle()
    for unit in bundle["balance_numbers.json"]["units"]:
        if unit["id"].startswith("u_"):
            unit["hp"] = 1
            unit["defense"] = 0
    bundle["balance_numbers.json"]["units"][3]["attack"] = 10_000
    defeat_dir = _write_bundle(tmp_path / "defeat-input", bundle)
    defeat = run_pipeline(defeat_dir, tmp_path / "defeat-state")
    training = next(
        item for item in defeat["aggregates"] if item["battle_id"] == "b_training"
    )
    assert training["balanced"] is False
    assert training["outcome"] == "defeat"
    assert any(
        check["check_id"] == "not_certain_defeat" and check["passed"] is False
        for check in training["checks"]
    )

    strict_bundle = build_sample_bundle()
    strict_bundle["levels.json"]["criteria"]["cr_training"]["max_rounds"] = 1
    strict_bundle["levels.json"]["criteria"]["cr_training"][
        "expected_reward_score"
    ] = 10_000.0
    strict_dir = _write_bundle(tmp_path / "strict-input", strict_bundle)
    strict = run_pipeline(strict_dir, tmp_path / "strict-state")
    strict_training = next(
        item for item in strict["aggregates"] if item["battle_id"] == "b_training"
    )
    checks = {check["check_id"]: check["passed"] for check in strict_training["checks"]}
    assert checks["round_budget"] is False
    assert checks["reward_lower_bound"] is False


def test_tight_residual_tolerance_reports_first_breach(
    sample_dir: Path, tmp_path: Path
) -> None:
    result = run_pipeline(
        sample_dir,
        tmp_path / "state",
        tmp_path / "report.json",
        config_overrides={"residual_abs_tolerance": 1e-12},
    )
    peak = next(item for item in result["aggregates"] if item["battle_id"] == "b_peak")
    assert peak["residual"]["breach_count"] > 0
    assert peak["residual"]["first_breach"]["round"] >= 1
    assert peak["residual"]["first_breach"]["skill_id"]


def test_parallelism_does_not_change_results_or_admission_order(
    sample_dir: Path, tmp_path: Path
) -> None:
    one = run_pipeline(
        sample_dir,
        tmp_path / "state-one",
        tmp_path / "one.json",
        verify=True,
        config_overrides={"parallelism": 1},
    )
    four_a = run_pipeline(
        sample_dir,
        tmp_path / "state-four-a",
        tmp_path / "four-a.json",
        config_overrides={"parallelism": 4},
    )
    four_b = run_pipeline(
        sample_dir,
        tmp_path / "state-four-b",
        tmp_path / "four-b.json",
        config_overrides={"parallelism": 4},
    )
    assert one["status"] == four_a["status"] == "completed"
    assert one["verification"]["consistent"]
    digests_one = [
        (item["battle_id"], item["content_digests"], item["final_state_digest"])
        for item in one["aggregates"]
    ]
    digests_four = [
        (item["battle_id"], item["content_digests"], item["final_state_digest"])
        for item in four_a["aggregates"]
    ]
    assert digests_one == digests_four
    assert (
        four_a["schedule"]["admission_sequence"]
        == four_b["schedule"]["admission_sequence"]
    )


def test_evolution_precise_invalidation_and_full_consistency(
    sample_dir: Path, tmp_path: Path
) -> None:
    result = run_evolution(
        sample_dir,
        tmp_path / "work",
        tmp_path / "state",
        tmp_path / "reports",
    )
    assert result["all_consistent"] is True
    counts = [step["invalid_chunk_count"] for step in result["steps"]]
    assert counts == [7, 10, 4, 6]

    first = result["steps"][0]
    assert first["affected_battles"] == ["b_core", "b_training"]
    assert all(item["battle_id"] != "b_peak" for item in first["invalid_chunks"])

    second_training = [
        item
        for item in result["steps"][1]["invalid_chunks"]
        if item["battle_id"] == "b_training"
    ]
    assert [item["chunk_index"] for item in second_training] == [1, 2]

    invalidation_doc = json.loads(
        (tmp_path / "reports" / "invalidation_report.json").read_text(encoding="utf-8")
    )
    assert invalidation_doc["all_consistent"] is True


def test_corrupt_chunk_digest_is_pruned_and_recomputed(
    sample_dir: Path, tmp_path: Path
) -> None:
    state_dir = tmp_path / "state"
    initial = run_pipeline(sample_dir, state_dir, tmp_path / "report.json")
    assert initial["status"] == "completed"
    db_path = state_dir / "balance.db"

    connection = sqlite3.connect(db_path)
    battle_id, chunk_index, state_blob = connection.execute(
        "SELECT battle_id, chunk_index, state_blob FROM chunks ORDER BY chunk_index LIMIT 1"
    ).fetchone()
    state = json.loads(state_blob)
    state["corruption_marker"] = True
    connection.execute(
        "UPDATE chunks SET state_blob=? WHERE battle_id=? AND chunk_index=?",
        (json.dumps(state), battle_id, chunk_index),
    )
    connection.commit()
    connection.close()

    with Ledger(db_path) as ledger:
        invalid = ledger.prune_invalid_chunks()
    assert invalid and invalid[0]["reason"] == "state_digest_mismatch"

    recovered = run_pipeline(
        sample_dir, state_dir, tmp_path / "report2.json", verify=True
    )
    assert recovered["status"] == "completed"
    assert recovered["verification"]["consistent"] is True


def test_memory_budget_blocks_oversized_chunk(sample_dir: Path, tmp_path: Path) -> None:
    with pytest.raises(BalanceLabError) as exc_info:
        run_pipeline(
            sample_dir,
            tmp_path / "state",
            tmp_path / "report.json",
            config_overrides={"memory_budget_bytes": 128},
        )
    assert exc_info.value.code == "E_MEMORY_CHUNK_EXCEEDS_BUDGET"


def test_logic_time_budget_defers_chunks_to_later_rounds(
    sample_dir: Path, tmp_path: Path
) -> None:
    result = run_pipeline(
        sample_dir,
        tmp_path / "state",
        tmp_path / "report.json",
        config_overrides={"logic_time_budget_ms": 1},
    )
    assert result["status"] == "completed"
    assert len(result["schedule"]["rounds"]) >= 5
    assert all(round_report["yielded_for_budget"] for round_report in result["schedule"]["rounds"])
    assert len(result["schedule"]["admission_sequence"]) == 13


def test_report_version_compare_and_swap(tmp_path: Path) -> None:
    report_path = tmp_path / "balance_report.json"
    first = atomic_write_report(report_path, {"hello": "first"})
    assert first["version_id"].startswith("rep_")
    with pytest.raises(ReportVersionError) as exc_info:
        atomic_write_report(
            report_path,
            {"hello": "conflict"},
            expected_existing_version_id="rep_wrong",
        )
    assert exc_info.value.code == "E_REPORT_VERSION_CONFLICT"
    second = atomic_write_report(
        report_path,
        {"hello": "second"},
        expected_existing_version_id=first["version_id"],
    )
    assert second["supersedes_version_id"] == first["version_id"]


def _subprocess_env() -> dict[str, str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    return env


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX SIGTERM subprocess harness")
def test_graceful_sigterm_then_restart_consistent(
    sample_dir: Path, tmp_path: Path
) -> None:
    state_dir = tmp_path / "state"
    report_path = tmp_path / "reports" / "balance_report.json"
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "balance_lab.cli",
            "run",
            "--input",
            str(sample_dir),
            "--state-dir",
            str(state_dir),
            "--reports-dir",
            str(tmp_path / "reports"),
            "--chunk-delay-ms",
            "300",
        ],
        cwd=ROOT,
        env=_subprocess_env(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    time.sleep(0.25)
    process.send_signal(signal.SIGTERM)
    stdout, stderr = process.communicate(timeout=10)
    assert process.returncode == 130, stderr + stdout
    marker = state_dir / "interrupt.json"
    assert marker.exists()
    assert json.loads(marker.read_text(encoding="utf-8"))["exit_code"] == 130

    recovered = run_pipeline(
        sample_dir, state_dir, report_path, verify=True
    )
    assert recovered["status"] == "completed"
    assert recovered["verification"]["consistent"] is True
    assert not marker.exists()


@pytest.mark.skipif(not hasattr(signal, "SIGKILL"), reason="POSIX SIGKILL recovery test")
def test_sigkill_restart_uses_committed_digests(
    sample_dir: Path, tmp_path: Path
) -> None:
    state_dir = tmp_path / "state"
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "balance_lab.cli",
            "run",
            "--input",
            str(sample_dir),
            "--state-dir",
            str(state_dir),
            "--reports-dir",
            str(tmp_path / "reports"),
            "--chunk-delay-ms",
            "400",
        ],
        cwd=ROOT,
        env=_subprocess_env(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    time.sleep(0.8)
    process.kill()
    process.wait(timeout=5)
    assert process.returncode != 0

    with Ledger(state_dir / "balance.db") as ledger:
        overview = ledger.completion_overview(
            {"b_training": 3, "b_core": 4, "b_peak": 6}
        )
        assert overview["all_complete"] is False

    recovered = run_pipeline(
        sample_dir,
        state_dir,
        tmp_path / "reports" / "balance_report.json",
        verify=True,
    )
    assert recovered["status"] == "completed"
    assert recovered["verification"]["consistent"] is True
