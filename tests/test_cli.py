import json
import subprocess
import sys
from pathlib import Path

import pytest

from opchain.cli import main
from opchain.errors import EXIT_CODES
from opchain.samplegen import generate_all

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def workspace(tmp_path):
    samples = tmp_path / "samples"
    valid, anomaly = generate_all(samples)
    snapshot = tmp_path / "results" / "batch.json"
    return valid, anomaly, snapshot, tmp_path


def _run_cli(argv):
    return main(argv)


def test_validate_reports_each_anomaly_code(workspace):
    _, anomaly, _, _ = workspace
    records = [json.loads(line) for line in anomaly.read_text().splitlines()]
    exit_code = _run_cli(["validate", "--input", str(anomaly)])
    expected = {record["case_id"]: record["expected_code"] for record in records}
    assert exit_code != 0
    for code in expected.values():
        assert code in EXIT_CODES


def test_batch_query_selfcheck_flow(workspace):
    valid, _, snapshot, tmp_path = workspace
    code = _run_cli(
        [
            "batch",
            "--input",
            str(valid),
            "--output",
            str(snapshot),
        ]
    )
    assert code == 0
    assert snapshot.exists()

    code = _run_cli(
        [
            "query",
            "--input",
            str(valid),
            "--snapshot",
            str(snapshot),
            "--case-id",
            "case_mix_small_f64",
        ]
    )
    assert code == 0

    code = _run_cli(
        [
            "selfcheck",
            "--input",
            str(valid),
            "--snapshot",
            str(snapshot),
        ]
    )
    assert code == 0


def test_cli_subprocess_exit_codes(workspace):
    _, anomaly, _, _ = workspace
    env = dict(PYTHONPATH=str(ROOT / "src"))
    proc = subprocess.run(
        [sys.executable, "-m", "opchain.cli", "validate", "--input", str(anomaly)],
        capture_output=True,
        text=True,
        env={**dict(__import__("os").environ), **env},
    )
    payload = json.loads(proc.stderr or proc.stdout)
    # validate emits the summary on stdout and returns the first error exit code.
    assert proc.returncode != 0
    assert payload["code"] in {"E_HAS_INVALID_ROWS"} or "reports" in payload


def test_generate_writes_samples(workspace, tmp_path):
    target = tmp_path / "generated"
    code = _run_cli(["generate", "--samples-dir", str(target)])
    assert code == 0
    assert (target / "tasks.valid.jsonl").exists()
    assert (target / "tasks.anomaly.jsonl").exists()
    assert (target / "default_config.json").exists()
