from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from audit_forge import store
from audit_forge.models import Event
from audit_forge.rules import case_identity, load_rule_config, parse_rule_document
from audit_forge.store import (
    LoadResult,
    StoreError,
    Workspace,
    atomic_write_json,
)
from audit_forge.util import sha256_value

from .conftest import PROJECT_ROOT, generate_samples, parse_stdout, run_cli, write_jsonl


def _populate(workspace: Path, rows: list[dict], rules_path: Path | None = None) -> LoadResult:
    store_workspace = Workspace(workspace)
    requested = load_rule_config(rules_path) if rules_path else None
    with store_workspace.lock():
        loaded = store_workspace.load(requested)
        for index, row in enumerate(rows, start=1):
            loaded.engine.ingest_event(Event.from_dict(row), "unit.jsonl", index)
        store_workspace.save(loaded.engine, loaded.engine.rules)
    return store_workspace.load(requested)


def test_checkpoint_round_trip_and_snapshot_validation(workspace: Path) -> None:
    loaded = _populate(workspace, generate_samples.clean_events())
    assert loaded.checkpoint_exists
    assert loaded.cases_source == "snapshot"
    assert loaded.engine.generation == 1
    assert len(loaded.engine.cases) == 3
    assert loaded.checks["checkpoint"]["checksum_valid"] is True


def test_rule_version_change_rebuilds_without_mixing_old_cases(
    workspace: Path, sample_dir: Path
) -> None:
    _populate(workspace, generate_samples.clean_events())
    old_cases_path = workspace / "cases.json"
    old_snapshot = json.loads(old_cases_path.read_text(encoding="utf-8"))
    old_identity = case_identity(old_snapshot["cases"])
    short_rules = load_rule_config(sample_dir / "rules-short-window.json")

    store_workspace = Workspace(workspace)
    with store_workspace.lock():
        loaded = store_workspace.load(short_rules)
        assert loaded.requires_rebuild
        assert loaded.cases_source == "rebuilt"
        assert any(
            issue["code"] == "E_RULE_VERSION_CHANGED"
            for issue in loaded.issues
        )
        assert case_identity(loaded.engine.cases) != old_identity
        store_workspace.save(loaded.engine, short_rules)

    with store_workspace.lock():
        loaded_again = store_workspace.load(short_rules)
    assert not loaded_again.requires_rebuild
    assert loaded_again.cases_source == "snapshot"
    assert loaded_again.engine.cases == []
    checkpoint = json.loads(
        (workspace / "checkpoint.json").read_text(encoding="utf-8")
    )
    assert checkpoint["rule_version"] == "short-window-v1"


def test_schema_version_change_triggers_rebuild(workspace: Path) -> None:
    _populate(workspace, generate_samples.clean_events())
    checkpoint_path = workspace / "checkpoint.json"
    checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    checkpoint["schema_version"] = 999
    checkpoint["checksum"] = sha256_value(
        {key: value for key, value in checkpoint.items() if key != "checksum"}
    )
    atomic_write_json(checkpoint_path, checkpoint)

    store_workspace = Workspace(workspace)
    with store_workspace.lock():
        loaded = store_workspace.load()
    assert loaded.requires_rebuild
    assert loaded.cases_source == "rebuilt"
    assert any(
        issue["code"] == "E_SCHEMA_VERSION_CHANGED"
        for issue in loaded.issues
    )


def test_stale_case_snapshot_is_ignored(workspace: Path) -> None:
    _populate(workspace, generate_samples.clean_events())
    cases_path = workspace / "cases.json"
    snapshot = json.loads(cases_path.read_text(encoding="utf-8"))
    snapshot["generation"] = 999
    snapshot["checksum"] = sha256_value(
        {key: value for key, value in snapshot.items() if key != "checksum"}
    )
    atomic_write_json(cases_path, snapshot)

    store_workspace = Workspace(workspace)
    with store_workspace.lock():
        loaded = store_workspace.load()
    assert loaded.cases_source == "rebuilt"
    assert any(
        issue["code"] == "E_CASE_SNAPSHOT_STALE"
        for issue in loaded.issues
    )
    assert len(loaded.engine.cases) == 3


