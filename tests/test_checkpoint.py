from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from authdiag.checkpoint import WorkdirLock
from authdiag.engine import EventDocument, Workbench


def _events(sample_dir: Path):
    return [
        EventDocument(
            str(sample_dir / name),
            (sample_dir / name).read_text(encoding="utf-8"),
        )
        for name in ("events_a.json", "events_b.json")
    ]


def _workbench(sample_dir: Path, workdir: Path, policy=None):
    return Workbench(
        policy
        or json.loads((sample_dir / "policy.json").read_text(encoding="utf-8")),
        source=str(sample_dir / "policy.json"),
        workdir=workdir,
        output_dir=workdir,
    )


def test_checkpoint_resume_does_not_double_apply_events(
    sample_dir: Path,
    tmp_path: Path,
):
    workdir = tmp_path / "resume"
    first = _workbench(sample_dir, workdir).run(_events(sample_dir))
    second = _workbench(sample_dir, workdir).run(_events(sample_dir))
    assert first.report["checkpoint"]["status"] == "fresh"
    assert second.report["checkpoint"]["status"] == "resumed"
    assert second.report["checkpoint"]["processed_event_count"] == 7
    assert set(token["token_id"] for token in second.report["affected_tokens"]) == {
        "tok-alice1",
        "tok-alice2",
        "tok-dave1",
    }
    assert len(second.report["active_sessions"]) == 3


def test_policy_version_mismatch_rebuilds_from_scratch(
    sample_dir: Path,
    tmp_path: Path,
):
    workdir = tmp_path / "version"
    policy = json.loads((sample_dir / "policy.json").read_text(encoding="utf-8"))
    _workbench(sample_dir, workdir, policy=policy).run(_events(sample_dir))

    changed = dict(policy)
    changed["policy_version"] = "9.0.0"
    result = _workbench(sample_dir, workdir, policy=changed).run(_events(sample_dir))
    assert result.report["checkpoint"]["status"] == "rebuilt"
    checkpoint_diagnostics = result.report["checkpoint"]["diagnostics"]
    assert checkpoint_diagnostics
    assert checkpoint_diagnostics[0]["code"] == "E_CHECKPOINT_VERSION"
    assert result.report["checkpoint"]["processed_event_count"] == 7
    assert len(result.report["active_sessions"]) == 3


def test_new_update_with_same_policy_version_replays_events(
    sample_dir: Path,
    tmp_path: Path,
):
    workdir = tmp_path / "append-update"
    base_policy = json.loads(
        (sample_dir / "policy.json").read_text(encoding="utf-8")
    )
    base_policy["updates"] = []
    base_policy["sync_markers"] = []
    first = _workbench(sample_dir, workdir, policy=base_policy).run(
        _events(sample_dir)
    )
    assert first.report["checkpoint"]["status"] == "fresh"
    assert first.report["summary"]["affected_tokens"] == 0

    updated_policy = json.loads(json.dumps(base_policy))
    updated_policy["updates"] = [
        {
            "update_id": "upd-late",
            "timestamp": "2026-02-02T00:00:00Z",
            "version": "1.0.1",
            "op": "put",
            "entry": {
                "id": "token:standard",
                "kind": "token_rule",
                "version": "1.0.1",
                "refs": ["role:member", "session:base"],
                "template": "standard-v2",
            },
        }
    ]
    second = _workbench(sample_dir, workdir, policy=updated_policy).run(
        _events(sample_dir)
    )
    assert second.report["checkpoint"]["status"] == "resumed"
    assert second.report["checkpoint"]["event_state_reset"] is True
    assert second.report["checkpoint"]["processed_event_count"] == 7
    assert set(
        token["token_id"] for token in second.report["affected_tokens"]
    ) == {"tok-dave1"}
    assert len(second.report["active_sessions"]) == 3


def test_structure_version_mismatch_rebuilds(sample_dir: Path, tmp_path: Path):
    workdir = tmp_path / "structure"
    _workbench(sample_dir, workdir).run(_events(sample_dir))
    checkpoint_path = workdir / "checkpoint.json"
    envelope = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    envelope["structure_version"] = "0.8"
    checkpoint_path.write_text(json.dumps(envelope), encoding="utf-8")

    result = _workbench(sample_dir, workdir).run(_events(sample_dir))
    assert result.report["checkpoint"]["status"] == "rebuilt"
    assert (
        result.report["checkpoint"]["diagnostics"][0]["code"]
        == "E_CHECKPOINT_STRUCTURE"
    )


