"""Checkpoint recovery boundary: half-write detection, forced-kill resume,
snapshot version drift."""

import json
import os
import sqlite3
import subprocess
import sys

from memguard import codes, store
from memguard.checkpoint import snapshot_path
from memguard.engine import db_path, import_batch, verify_checkpoint
from memguard.report import build_report

from conftest import import_all


def test_half_write_residue_detected_and_rebuilt(sample_dir, decl_file,
                                                 batch_files, tmp_path):
    workdir = str(tmp_path / "w")
    import_all(workdir, [decl_file], batch_files)
    expected = build_report(workdir)

    # simulate half-written residue: corrupt the ledger behind the checkpoint
    conn = sqlite3.connect(db_path(workdir))
    conn.execute("UPDATE ledger SET oob_count = oob_count + 1")
    conn.commit()
    conn.close()

    result = verify_checkpoint(workdir)
    assert not result["ok"]
    assert result["code"] == codes.E_CHECKPOINT
    assert any("digest mismatch" in a["detail"] for a in result["anomalies"])

    # after recovery the rebuilt ledger equals the pre-corruption state
    recovered = build_report(workdir)
    assert recovered["regions"] == expected["regions"]
    assert verify_checkpoint(workdir)["ok"]


def test_forced_kill_resumes_from_last_checkpoint(sample_dir, decl_file,
                                                  batch_files, tmp_path):
    workdir = str(tmp_path / "w")
    import_batch(workdir, [decl_file], [])
    import_batch(workdir, [], [batch_files[0]])

    # forced kill after the DB commit, before the snapshot replacement
    env = dict(os.environ, MEMGUARD_CRASH_AFTER_COMMIT="1")
    proc = subprocess.run(
        [sys.executable, "-m", "memguard", "import", "--workdir", workdir,
         "--traces", batch_files[1]],
        env=env, capture_output=True, text=True)
    assert proc.returncode == 86          # killed: no exit report promised

    # recovery continues from the last complete checkpoint; nothing lost
    result = verify_checkpoint(workdir)
    assert result["ok"], result
    assert result["batch_seq"] == 3

    expected_dir = str(tmp_path / "w_expected")
    import_all(expected_dir, [decl_file], batch_files)
    assert build_report(workdir)["regions"] == build_report(expected_dir)["regions"]


def test_snapshot_version_drift_flagged(sample_dir, decl_file, batch_files, tmp_path):
    workdir = str(tmp_path / "w")
    import_all(workdir, [decl_file], batch_files)

    with open(snapshot_path(workdir), "r", encoding="utf-8") as fh:
        snap = json.load(fh)
    snap["snapshot_version"] = 999
    with open(snapshot_path(workdir), "w", encoding="utf-8") as fh:
        json.dump(snap, fh)

    result = verify_checkpoint(workdir)
    assert not result["ok"]
    assert any(a["code"] == codes.E_VERSION for a in result["anomalies"])


def test_ledger_layout_version_change_forces_rebuild(tmp_path):
    workdir = str(tmp_path / "w")
    os.makedirs(workdir, exist_ok=True)
    conn = store.connect(db_path(workdir))
    conn.execute("INSERT INTO traces(trace_id, access_seq, region_id, base_offset,"
                 " access_size, access_kind, arena_size, ts_ms, file_id, line_no,"
                 " batch_seq) VALUES ('x', 0, 'R', 0, 1, 'read', 8, 0, 'f', 1, 1)")
    conn.commit()
    conn.execute("UPDATE meta SET value='0' WHERE key='ledger_layout_version'")
    conn.commit()
    conn.close()

    try:
        store.connect(db_path(workdir))
        raised = False
    except store.LayoutMismatch:
        raised = True
    assert raised
    # rebuilt empty ledger
    conn = store.connect(db_path(workdir))
    assert conn.execute("SELECT COUNT(*) FROM traces").fetchone()[0] == 0
    conn.close()
