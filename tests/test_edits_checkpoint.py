from __future__ import annotations

import copy
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from sensitive_flow.checkpoint import (
    CheckpointStore,
    restore,
    run_edit_session,
)
from sensitive_flow.engine import (
    apply_edit,
    apply_edits,
    build_diagnostic_report,
    build_state,
    extract_edit_items,
    replay,
    state_hash,
)
from sensitive_flow.errors import ForcedTermination, TraceError
from sensitive_flow.storage import Workspace, atomic_write_json


def _prepare(workspace: Workspace, samples: dict) -> tuple[list, list]:
    snapshots = samples["snapshots"]["snapshots"]
    edits = samples["edits"]["edits"]
    workspace.write_snapshots(snapshots)
    workspace.write_edits(edits)
    return snapshots, edits


def test_failed_edit_does_not_mutate_tracking_state(samples: dict) -> None:
    state = build_state(samples["snapshots"])
    before = copy.deepcopy(state.documents["DOC-MAIN"])
    outcome = apply_edit(
        state,
        {
            "seq": 1,
            "document_id": "DOC-MAIN",
            "type": "set_level",
            "path": "customer.id",
            "level": 99,
        },
    )
    assert outcome["accepted"] is False
    assert outcome["code"] == "E_LEVEL"
    after = state.documents["DOC-MAIN"]
    assert after.fields == before.fields
    assert after.derivations == before.derivations
    assert after.tombstones == before.tombstones
    assert len(after.rejections) == len(before.rejections) + 1
    assert state.processed_edits == 1


def test_edit_replay_is_batch_invariant(samples: dict) -> None:
    all_at_once = replay(samples["snapshots"], samples["edits"])
    incremental = build_state(samples["snapshots"])
    for index, operation in enumerate(extract_edit_items(samples["edits"])):
        apply_edit(incremental, operation, index)
    assert incremental.to_dict() == all_at_once.to_dict()


def test_checkpoint_resume_equals_full_replay(tmp_path: Path, samples: dict) -> None:
    workspace = Workspace(tmp_path / "workspace").ensure()
    snapshots, edits = _prepare(workspace, samples)
    full = replay(snapshots, edits)

    state = build_state(snapshots)
    apply_edits(state, {"edits": edits[:7]})
    store = CheckpointStore(workspace)
    store.save(state)

    recovered = restore(workspace, snapshots, edits)
    assert recovered.mode == "resume"
    assert recovered.checkpoint_used is True
    assert recovered.state.to_dict() == full.to_dict()
    assert state_hash(recovered.state) == state_hash(full)


def test_forced_termination_leaves_only_checkpoint_boundary(
    tmp_path: Path, samples: dict
) -> None:
    workspace = Workspace(tmp_path / "workspace").ensure()
    snapshots, edits = _prepare(workspace, samples)
    full = replay(snapshots, edits)

    with workspace.lock:
        state = build_state(snapshots)
        store = CheckpointStore(workspace)
        store.save(state, lock=workspace.lock)
        with pytest.raises(ForcedTermination) as caught:
            run_edit_session(
                state,
                edits,
                store,
                workspace.lock,
                checkpoint_every=1,
                crash_after=6,
            )
        assert caught.value.checkpoint_edit_count == 6

    checkpoint = json.loads(workspace.checkpoint_file.read_text(encoding="utf-8"))
    assert checkpoint["processed_edits"] == 6
    assert checkpoint["state"]["processed_edits"] == 6
    assert not (workspace.reports_dir / "edit.json").exists()

    recovered = restore(workspace, snapshots, edits)
    assert recovered.mode == "resume"
    assert recovered.state.to_dict() == full.to_dict()


