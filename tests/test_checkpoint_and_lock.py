from __future__ import annotations

import json
import subprocess
import sys
import time
from itertools import permutations
from pathlib import Path

from conftest import REPOSITORY_ROOT, write_json
from trustman.atomic import workspace_lock
from trustman.checkpoint import (
    CheckpointStore,
    checkpoint_checksum,
)
from trustman.engine import (
    import_snapshots,
    inspect_checkpoint,
    load_and_merge_snapshots,
    run_workspace_validation,
)
from trustman.models import canonical_json
from trustman.scheduler import SchedulerConfig


def _validate(
    workspace: Path,
    samples: Path,
    snapshot_files: list[Path],
    *,
    report_name: str = "report.json",
    use_resume: bool = True,
):
    return run_workspace_validation(
        workspace=workspace,
        certificate_paths=[samples / "certificates.json"],
        chain_paths=[samples / "chains.json"],
        snapshot_paths=snapshot_files,
        config=SchedulerConfig(workers=2),
        use_resume=use_resume,
        report_path=workspace / report_name,
    )


def _rewrite_checkpoint(
    workspace: Path,
    *,
    structure_version: int | None = None,
    mutate_payload=None,
) -> None:
    store = CheckpointStore(workspace)
    loaded = store.load()
    assert loaded.status == "ok"
    payload = loaded.payload
    assert payload is not None
    if mutate_payload is not None:
        mutate_payload(payload)
    version = (
        loaded.structure_version
        if structure_version is None
        else structure_version
    )
    envelope = {
        "schema": "trustman.checkpoint/v1",
        "structure_version": version,
        "payload": payload,
        "checksum": checkpoint_checksum(payload, version),
    }
    store.path.write_text(
        json.dumps(envelope, sort_keys=True) + "\n", encoding="utf-8"
    )


def test_checkpoint_resume_rebuild_replay_and_half_write(
    tmp_path: Path, samples: Path, snapshot_files: list[Path]
):
    workspace = tmp_path / "workspace"
    first = _validate(workspace, samples, snapshot_files)
    assert first.report["summary"]["succeeded"] == 9
    assert first.metadata.report_written is True
    assert inspect_checkpoint(workspace)["ok"] is True

    second = _validate(workspace, samples, snapshot_files)
    assert second.metadata.checkpoint_loaded is True
    assert second.metadata.checkpoint_rebuilt is False
    assert second.metadata.executed_chain_ids == []
    assert len(second.metadata.reused_chain_ids) == 9
    assert len(second.metadata.replay_checked_chain_ids) == 9

    residue = workspace / "state" / ".checkpoint.json.999.dead.tmp"
    residue.write_text('{"partial":', encoding="utf-8")
    checked = inspect_checkpoint(workspace)
    assert checked["ok"] is True
    assert checked["has_half_write_residue"] is True
    residue.unlink()

    _rewrite_checkpoint(
        workspace,
        mutate_payload=lambda payload: payload["results"]["chain-ok"].update(
            {"conclusion": "untrusted"}
        ),
    )
    replay = _validate(workspace, samples, snapshot_files)
    assert replay.metadata.checkpoint_rebuilt is True
    assert (
        replay.metadata.checkpoint_reason
        == "E_CHECKPOINT_REPLAY_MISMATCH"
    )
    replay_chains = {
        item["chain_id"]: item for item in replay.report["chains"]
    }
    assert replay_chains["chain-ok"]["conclusion"] == "trusted"

    _rewrite_checkpoint(workspace, structure_version=999)
    structure = _validate(workspace, samples, snapshot_files)
    assert structure.metadata.checkpoint_rebuilt is True
    assert (
        structure.metadata.checkpoint_reason
        == "E_CHECKPOINT_STRUCTURE_CHANGED"
    )

    store = CheckpointStore(workspace)
    store.path.write_text("{broken", encoding="utf-8")
    corrupt = _validate(workspace, samples, snapshot_files)
    assert corrupt.metadata.checkpoint_rebuilt is True
    assert corrupt.metadata.checkpoint_reason == "E_CHECKPOINT_CORRUPT"
    assert corrupt.report["summary"]["succeeded"] == 9

    policy_snapshot = write_json(
        tmp_path / "policy-v4.json",
        {
            "schema": "trustman.snapshots/v1",
            "snapshots": [
                {
                    "version": 4,
                    "policy_version": "2.0.0",
                    "trusted_roots": ["root-a"],
                    "revoked_entries": [],
                    "observed_at_ms": 1_700_000_000_000,
                }
            ],
        },
    )
    imported = import_snapshots(
        workspace=workspace, snapshot_paths=[policy_snapshot]
    )[1]
    assert imported["effective_policy_version"] == "2.0.0"
    policy = _validate(workspace, samples, snapshot_files)
    assert policy.metadata.checkpoint_rebuilt is True
    assert (
        policy.metadata.checkpoint_reason == "E_CHECKPOINT_POLICY_CHANGED"
    )


