from __future__ import annotations

import json

import pytest

from chaos_budget.bundle import load_bundle
from chaos_budget.errors import ChaosError
from chaos_budget.utils import canonical_json
from generate_samples import generate
import main as cli


@pytest.fixture()
def sample_dir(tmp_path):
    path = tmp_path / "sample_data"
    generate(path)
    return path


def test_generated_bundle_validates_and_merges_policy(sample_dir):
    bundle = load_bundle(sample_dir)

    assert len(bundle["experiments"]) == 11
    assert len(bundle["observations"]) == 12
    policies = bundle["policy_view"]["policies"]

    phase = policies["phase_charge_points"]
    assert phase["value"] == 3
    assert phase["source_format"] == "v2_shard_sqlite"
    assert phase["selection_reason"] == "higher_declared_logical_clock"

    recovery = policies["recovery_restore_bps"]
    assert recovery["value"] == 6000
    assert recovery["source_format"] == "v2_shard_sqlite"
    assert recovery["selection_reason"] == "equal_clock_and_generation_used_source_format_priority"

    conversion = next(
        item["time_conversion"]
        for item in policies.values()
        if item["time_conversion"] is not None
    )
    assert conversion["scale"] == 1000
    assert conversion["rounding_direction"] == "ceil_to_positive_infinity"
    assert conversion["rounded_milliseconds"] == 251
    assert conversion["effective_absolute_clock_ms"] == 1251


@pytest.mark.parametrize(
    "mutate",
    [
        "duplicate_experiment",
        "negative_clock",
        "zero_duration",
        "bad_event_sequence",
        "summary_mismatch",
    ],
)
def test_malformed_input_returns_business_code_without_state(tmp_path, sample_dir, mutate, capsys):
    if mutate == "duplicate_experiment":
        path = sample_dir / "experiments.json"
        payload = json.loads(path.read_text())
        payload["experiments"][1]["experiment_id"] = payload["experiments"][0]["experiment_id"]
        path.write_text(json.dumps(payload))
    elif mutate == "negative_clock":
        path = sample_dir / "experiments.json"
        payload = json.loads(path.read_text())
        payload["experiments"][0]["start_logical_clock_ms"] = -1
        path.write_text(json.dumps(payload))
    elif mutate == "zero_duration":
        path = sample_dir / "budget_config.json"
        payload = json.loads(path.read_text())
        payload["budget_config"]["backoff_base_ms"] = 0
        path.write_text(json.dumps(payload))
    elif mutate == "bad_event_sequence":
        path = sample_dir / "observations.json"
        payload = json.loads(path.read_text())
        exp1 = [e for e in payload["observations"] if e["experiment_id"] == "EXP-001"]
        exp1[1]["sequence"] = exp1[0]["sequence"]
        path.write_text(json.dumps(payload))
    elif mutate == "summary_mismatch":
        path = sample_dir / "observations.json"
        payload = json.loads(path.read_text())
        payload["observations"][0]["summary"] = "summary does not match declaration"
        path.write_text(json.dumps(payload))

    db_path = tmp_path / "does" / "not" / "exist" / "state.sqlite3"
    rc = cli.main(
        [
            "--sample-dir",
            str(sample_dir),
            "--db",
            str(db_path),
            "load",
            "--reset",
        ]
    )
    emitted = json.loads(capsys.readouterr().out)

    assert rc == 2
    assert emitted["ok"] is False
    assert emitted["error"]["code"] in {"VALIDATION_FAILED", "INVALID_POSITIVE_INTEGER"}
    assert not db_path.exists()
    assert not db_path.parent.exists()


def test_status_without_database_is_stable_baseline(sample_dir, tmp_path, capsys):
    db_path = tmp_path / "missing.sqlite3"
    rc = cli.main(["--sample-dir", str(sample_dir), "--db", str(db_path), "status"])
    payload = json.loads(capsys.readouterr().out)

    assert rc == 0
    assert payload["persisted"] is False
    assert [e["experiment_id"] for e in payload["experiments"]] == sorted(
        e["experiment_id"] for e in payload["experiments"]
    )
    assert payload["experiments"][0]["remaining_budget_points"] == 1000
    first = canonical_json(payload)
    cli.main(["--sample-dir", str(sample_dir), "--db", str(db_path), "status"])
    second = canonical_json(json.loads(capsys.readouterr().out))
    assert first == second


def test_invalid_json_has_explicit_code(tmp_path, sample_dir, capsys):
    (sample_dir / "budget_config.json").write_text("{not json", encoding="utf-8")
    db_path = tmp_path / "state.sqlite3"
    rc = cli.main(["--sample-dir", str(sample_dir), "--db", str(db_path), "load"])
    payload = json.loads(capsys.readouterr().out)
    assert rc == 2
    assert payload["error"]["code"] == "MALFORMED_JSON"
    assert not db_path.exists()


def test_chaos_error_carries_code():
    with pytest.raises(ChaosError) as exc_info:
        raise ChaosError("X", "y")
    assert exc_info.value.code == "X"