def test_half_written_temp_is_ignored(tmp_path: Path, samples: dict) -> None:
    workspace = Workspace(tmp_path / "workspace").ensure()
    snapshots, edits = _prepare(workspace, samples)
    state = build_state(snapshots)
    CheckpointStore(workspace).save(state)
    residue = workspace.checkpoints_dir / ".checkpoint.json.dead.tmp"
    residue.write_text('{"complete": false, "partial": ', encoding="utf-8")

    recovered = restore(workspace, snapshots, edits)
    assert "E_CHECKPOINT_TEMP_RESIDUE" in recovered.as_recovery_dict()["codes"]
    assert recovered.state.to_dict() == replay(snapshots, edits).to_dict()
    assert residue.exists()


def test_corrupt_checkpoint_triggers_full_rebuild(tmp_path: Path, samples: dict) -> None:
    workspace = Workspace(tmp_path / "workspace").ensure()
    snapshots, edits = _prepare(workspace, samples)
    state = build_state(snapshots)
    CheckpointStore(workspace).save(state)
    workspace.checkpoint_file.write_text('{"complete": true, ', encoding="utf-8")

    recovered = restore(workspace, snapshots, edits)
    assert recovered.mode == "rebuild"
    assert "E_CHECKPOINT_CORRUPT" in recovered.as_recovery_dict()["codes"]
    assert recovered.state.to_dict() == replay(snapshots, edits).to_dict()


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        ("checkpoint_schema_version", "0.9", "E_CHECKPOINT_SCHEMA"),
        ("rule_version", "0.1", "E_CHECKPOINT_RULE"),
    ],
)
def test_checkpoint_version_mismatch_rebuilds(
    tmp_path: Path, samples: dict, field: str, value: str, code: str
) -> None:
    workspace = Workspace(tmp_path / f"workspace-{field}").ensure()
    snapshots, edits = _prepare(workspace, samples)
    state = build_state(snapshots)
    apply_edits(state, {"edits": edits[:4]})
    store = CheckpointStore(workspace)
    store.save(state)
    payload = json.loads(workspace.checkpoint_file.read_text(encoding="utf-8"))
    payload[field] = value
    atomic_write_json(workspace.checkpoint_file, payload)

    recovered = restore(workspace, snapshots, edits)
    assert recovered.mode == "rebuild"
    assert code in recovered.as_recovery_dict()["codes"]
    assert recovered.state.to_dict() == replay(snapshots, edits).to_dict()


def test_replay_mismatch_detects_wrong_state(tmp_path: Path, samples: dict) -> None:
    workspace = Workspace(tmp_path / "workspace").ensure()
    snapshots, edits = _prepare(workspace, samples)
    state = build_state(snapshots)
    apply_edits(state, {"edits": edits[:5]})
    store = CheckpointStore(workspace)
    store.save(state)
    payload = json.loads(workspace.checkpoint_file.read_text(encoding="utf-8"))
    payload["state"]["documents"]["DOC-MAIN"]["fields"]["customer.id"]["level"] = 0
    from sensitive_flow.engine import TraceState

    tampered = TraceState.from_dict(payload["state"])
    payload["state_hash"] = state_hash(tampered)
    atomic_write_json(workspace.checkpoint_file, payload)

    recovered = restore(workspace, snapshots, edits)
    assert recovered.mode == "rebuild"
    assert "E_CHECKPOINT_STATE" in recovered.as_recovery_dict()["codes"]
    assert recovered.state.to_dict() == replay(snapshots, edits).to_dict()


def test_report_write_is_byte_stable(tmp_path: Path, samples: dict) -> None:
    workspace = Workspace(tmp_path / "workspace").ensure()
    state = replay(samples["snapshots"], samples["edits"])
    report = build_diagnostic_report(state)
    first = workspace.reports_dir / "one.json"
    second = workspace.reports_dir / "two.json"
    workspace.write_report(report, first)
    workspace.write_report(report, second)
    assert first.read_bytes() == second.read_bytes()


