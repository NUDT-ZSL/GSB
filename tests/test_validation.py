from __future__ import annotations

from pathlib import Path
import shutil

import pytest

from walrec.engine import Engine
from walrec.errors import WalRecError
from walrec.sample import parse_sample

from conftest import ABNORMAL, NORMAL, run_cli


@pytest.mark.parametrize(
    ("variant", "code"),
    [
        ("bad_wal", "E_BAD_WAL"),
        ("chain_break", "E_CHAIN_BREAK"),
        ("summary_mismatch", "E_SUMMARY_MISMATCH"),
        ("checkpoint_missing", "E_CHECKPOINT_MISSING"),
        ("replay_generation", "E_REPLAY_GENERATION"),
    ],
)
def test_invalid_samples_return_distinct_codes_without_state(
    variant: str,
    code: str,
    tmp_path: Path,
) -> None:
    db_path = tmp_path / f"{variant}.db"
    engine = Engine(db_path)
    with pytest.raises(WalRecError) as captured:
        engine.load(ABNORMAL / variant)
    assert captured.value.code == code
    assert not db_path.exists()
    assert not Path(f"{db_path}-wal").exists()


def test_v1_conversion_metadata_is_recorded() -> None:
    sample = parse_sample(NORMAL)
    checkpoint = sample.checkpoints.by_gen[1]
    assert checkpoint.v1_conversion is not None
    assert checkpoint.v1_conversion.scale == "1.5"
    assert checkpoint.v1_conversion.rounding == "half_up"
    assert checkpoint.v1_conversion.absolute_ms == 1375


def test_invalid_replay_does_not_mutate_loaded_state(tmp_path: Path) -> None:
    engine = Engine(tmp_path / "state.db")
    engine.load(NORMAL, checkpoint_gen=1)
    before = engine.snapshot()
    with pytest.raises(WalRecError) as captured:
        engine.apply(ABNORMAL / "summary_mismatch")
    assert captured.value.code == "E_SUMMARY_MISMATCH"
    assert engine.snapshot() == before


def test_cli_invalid_load_writes_only_business_error(tmp_path: Path) -> None:
    db_path = tmp_path / "cli-invalid.db"
    result = run_cli(
        "load",
        "--sample",
        str(ABNORMAL / "bad_wal"),
        "--db",
        str(db_path),
        check=False,
    )
    assert result.returncode == 2
    assert '"code": "E_BAD_WAL"' in result.stdout
    assert not db_path.exists()


def test_v2_shard_hash_tamper_is_summary_mismatch(tmp_path: Path) -> None:
    sample_copy = tmp_path / "tampered-v2"
    shutil.copytree(NORMAL, sample_copy)
    shard = next((sample_copy / "checkpoints" / "v2").glob("*/shards/*.json"))
    shard.write_text(shard.read_text(encoding="utf-8").replace('"balance"', '"balance" '), encoding="utf-8")
    shard.write_bytes(shard.read_bytes() + b" ")
    db_path = tmp_path / "tampered.db"
    engine = Engine(db_path)
    with pytest.raises(WalRecError) as captured:
        engine.load(sample_copy)
    assert captured.value.code == "E_SUMMARY_MISMATCH"
    assert not db_path.exists()
