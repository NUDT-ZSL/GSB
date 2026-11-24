from __future__ import annotations

import hashlib
from pathlib import Path

import numpy as np
import pytest

from voice_repair.errors import LedgerError
from voice_repair.ledger import Ledger


def test_ledger_transaction_and_digest_conflict_protection(tmp_path: Path) -> None:
    path = tmp_path / "ledger.sqlite"
    values = np.linspace(-0.3, 0.4, 32, dtype=np.float64)
    with Ledger(path) as ledger:
        ledger.initialize_task("task-a", {"rec": "input-a"})
        run = ledger.begin_run(mode="test", workers=1, parallelism=1)
        assert ledger.put_block(
            recording_id="rec",
            stage=0,
            block_index=0,
            start_sample=0,
            end_sample=32,
            values=values,
            logical_cost_ns=123,
            run_id=run.run_id,
        )
        assert not ledger.put_block(
            recording_id="rec",
            stage=0,
            block_index=0,
            start_sample=0,
            end_sample=32,
            values=values,
            logical_cost_ns=123,
            run_id=run.run_id,
        )
        with pytest.raises(LedgerError, match="different content"):
            ledger.put_block(
                recording_id="rec",
                stage=0,
                block_index=0,
                start_sample=0,
                end_sample=32,
                values=values + 0.01,
                logical_cost_ns=123,
                run_id=run.run_id,
            )
        fetched = ledger.fetch_block("rec", 0, 0)
        assert np.array_equal(fetched, values)
        record = ledger.block_record("rec", 0, 0)
        expected_digest = hashlib.sha256(
            np.ascontiguousarray(values, dtype="<f8").tobytes()
        ).hexdigest()
        assert record.content_sha256 == expected_digest
        ledger.finish_run(run.run_id, "completed")


def test_ledger_rejects_task_or_input_mismatch(tmp_path: Path) -> None:
    path = tmp_path / "ledger.sqlite"
    with Ledger(path) as ledger:
        ledger.initialize_task("task-a", {"rec": "input-a"})
    with Ledger(path) as ledger:
        with pytest.raises(LedgerError) as exc_info:
            ledger.initialize_task("task-a", {"rec": "input-b"})
        assert exc_info.value.code == "E_LEDGER_INPUT_MISMATCH"
