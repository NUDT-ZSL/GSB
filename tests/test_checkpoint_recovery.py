from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import pytest

from walrec.checkpoint_io import write_v2_package
from walrec.engine import Engine
from walrec.errors import WalRecError
from walrec.sample import parse_sample

from conftest import ABNORMAL, NORMAL, json_stdout, run_cli


def _file_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_package_from_engine(
    engine: Engine,
    destination: Path,
    *,
    update_kind: str,
    clock_ms: int,
) -> None:
    pointer = engine.store.get_pointer()
    assert pointer is not None
    rows = engine.store.rows_snapshot()
    manifest_base = {
        "applied_watermark": pointer["applied_watermark"],
        "chain_hash": pointer["chain_hash"],
        "checkpoint_gen": pointer["checkpoint_gen"] + 1,
        "commit_ts_ms": clock_ms,
        "dirty_pages": {},
        "logical_clock_ms": clock_ms,
        "manifest_version": pointer["manifest_version"] + 1,
        "min_replay_generation": pointer["replay_generation"],
        "parent_checkpoint_gen": pointer["checkpoint_gen"],
        "replay_generation": pointer["replay_generation"] + 1,
        "update_kind": update_kind,
    }
    write_v2_package(destination, manifest_base, rows)


def test_checkpoint_replacement_delta_noop_and_rollback(tmp_path: Path) -> None:
    engine = Engine(tmp_path / "checkpoint.db")
    engine.load(NORMAL)
    engine.apply(NORMAL)
    created = engine.checkpoint(NORMAL, clock_ms=1700)
    assert created["created"]
    assert created["decision"] == "full_replace"
    current_gen = created["checkpoint"]["checkpoint_gen"]
    current_package = tmp_path / "checkpoints" / created["checkpoint"]["source"]
    assert (current_package / "manifest.json").is_file()
    assert (current_package / "meta.sqlite").is_file()
    manifest_digest = _file_digest(current_package / "manifest.json")

    pointer_before = engine.store.get_pointer()
    noop = engine.install_checkpoint(current_package)
    assert noop["decision"] == "noop"
    assert engine.store.get_pointer() == pointer_before

    rollback = ABNORMAL / "version_rollback" / "checkpoints" / "incoming" / "rollback"
    rollback_manifest_before = _file_digest(rollback / "manifest.json")
    with pytest.raises(WalRecError) as captured:
        engine.install_checkpoint(rollback)
    assert captured.value.code == "E_VERSION_ROLLBACK"
    assert engine.store.get_pointer() == pointer_before
    assert _file_digest(rollback / "manifest.json") == rollback_manifest_before
    assert _file_digest(current_package / "manifest.json") == manifest_digest

    delta_path = tmp_path / "incoming" / f"gen-{current_gen + 1:06d}"
    _write_package_from_engine(
        engine,
        delta_path,
        update_kind="delta",
        clock_ms=1800,
    )
    delta = engine.install_checkpoint(delta_path)
    assert delta["decision"] == "incremental_append"
    assert delta["checkpoint"]["checkpoint_gen"] == current_gen + 1
    assert _file_digest(current_package / "manifest.json") == manifest_digest

    full_path = tmp_path / "incoming" / f"gen-{current_gen + 2:06d}"
    _write_package_from_engine(
        engine,
        full_path,
        update_kind="full",
        clock_ms=1900,
    )
    full = engine.install_checkpoint(full_path)
    assert full["decision"] == "full_replace"
    assert full["checkpoint"]["checkpoint_gen"] == current_gen + 2
    assert _file_digest(current_package / "manifest.json") == manifest_digest

    with sqlite3.connect(tmp_path / "checkpoint.db") as connection:
        decisions = [
            row[0]
            for row in connection.execute(
                "SELECT decision FROM checkpoint_history ORDER BY id"
            ).fetchall()
        ]
    assert decisions[-2:] == ["incremental_append", "full_replace"]