def test_corrupt_checkpoint_is_not_accepted(workspace: Path) -> None:
    _populate(workspace, generate_samples.clean_events())
    checkpoint_path = workspace / "checkpoint.json"
    checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    checkpoint["events"] = []
    atomic_write_json(checkpoint_path, checkpoint)
    with pytest.raises(StoreError) as caught:
        Workspace(workspace).read_checkpoint()
    assert caught.value.code == "E_CHECKPOINT_CORRUPT"


def test_check_command_detects_semantically_wrong_but_checksummed_snapshot(
    workspace: Path,
) -> None:
    _populate(workspace, generate_samples.clean_events())
    snapshot_path = workspace / "cases.json"
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    snapshot["cases"][0]["event_ids"] = ["tampered"]
    snapshot["checksum"] = sha256_value(
        {key: value for key, value in snapshot.items() if key != "checksum"}
    )
    atomic_write_json(snapshot_path, snapshot)

    checkpoint_path = workspace / "checkpoint.json"
    checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    checkpoint["cases_ref"]["snapshot_checksum"] = snapshot["checksum"]
    checkpoint["cases_ref"]["cases_checksum"] = sha256_value(snapshot["cases"])
    checkpoint["checksum"] = sha256_value(
        {key: value for key, value in checkpoint.items() if key != "checksum"}
    )
    atomic_write_json(checkpoint_path, checkpoint)

    result = run_cli(
        "check-checkpoint",
        "--workspace",
        str(workspace),
    )
    assert result.returncode == 1
    report = parse_stdout(result)
    assert report["cases_recompute_matches"] is False
    assert any(
        error["code"] == "E_CASES_MISMATCH"
        for error in report["errors"]
    )


def test_forced_termination_recovers_last_complete_checkpoint(
    workspace: Path, tmp_path: Path
) -> None:
    first = generate_samples.clean_events()[:5]
    second = generate_samples.clean_events()[5:]
    first_path = tmp_path / "first.jsonl"
    second_path = tmp_path / "second.jsonl"
    write_jsonl(first_path, first)
    write_jsonl(second_path, second)

    result = run_cli(
        "import",
        "--workspace",
        str(workspace),
        "--input",
        str(first_path),
    )
    assert result.returncode == 0, result.stdout + result.stderr

    crash = subprocess.run(
        [
            sys.executable,
            str(PROJECT_ROOT / "tests" / "crash_import.py"),
            "import",
            "--workspace",
            str(workspace),
            "--input",
            str(second_path),
        ],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert crash.returncode == 137

    store_workspace = Workspace(workspace)
    with store_workspace.lock():
        loaded = store_workspace.load()
    assert loaded.checkpoint_exists
    assert loaded.engine.generation == 1
    assert sorted(loaded.engine.events) == sorted(
        row["event_id"] for row in first
    )
    assert loaded.cases_source == "rebuilt"
    assert any(
        issue["code"] == "E_CASE_SNAPSHOT_STALE"
        for issue in loaded.issues
    )
    assert store_workspace.stale_temp_files()

    check = run_cli(
        "check-checkpoint",
        "--workspace",
        str(workspace),
    )
    assert check.returncode == 1
    check_report = parse_stdout(check)
    assert check_report["load"]["generation_before"] == 1
    assert any(
        error["code"] == "E_CASES_NOT_CURRENT"
        for error in check_report["errors"]
    )

    repaired = run_cli(
        "check-checkpoint",
        "--workspace",
        str(workspace),
        "--repair",
    )
    assert repaired.returncode == 0, repaired.stdout + repaired.stderr
    assert parse_stdout(repaired)["repair"]["performed"] is True

    completed = run_cli(
        "import",
        "--workspace",
        str(workspace),
        "--input",
        str(second_path),
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    with store_workspace.lock():
        final = store_workspace.load()
    assert len(final.engine.events) == len(generate_samples.clean_events())


def test_lock_conflict_returns_e_lock_without_state_change(
    workspace: Path, tmp_path: Path
) -> None:
    input_path = tmp_path / "events.jsonl"
    write_jsonl(input_path, generate_samples.clean_events())
    store_workspace = Workspace(workspace)
    with store_workspace.lock():
        result = run_cli(
            "import",
            "--workspace",
            str(workspace),
            "--input",
            str(input_path),
        )
    assert result.returncode == 1
    report = parse_stdout(result)
    assert report["status"] == "error"
    assert report["errors"][0]["code"] == "E_LOCK"
    assert not (workspace / "checkpoint.json").exists()

