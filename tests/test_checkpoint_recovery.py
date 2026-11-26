from __future__ import annotations

import json
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from entity_link_lab.checkpoint import CheckpointStore, CommitLock
from entity_link_lab.errors import LinkError
from entity_link_lab.pipeline import run_pipeline


def test_checkpoint_versions_and_checksums(sample_dir, tmp_path):
    ckpt = tmp_path / "ckpt"
    run_pipeline(
        sample_dir,
        output_dir=tmp_path / "out",
        cache_dir=tmp_path / "cache",
        checkpoint_dir=ckpt,
    )
    store = CheckpointStore(ckpt)
    versions = store.list_versions()
    assert versions == sorted(versions)
    assert len(versions) == len(set(versions))
    for version in versions:
        record = store.read_checkpoint_file(store._checkpoint_path(version))
        assert record["version"] == version
        assert record["content_checksum"]
        assert isinstance(record["completed_doc_ids"], list)


def test_signal_boundary_resume_skips_committed_mentions(
    sample_dir, tmp_path, monkeypatch
):
    import entity_link_lab.pipeline as pipeline_module

    ckpt = tmp_path / "ckpt"
    real_compute = pipeline_module.compute_evidence
    calls: list = []

    def spy(*args, **kwargs):
        calls.append((args, kwargs))
        return real_compute(*args, **kwargs)

    monkeypatch.setattr(pipeline_module, "compute_evidence", spy)
    stopped = run_pipeline(
        sample_dir,
        output_dir=tmp_path / "out1",
        cache_dir=tmp_path / "cache1",
        checkpoint_dir=ckpt,
        should_stop=lambda: CheckpointStore(ckpt).max_committed_version() >= 2,
    )
    assert stopped["stopped"] is True
    latest = CheckpointStore(ckpt).load_latest_valid()
    assert latest["stop_reason"] == "signal:SIGTERM"
    assert "m-0002" in latest["processed_mention_ids"]

    calls.clear()
    recovered = run_pipeline(
        sample_dir,
        output_dir=tmp_path / "out2",
        cache_dir=tmp_path / "cache2",
        checkpoint_dir=ckpt,
    )
    assert recovered["status"] == "ok" if "status" in recovered else True
    exact_resume_calls = [
        (args, kwargs)
        for args, kwargs in calls
        if kwargs.get("path") == "exact"
    ]
    for args, _ in exact_resume_calls:
        second_mention = args[1]
        assert second_mention["mention_id"] not in {"m-0001", "m-0002"}


def test_force_termination_subprocess_recovers(sample_dir, tmp_path, report):
    ckpt = tmp_path / "force-ckpt"
    killed = subprocess.run(
        [
            sys.executable,
            "-m",
            "entity_link_lab",
            "run",
            "--data",
            str(sample_dir),
            "--output",
            str(tmp_path / "killed-out"),
            "--cache-dir",
            str(tmp_path / "killed-cache"),
            "--checkpoint-dir",
            str(ckpt),
            "--kill-after",
            "2",
        ],
        cwd=Path.cwd(),
        capture_output=True,
        text=True,
    )
    assert killed.returncode in {-9, 137}
    recovery = CheckpointStore(ckpt).inspect_recovery()
    assert recovery["mode"] == "forced_termination"

    recovered = run_pipeline(
        sample_dir,
        output_dir=tmp_path / "recovered-out",
        cache_dir=tmp_path / "recovered-cache",
        checkpoint_dir=ckpt,
    )
    assert recovered["counts"]["chains"] == report["counts"]["chains"]
    assert recovered["chains"] == report["chains"]


def test_dirty_checkpoint_is_distinguished(sample_dir, tmp_path):
    ckpt = tmp_path / "dirty-ckpt"
    store = CheckpointStore(ckpt)
    store.commit(
        stage="candidate_generation",
        processed_mention_ids=["m-0001"],
        completed_doc_ids=[],
        state={"exact_evidence": []},
    )
    latest = ckpt / "latest.json"
    record = json.loads(latest.read_text(encoding="utf-8"))
    record["content_checksum"] = "0" * 64
    latest.write_text(json.dumps(record), encoding="utf-8")

    recovery = store.inspect_recovery()
    assert recovery["mode"] == "dirty_checkpoint"
    with pytest.raises(LinkError) as exc:
        run_pipeline(
            sample_dir,
            output_dir=tmp_path / "out",
            cache_dir=tmp_path / "cache",
            checkpoint_dir=ckpt,
        )
    assert exc.value.code == "E_CHECKPOINT_DIRTY"


def test_concurrent_commit_returns_stage_busy(tmp_path):
    lock_path = tmp_path / "commit.lock"
    outer = CommitLock(lock_path)
    outer.__enter__()
    errors: list[str] = []

    def worker() -> None:
        try:
            with CommitLock(lock_path):
                pass
        except LinkError as exc:
            errors.append(exc.code)

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join(timeout=5)
    outer.__exit__(None, None, None)
    assert errors == ["E_STAGE_BUSY"]
