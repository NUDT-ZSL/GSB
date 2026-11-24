from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

from supply_chain_audit.checkpoint import CheckpointManager
from supply_chain_audit.codes import E_CHECKPOINT_VERSION
from supply_chain_audit.io_utils import digest_value
from supply_chain_audit.service import build_seed_engine

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_forced_exit_recovers_last_complete_checkpoint(sample_paths, tmp_path):
    workspace = tmp_path / "checkpoint-ws"
    engine = build_seed_engine(sample_paths["root"])
    manager = CheckpointManager(workspace)
    saved = manager.save_checkpoint(engine.view, engine.applied_batches)
    assert saved["status"] == "complete"

    child_code = """
import os, sys
from supply_chain_audit.checkpoint import CheckpointManager
from supply_chain_audit.io_utils import digest_value
workspace = sys.argv[1]
manager = CheckpointManager(workspace)
policy = {
    "checkpoint_schema_version": 1,
    "algorithm_whitelist_version": "algo-whitelist-1",
    "merge_policy_version": "merge-policy-1",
}
view = {"schema": "crash-fake", "policy_versions": policy, "artifacts": {}, "stats": {"watermark": 3}}
envelope = {
    "checkpoint_id": "cp-000003-crashorphan",
    "checkpoint_schema_version": 1,
    "policy_versions": policy,
    "watermark": 3,
    "applied_batches": [],
    "view_digest": digest_value(view),
    "view": view,
}
manager.write_snapshot_file(envelope)
(manager.snapshot_dir / "checkpoint-000003.partial.json.tmp").write_text("{partial", encoding="utf-8")
os._exit(137)
"""
    env = dict(os.environ)
    env["PYTHONPATH"] = str(PROJECT_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    completed = subprocess.run(
        [sys.executable, "-c", child_code, str(workspace)],
        cwd=PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    assert completed.returncode != 0

    orphan_files = list((workspace / "checkpoints").glob("checkpoint-000003-*.json"))
    partial_files = list((workspace / "checkpoints").glob("*.tmp"))
    assert orphan_files
    assert partial_files

    verification = manager.verify_latest()
    assert verification["status"] == "valid"
    assert verification["watermark"] == 2
    recovered_view = verification["view"]
    assert recovered_view["stats"]["watermark"] == 2
    assert digest_value(recovered_view) == saved["view_digest"]


def test_policy_version_change_forces_full_rebuild(sample_paths, tmp_path):
    workspace = tmp_path / "version-ws"
    manager = CheckpointManager(workspace)
    engine = build_seed_engine(sample_paths["root"])
    manager.save_checkpoint(engine.view, engine.applied_batches)

    with sqlite3.connect(manager.db_path) as connection:
        connection.execute(
            "UPDATE checkpoints SET algorithm_whitelist_version = ?",
            ("algo-whitelist-old",),
        )
        connection.commit()

    verification = manager.verify_latest()
    assert verification["code"] == E_CHECKPOINT_VERSION
    assert verification["rebuild_required"] is True

    rebuilt_engine = build_seed_engine(sample_paths["root"])
    recovery = manager.recover_or_rebuild(lambda: rebuilt_engine.view)
    assert recovery["rebuilt"] is True
    assert recovery["recovered"] is False
    latest = manager.verify_latest()
    assert latest["status"] == "valid"
    assert latest["policy_versions"]["algorithm_whitelist_version"] == "algo-whitelist-1"


def test_corrupt_digest_is_detected(sample_paths, tmp_path):
    workspace = tmp_path / "corrupt-ws"
    engine = build_seed_engine(sample_paths["root"])
    manager = CheckpointManager(workspace)
    saved = manager.save_checkpoint(engine.view, engine.applied_batches)
    snapshot_path = Path(saved["snapshot_file"])
    envelope = json.loads(snapshot_path.read_text(encoding="utf-8"))
    envelope["view"]["stats"]["artifact_count"] = 999
    snapshot_path.write_text(json.dumps(envelope), encoding="utf-8")

    verification = manager.verify_latest()
    assert verification["status"] == "corrupt"
    assert verification["rebuild_required"] is True
