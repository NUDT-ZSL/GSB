from __future__ import annotations

import json

import pytest

from klp_audit.atomic_io import FileLock, atomic_write_json
from klp_audit.config import AuditConfig
from klp_audit.engine import AuditEngine
from klp_audit.errors import KlpLockError

from .helpers import load_json


class SimulatedTermination(Exception):
    pass


def _config() -> AuditConfig:
    return AuditConfig(
        concurrency=1,
        session_timeout_seconds=None,
        checkpoint_interval=1,
        scale_bytes_threshold=10_000_000,
        scale_derived_threshold=10_000,
    )


def _sample(generated_root, manifest):
    return generated_root / manifest["files"]["checkpoint"]["sample"]


def test_forced_termination_then_resume_from_watermark(
    generated_root, manifest, tmp_path
):
    sample = _sample(generated_root, manifest)
    checkpoint = tmp_path / "checkpoint.json"
    workspace = tmp_path / "workspace"

    def terminate_after_first_checkpoint(snapshot):
        if not snapshot["complete"]:
            raise SimulatedTermination("simulated forced termination")

    engine = AuditEngine(
        _config(),
        checkpoint_path=checkpoint,
        workspace=workspace,
        on_checkpoint=terminate_after_first_checkpoint,
    )
    with pytest.raises(SimulatedTermination):
        engine.audit_file(sample)

    saved = load_json(checkpoint)
    assert saved["complete"] is False
    assert saved["processed_count"] == 1
    assert saved["watermark_session_id"] == "ck-00"

    resumed = AuditEngine(
        _config(),
        checkpoint_path=checkpoint,
        workspace=workspace,
    ).audit_file(sample)
    assert resumed["checkpoint"]["resumed"] is True
    assert resumed["checkpoint"]["reused_sessions"] == 1
    assert resumed["checkpoint"]["previous_watermark"] == "ck-00"
    assert resumed["checkpoint"]["current_processed_count"] == 6
    assert resumed["file"]["conclusion"] == "PASS"
    assert all(session["status"] == "pass" for session in resumed["sessions"])

    final = load_json(checkpoint)
    assert final["complete"] is True
    assert final["processed_count"] == 6
    assert final["watermark_session_id"] == "ck-05"


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        (
            "checkpoint_format_version",
            999,
            "checkpoint_format_version_changed",
        ),
        (
            "algorithm_policy_version",
            "legacy-algorithm-policy",
            "algorithm_policy_version_changed",
        ),
    ],
)
def test_version_change_rebuilds_instead_of_mixing_old_results(
    generated_root, manifest, tmp_path, field, value, reason
):
    sample = _sample(generated_root, manifest)
    checkpoint = tmp_path / "checkpoint.json"
    workspace = tmp_path / "workspace"
    AuditEngine(
        _config(), checkpoint_path=checkpoint, workspace=workspace
    ).audit_file(sample)

    raw = load_json(checkpoint)
    raw[field] = value
    raw["sessions"][0]["status"] = "fail"
    raw["sessions"][0]["error_codes"] = ["E_OLD_POLICY"]
    checkpoint.write_text(json.dumps(raw, sort_keys=True), encoding="utf-8")

    report = AuditEngine(
        _config(), checkpoint_path=checkpoint, workspace=workspace
    ).audit_file(sample)
    assert report["checkpoint"]["rebuilt"] is True
    assert report["checkpoint"]["reason"] == reason
    assert report["checkpoint"]["reused_sessions"] == 0
    assert report["file"]["conclusion"] == "PASS"
    assert all(session["status"] == "pass" for session in report["sessions"])
    assert "E_OLD_POLICY" not in report["summary"]["business_error_codes"]


def test_integrity_mismatch_rebuilds(generated_root, manifest, tmp_path):
    sample = _sample(generated_root, manifest)
    checkpoint = tmp_path / "checkpoint.json"
    workspace = tmp_path / "workspace"
    AuditEngine(
        _config(), checkpoint_path=checkpoint, workspace=workspace
    ).audit_file(sample)
    raw = load_json(checkpoint)
    raw["sessions"][0]["status"] = "degraded_pass"
    checkpoint.write_text(json.dumps(raw, sort_keys=True), encoding="utf-8")
    report = AuditEngine(
        _config(), checkpoint_path=checkpoint, workspace=workspace
    ).audit_file(sample)
    assert report["checkpoint"]["rebuilt"] is True
    assert report["checkpoint"]["reason"] == "checkpoint_integrity_mismatch"


def test_lock_conflict_is_e_lock_and_destination_is_untouched(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    lock_path = workspace / ".klp-workspace.lock"
    destination = workspace / "report.json"
    destination.write_text("original", encoding="utf-8")
    with FileLock(lock_path):
        with pytest.raises(KlpLockError) as exc_info:
            atomic_write_json(destination, {"new": True}, lock_path=lock_path)
    assert exc_info.value.code == "E_LOCK"
    assert destination.read_text(encoding="utf-8") == "original"
    assert not list(workspace.glob("*.tmp"))

