from __future__ import annotations

import json
import signal

from formbench.cli import _install_signal_handlers, main
from formbench.scheduler import StopController


def test_all_injected_faults_have_distinguishable_codes(task, tmp_path):
    from formbench.runner import RunOptions, Workbench

    with Workbench(
        task,
        options=RunOptions(state_dir=tmp_path / "state", output_dir=tmp_path / "output"),
    ) as bench:
        summary = bench.run()

    by_image = {error["image_id"]: error for error in summary["errors"]}
    assert set(by_image) == {"form-corrupt", "form-grid", "form-ledger", "form-vocab"}
    assert by_image["form-corrupt"]["reason_code"] == "E_CORRUPT_PIXELS"
    assert by_image["form-grid"]["reason_code"] == "E_GRID_NOT_DIVISIBLE"
    assert by_image["form-ledger"]["reason_code"] == "E_LEDGER_DIGEST"
    assert by_image["form-vocab"]["reason_code"] == "E_VOCAB_MISSING_INJECTED"
    assert summary["form_count"] == 3

    form_files = list((tmp_path / "output" / "forms").glob("*.json"))
    assert {path.stem for path in form_files} == {"form-001", "form-002", "form-003"}
    ledger_error_output = tmp_path / "output" / "forms" / "form-ledger.json"
    assert not ledger_error_output.exists()


def test_cli_generated_samples_and_partial_exit(generated_root, tmp_path):
    code = main(
        [
            "run",
            "--task",
            str(generated_root / "samples" / "task.json"),
            "--state-dir",
            str(tmp_path / "state"),
            "--output-dir",
            str(tmp_path / "output"),
        ]
    )
    assert code == 1
    results = json.loads((tmp_path / "output" / "results.json").read_text(encoding="utf-8"))
    assert results["status"] == "completed_with_errors"
    assert results["form_count"] == 3
    assert results["error_count"] == 4


def test_signal_handler_only_sets_boundary_stop_event():
    stop = StopController()
    previous = signal.getsignal(signal.SIGINT)
    try:
        _install_signal_handlers(stop)
        handler = signal.getsignal(signal.SIGINT)
        assert callable(handler)
        handler(signal.SIGINT, None)
        assert stop.stopped is True
        assert stop.signum == signal.SIGINT
    finally:
        signal.signal(signal.SIGINT, previous)
