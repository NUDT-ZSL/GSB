from __future__ import annotations

import json
from pathlib import Path

from account_verify.cli import main
from account_verify.integrity import verify_samples_against_truth


def test_full_cli_workflow_exit_codes_and_report(sample_dir: Path, tmp_path: Path, capsys):
    state = tmp_path / "state"
    report = tmp_path / "reports" / "report.json"

    assert main(["import", "--samples", str(sample_dir), "--state", str(state)]) == 0
    capsys.readouterr()
    assert main(["migrate", "--state", str(state), "--batch-size", "2"]) == 1
    out = json.loads(capsys.readouterr().out)
    assert out["policy_conflicts"] == 1

    assert main(["use-token", "tok-alpha-valid", "--state", str(state)]) == 0
    first = json.loads(capsys.readouterr().out)
    assert first["code"] == "OK"

    assert main(["use-token", "tok-alpha-valid", "--state", str(state)]) == 1
    second = json.loads(capsys.readouterr().out)
    assert second["code"] == "E_TOKEN_REUSED"

    assert main(["export-report", "--state", str(state), "--samples", str(sample_dir), "--output", str(report)]) == 1
    summary = json.loads(capsys.readouterr().out)
    assert summary["report_path"] == str(report)
    assert report.exists()
    assert not report.with_name(report.name + ".tmp").exists()

    data = json.loads(report.read_text(encoding="utf-8"))
    assert data["schema"] == "account-verify/diagnostic-report/v1"
    assert data["failure_closure"] is None
    assert len(data["users"]) == 11
    assert len(data["tokens"]) == 9
    assert main(["verify-checkpoint", "--state", str(state)]) == 0


def test_cli_policy_upgrade_report_contains_closure(sample_dir: Path, tmp_path: Path):
    state = tmp_path / "state"
    report = tmp_path / "report.json"
    assert main(["import", "--samples", str(sample_dir), "--state", str(state)]) == 0
    assert main(["migrate", "--state", str(state)]) == 1
    assert main([
        "migrate",
        "--state",
        str(state),
        "--upgrade-policy",
        str(sample_dir / "policy_upgrade_v3.json"),
    ]) == 0
    assert main([
        "export-report",
        "--state",
        str(state),
        "--samples",
        str(sample_dir),
        "--output",
        str(report),
    ]) == 1
    data = json.loads(report.read_text(encoding="utf-8"))
    assert data["failure_closure"]["trigger_source_policy_keys"] == [
        "password/default",
        "password/user/u-direct",
    ]


def test_missing_manifest_is_parameter_error(tmp_path: Path):
    missing = tmp_path / "missing-samples"
    state = tmp_path / "state"
    code = main(["import", "--samples", str(missing), "--state", str(state)])
    assert code == 2


def test_samples_are_unchanged_after_verification(sample_dir: Path, truth):
    result = verify_samples_against_truth(sample_dir)
    assert result["unchanged"] is True
    assert result["code"] == "OK"
    assert result["files_checked"] == len(truth["input_sha256"])


def test_generator_is_reproducible(tmp_path: Path):
    from account_verify.samplegen import generate_sample_tree

    first = tmp_path / "first"
    second = tmp_path / "second"
    hashes_a = generate_sample_tree(first)
    hashes_b = generate_sample_tree(second)
    assert hashes_a == hashes_b
    for relative, expected_hash in hashes_a.items():
        if relative.endswith(".sqlite"):
            continue
        assert (second / relative).read_bytes() == (first / relative).read_bytes()
        assert expected_hash