def test_forced_termination_then_resume_does_not_duplicate_transactions(
    tmp_path: Path,
) -> None:
    crashed_db = tmp_path / "crashed.db"
    clean_db = tmp_path / "clean.db"

    run_cli(
        "load",
        "--sample",
        str(NORMAL),
        "--db",
        str(crashed_db),
        "--checkpoint-gen",
        "1",
        "--reset",
    )
    crash = run_cli(
        "apply",
        "--sample",
        str(NORMAL),
        "--db",
        str(crashed_db),
        env={
            "WALREC_FAILPOINT": "after_data_before_commit",
            "WALREC_FAIL_TXID": "3",
        },
        check=False,
    )
    assert crash.returncode == 91
    status_before = json_stdout(
        run_cli(
            "status",
            "--sample",
            str(NORMAL),
            "--db",
            str(crashed_db),
        )
    )["status"]
    assert status_before["applied_watermark"] == 2
    assert [item["txid"] for item in status_before["in_flight"]] == [3]

    resumed = json_stdout(
        run_cli(
            "resume",
            "--sample",
            str(NORMAL),
            "--db",
            str(crashed_db),
        )
    )
    assert resumed["ok"]
    assert resumed["recovery"]["in_flight_cleared"] == [3]
    assert resumed["status"]["applied_watermark"] == 17
    assert resumed["status"]["in_flight"] == []
    assert json_stdout(
        run_cli(
            "verify",
            "--sample",
            str(NORMAL),
            "--db",
            str(crashed_db),
        )
    )["ok"]

    engine = Engine(clean_db)
    engine.load(NORMAL, checkpoint_gen=1)
    engine.apply(NORMAL)
    assert Engine(crashed_db).compare(clean_db)["ok"]


def test_checkpoint_interrupted_after_package_write_is_reused(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "checkpoint-crash.db"
    run_cli(
        "load",
        "--sample",
        str(NORMAL),
        "--db",
        str(db_path),
        "--reset",
    )
    run_cli(
        "apply",
        "--sample",
        str(NORMAL),
        "--db",
        str(db_path),
        "--no-auto-checkpoint",
    )
    pointer_before = Engine(db_path).store.get_pointer()
    assert pointer_before is not None
    crashed = run_cli(
        "checkpoint",
        "--sample",
        str(NORMAL),
        "--db",
        str(db_path),
        "--clock-ms",
        "1700",
        env={"WALREC_FAILPOINT": "checkpoint_after_package"},
        check=False,
    )
    assert crashed.returncode == 91
    pointer_after_crash = Engine(db_path).store.get_pointer()
    assert pointer_after_crash == pointer_before

    retried = json_stdout(
        run_cli(
            "checkpoint",
            "--sample",
            str(NORMAL),
            "--db",
            str(db_path),
        )
    )
    assert retried["created"]
    assert retried["checkpoint"]["checkpoint_gen"] == pointer_before["checkpoint_gen"] + 1
    assert retried["checkpoint"]["commit_ts_ms"] == 1700
    assert json_stdout(
        run_cli(
            "verify",
            "--sample",
            str(NORMAL),
            "--db",
            str(db_path),
        )
    )["ok"]


def test_recovery_repairs_stale_watermark_and_half_applied_row(
    tmp_path: Path,
) -> None:
    stale_db = tmp_path / "stale.db"
    engine = Engine(stale_db)
    engine.load(NORMAL, checkpoint_gen=1)
    engine.apply(NORMAL, no_auto_checkpoint=True)
    with sqlite3.connect(stale_db) as connection:
        connection.execute(
            "UPDATE meta SET value = '1' WHERE key = 'applied_watermark'"
        )
        connection.commit()
    resumed = engine.resume(NORMAL, no_auto_checkpoint=True)
    assert resumed["recovery"]["watermark_before"] == 1
    assert resumed["recovery"]["watermark_after"] == 17
    assert resumed["applied"] == []
    assert engine.verify(NORMAL)["ok"]

    half_db = tmp_path / "half.db"
    half = Engine(half_db)
    half.load(NORMAL, checkpoint_gen=1)
    sample = parse_sample(NORMAL)
    with half.store.transaction() as connection:
        for record in sample.wal:
            if record.logical_tx_id <= 3:
                half.store.apply_record(
                    record,
                    applied_at_ms=1400 + record.logical_tx_id,
                    connection=connection,
                )
        half.store.set_meta(connection, "logical_clock_ms", "1403")
    with sqlite3.connect(half_db) as connection:
        connection.execute("DELETE FROM applied_txs WHERE txid = 3")
        connection.execute(
            "UPDATE meta SET value = '2' WHERE key = 'applied_watermark'"
        )
        connection.commit()
    repaired = half.resume(NORMAL, no_auto_checkpoint=True)
    assert repaired["recovery"]["repaired_half_applied_tx_ids"] == [3]
    assert repaired["status"]["applied_watermark"] == 17
    assert half.verify(NORMAL)["ok"]
    assert half.compare(stale_db)["ok"]
