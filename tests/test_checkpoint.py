from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from smuggle_detector.checkpoint import CheckpointManager
from smuggle_detector.detector import detect_connection
from smuggle_detector.engine import run_detection_batch
from smuggle_detector.errors import LockUnavailable
from smuggle_detector.io_utils import WorkspaceLock, atomic_write_json, read_json
from smuggle_detector.parsing import (
    compute_input_digest,
    compute_plan_digest,
    parse_sample_dir,
)
from smuggle_detector.report import build_report
from smuggle_detector.rules import build_rule_catalog

from .helpers import conclusion_projection, manager_for, run_sample


def _outcome_to_report(sample, plan, outcome) -> dict:
    return build_report(
        sample=sample,
        plan=plan,
        input_digest=compute_input_digest(sample),
        plan_digest=compute_plan_digest(plan),
        catalog=outcome["catalog"],
        connections=outcome["connections"],
        summary=outcome["summary"],
        scheduling=outcome["scheduling"],
        checkpoint=outcome["checkpoint"],
    )


def test_resume_uses_only_last_complete_checkpoint_and_ignores_residue(
    sample_dir: Path, tmp_path: Path
) -> None:
    baseline, sample, plan, _ = run_sample(sample_dir, tmp_path / "baseline")
    workspace = tmp_path / "resume"
    manager = manager_for(workspace, sample, plan)
    catalog = build_rule_catalog(sample.rules)
    grouped = {}
    for record in sample.messages:
        grouped.setdefault(record.connection_id, []).append(record)
    initial_ids = sorted(grouped)[:2]
    for connection_id in initial_ids:
        manager.commit(
            detect_connection(
                connection_id,
                grouped[connection_id],
                catalog,
                timeout_ms=plan.connection_timeout_ms,
            )
        )
    assert manager.load().valid
    assert set(manager.load().completed) == set(initial_ids)

    (workspace / ".checkpoint.sqlite3.tmp-dead").write_bytes(b"partial sqlite")
    (workspace / ".checkpoint_snapshot.json.tmp-dead").write_text(
        "{partial", encoding="utf-8"
    )

    restarted = manager_for(workspace, sample, plan)
    outcome = run_detection_batch(sample, plan, restarted, resume=True)
    report = _outcome_to_report(sample, plan, outcome)
    assert outcome["checkpoint"]["resumed"] is True
    assert outcome["checkpoint"]["completed_count"] == 14
    assert {
        ".checkpoint.sqlite3.tmp-dead",
        ".checkpoint_snapshot.json.tmp-dead",
    }.issubset(set(outcome["checkpoint"]["residues_detected"]))
    assert conclusion_projection(report) == conclusion_projection(baseline)
    assert restarted.verify()["valid"] is True


def test_snapshot_disagreement_causes_full_rebuild(
    sample_dir: Path, tmp_path: Path
) -> None:
    baseline, sample, plan, manager = run_sample(
        sample_dir, tmp_path / "baseline"
    )
    snapshot = read_json(manager.snapshot_path)
    snapshot["results"] = {}
    atomic_write_json(manager.snapshot_path, snapshot)

    restarted = manager_for(manager.workspace, sample, plan)
    outcome = run_detection_batch(sample, plan, restarted, resume=True)
    report = _outcome_to_report(sample, plan, outcome)
    assert outcome["checkpoint"]["resumed"] is False
    assert (
        "disagree" in outcome["checkpoint"]["rebuild_reason"]
        or "corrupt" in outcome["checkpoint"]["rebuild_reason"]
    )
    assert conclusion_projection(report) == conclusion_projection(baseline)
    assert restarted.verify()["valid"] is True


def test_checkpoint_schema_version_change_rebuilds_instead_of_resuming(
    sample_dir: Path, tmp_path: Path
) -> None:
    _, sample, plan, manager = run_sample(sample_dir, tmp_path / "baseline")
    connection = sqlite3.connect(manager.db_path)
    try:
        row = connection.execute(
            "SELECT value FROM checkpoint_meta WHERE key='meta'"
        ).fetchone()
        meta = json.loads(row[0])
        meta["checkpoint_schema_version"] = 99
        connection.execute(
            "UPDATE checkpoint_meta SET value=? WHERE key='meta'",
            (json.dumps(meta, sort_keys=True),),
        )
        connection.commit()
    finally:
        connection.close()
    snapshot = read_json(manager.snapshot_path)
    snapshot["meta"]["checkpoint_schema_version"] = 99
    atomic_write_json(manager.snapshot_path, snapshot)

    restarted = manager_for(manager.workspace, sample, plan)
    outcome = run_detection_batch(sample, plan, restarted, resume=True)
    assert outcome["checkpoint"]["resumed"] is False
    assert (
        outcome["checkpoint"]["rebuild_reason"]
        == "checkpoint_version_or_rule_identity_changed"
    )
    assert outcome["checkpoint"]["completed_count"] == 14


def test_workspace_lock_conflict_returns_e_lock(tmp_path: Path) -> None:
    lock_path = tmp_path / ".lock"
    with WorkspaceLock(lock_path):
        with pytest.raises(LockUnavailable) as exc_info:
            with WorkspaceLock(lock_path):
                pass
    assert exc_info.value.code == "E_LOCK"

