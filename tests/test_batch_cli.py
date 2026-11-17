from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

import sensitive_guard.io_utils as io_utils
from sensitive_guard.batch import BatchOptions, load_batch, run_batch
from sensitive_guard.engine import analyze_dataset
from sensitive_guard.generator import generate
from sensitive_guard.io_utils import atomic_write_json

from conftest import make_dataset, make_record


ROOT = Path(__file__).resolve().parents[1]
PYTHON = sys.executable


def _run_cli(*args: str, cwd: Path = ROOT) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [PYTHON, "-m", "sensitive_guard", *args],
        cwd=cwd,
        text=True,
        capture_output=True,
        check=False,
    )


def test_concurrency_does_not_change_dataset_semantics(tmp_path: Path) -> None:
    sample_dir = tmp_path / "samples"
    generate(sample_dir, seed=99, clean=True)
    sources = load_batch(sample_dir / "batch.json")
    report_path = tmp_path / "report.json"

    serial = run_batch(
        sources,
        input_path=sample_dir / "batch.json",
        report_path=report_path,
        options=BatchOptions(concurrency=1, timeout_ms=2000, max_fields=100),
    )
    concurrent = run_batch(
        sources,
        input_path=sample_dir / "batch.json",
        report_path=report_path,
        options=BatchOptions(concurrency=4, timeout_ms=2000, max_fields=100),
    )

    assert serial["datasets"] == concurrent["datasets"]
    assert [item["dataset_id"] for item in serial["datasets"]] == [
        item.dataset["dataset_id"] for item in sources
    ]


def test_analyze_does_not_modify_dataset_or_source_file(tmp_path: Path) -> None:
    sample_dir = tmp_path / "samples"
    generate(sample_dir, seed=7, clean=True)
    input_path = sample_dir / "datasets" / "order_independence.json"
    before_bytes = input_path.read_bytes()
    before_hash = hashlib.sha256(before_bytes).hexdigest()
    dataset = json.loads(before_bytes)
    snapshot = json.dumps(dataset, sort_keys=True)

    analyze_dataset(dataset)

    assert json.dumps(dataset, sort_keys=True) == snapshot
    assert input_path.read_bytes() == before_bytes
    assert hashlib.sha256(input_path.read_bytes()).hexdigest() == before_hash


def test_cli_exit_zero_for_clean_dataset(tmp_path: Path) -> None:
    sample_dir = tmp_path / "samples"
    generate(sample_dir, seed=7, clean=True)
    report_path = tmp_path / "report.json"

    completed = _run_cli(
        "validate",
        "--input",
        str(sample_dir / "batch.json"),
        "--dataset-id",
        "clean",
        "--report",
        str(report_path),
        "--concurrency",
        "2",
    )

    assert completed.returncode == 0, completed.stderr
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    assert payload["summary"]["all_success"] is True
    assert [item["dataset_id"] for item in payload["datasets"]] == ["clean"]
    assert payload["datasets"][0]["diagnostics"]["commands"]
    assert not list(report_path.parent.glob("*.tmp"))


def test_cli_partial_failure_exit_and_business_codes(tmp_path: Path) -> None:
    sample_dir = tmp_path / "samples"
    generate(sample_dir, seed=7, clean=True)
    report_path = tmp_path / "report.json"

    completed = _run_cli(
        "validate",
        "--input",
        str(sample_dir / "batch.json"),
        "--report",
        str(report_path),
        "--concurrency",
        "4",
        "--timeout-ms",
        "100",
        "--max-fields",
        "30",
    )

    assert completed.returncode == 1
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    assert payload["summary"]["all_success"] is False
    codes = payload["summary"]["code_counts"]
    assert "E_TIMEOUT" in codes
    assert "E_FIELDS" in codes
    assert "E_CYCLE" in codes
    assert "E_VERSION_CONFLICT" in codes
    assert "E_DOWNGRADE" in codes
    assert "E_LEAK" in codes
    ids = [item["dataset_id"] for item in payload["datasets"]]
    assert ids == [
        "clean",
        "order_independence",
        "cycle_injection",
        "version_mix",
        "downgrade_leak",
        "deep_chain",
        "timeout_injection",
        "fields_limit",
        "self_cycle",
    ]
    assert payload["summary"]["observation_inconsistent"] == 0
    assert all(item["diagnostics"]["commands"] for item in payload["datasets"])


def test_cli_parameter_error_exit_two(tmp_path: Path) -> None:
    sample_dir = tmp_path / "samples"
    generate(sample_dir, seed=7, clean=True)
    completed = _run_cli(
        "validate",
        "--input",
        str(sample_dir / "batch.json"),
        "--report",
        str(tmp_path / "report.json"),
        "--concurrency",
        "0",
    )
    assert completed.returncode == 2


def test_one_malformed_dataset_does_not_abort_batch(tmp_path: Path) -> None:
    clean = make_dataset(
        [make_record("ok.root", [], "masked")],
        dataset_id="good",
    )
    malformed = {"dataset_id": "bad", "records": "not-a-list", "injection": {}}
    batch_path = tmp_path / "batch.json"
    batch_path.write_text(
        json.dumps({"datasets": [malformed, clean]}),
        encoding="utf-8",
    )
    report_path = tmp_path / "report.json"

    completed = _run_cli(
        "validate",
        "--input",
        str(batch_path),
        "--report",
        str(report_path),
    )

    assert completed.returncode == 1
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    assert [item["dataset_id"] for item in payload["datasets"]] == ["bad", "good"]
    assert payload["datasets"][0]["codes"] == ["E_RECORD"]
    assert payload["datasets"][1]["status"] == "ok"


def test_atomic_write_keeps_old_report_on_replace_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "report.json"
    target.write_text('{"old": true}\n', encoding="utf-8")

    def fail_replace(source: Path, destination: Path) -> None:
        raise OSError("replace failed")

    monkeypatch.setattr(io_utils.os, "replace", fail_replace)
    with pytest.raises(OSError):
        atomic_write_json(target, {"new": True})

    assert json.loads(target.read_text(encoding="utf-8")) == {"old": True}
    assert not list(tmp_path.glob("*.tmp"))


def test_cli_concurrency_report_order_is_stable(tmp_path: Path) -> None:
    sample_dir = tmp_path / "samples"
    generate(sample_dir, seed=7, clean=True)
    report_one = tmp_path / "one.json"
    report_four = tmp_path / "four.json"

    first = _run_cli(
        "validate",
        "--input",
        str(sample_dir / "batch.json"),
        "--report",
        str(report_one),
        "--concurrency",
        "1",
        "--timeout-ms",
        "2000",
        "--max-fields",
        "100",
    )
    second = _run_cli(
        "validate",
        "--input",
        str(sample_dir / "batch.json"),
        "--report",
        str(report_four),
        "--concurrency",
        "4",
        "--timeout-ms",
        "2000",
        "--max-fields",
        "100",
    )
    assert first.returncode == 1
    assert second.returncode == 1

    payload_one = json.loads(report_one.read_text(encoding="utf-8"))
    payload_four = json.loads(report_four.read_text(encoding="utf-8"))

    def projection(payload: dict) -> list[dict]:
        return [
            {
                "dataset_id": item["dataset_id"],
                "status": item["status"],
                "codes": item["codes"],
                "cycles": item["cycles"],
                "violations": item["violations"],
                "fields": item["fields"],
                "observations": item["observations"],
            }
            for item in payload["datasets"]
        ]

    assert projection(payload_one) == projection(payload_four)
