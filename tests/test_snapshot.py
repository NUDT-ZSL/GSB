from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from linear_path_engine.errors import (
    E_SNAPSHOT_CORRUPT,
    E_SNAPSHOT_ROLLBACK,
    E_STAGE_BUSY,
    EngineError,
)
from linear_path_engine.snapshot import SnapshotStore, StageCommitGuard

from .helpers import cli_env


def test_snapshot_versions_are_monotonic_and_rollback_is_rejected(tmp_path: Path) -> None:
    store = SnapshotStore(tmp_path / "snap", "run-snapshot")
    first = store.commit(
        payload={"value": 1},
        stage="standardization",
        stage_detail="features",
        expected_version=0,
        expected_checksum=None,
    )
    second = store.commit(
        payload={"value": 2},
        stage="path_solve",
        stage_detail="strength-00000001",
        expected_version=first.version,
        expected_checksum=first.checksum,
    )
    assert (first.version, second.version) == (1, 2)
    assert first.path.exists()
    assert store.load_current().payload == {"value": 2}

    with pytest.raises(EngineError) as stale:
        store.commit(
            payload={"value": 99},
            stage="path_solve",
            stage_detail="stale",
            expected_version=0,
            expected_checksum=None,
        )
    assert stale.value.code == E_SNAPSHOT_ROLLBACK

    with pytest.raises(EngineError) as checksum_changed:
        store.commit(
            payload={"value": 99},
            stage="path_solve",
            stage_detail="stale-checksum",
            expected_version=second.version,
            expected_checksum="0" * 64,
        )
    assert checksum_changed.value.code == E_SNAPSHOT_ROLLBACK

    with pytest.raises(EngineError) as rollback:
        store.request_rollback(1)
    assert rollback.value.code == E_SNAPSHOT_ROLLBACK
    assert store.load_current().version == 2


def test_snapshot_corruption_reports_stage_and_version(tmp_path: Path) -> None:
    store = SnapshotStore(tmp_path / "snap", "run-corrupt")
    first = store.commit(
        payload={"value": 1},
        stage="standardization",
        stage_detail="features",
        expected_version=0,
        expected_checksum=None,
    )
    second = store.commit(
        payload={"value": 2, "path": [{"objective": 3.0}]},
        stage="path_summary",
        stage_detail="complete",
        expected_version=first.version,
        expected_checksum=first.checksum,
    )
    envelope = json.loads(second.path.read_text(encoding="utf-8"))
    envelope["payload"]["path"][0]["objective"] = 4.0
    second.path.write_text(
        json.dumps(envelope, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(EngineError) as raised:
        store.load_current()
    error = raised.value
    assert error.code == E_SNAPSHOT_CORRUPT
    assert error.stage == "path_summary"
    assert error.version == 2
    assert "expected_payload_checksum" in error.details
    assert first.path.exists()


def test_concurrent_stage_commit_returns_busy_immediately(tmp_path: Path) -> None:
    directory = tmp_path / "snap"
    directory.mkdir()
    holder_script = tmp_path / "holder.py"
    holder_script.write_text(
        "\n".join(
            [
                "import time",
                "from pathlib import Path",
                "from linear_path_engine.snapshot import StageCommitGuard",
                f"with StageCommitGuard(Path({str(directory)!r})):",
                "    print('READY', flush=True)",
                "    time.sleep(2.0)",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    holder = subprocess.Popen(
        [sys.executable, str(holder_script)],
        cwd=tmp_path,
        env=cli_env(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout is not None
        assert holder.stdout.readline().strip() == "READY"
        started = time.monotonic()
        with pytest.raises(EngineError) as raised:
            with StageCommitGuard(directory):
                pass
        elapsed = time.monotonic() - started
        assert raised.value.code == E_STAGE_BUSY
        assert elapsed < 0.5
    finally:
        holder.terminate()
        holder.wait(timeout=5)


def test_whole_snapshot_replacement_preserves_old_file_but_not_authority(
    tmp_path: Path,
) -> None:
    store = SnapshotStore(tmp_path / "snap", "run-replace")
    first = store.commit(
        payload={"items": ["a"]},
        stage="standardization",
        stage_detail="features",
        expected_version=0,
        expected_checksum=None,
    )
    second = store.commit(
        payload={"items": ["a", "b"]},
        stage="path_solve",
        stage_detail="strength-00000001",
        expected_version=first.version,
        expected_checksum=first.checksum,
    )
    third = store.commit(
        payload={"items": ["a", "b", "c"]},
        stage="path_validation",
        stage_detail="all-strengths",
        expected_version=second.version,
        expected_checksum=second.checksum,
    )
    current = store.load_current()
    assert current is not None
    assert current.version == third.version == 3
    assert current.payload == {"items": ["a", "b", "c"]}
    assert first.path.exists() and second.path.exists()
    manifest = json.loads((tmp_path / "snap" / "manifest.json").read_text())
    assert manifest["replacement_mode"] == "whole"
    assert manifest["previous_version"] == 2
    assert manifest["snapshot_file"] == third.path.name


def test_corrupt_manifest_uses_snapshot_file_metadata(tmp_path: Path) -> None:
    store = SnapshotStore(tmp_path / "snap", "run-manifest-corrupt")
    committed = store.commit(
        payload={"value": 1},
        stage="standardization",
        stage_detail="features",
        expected_version=0,
        expected_checksum=None,
    )
    store.manifest_path.write_text("{not-json", encoding="utf-8")
    with pytest.raises(EngineError) as raised:
        store.load_current()
    assert raised.value.code == E_SNAPSHOT_CORRUPT
    assert raised.value.stage == "standardization"
    assert raised.value.version == 1
    assert committed.path.exists()