def test_update_watermark_mismatch_rebuilds(sample_dir: Path, tmp_path: Path):
    workdir = tmp_path / "watermark"
    policy = json.loads((sample_dir / "policy.json").read_text(encoding="utf-8"))
    _workbench(sample_dir, workdir, policy=policy).run(_events(sample_dir))

    changed = json.loads(json.dumps(policy))
    changed["updates"][0]["entry"]["template"] = "different-content"
    result = _workbench(sample_dir, workdir, policy=changed).run(_events(sample_dir))
    assert result.report["checkpoint"]["status"] == "rebuilt"
    assert (
        result.report["checkpoint"]["diagnostics"][0]["code"]
        == "E_CHECKPOINT_UPDATE"
    )


def test_partial_temp_file_is_ignored_and_removed(sample_dir: Path, tmp_path: Path):
    workdir = tmp_path / "temp-clean"
    _workbench(sample_dir, workdir).run(_events(sample_dir))
    stale = workdir / ".checkpoint.json.deadbeef.tmp"
    stale.write_text('{"format": "authdiag.checkpoint",', encoding="utf-8")

    result = _workbench(sample_dir, workdir).run(_events(sample_dir))
    assert result.report["checkpoint"]["removed_temp_files"] == [stale.name]
    assert not stale.exists()
    assert not list(workdir.glob(".*.tmp"))


def test_classic_checkpoint_temp_suffix_is_cleaned(sample_dir: Path, tmp_path: Path):
    workdir = tmp_path / "classic-temp"
    _workbench(sample_dir, workdir).run(_events(sample_dir))
    stale = workdir / "checkpoint.json.tmp"
    stale.write_text('{"partial":', encoding="utf-8")
    result = _workbench(sample_dir, workdir).run(_events(sample_dir))
    assert stale.name in result.report["checkpoint"]["removed_temp_files"]
    assert not stale.exists()


def test_lock_conflict_returns_e_lock(sample_dir: Path, tmp_path: Path):
    workdir = tmp_path / "lock"
    marker = tmp_path / "lock-ready"
    holder = subprocess.Popen(
        [
            sys.executable,
            str(Path(__file__).with_name("workers.py")),
            "lock",
            str(workdir),
            str(marker),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 10
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert marker.exists(), holder.stderr.read()

        process = subprocess.run(
            [
                sys.executable,
                "-m",
                "authdiag",
                str(sample_dir / "policy.json"),
                str(sample_dir / "events_a.json"),
                "--out",
                str(workdir),
                "--workdir",
                str(workdir),
            ],
            cwd=Path(__file__).parents[1],
            text=True,
            capture_output=True,
            timeout=20,
        )
        assert process.returncode == 1
        payload = json.loads(process.stdout.strip().splitlines()[-1])
        assert payload["code"] == "E_LOCK"
        assert payload["details"]["workdir"] == str(workdir)
    finally:
        holder.kill()
        holder.wait(timeout=10)


def test_force_kill_recovers_last_complete_checkpoint(
    sample_dir: Path,
    tmp_path: Path,
):
    workdir = tmp_path / "crash"
    marker = tmp_path / "checkpoint-ready"
    worker = subprocess.Popen(
        [
            sys.executable,
            str(Path(__file__).with_name("workers.py")),
            "crash",
            str(sample_dir),
            str(workdir),
            str(marker),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 10
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert marker.exists(), worker.stderr.read()
        worker.kill()
        worker.wait(timeout=10)
    finally:
        if worker.poll() is None:
            worker.kill()
            worker.wait(timeout=10)

    result = _workbench(sample_dir, workdir).run(_events(sample_dir))
    assert result.report["checkpoint"]["status"] == "resumed"
    assert result.report["checkpoint"]["applied_update_count"] == 3
    assert result.report["summary"]["affected_tokens"] == 3
    assert not list(workdir.glob(".*.tmp"))
    checkpoint = json.loads((workdir / "checkpoint.json").read_text(encoding="utf-8"))
    assert checkpoint["format"] == "authdiag.checkpoint"


@pytest.mark.skipif(os.name == "nt", reason="Windows has no exact SIGKILL equivalent")
def test_sigkill_after_atomic_checkpoint_leaves_lock_releasable(
    sample_dir: Path,
    tmp_path: Path,
):
    workdir = tmp_path / "sigkill"
    marker = tmp_path / "sigkill-ready"
    worker = subprocess.Popen(
        [
            sys.executable,
            str(Path(__file__).with_name("workers.py")),
            "crash",
            str(sample_dir),
            str(workdir),
            str(marker),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 10
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert marker.exists()
        worker.send_signal(9)
        worker.wait(timeout=10)
    finally:
        if worker.poll() is None:
            worker.kill()
            worker.wait(timeout=10)
    with WorkdirLock(workdir):
        assert True
