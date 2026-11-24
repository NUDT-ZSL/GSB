from __future__ import annotations

import pytest

from alert_engine.engine import InputPaths
from alert_engine.errors import EngineError
from alert_engine.ledger import RuleLedger
from alert_engine.models import PatchBatch, RulePatch
from alert_engine.util import FileLock


def _new_ledger(tmp_path, parsed):
    ledger = RuleLedger(tmp_path / "ledger.sqlite3")
    ledger.reset()
    ledger.seed_baseline(parsed.v1_versions + parsed.v2_versions)
    ledger.set_meta("observation_clock_ms", parsed.observation_clock_ms)
    return ledger


def _counts(ledger):
    batches = ledger.connection.execute(
        "SELECT COUNT(*) AS value FROM patch_batches"
    ).fetchone()["value"]
    rules = ledger.connection.execute(
        "SELECT COUNT(*) AS value FROM patch_rules"
    ).fetchone()["value"]
    return batches, rules


def test_patch_batch_is_atomic_and_rolls_back_on_chain_failure(
    sample_dir, tmp_path
):
    parsed = InputPaths.from_directory(sample_dir).load()
    ledger = _new_ledger(tmp_path, parsed)
    try:
        first = parsed.patch_batches[0]
        ledger.apply_patch_batch(first)
        assert _counts(ledger) == (1, 1)

        invalid = PatchBatch(
            batch_id="P-BAD",
            logical_ts_ms=999,
            rules=(
                RulePatch(
                    rule_key="R-NEW",
                    source_rule_key="R-NEW",
                    effective_ms=parsed.observation_clock_ms,
                    generation=2,
                    dedup_window_ms=1_000,
                    match_strings=("new",),
                    enabled=True,
                    tombstone=False,
                    parent_rule_keys=(),
                    record_index=1,
                ),
                RulePatch(
                    rule_key="R-PATCH",
                    source_rule_key="R-PATCH",
                    effective_ms=parsed.observation_clock_ms + 26_000,
                    generation=1,
                    dedup_window_ms=1_000,
                    match_strings=("patch",),
                    enabled=True,
                    tombstone=False,
                    parent_rule_keys=(),
                    record_index=2,
                ),
            ),
            record_index=99,
            arrival_index=1,
        )
        with pytest.raises(EngineError) as exc_info:
            ledger.apply_patch_batch(invalid)
        assert exc_info.value.code == "E_PATCH_CHAIN"
        assert _counts(ledger) == (1, 1)

        with pytest.raises(EngineError) as exc_info:
            ledger.apply_patch_batch(first)
        assert exc_info.value.code == "E_PATCH_DUP"
        assert _counts(ledger) == (1, 1)
    finally:
        ledger.close()


def test_patch_payload_audit_is_persisted(sample_dir, tmp_path):
    parsed = InputPaths.from_directory(sample_dir).load()
    ledger = _new_ledger(tmp_path, parsed)
    try:
        for batch in parsed.patch_batches:
            ledger.apply_patch_batch(batch)
        applied = ledger.applied_batches()
        assert [item.batch_id for item in applied] == [
            "P200",
            "P100",
            "P300",
            "P150",
        ]
        assert ledger.applied_watermark_ms() == 300
    finally:
        ledger.close()


def test_ledger_write_lock_conflict_is_e_lock(sample_dir, tmp_path):
    parsed = InputPaths.from_directory(sample_dir).load()
    ledger = _new_ledger(tmp_path, parsed)
    try:
        with FileLock(ledger.lock_path):
            with pytest.raises(EngineError) as exc_info:
                ledger.apply_patch_batch(parsed.patch_batches[0])
        assert exc_info.value.code == "E_LOCK"
        assert _counts(ledger) == (0, 0)
    finally:
        ledger.close()
