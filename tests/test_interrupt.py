from __future__ import annotations

import json
import threading
from pathlib import Path

from voice_repair.engine import BatchEngine, RunOptions
from voice_repair.errors import EXIT_GRACEFUL_INTERRUPT
from voice_repair.ledger import Ledger
from voice_repair.samples import generate_demo_dataset
from voice_repair.task import load_task


def test_graceful_stop_commits_whole_blocks_and_writes_marker(
    tmp_path: Path,
    monkeypatch,
) -> None:
    task_path = generate_demo_dataset(tmp_path / "data", force=True)
    validation = load_task(task_path)
    assert validation.task is not None
    state = tmp_path / "state"
    stop_event = threading.Event()
    original_put_block = Ledger.put_block

    def put_block_then_stop(self, **kwargs):
        inserted = original_put_block(self, **kwargs)
        stop_event.set()
        return inserted

    monkeypatch.setattr(Ledger, "put_block", put_block_then_stop)
    engine = BatchEngine(
        task=validation.task,
        validation_issues=validation.issues,
        ledger_path=state / "ledger.sqlite",
        output_dir=state / "output",
        report_path=state / "report.json",
        state_dir=state,
        options=RunOptions(
            mode="interrupt-test",
            workers=3,
            stop_event=stop_event,
            signal_number=lambda: 2,
        ),
    )
    outcome = engine.run()
    assert outcome.exit_code == EXIT_GRACEFUL_INTERRUPT
    assert outcome.status == "interrupted"
    assert (state / "interrupted.json").is_file()
    marker = json.loads((state / "interrupted.json").read_text())
    assert marker["status"] == "interrupted"
    assert marker["signal_number"] == 2

    report = json.loads((state / "report.json").read_text())
    assert report["status"] == "interrupted"
    assert 0 < report["summary"]["completed_blocks"] < report["summary"]["total_blocks"]
    assert not list((state / "output").glob("*.wav"))

    ledger = Ledger(state / "ledger.sqlite")
    assert ledger.total_blocks() == report["summary"]["completed_blocks"]
    for recording in report["recordings"]:
        for stage in recording["stages"]:
            assert stage["completed_blocks"] == len(stage["block_sha256"])
    ledger.close()