def test_forced_termination_uses_last_complete_checkpoint(
    tmp_path: Path, samples: Path, snapshot_files: list[Path]
):
    workspace = tmp_path / "killed-workspace"
    report = workspace / "report.json"
    worker = REPOSITORY_ROOT / "tests" / "forced_kill_worker.py"
    process = subprocess.Popen(
        [
            sys.executable,
            str(worker),
            str(workspace),
            str(samples / "certificates.json"),
            str(samples / "chains.json"),
            str(snapshot_files[0]),
            str(snapshot_files[1]),
            str(snapshot_files[2]),
            str(report),
        ],
        cwd=REPOSITORY_ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 10
        completed = 0
        while time.monotonic() < deadline:
            if process.poll() is not None:
                break
            loaded = CheckpointStore(workspace).load()
            if loaded.status == "ok" and loaded.payload is not None:
                completed = len(loaded.payload.get("results", {}))
                if completed >= 2:
                    break
            time.sleep(0.02)
        assert process.poll() is None, "worker exited before forced termination"
        assert completed >= 2
        process.kill()
        process.wait(timeout=10)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=10)
    assert report.exists() is False
    loaded = CheckpointStore(workspace).load()
    assert loaded.status == "ok"
    assert loaded.payload is not None
    assert len(loaded.payload["results"]) >= 2

    resumed = _validate(workspace, samples, snapshot_files)
    assert resumed.metadata.checkpoint_loaded is True
    assert resumed.metadata.checkpoint_rebuilt is False
    assert len(resumed.metadata.reused_chain_ids) >= 2
    assert resumed.report["summary"]["succeeded"] == 9
    assert resumed.report["reconciliation"]["task_counts_match"] is True
    assert report.is_file()


def test_e_lock_leaves_existing_snapshot_state_untouched(
    tmp_path: Path, samples: Path, snapshot_files: list[Path]
):
    workspace = tmp_path / "locked-workspace"
    import_snapshots(
        workspace=workspace, snapshot_paths=[snapshot_files[0]]
    )
    state_path = workspace / "state" / "snapshots.json"
    before = state_path.read_bytes()
    with workspace_lock(workspace):
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "trustman",
                "import-snapshot",
                "--workspace",
                str(workspace),
                "--snapshot",
                str(snapshot_files[1]),
            ],
            cwd=REPOSITORY_ROOT,
            capture_output=True,
            text=True,
            timeout=20,
        )
    assert result.returncode == 1
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert payload["code"] == "E_LOCK"
    assert state_path.read_bytes() == before


def test_snapshot_arrival_permutations_converge(
    tmp_path: Path, samples: Path, snapshot_files: list[Path]
):
    reports: list[str] = []
    expected_fingerprint = None
    for index, order in enumerate(permutations(snapshot_files)):
        workspace = tmp_path / f"arrival-{index}"
        for snapshot_path in order:
            result = import_snapshots(
                workspace=workspace, snapshot_paths=[snapshot_path]
            )[1]
            assert result["ok"] is True
        context = load_and_merge_snapshots(workspace, [])
        assert context.effective is not None
        assert context.effective.version == 3
        if expected_fingerprint is None:
            expected_fingerprint = context.effective.content_fingerprint
        assert context.effective.content_fingerprint == expected_fingerprint
        outcome = run_workspace_validation(
            workspace=workspace,
            certificate_paths=[samples / "certificates.json"],
            chain_paths=[samples / "chains.json"],
            snapshot_paths=[],
            config=SchedulerConfig(workers=3),
            report_path=workspace / "report.json",
        )
        reports.append(canonical_json(outcome.report))
    assert all(report == reports[0] for report in reports)


def test_conflicting_snapshot_version_is_order_independent(tmp_path: Path):
    first = write_json(
        tmp_path / "conflict-a.json",
        {
            "schema": "trustman.snapshots/v1",
            "snapshots": [
                {
                    "version": 1,
                    "policy_version": "1.0.0",
                    "trusted_roots": ["root-a"],
                    "revoked_entries": [],
                    "observed_at_ms": 100,
                }
            ],
        },
    )
    second = write_json(
        tmp_path / "conflict-b.json",
        {
            "schema": "trustman.snapshots/v1",
            "snapshots": [
                {
                    "version": 1,
                    "policy_version": "1.0.0",
                    "trusted_roots": ["root-b"],
                    "revoked_entries": [],
                    "observed_at_ms": 100,
                }
            ],
        },
    )
    fingerprints = []
    rejection_sets = []
    for index, order in enumerate(((first, second), (second, first))):
        workspace = tmp_path / f"conflict-{index}"
        last_result = None
        for path in order:
            last_result = import_snapshots(
                workspace=workspace, snapshot_paths=[path]
            )[1]
        assert last_result is not None and last_result["ok"] is False
        context = load_and_merge_snapshots(workspace, [])
        assert len(context.accepted) == 1
        assert len(context.accepted[0].trusted_roots) == 1
        fingerprints.append(context.accepted[0].content_fingerprint)
        rejection_sets.append(
            canonical_json(
                [
                    rejection.as_dict()
                    for rejection in context.rejections
                    if rejection.reason == "snapshot_version_conflict"
                ]
            )
        )
    assert fingerprints[0] == fingerprints[1]
    assert rejection_sets[0] == rejection_sets[1]