def test_lock_conflict_returns_e_lock_without_mutation(
    tmp_path: Path, samples: dict, root: Path
) -> None:
    workspace = Workspace(tmp_path / "workspace").ensure()
    snapshots, edits = _prepare(workspace, samples)
    state = build_state(snapshots)
    CheckpointStore(workspace).save(state)
    before = workspace.checkpoint_file.read_bytes()
    report_path = workspace.reports_dir / "locked-report.json"

    with workspace.lock:
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "sensitive_flow",
                "export",
                "--workspace",
                str(workspace.root),
                "--report",
                str(report_path),
            ],
            cwd=root,
            text=True,
            capture_output=True,
            timeout=30,
            env={**os.environ, "PYTHONPATH": str(root)},
        )

    assert completed.returncode == 1
    payload = json.loads(completed.stderr)
    assert payload["errors"][0]["code"] == "E_LOCK"
    assert workspace.checkpoint_file.read_bytes() == before
    assert not report_path.exists()


def test_cli_exit_codes(tmp_path: Path, samples: dict, root: Path) -> None:
    env = {**os.environ, "PYTHONPATH": str(root)}
    missing = subprocess.run(
        [sys.executable, "-m", "sensitive_flow", "trace", "--workspace", str(tmp_path / "x")],
        cwd=root,
        text=True,
        capture_output=True,
        env=env,
        timeout=30,
    )
    assert missing.returncode == 2
    assert json.loads(missing.stderr)["errors"][0]["code"] == "E_ARGUMENT"

    source = tmp_path / "invalid.json"
    source.write_text(json.dumps(samples["invalid_snapshots"]), encoding="utf-8")
    workspace = tmp_path / "partial-workspace"
    partial = subprocess.run(
        [
            sys.executable,
            "-m",
            "sensitive_flow",
            "import",
            "--workspace",
            str(workspace),
            "--snapshots",
            str(source),
        ],
        cwd=root,
        text=True,
        capture_output=True,
        env=env,
        timeout=30,
    )
    assert partial.returncode == 1
    assert json.loads(partial.stdout)["status"] == "partial"


def test_cli_forced_termination_resumes_at_checkpoint(
    tmp_path: Path, samples: dict, root: Path
) -> None:
    env = {**os.environ, "PYTHONPATH": str(root)}
    snapshots_path = tmp_path / "snapshots.json"
    edits_path = tmp_path / "edits.json"
    snapshots_path.write_text(json.dumps(samples["snapshots"]), encoding="utf-8")
    edits_path.write_text(json.dumps(samples["edits"]), encoding="utf-8")
    workspace = tmp_path / "crash-workspace"

    imported = subprocess.run(
        [
            sys.executable,
            "-m",
            "sensitive_flow",
            "import",
            "--workspace",
            str(workspace),
            "--snapshots",
            str(snapshots_path),
        ],
        cwd=root,
        text=True,
        capture_output=True,
        env=env,
        timeout=30,
    )
    assert imported.returncode == 1

    crashed = subprocess.run(
        [
            sys.executable,
            "-m",
            "sensitive_flow",
            "edit",
            "--workspace",
            str(workspace),
            "--edits",
            str(edits_path),
            "--checkpoint-every",
            "3",
            "--crash-after",
            "6",
        ],
        cwd=root,
        text=True,
        capture_output=True,
        env=env,
        timeout=30,
    )
    assert crashed.returncode == 137
    assert json.loads(crashed.stderr)["errors"][0]["code"] == "E_FORCED_TERMINATION"
    assert not (Path(workspace) / "reports" / "edit.json").exists()
    checkpoint = json.loads(
        (Path(workspace) / "checkpoints" / "checkpoint.json").read_text(encoding="utf-8")
    )
    assert checkpoint["processed_edits"] == 6

    resumed = subprocess.run(
        [
            sys.executable,
            "-m",
            "sensitive_flow",
            "edit",
            "--workspace",
            str(workspace),
            "--checkpoint-every",
            "3",
        ],
        cwd=root,
        text=True,
        capture_output=True,
        env=env,
        timeout=30,
    )
    assert resumed.returncode == 1
    result = json.loads(resumed.stdout)
    assert result["summary"]["processed_edits"] == 16
    assert result["recovery"]["mode"] == "resume"
