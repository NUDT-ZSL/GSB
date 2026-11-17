"""Versioned, integrity-protected checkpoint persistence."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from .atomic_io import atomic_write_json, read_json
from .config import AuditConfig
from .constants import CHECKPOINT_FORMAT_VERSION, REPORT_SCHEMA_VERSION


@dataclass(frozen=True)
class CheckpointLoad:
    enabled: bool
    resumed: bool
    rebuilt: bool
    reason: str
    reused_sessions: dict[str, dict[str, object]]
    previous_processed_count: int
    previous_watermark: str | None
    previous_complete: bool | None

    def public_dict(self) -> dict[str, object]:
        return {
            "enabled": self.enabled,
            "resumed": self.resumed,
            "rebuilt": self.rebuilt,
            "reason": self.reason,
            "reused_sessions": len(self.reused_sessions),
            "previous_processed_count": self.previous_processed_count,
            "previous_watermark": self.previous_watermark,
            "previous_complete": self.previous_complete,
        }


def _core_without_integrity(document: dict[str, object]) -> dict[str, object]:
    return {key: value for key, value in document.items() if key != "integrity"}


def _integrity_digest(document: dict[str, object]) -> str:
    core = _core_without_integrity(document)
    canonical = json.dumps(core, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _aggregate(results: list[dict[str, object]]) -> dict[str, int]:
    statuses = [str(result.get("status", "fail")) for result in results]
    return {
        "sessions": len(statuses),
        "passed": sum(status == "pass" for status in statuses),
        "degraded_pass": sum(status == "degraded_pass" for status in statuses),
        "failed": sum(status not in {"pass", "degraded_pass"} for status in statuses),
    }


def _empty_load(enabled: bool, reason: str) -> CheckpointLoad:
    return CheckpointLoad(
        enabled=enabled,
        resumed=False,
        rebuilt=False,
        reason=reason,
        reused_sessions={},
        previous_processed_count=0,
        previous_watermark=None,
        previous_complete=None,
    )


def _rebuild_load(
    *,
    reason: str,
    previous_count: int = 0,
    watermark: str | None = None,
    complete: bool | None = None,
) -> CheckpointLoad:
    return CheckpointLoad(
        enabled=True,
        resumed=False,
        rebuilt=True,
        reason=reason,
        reused_sessions={},
        previous_processed_count=previous_count,
        previous_watermark=watermark,
        previous_complete=complete,
    )


def load_checkpoint(
    path: str | Path,
    *,
    config: AuditConfig,
    source_sha256: str,
    source_size: int,
    expected_session_ids: list[str],
) -> CheckpointLoad:
    checkpoint_path = Path(path)
    if not checkpoint_path.exists():
        return _empty_load(True, "checkpoint_absent")
    try:
        raw = read_json(checkpoint_path)
    except (OSError, ValueError, TypeError):
        return _rebuild_load(reason="checkpoint_unreadable")
    if not isinstance(raw, dict):
        return _rebuild_load(reason="checkpoint_not_object")

    previous_count = raw.get("processed_count")
    previous_count_int = previous_count if isinstance(previous_count, int) else 0
    watermark = raw.get("watermark_session_id")
    watermark_value = watermark if isinstance(watermark, str) else None
    complete = raw.get("complete")
    complete_value = complete if isinstance(complete, bool) else None

    if raw.get("checkpoint_format_version") != CHECKPOINT_FORMAT_VERSION:
        return _rebuild_load(
            reason="checkpoint_format_version_changed",
            previous_count=previous_count_int,
            watermark=watermark_value,
            complete=complete_value,
        )
    if raw.get("report_schema_version") != REPORT_SCHEMA_VERSION:
        return _rebuild_load(
            reason="report_schema_version_changed",
            previous_count=previous_count_int,
            watermark=watermark_value,
            complete=complete_value,
        )
    if raw.get("algorithm_policy_version") != config.algorithm_policy_version:
        return _rebuild_load(
            reason="algorithm_policy_version_changed",
            previous_count=previous_count_int,
            watermark=watermark_value,
            complete=complete_value,
        )
    if raw.get("config_fingerprint") != config.policy_fingerprint():
        return _rebuild_load(
            reason="config_policy_changed",
            previous_count=previous_count_int,
            watermark=watermark_value,
            complete=complete_value,
        )

    source = raw.get("source")
    if not isinstance(source, dict) or source.get("sha256") != source_sha256:
        return _rebuild_load(reason="source_changed")
    if source.get("size") != source_size:
        return _rebuild_load(reason="source_changed")

    integrity = raw.get("integrity")
    if not isinstance(integrity, dict) or integrity.get("digest") != _integrity_digest(raw):
        return _rebuild_load(
            reason="checkpoint_integrity_mismatch",
            previous_count=previous_count_int,
            watermark=watermark_value,
            complete=complete_value,
        )

    sessions = raw.get("sessions")
    if not isinstance(sessions, list) or not all(isinstance(item, dict) for item in sessions):
        return _rebuild_load(reason="checkpoint_sessions_invalid")
    session_ids = [item.get("session_id") for item in sessions]
    if not all(isinstance(item, str) for item in session_ids):
        return _rebuild_load(reason="checkpoint_sessions_invalid")
    if session_ids != sorted(session_ids) or len(session_ids) != len(set(session_ids)):
        return _rebuild_load(reason="checkpoint_watermark_invalid")
    if raw.get("processed_count") != len(sessions):
        return _rebuild_load(reason="checkpoint_watermark_invalid")
    expected_prefix = expected_session_ids[: len(session_ids)]
    if session_ids != expected_prefix:
        return _rebuild_load(reason="checkpoint_watermark_invalid")
    expected_watermark = session_ids[-1] if session_ids else None
    if raw.get("watermark_session_id") != expected_watermark:
        return _rebuild_load(reason="checkpoint_watermark_invalid")
    if raw.get("aggregate") != _aggregate(sessions):
        return _rebuild_load(reason="checkpoint_aggregate_invalid")

    reused = {str(item["session_id"]): item for item in sessions}
    return CheckpointLoad(
        enabled=True,
        resumed=bool(reused),
        rebuilt=False,
        reason="checkpoint_resumed" if reused else "checkpoint_empty",
        reused_sessions=reused,
        previous_processed_count=len(reused),
        previous_watermark=expected_watermark,
        previous_complete=bool(complete),
    )


def write_checkpoint(
    path: str | Path,
    *,
    config: AuditConfig,
    source_sha256: str,
    source_size: int,
    session_results: list[dict[str, object]],
    complete: bool,
    lock_path: str | Path,
) -> None:
    watermark = session_results[-1]["session_id"] if session_results else None
    document: dict[str, object] = {
        "checkpoint_format_version": CHECKPOINT_FORMAT_VERSION,
        "report_schema_version": REPORT_SCHEMA_VERSION,
        "algorithm_policy_version": config.algorithm_policy_version,
        "config_fingerprint": config.policy_fingerprint(),
        "source": {
            "sha256": source_sha256,
            "size": source_size,
        },
        "watermark_session_id": watermark,
        "processed_count": len(session_results),
        "aggregate": _aggregate(session_results),
        "sessions": session_results,
        "complete": complete,
    }
    document["integrity"] = {
        "algorithm": "sha256",
        "digest": _integrity_digest(document),
    }
    atomic_write_json(path, document, lock_path=lock_path)

