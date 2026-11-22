"""Core multipart upload domain service."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from .db import (
    StoreDatabase,
    canonical_json,
    checksum_valid,
    sha256_text,
    visible_set_digest,
)
from .errors import DomainError, ErrorCode
from .manifest import (
    Manifest,
    StoreConfig,
    digest_hex,
    normalize_checksum,
    normalize_digest,
)
from .storage import (
    ObjectWriter,
    atomic_write_bytes,
    chunks,
    fault,
    stream_digest,
)

_ACTIVE_PHASES = ("pending_upload", "uploading", "pending_completion")
_FINAL_PHASES = ("completed", "aborted")
_ATTEMPT_ACTIVE = ("prepared", "validated", "materialized")


class MultipartStore:
    def __init__(self, data_dir: str | Path) -> None:
        self.db = StoreDatabase(data_dir)

    def load(self, manifest: Manifest) -> dict[str, Any]:
        with self.db.locked():
            with self.db.connection() as conn:
                existing_hash = self._meta(conn, "manifest_sha256")
                existing_count = conn.execute("SELECT COUNT(*) AS n FROM sessions").fetchone()["n"]
            if existing_hash == manifest.manifest_sha256:
                return {
                    "ok": True,
                    "loaded": False,
                    "idempotent": True,
                    "manifest_sha256": manifest.manifest_sha256,
                    "sessions": len(manifest.sessions),
                    "events": len(manifest.events),
                }
            if existing_hash is not None or existing_count:
                raise DomainError(
                    ErrorCode.STORE_CONFLICT,
                    "data directory already contains a different manifest",
                    {
                        "data_dir": str(self.db.data_dir),
                        "existing_manifest_sha256": existing_hash,
                        "incoming_manifest_sha256": manifest.manifest_sha256,
                    },
                )

            parts_root = self.db.parts_root(manifest.config)
            self._validate_parts_root(parts_root)
            self._ensure_writable_directory(parts_root)
            self._ensure_writable_directory(self.db.staging_root())
            self._ensure_writable_directory(self.db.objects_root())
            now = 0
            with self.db.transaction() as conn:
                self.db.set_meta(conn, "manifest_sha256", manifest.manifest_sha256)
                self.db.set_meta(conn, "config_json", canonical_json(manifest.config.as_dict()))
                self.db.set_meta(conn, "manifest_source", manifest.source_path or "")
                self.db.set_meta(conn, "clock_watermark_ms", "0")
                self.db.set_meta(conn, "round_no", "0")
                for bucket in manifest.buckets:
                    conn.execute(
                        "INSERT INTO buckets(name, created_at_ms) VALUES (?, ?)",
                        (bucket.name, now),
                    )
                objects_by_ref = {
                    (obj.bucket, obj.key): obj for obj in manifest.objects
                }
                for obj in manifest.objects:
                    conn.execute(
                        "INSERT INTO objects("
                        "bucket, object_key, digest_algorithm, expected_digest"
                        ") VALUES (?, ?, ?, ?)",
                        (obj.bucket, obj.key, obj.digest_algorithm, obj.expected_digest),
                    )
                for session in manifest.sessions:
                    obj = objects_by_ref[session.object_ref]
                    conn.execute(
                        "INSERT INTO sessions("
                        "session_id, bucket, object_key, part_size_limit, "
                        "max_concurrency, declared_parts_json, phase, visible_version, "
                        "visible_set_digest, clock_watermark_ms, created_at_ms, updated_at_ms"
                        ") VALUES (?, ?, ?, ?, ?, ?, 'pending_upload', 0, ?, 0, ?, ?)",
                        (
                            session.session_id,
                            session.bucket,
                            session.object_key,
                            session.part_size_limit,
                            session.max_concurrency,
                            canonical_json(list(session.declared_parts)),
                            visible_set_digest(0, []),
                            now,
                            now,
                        ),
                    )
                for event in manifest.events:
                    conn.execute(
                        "INSERT INTO events("
                        "event_id, session_id, part_number, content_base64, checksum, "
                        "logical_time_ms, declared_overwrite, status, disposition, "
                        "is_direct"
                        ") VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', 'pending', 0)",
                        (
                            event.event_id,
                            event.session_id,
                            event.part_number,
                            base64.b64encode(event.content).decode("ascii"),
                            event.checksum,
                            event.logical_time_ms,
                            1 if event.declared_overwrite else 0,
                        ),
                    )
            return {
                "ok": True,
                "loaded": True,
                "idempotent": False,
                "manifest_sha256": manifest.manifest_sha256,
                "buckets": len(manifest.buckets),
                "objects": len(manifest.objects),
                "sessions": len(manifest.sessions),
                "events": len(manifest.events),
                "part_temp_dir": str(parts_root),
            }

    def upload_round(self, round_no: int | None = None) -> dict[str, Any]:
        with self.db.locked():
            config = self.db.require_config()
            with self.db.connection() as conn:
                active_attempt = conn.execute(
                    "SELECT attempt_id, session_id FROM completion_attempts "
                    "WHERE status IN ('prepared', 'validated', 'materialized') "
                    "ORDER BY attempt_id LIMIT 1"
                ).fetchone()
                if active_attempt is not None:
                    raise DomainError(
                        ErrorCode.COMPLETION_PENDING,
                        "a completion decision is pending recovery",
                        {
                            "attempt_id": active_attempt["attempt_id"],
                            "session_id": active_attempt["session_id"],
                            "resolution": "run `resume` before starting another upload round",
                        },
                    )
            self._reset_interrupted_events_locked("upload_round_started")
            with self.db.transaction() as conn:
                if round_no is None:
                    row = conn.execute(
                        "SELECT value FROM meta WHERE key = 'round_no'"
                    ).fetchone()
                    round_no = (int(row["value"]) if row else 0) + 1
                self.db.set_meta(conn, "round_no", str(round_no))
                pending_rows = conn.execute(
                    "SELECT e.*, s.phase AS session_phase, s.max_concurrency "
                    "FROM events e JOIN sessions s ON s.session_id = e.session_id "
                    "WHERE e.status = 'pending' ORDER BY e.event_id"
                ).fetchall()
                active_events: list[Any] = []
                terminal_events: list[Any] = []
                for row in pending_rows:
                    if row["session_phase"] in _FINAL_PHASES:
                        terminal_events.append(row)
                    else:
                        active_events.append(row)

                capacity = config.round_time_budget_ms // config.clock_step_ms
                runnable_active = active_events[:capacity]
                budget_abandoned = active_events[capacity:]
                for row in budget_abandoned:
                    conn.execute(
                        "UPDATE events SET disposition = 'abandoned_budget', "
                        "reason = ?, round_no = ? WHERE event_id = ?",
                        (
                            "round logical-time budget exhausted before start",
                            round_no,
                            row["event_id"],
                        ),
                    )
                selected = runnable_active + terminal_events
                for row in selected:
                    conn.execute(
                        "UPDATE events SET status = 'queued', disposition = 'queued', "
                        "reason = NULL, round_no = ? WHERE event_id = ?",
                        (round_no, row["event_id"]),
                    )
                selected_ids = [int(row["event_id"]) for row in selected]
                abandoned_ids = [int(row["event_id"]) for row in budget_abandoned]
                selected_rows = [
                    conn.execute(
                        "SELECT * FROM events WHERE event_id = ?", (event_id,)
                    ).fetchone()
                    for event_id in selected_ids
                ]

            groups: dict[str, list[int]] = {}
            for row in selected_rows:
                groups.setdefault(str(row["session_id"]), []).append(int(row["event_id"]))

            results: list[dict[str, Any]] = []
            results_lock = threading.Lock()

            def run_group(event_ids: list[int]) -> None:
                for event_id in sorted(event_ids):
                    try:
                        result = self._apply_event_locked(
                            event_id, expected_version=None, round_no=round_no
                        )
                    except DomainError as exc:
                        self._mark_event_failure_locked(event_id, exc, round_no)
                        result = {
                            "event_id": event_id,
                            "status": "failed",
                            "error": exc.to_dict(),
                        }
                    except Exception as exc:
                        internal = DomainError(
                            ErrorCode.INTERNAL_ERROR,
                            "event processing failed unexpectedly",
                            {"event_id": event_id, "cause": str(exc)},
                        )
                        self._mark_event_failure_locked(event_id, internal, round_no)
                        result = {
                            "event_id": event_id,
                            "status": "failed",
                            "error": internal.to_dict(),
                        }
                    with results_lock:
                        results.append(result)

            worker_count = max(1, min(len(groups), 16, os.cpu_count() or 1))
            if groups:
                with ThreadPoolExecutor(max_workers=worker_count) as executor:
                    futures = [
                        executor.submit(run_group, event_ids)
                        for _, event_ids in sorted(groups.items())
                    ]
                    for future in futures:
                        future.result()
            results.sort(key=lambda item: int(item["event_id"]))

            with self.db.connection() as conn:
                selected_final = [
                    self._event_public(conn, event_id) for event_id in selected_ids
                ]
                budget_rows = [
                    self._event_public(conn, event_id) for event_id in abandoned_ids
                ]
            started = sum(1 for item in selected_final if item.get("started"))
            succeeded = sum(1 for item in selected_final if item["status"] == "succeeded")
            failed = sum(1 for item in selected_final if item["status"] == "failed")
            late = sum(1 for item in selected_final if item["status"] == "late")
            return {
                "ok": True,
                "round_no": round_no,
                "round_time_budget_ms": config.round_time_budget_ms,
                "clock_step_ms": config.clock_step_ms,
                "budget_units": capacity,
                "budget_spent_ms": started * config.clock_step_ms,
                "budget_exhausted": bool(budget_abandoned),
                "selected_event_ids": selected_ids,
                "abandoned_budget_event_ids": abandoned_ids,
                "started_count": started,
                "succeeded_count": succeeded,
                "failed_count": failed,
                "late_count": late,
                "not_started_count": sum(
                    1
                    for item in selected_final
                    if item["status"] == "failed" and not item.get("started")
                ),
                "events": selected_final,
                "abandoned_events": budget_rows,
            }

    def upload(
        self,
        session_id: str,
        part_number: int,
        content: bytes,
        checksum: str,
        logical_time_ms: int,
        *,
        overwrite: bool = False,
        event_id: int | None = None,
        expected_version: int | None = None,
        round_no: int | None = None,
    ) -> dict[str, Any]:
        if not isinstance(content, (bytes, bytearray, memoryview)):
            raise DomainError(
                ErrorCode.CONTENT_INVALID,
                "content must be bytes",
                {"type": type(content).__name__},
            )
        content_bytes = bytes(content)
        if not isinstance(logical_time_ms, int) or isinstance(logical_time_ms, bool):
            raise DomainError(
                ErrorCode.LOGICAL_TIME_INVALID,
                "logical_time_ms must be an integer",
                {"logical_time_ms": logical_time_ms},
            )
        if logical_time_ms < 0:
            raise DomainError(
                ErrorCode.LOGICAL_TIME_INVALID,
                "logical_time_ms must be non-negative",
                {"logical_time_ms": logical_time_ms},
            )
        with self.db.locked():
            config = self.db.require_config()
            with self.db.connection() as conn:
                session = self._session_row(conn, session_id)
                obj = self._object_row(conn, session["bucket"], session["object_key"])
            normalized_checksum = normalize_checksum(
                checksum,
                obj["digest_algorithm"],
                config.checksum_policy,
                "upload.checksum",
            )
            base64_content = base64.b64encode(content_bytes).decode("ascii")
            with self.db.transaction() as conn:
                session = self._session_row(conn, session_id)
                if not isinstance(part_number, int) or isinstance(part_number, bool):
                    raise DomainError(
                        ErrorCode.PART_NUMBER_INVALID,
                        "part_number must be an integer",
                        {"part_number": part_number},
                    )
                declared = json.loads(session["declared_parts_json"])
                if part_number not in declared:
                    raise DomainError(
                        ErrorCode.PART_NUMBER_INVALID,
                        "part_number is not declared by the session",
                        {"part_number": part_number, "declared_parts": declared},
                    )
                if len(content_bytes) > session["part_size_limit"]:
                    raise DomainError(
                        ErrorCode.PART_TOO_LARGE,
                        "content exceeds the session part_size_limit",
                        {
                            "size_bytes": len(content_bytes),
                            "part_size_limit": session["part_size_limit"],
                        },
                    )
                if event_id is None:
                    row = conn.execute(
                        "SELECT COALESCE(MAX(event_id), -1) + 1 AS next_id FROM events"
                    ).fetchone()
                    event_id = int(row["next_id"])
                elif not isinstance(event_id, int) or isinstance(event_id, bool) or event_id < 0:
                    raise DomainError(
                        ErrorCode.EVENT_ORDER_INVALID,
                        "event_id must be a non-negative integer",
                        {"event_id": event_id},
                    )
                existing = conn.execute(
                    "SELECT * FROM events WHERE event_id = ?", (event_id,)
                ).fetchone()
                if existing is not None:
                    same_payload = (
                        existing["session_id"] == session_id
                        and int(existing["part_number"]) == part_number
                        and existing["content_base64"] == base64_content
                        and existing["checksum"] == normalized_checksum
                        and int(existing["logical_time_ms"]) == logical_time_ms
                        and bool(existing["declared_overwrite"]) == bool(overwrite)
                    )
                    if not same_payload:
                        raise DomainError(
                            ErrorCode.EVENT_CONFLICT,
                            "event_id already exists with a different payload",
                            {"event_id": event_id},
                        )
                else:
                    conn.execute(
                        "INSERT INTO events("
                        "event_id, session_id, part_number, content_base64, checksum, "
                        "logical_time_ms, declared_overwrite, status, disposition, "
                        "is_direct"
                        ") VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', 'pending', 1)",
                        (
                            event_id,
                            session_id,
                            part_number,
                            base64_content,
                            normalized_checksum,
                            logical_time_ms,
                            1 if overwrite else 0,
                        ),
                    )
            try:
                result = self._apply_event_locked(
                    event_id, expected_version=expected_version, round_no=round_no
                )
            except DomainError as exc:
                self._mark_event_failure_locked(event_id, exc, round_no)
                raise
            result["idempotent"] = bool(result.get("idempotent", False))
            return result

    def complete(
        self,
        session_id: str,
        *,
        expected_version: int | None = None,
        logical_time_ms: int | None = None,
    ) -> dict[str, Any]:
        with self.db.locked():
            if logical_time_ms is not None:
                if (
                    not isinstance(logical_time_ms, int)
                    or isinstance(logical_time_ms, bool)
                    or logical_time_ms < 0
                ):
                    raise DomainError(
                        ErrorCode.LOGICAL_TIME_INVALID,
                        "logical_time_ms must be a non-negative integer",
                        {"logical_time_ms": logical_time_ms},
                    )
            with self.db.connection() as conn:
                session = self._session_row(conn, session_id)
                obj = self._object_row(conn, session["bucket"], session["object_key"])
                active = conn.execute(
                    "SELECT * FROM completion_attempts WHERE session_id = ? "
                    "AND status IN ('prepared', 'validated', 'materialized') "
                    "ORDER BY attempt_id DESC LIMIT 1",
                    (session_id,),
                ).fetchone()
            if session["phase"] == "completed":
                if obj["completed_session_id"] == session_id:
                    return {
                        "ok": True,
                        "session_id": session_id,
                        "bucket": session["bucket"],
                        "object_key": session["object_key"],
                        "status": "completed",
                        "visible_version": session["visible_version"],
                        "decision_version": session["decision_version"],
                        "final_digest": obj["final_digest"],
                        "final_path": obj["final_path"],
                        "idempotent": True,
                    }
                raise DomainError(
                    ErrorCode.SESSION_COMPLETED,
                    "session is already completed",
                    {"session_id": session_id},
                )
            if session["phase"] == "aborted":
                raise DomainError(
                    ErrorCode.SESSION_ABORTED,
                    "aborted session cannot complete",
                    {
                        "session_id": session_id,
                        "trigger": session["aborted_trigger"],
                        "reason": session["aborted_reason"],
                    },
                )
            if active is not None:
                raise DomainError(
                    ErrorCode.COMPLETION_PENDING,
                    "completion attempt already exists for this session",
                    {
                        "attempt_id": active["attempt_id"],
                        "status": active["status"],
                        "resolution": "run `resume`",
                    },
                )
            if obj["completed_session_id"] is not None:
                self._abort_losers_locked(
                    session["bucket"],
                    session["object_key"],
                    winner_session_id=str(obj["completed_session_id"]),
                    winner_version=int(obj["completed_version"] or 0),
                )
                raise DomainError(
                    ErrorCode.OBJECT_ALREADY_COMPLETED,
                    "object already has a completed session",
                    {
                        "bucket": session["bucket"],
                        "object_key": session["object_key"],
                        "completed_session_id": obj["completed_session_id"],
                        "session_id": session_id,
                    },
                )
            if expected_version is not None and expected_version != session["visible_version"]:
                raise DomainError(
                    ErrorCode.VERSION_CONFLICT,
                    "visible set version changed before completion",
                    {
                        "expected_version": expected_version,
                        "actual_version": session["visible_version"],
                        "session_id": session_id,
                    },
                )
            with self.db.connection() as conn:
                visible = conn.execute(
                    "SELECT * FROM visible_parts WHERE session_id = ? ORDER BY part_number",
                    (session_id,),
                ).fetchall()
            listed = {int(row["part_number"]) for row in visible}
            missing = [
                part for part in json.loads(session["declared_parts_json"]) if part not in listed
            ]
            if missing:
                raise DomainError(
                    ErrorCode.PART_MISSING,
                    "not all declared parts have been submitted",
                    {
                        "session_id": session_id,
                        "visible_version": session["visible_version"],
                        "missing_parts": missing,
                        "first_bad_part": missing[0],
                        "expected_digest": obj["expected_digest"],
                        "actual_digest": None,
                    },
                )

            bound_version = int(session["visible_version"])
            now = self.db.current_clock()
            if logical_time_ms is not None:
                now = max(now, logical_time_ms)
            with self.db.transaction() as conn:
                claim = conn.execute(
                    "UPDATE objects SET completion_claim_session_id = ? "
                    "WHERE bucket = ? AND object_key = ? "
                    "AND completed_session_id IS NULL "
                    "AND (completion_claim_session_id IS NULL "
                    "OR completion_claim_session_id = ?)",
                    (
                        session_id,
                        session["bucket"],
                        session["object_key"],
                        session_id,
                    ),
                )
                if claim.rowcount != 1:
                    raise DomainError(
                        ErrorCode.COMPLETION_PENDING,
                        "object completion is already claimed by another session",
                        {
                            "bucket": session["bucket"],
                            "object_key": session["object_key"],
                            "session_id": session_id,
                        },
                    )
                cursor = conn.execute(
                    "INSERT INTO completion_attempts("
                    "session_id, bucket, object_key, bound_version, status, "
                    "expected_digest, created_at_ms, updated_at_ms"
                    ") VALUES (?, ?, ?, ?, 'prepared', ?, ?, ?)",
                    (
                        session_id,
                        session["bucket"],
                        session["object_key"],
                        bound_version,
                        obj["expected_digest"],
                        now,
                        now,
                    ),
                )
                attempt_id = int(cursor.lastrowid)
                if logical_time_ms is not None:
                    self.db.bump_clock(conn, logical_time_ms)
            fault("completion_after_prepare")
            return self._finalize_completion_locked(attempt_id)

    def abort(
        self,
        session_id: str,
        *,
        reason: str = "manual abort",
        trigger_type: str = "manual",
        trigger_source: str = "cli",
        logical_time_ms: int | None = None,
    ) -> dict[str, Any]:
        if not isinstance(reason, str) or not reason.strip():
            raise DomainError(
                ErrorCode.SCHEMA_INVALID,
                "abort reason must be a non-empty string",
                {},
            )
        with self.db.locked():
            now = self.db.current_clock()
            if logical_time_ms is not None:
                if (
                    not isinstance(logical_time_ms, int)
                    or isinstance(logical_time_ms, bool)
                    or logical_time_ms < 0
                ):
                    raise DomainError(
                        ErrorCode.LOGICAL_TIME_INVALID,
                        "logical_time_ms must be a non-negative integer",
                        {"logical_time_ms": logical_time_ms},
                    )
                now = max(now, logical_time_ms)
            with self.db.transaction() as conn:
                session = self._session_row(conn, session_id)
                if session["phase"] == "completed":
                    raise DomainError(
                        ErrorCode.SESSION_COMPLETED,
                        "completed session cannot be aborted",
                        {"session_id": session_id},
                    )
                if session["phase"] == "aborted":
                    return {
                        "ok": True,
                        "session_id": session_id,
                        "status": "aborted",
                        "idempotent": True,
                        "trigger": session["aborted_trigger"],
                        "reason": session["aborted_reason"],
                        "decision_version": session["decision_version"],
                    }
                active = conn.execute(
                    "SELECT attempt_id FROM completion_attempts WHERE session_id = ? "
                    "AND status IN ('prepared', 'validated', 'materialized')",
                    (session_id,),
                ).fetchone()
                if active is not None:
                    raise DomainError(
                        ErrorCode.COMPLETION_PENDING,
                        "cannot abort while a completion decision is pending recovery",
                        {
                            "session_id": session_id,
                            "attempt_id": active["attempt_id"],
                        },
                    )
                trigger = f"{trigger_type}:{trigger_source}"
                conn.execute(
                    "UPDATE sessions SET phase = 'aborted', decision = 'aborted', "
                    "decision_version = ?, decision_at_ms = ?, "
                    "aborted_trigger = ?, aborted_reason = ?, "
                    "updated_at_ms = ?, clock_watermark_ms = ? WHERE session_id = ?",
                    (
                        session["visible_version"],
                        now,
                        trigger,
                        reason,
                        now,
                        max(now, session["clock_watermark_ms"]),
                        session_id,
                    ),
                )
                conn.execute(
                    "INSERT INTO aborts("
                    "session_id, trigger_type, trigger_source, trigger_version, "
                    "reason, created_at_ms"
                    ") VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        session_id,
                        trigger_type,
                        trigger_source,
                        session["visible_version"],
                        reason,
                        now,
                    ),
                )
                conn.execute(
                    "UPDATE events SET status = 'abandoned', "
                    "disposition = 'abandoned_manual', reason = ?, finished_at_ms = ? "
                    "WHERE session_id = ? AND status IN ('pending', 'queued', 'started', 'failed')",
                    (f"session aborted: {reason}", now, session_id),
                )
                if logical_time_ms is not None:
                    self.db.bump_clock(conn, logical_time_ms)
                else:
                    self.db.bump_clock(conn, now)
            return {
                "ok": True,
                "session_id": session_id,
                "status": "aborted",
                "idempotent": False,
                "trigger": trigger,
                "reason": reason,
                "visible_version": session["visible_version"],
                "decision_version": session["visible_version"],
            }

    def resume(self, cleanup: bool = False) -> dict[str, Any]:
        with self.db.locked():
            config = self.db.require_config()
            actions: list[dict[str, Any]] = []
            reset_queued = self._reset_interrupted_events_locked("resume")
            if reset_queued:
                actions.append(
                    {
                        "action": "reset_interrupted_events",
                        "event_ids": sorted(reset_queued),
                    }
                )
            with self.db.connection() as conn:
                attempts = conn.execute(
                    "SELECT attempt_id, session_id, status FROM completion_attempts "
                    "WHERE status IN ('prepared', 'validated', 'materialized') "
                    "ORDER BY attempt_id"
                ).fetchall()
            for row in attempts:
                try:
                    result = self._finalize_completion_locked(int(row["attempt_id"]))
                    actions.append(
                        {
                            "action": "replay_completion",
                            "attempt_id": row["attempt_id"],
                            "session_id": row["session_id"],
                            "result_status": result["status"],
                        }
                    )
                except DomainError as exc:
                    actions.append(
                        {
                            "action": "replay_completion_rejected",
                            "attempt_id": row["attempt_id"],
                            "session_id": row["session_id"],
                            "error": exc.to_dict(),
                        }
                    )

            staging_removed = self._remove_staging_debris_locked()
            if staging_removed:
                actions.append(
                    {
                        "action": "remove_staging_debris",
                        "paths": staging_removed,
                    }
                )
            cleanup_result = (
                self._cleanup_orphans_locked() if cleanup else {"deleted": [], "count": 0}
            )
            with self.db.connection() as conn:
                clock = self.db.current_clock(conn)
                round_row = conn.execute(
                    "SELECT value FROM meta WHERE key = 'round_no'"
                ).fetchone()
            return {
                "ok": True,
                "clock_watermark_ms": clock,
                "round_no": int(round_row["value"]) if round_row else 0,
                "actions": actions,
                "cleanup": cleanup_result,
                "part_temp_dir": str(self.db.parts_root(config)),
            }

    def status(self) -> dict[str, Any]:
        with self.db.locked():
            config = self.db.get_config()
            with self.db.connection() as conn:
                manifest_hash = self._meta(conn, "manifest_sha256")
                clock = self.db.current_clock(conn)
                round_row = conn.execute(
                    "SELECT value FROM meta WHERE key = 'round_no'"
                ).fetchone()
                buckets = [
                    {"name": row["name"]}
                    for row in conn.execute("SELECT name FROM buckets ORDER BY name")
                ]
                objects = []
                for row in conn.execute(
                    "SELECT * FROM objects ORDER BY bucket, object_key"
                ):
                    objects.append(
                        {
                            "bucket": row["bucket"],
                            "object_key": row["object_key"],
                            "digest_algorithm": row["digest_algorithm"],
                            "expected_digest": row["expected_digest"],
                            "status": (
                                "completed"
                                if row["completed_session_id"] is not None
                                else "incomplete"
                            ),
                            "completed_session_id": row["completed_session_id"],
                            "completed_version": row["completed_version"],
                            "final_digest": row["final_digest"],
                            "final_path": row["final_path"],
                            "final_size_bytes": row["final_size"],
                            "completion_claim_session_id": row[
                                "completion_claim_session_id"
                            ],
                        }
                    )
                sessions = []
                for row in conn.execute(
                    "SELECT * FROM sessions ORDER BY session_id"
                ):
                    sid = row["session_id"]
                    visible = []
                    for part in conn.execute(
                        "SELECT * FROM visible_parts WHERE session_id = ? "
                        "ORDER BY part_number",
                        (sid,),
                    ):
                        visible.append(
                            {
                                "part_number": part["part_number"],
                                "version": part["version"],
                                "content_digest": part["content_digest"],
                                "checksum": part["checksum"],
                                "size_bytes": part["size_bytes"],
                                "storage_path": part["storage_path"],
                                "event_id": part["event_id"],
                            }
                        )
                    event_counts = {
                        event_status: count
                        for event_status, count in conn.execute(
                            "SELECT status, COUNT(*) AS n FROM events "
                            "WHERE session_id = ? GROUP BY status ORDER BY status",
                            (sid,),
                        )
                    }
                    disposition_counts = {
                        disposition: count
                        for disposition, count in conn.execute(
                            "SELECT disposition, COUNT(*) AS n FROM events "
                            "WHERE session_id = ? GROUP BY disposition ORDER BY disposition",
                            (sid,),
                        )
                    }
                    late_count = conn.execute(
                        "SELECT COUNT(*) AS n FROM late_uploads WHERE session_id = ?",
                        (sid,),
                    ).fetchone()["n"]
                    overwrite_count = conn.execute(
                        "SELECT COUNT(*) AS n FROM overwrite_records WHERE session_id = ?",
                        (sid,),
                    ).fetchone()["n"]
                    sessions.append(
                        {
                            "session_id": sid,
                            "bucket": row["bucket"],
                            "object_key": row["object_key"],
                            "part_size_limit": row["part_size_limit"],
                            "max_concurrency": row["max_concurrency"],
                            "declared_parts": json.loads(row["declared_parts_json"]),
                            "phase": row["phase"],
                            "visible_version": row["visible_version"],
                            "visible_set_digest": row["visible_set_digest"],
                            "visible_parts": visible,
                            "clock_watermark_ms": row["clock_watermark_ms"],
                            "completion_bound_version": row[
                                "completion_bound_version"
                            ],
                            "decision": row["decision"],
                            "decision_version": row["decision_version"],
                            "decision_at_ms": row["decision_at_ms"],
                            "aborted_trigger": row["aborted_trigger"],
                            "aborted_reason": row["aborted_reason"],
                            "event_counts": event_counts,
                            "disposition_counts": disposition_counts,
                            "late_upload_count": late_count,
                            "overwrite_count": overwrite_count,
                        }
                    )
                events = []
                for row in conn.execute(
                    "SELECT event_id, session_id, part_number, logical_time_ms, "
                    "declared_overwrite, status, disposition, reason, round_no, "
                    "visible_version_before, visible_version_after, started, "
                    "started_at_ms, finished_at_ms, is_direct "
                    "FROM events ORDER BY event_id"
                ):
                    events.append(dict(row))
                late_uploads = [
                    dict(row)
                    for row in conn.execute(
                        "SELECT id, event_id, session_id, part_number, "
                        "logical_time_ms, received_at_ms, reason, decision_phase, "
                        "decision_version, content_digest, size_bytes "
                        "FROM late_uploads ORDER BY id"
                    )
                ]
                completions = [
                    dict(row)
                    for row in conn.execute(
                        "SELECT attempt_id, session_id, bucket, object_key, "
                        "bound_version, status, expected_digest, actual_digest, "
                        "first_bad_part, failure_code, final_path, final_size, "
                        "created_at_ms, updated_at_ms "
                        "FROM completion_attempts ORDER BY attempt_id"
                    )
                ]
                aborts = [
                    dict(row)
                    for row in conn.execute(
                        "SELECT id, session_id, trigger_type, trigger_source, "
                        "trigger_version, reason, created_at_ms FROM aborts ORDER BY id"
                    )
                ]
                overwrite_records = [
                    dict(row)
                    for row in conn.execute(
                        "SELECT id, session_id, part_number, from_version, "
                        "to_version, previous_digest, new_digest, source_event_id, "
                        "same_content, created_at_ms "
                        "FROM overwrite_records ORDER BY id"
                    )
                ]
                recoveries = [
                    {
                        "id": row["id"],
                        "action": row["action"],
                        "detail": json.loads(row["detail_json"]),
                        "created_at_ms": row["created_at_ms"],
                    }
                    for row in conn.execute(
                        "SELECT * FROM recovery_log ORDER BY id"
                    )
                ]
                gc_entries = [
                    dict(row)
                    for row in conn.execute(
                        "SELECT id, path, reason, size_bytes, digest, created_at_ms "
                        "FROM gc_log ORDER BY id"
                    )
                ]
            orphans = self._scan_orphans()
            return {
                "ok": True,
                "store": {
                    "data_dir": str(self.db.data_dir),
                    "db_path": str(self.db.db_path),
                    "manifest_sha256": manifest_hash,
                    "loaded": manifest_hash is not None,
                    "part_temp_dir": (
                        str(self.db.parts_root(config)) if config is not None else None
                    ),
                },
                "config": config.as_dict() if config is not None else None,
                "clock_watermark_ms": clock,
                "round_no": int(round_row["value"]) if round_row else 0,
                "buckets": buckets,
                "objects": objects,
                "sessions": sessions,
                "events": events,
                "late_uploads": late_uploads,
                "completion_attempts": completions,
                "aborts": aborts,
                "overwrite_records": overwrite_records,
                "recovery_log": recoveries,
                "gc_log": gc_entries,
                "orphan_parts": orphans["parts"],
                "staging_debris": orphans["staging"],
            }

    def verify(self, session_id: str | None = None) -> dict[str, Any]:
        with self.db.locked():
            config = self.db.require_config()
            issues: list[dict[str, Any]] = []
            checked_sessions: list[dict[str, Any]] = []
            with self.db.connection() as conn:
                objects = conn.execute(
                    "SELECT * FROM objects ORDER BY bucket, object_key"
                ).fetchall()
                for obj in objects:
                    if obj["completed_session_id"] is None:
                        continue
                    final_path = self.db.resolve_reference(str(obj["final_path"]))
                    if not final_path.exists():
                        issues.append(
                            {
                                "object": f"{obj['bucket']}/{obj['object_key']}",
                                "code": ErrorCode.FINAL_OBJECT_MISSING,
                                "path": str(final_path),
                            }
                        )
                        continue
                    actual, size = stream_digest(final_path, obj["digest_algorithm"])
                    expected = normalize_digest(
                        obj["expected_digest"],
                        obj["digest_algorithm"],
                        "object.expected_digest",
                    )
                    if actual != expected:
                        issues.append(
                            {
                                "object": f"{obj['bucket']}/{obj['object_key']}",
                                "code": ErrorCode.FINAL_OBJECT_MISMATCH,
                                "expected_digest": expected,
                                "actual_digest": actual,
                                "path": str(final_path),
                            }
                        )
                    if size != obj["final_size"]:
                        issues.append(
                            {
                                "object": f"{obj['bucket']}/{obj['object_key']}",
                                "code": ErrorCode.FINAL_OBJECT_MISMATCH,
                                "expected_size": obj["final_size"],
                                "actual_size": size,
                            }
                        )
                if session_id is not None:
                    session_ids = [session_id]
                else:
                    session_ids = [
                        row["session_id"]
                        for row in conn.execute(
                            "SELECT session_id FROM sessions ORDER BY session_id"
                        )
                    ]
                for sid in session_ids:
                    try:
                        report = self._verify_session(conn, sid, config)
                    except DomainError as exc:
                        issues.append({"session_id": sid, **exc.to_dict()})
                        continue
                    checked_sessions.append(report)
                    issues.extend(report["issues"])
            return {
                "ok": True,
                "valid": not issues,
                "issues": issues,
                "checked_sessions": checked_sessions,
                "store": str(self.db.data_dir),
            }

    def _apply_event_locked(
        self,
        event_id: int,
        *,
        expected_version: int | None,
        round_no: int | None,
    ) -> dict[str, Any]:
        config = self.db.require_config()
        with self.db.transaction() as conn:
            event = conn.execute(
                "SELECT * FROM events WHERE event_id = ?", (event_id,)
            ).fetchone()
            if event is None:
                raise DomainError(
                    ErrorCode.SCHEMA_INVALID,
                    "event does not exist",
                    {"event_id": event_id},
                )
            if event["status"] == "succeeded":
                result = json.loads(event["result_json"])
                result["idempotent"] = True
                return result
            if event["status"] == "late":
                result = json.loads(event["result_json"])
                result["idempotent"] = True
                return result
            session = self._session_row(conn, str(event["session_id"]))
            obj = self._object_row(conn, session["bucket"], session["object_key"])
            if session["phase"] in _FINAL_PHASES:
                content = base64.b64decode(event["content_base64"])
                digest = digest_hex(content, obj["digest_algorithm"])
                now = self.db.current_clock(conn)
                reason = (
                    "session completed before this event arrived"
                    if session["phase"] == "completed"
                    else "session aborted before this event arrived"
                )
                cursor = conn.execute(
                    "INSERT INTO late_uploads("
                    "event_id, session_id, part_number, logical_time_ms, "
                    "received_at_ms, reason, decision_phase, decision_version, "
                    "content_digest, size_bytes"
                    ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        event_id,
                        event["session_id"],
                        event["part_number"],
                        event["logical_time_ms"],
                        now,
                        reason,
                        session["phase"],
                        session["visible_version"],
                        digest,
                        len(content),
                    ),
                )
                result = {
                    "ok": False,
                    "event_id": event_id,
                    "session_id": event["session_id"],
                    "part_number": event["part_number"],
                    "status": "late",
                    "disposition": "late_rejected",
                    "reason": reason,
                    "decision_phase": session["phase"],
                    "decision_version": session["visible_version"],
                    "late_upload_id": int(cursor.lastrowid),
                    "idempotent": False,
                }
                conn.execute(
                    "UPDATE events SET status = 'late', disposition = 'late_rejected', "
                    "reason = ?, round_no = COALESCE(?, round_no), "
                    "visible_version_before = ?, visible_version_after = ?, "
                    "result_json = ?, finished_at_ms = ? WHERE event_id = ?",
                    (
                        reason,
                        round_no,
                        session["visible_version"],
                        session["visible_version"],
                        canonical_json(result),
                        now,
                        event_id,
                    ),
                )
                self.db.bump_clock(conn, int(event["logical_time_ms"]))
                return result
            if obj["completion_claim_session_id"] is not None:
                raise DomainError(
                    ErrorCode.COMPLETION_PENDING,
                    "object has a pending completion claim",
                    {
                        "object": f"{session['bucket']}/{session['object_key']}",
                        "claim_session_id": obj["completion_claim_session_id"],
                    },
                )
            if event["logical_time_ms"] < (
                session["clock_watermark_ms"] - config.clock_tolerance_ms
            ):
                raise DomainError(
                    ErrorCode.CLOCK_REGRESSION,
                    "logical clock moved backwards beyond tolerance",
                    {
                        "session_id": session["session_id"],
                        "event_id": event_id,
                        "clock_watermark_ms": session["clock_watermark_ms"],
                        "event_logical_time_ms": event["logical_time_ms"],
                        "tolerance_ms": config.clock_tolerance_ms,
                    },
                )
            if (
                expected_version is not None
                and expected_version != session["visible_version"]
            ):
                raise DomainError(
                    ErrorCode.VERSION_CONFLICT,
                    "visible set version changed before upload commit",
                    {
                        "session_id": session["session_id"],
                        "expected_version": expected_version,
                        "actual_version": session["visible_version"],
                    },
                )
            current = conn.execute(
                "SELECT * FROM visible_parts WHERE session_id = ? AND part_number = ?",
                (session["session_id"], event["part_number"]),
            ).fetchone()
            content = base64.b64decode(event["content_base64"])
            if len(content) > session["part_size_limit"]:
                raise DomainError(
                    ErrorCode.PART_TOO_LARGE,
                    "event content exceeds part_size_limit",
                    {
                        "event_id": event_id,
                        "size_bytes": len(content),
                        "part_size_limit": session["part_size_limit"],
                    },
                )
            event_started_at = event["logical_time_ms"]
            conn.execute(
                "UPDATE events SET status = 'started', disposition = 'started', "
                "reason = NULL, round_no = COALESCE(?, round_no), started = 1, "
                "started_at_ms = ?, visible_version_before = ? WHERE event_id = ?",
                (
                    round_no,
                    event_started_at,
                    session["visible_version"],
                    event_id,
                ),
            )
            session_clock = max(
                int(session["clock_watermark_ms"]), int(event["logical_time_ms"])
            )
            conn.execute(
                "UPDATE sessions SET clock_watermark_ms = ?, updated_at_ms = ? "
                "WHERE session_id = ?",
                (session_clock, event["logical_time_ms"], session["session_id"]),
            )
            self.db.bump_clock(conn, int(event["logical_time_ms"]))

        digest = digest_hex(content, obj["digest_algorithm"])
        version_before = int(session["visible_version"])
        version_after = version_before + 1
        same_content = current is not None and current["content_digest"] == digest
        actual_overwrite = current is not None
        new_file_created = False
        if same_content:
            storage_path = current["storage_path"]
        else:
            parts_root = self.db.parts_root(config)
            session_dir = parts_root / sha256_text(session["session_id"])[:20]
            final_path = session_dir / (
                f"part-{int(event['part_number']):08d}-v{version_after}-"
                f"{digest[:16]}.bin"
            )
            atomic_write_bytes(self.db.staging_root(), final_path, content)
            new_file_created = True
            storage_path = self.db.storage_reference(final_path)
            fault("part_after_rename")

        try:
            with self.db.transaction() as conn:
                session = self._session_row(conn, session["session_id"])
                current = conn.execute(
                    "SELECT * FROM visible_parts WHERE session_id = ? AND part_number = ?",
                    (session["session_id"], event["part_number"]),
                ).fetchone()
                if session["phase"] in _FINAL_PHASES:
                    raise DomainError(
                        ErrorCode.VERSION_CONFLICT,
                        "session reached a final decision while upload was in flight",
                        {
                            "session_id": session["session_id"],
                            "phase": session["phase"],
                        },
                    )
                if obj["completion_claim_session_id"] is not None:
                    raise DomainError(
                        ErrorCode.COMPLETION_PENDING,
                        "object completion was claimed while upload was in flight",
                        {
                            "claim_session_id": obj[
                                "completion_claim_session_id"
                            ]
                        },
                    )
                if int(session["visible_version"]) != version_before:
                    raise DomainError(
                        ErrorCode.VERSION_CONFLICT,
                        "visible set version changed during upload",
                        {
                            "session_id": session["session_id"],
                            "expected_version": version_before,
                            "actual_version": session["visible_version"],
                        },
                    )
                same_content = (
                    current is not None and current["content_digest"] == digest
                )
                if same_content:
                    storage_path = current["storage_path"]
                conn.execute(
                    "INSERT INTO part_versions("
                    "session_id, part_number, version, content_digest, checksum, "
                    "checksum_algorithm, size_bytes, storage_path, event_id, "
                    "logical_time_ms, declared_overwrite, actual_overwrite, "
                    "previous_version, previous_digest, reused_existing_file, "
                    "acknowledged_by_digest, created_at_ms"
                    ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)",
                    (
                        session["session_id"],
                        event["part_number"],
                        version_after,
                        digest,
                        event["checksum"],
                        obj["digest_algorithm"],
                        len(content),
                        storage_path,
                        event_id,
                        event["logical_time_ms"],
                        1 if event["declared_overwrite"] else 0,
                        1 if actual_overwrite else 0,
                        current["version"] if current is not None else None,
                        current["content_digest"] if current is not None else None,
                        1 if same_content else 0,
                        event["logical_time_ms"],
                    ),
                )
                conn.execute(
                    "INSERT INTO visible_parts("
                    "session_id, part_number, version, content_digest, checksum, "
                    "checksum_algorithm, size_bytes, storage_path, event_id"
                    ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(session_id, part_number) DO UPDATE SET "
                    "version = excluded.version, "
                    "content_digest = excluded.content_digest, "
                    "checksum = excluded.checksum, "
                    "checksum_algorithm = excluded.checksum_algorithm, "
                    "size_bytes = excluded.size_bytes, "
                    "storage_path = excluded.storage_path, "
                    "event_id = excluded.event_id",
                    (
                        session["session_id"],
                        event["part_number"],
                        version_after,
                        digest,
                        event["checksum"],
                        obj["digest_algorithm"],
                        len(content),
                        storage_path,
                        event_id,
                    ),
                )
                if current is not None:
                    conn.execute(
                        "INSERT INTO overwrite_records("
                        "session_id, part_number, from_version, to_version, "
                        "previous_digest, new_digest, source_event_id, same_content, "
                        "created_at_ms"
                        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            session["session_id"],
                            event["part_number"],
                            current["version"],
                            version_after,
                            current["content_digest"],
                            digest,
                            event_id,
                            1 if same_content else 0,
                            event["logical_time_ms"],
                        ),
                    )
                visible_rows = conn.execute(
                    "SELECT * FROM visible_parts WHERE session_id = ?",
                    (session["session_id"],),
                ).fetchall()
                declared = set(json.loads(session["declared_parts_json"]))
                present = {int(row["part_number"]) for row in visible_rows}
                new_phase = (
                    "pending_completion" if declared <= present else "uploading"
                )
                set_digest = visible_set_digest(version_after, visible_rows)
                conn.execute(
                    "UPDATE sessions SET visible_version = ?, phase = ?, "
                    "visible_set_digest = ?, updated_at_ms = ? WHERE session_id = ?",
                    (
                        version_after,
                        new_phase,
                        set_digest,
                        event["logical_time_ms"],
                        session["session_id"],
                    ),
                )
                result = {
                    "ok": True,
                    "event_id": event_id,
                    "session_id": session["session_id"],
                    "part_number": event["part_number"],
                    "status": "succeeded",
                    "disposition": "started_ok",
                    "visible_version_before": version_before,
                    "visible_version_after": version_after,
                    "visible_set_digest": set_digest,
                    "phase": new_phase,
                    "content_digest": digest,
                    "checksum": event["checksum"],
                    "size_bytes": len(content),
                    "actual_overwrite": actual_overwrite,
                    "same_content": same_content,
                    "reused_existing_file": same_content,
                    "storage_path": storage_path,
                    "idempotent": False,
                }
                conn.execute(
                    "UPDATE events SET status = 'succeeded', "
                    "disposition = 'started_ok', reason = NULL, "
                    "visible_version_after = ?, finished_at_ms = ?, result_json = ? "
                    "WHERE event_id = ?",
                    (
                        version_after,
                        event["logical_time_ms"],
                        canonical_json(result),
                        event_id,
                    ),
                )
                return result
        except BaseException:
            if new_file_created:
                try:
                    Path(
                        self.db.resolve_reference(storage_path)
                    ).unlink(missing_ok=True)
                except OSError:
                    pass
            raise

    def _mark_event_failure_locked(
        self, event_id: int, error: DomainError, round_no: int | None
    ) -> None:
        with self.db.transaction() as conn:
            row = conn.execute(
                "SELECT status, started FROM events WHERE event_id = ?", (event_id,)
            ).fetchone()
            if row is None or row["status"] in {"succeeded", "late"}:
                return
            disposition = "started_failed" if row["started"] else "not_started"
            conn.execute(
                "UPDATE events SET status = 'failed', disposition = ?, reason = ?, "
                "round_no = COALESCE(?, round_no), result_json = ?, "
                "finished_at_ms = ? WHERE event_id = ?",
                (
                    disposition,
                    error.code,
                    round_no,
                    canonical_json(error.to_dict()),
                    self.db.current_clock(conn),
                    event_id,
                ),
            )

    def _finalize_completion_locked(self, attempt_id: int) -> dict[str, Any]:
        config = self.db.require_config()
        with self.db.connection() as conn:
            attempt = conn.execute(
                "SELECT * FROM completion_attempts WHERE attempt_id = ?",
                (attempt_id,),
            ).fetchone()
            if attempt is None:
                raise DomainError(
                    ErrorCode.SCHEMA_INVALID,
                    "completion attempt does not exist",
                    {"attempt_id": attempt_id},
                )
            if attempt["status"] == "committed":
                obj = self._object_row(
                    conn, attempt["bucket"], attempt["object_key"]
                )
                return {
                    "ok": True,
                    "session_id": attempt["session_id"],
                    "bucket": attempt["bucket"],
                    "object_key": attempt["object_key"],
                    "status": "completed",
                    "visible_version": attempt["bound_version"],
                    "final_digest": obj["final_digest"],
                    "final_path": obj["final_path"],
                    "attempt_id": attempt_id,
                    "idempotent": True,
                }
            if attempt["status"] in {"rejected", "failed"}:
                details = (
                    json.loads(attempt["failure_json"])
                    if attempt["failure_json"]
                    else {}
                )
                raise DomainError(
                    str(attempt["failure_code"] or ErrorCode.INTERNAL_ERROR),
                    "completion attempt was already rejected",
                    details,
                )
            session = self._session_row(conn, str(attempt["session_id"]))
            obj = self._object_row(conn, attempt["bucket"], attempt["object_key"])
            if session["phase"] == "aborted":
                self._reject_attempt_locked(
                    attempt_id,
                    ErrorCode.SESSION_ABORTED,
                    {
                        "session_id": session["session_id"],
                        "reason": session["aborted_reason"],
                    },
                    first_bad_part=None,
                )
            if session["phase"] == "completed":
                self._reject_attempt_locked(
                    attempt_id,
                    ErrorCode.SESSION_COMPLETED,
                    {"session_id": session["session_id"]},
                    first_bad_part=None,
                )
            if int(session["visible_version"]) != int(attempt["bound_version"]):
                self._reject_attempt_locked(
                    attempt_id,
                    ErrorCode.VERSION_CONFLICT,
                    {
                        "session_id": session["session_id"],
                        "bound_version": attempt["bound_version"],
                        "actual_version": session["visible_version"],
                    },
                    first_bad_part=None,
                )
            if obj["completion_claim_session_id"] != session["session_id"]:
                self._reject_attempt_locked(
                    attempt_id,
                    ErrorCode.COMPLETION_PENDING,
                    {
                        "object": f"{attempt['bucket']}/{attempt['object_key']}",
                        "expected_claim": session["session_id"],
                        "actual_claim": obj["completion_claim_session_id"],
                    },
                    first_bad_part=None,
                )
            visible = conn.execute(
                "SELECT * FROM visible_parts WHERE session_id = ? ORDER BY part_number",
                (session["session_id"],),
            ).fetchall()
        listed = {int(row["part_number"]) for row in visible}
        declared = [int(part) for part in json.loads(session["declared_parts_json"])]
        missing = [part for part in declared if part not in listed]
        if missing:
            self._reject_attempt_locked(
                attempt_id,
                ErrorCode.PART_MISSING,
                {
                    "session_id": session["session_id"],
                    "missing_parts": missing,
                    "first_bad_part": missing[0],
                    "expected_digest": attempt["expected_digest"],
                    "actual_digest": None,
                    "visible_version": session["visible_version"],
                },
                first_bad_part=missing[0],
            )

        for part in visible:
            part_number = int(part["part_number"])
            path = self.db.resolve_reference(str(part["storage_path"]))
            try:
                actual_digest, actual_size = stream_digest(
                    path, str(part["checksum_algorithm"])
                )
            except DomainError as exc:
                self._reject_attempt_locked(
                    attempt_id,
                    ErrorCode.PART_FILE_MISSING,
                    {
                        "session_id": session["session_id"],
                        "part_number": part_number,
                        "first_bad_part": part_number,
                        "expected_checksum": part["checksum"],
                        "expected_digest": attempt["expected_digest"],
                        "actual_digest": None,
                        "cause": exc.details,
                    },
                    first_bad_part=part_number,
                )
            expected_ok = checksum_valid(
                str(part["checksum"]),
                str(part["checksum_algorithm"]),
                actual_digest,
                config,
            )
            if (
                not expected_ok
                or actual_digest != part["content_digest"]
                or actual_size != int(part["size_bytes"])
            ):
                self._reject_attempt_locked(
                    attempt_id,
                    ErrorCode.CHECKSUM_MISMATCH,
                    {
                        "session_id": session["session_id"],
                        "part_number": part_number,
                        "first_bad_part": part_number,
                        "expected_checksum": part["checksum"],
                        "actual_digest": actual_digest,
                        "stored_content_digest": part["content_digest"],
                        "expected_size_bytes": part["size_bytes"],
                        "actual_size_bytes": actual_size,
                    },
                    first_bad_part=part_number,
                )

        final_path = self._final_object_path(
            str(attempt["bucket"]), str(attempt["object_key"])
        )
        writer = ObjectWriter(
            self.db.staging_root(),
            final_path,
            str(obj["digest_algorithm"]),
        )
        try:
            with writer:
                for part in visible:
                    for chunk in chunks(
                        self.db.resolve_reference(str(part["storage_path"]))
                    ):
                        writer.write(chunk)
                actual_digest, _ = writer.finish()
        except BaseException:
            writer.discard()
            raise
        expected_digest = normalize_digest(
            str(attempt["expected_digest"]),
            str(obj["digest_algorithm"]),
            "object.expected_digest",
        )
        if actual_digest != expected_digest:
            writer.discard()
            self._reject_attempt_locked(
                attempt_id,
                ErrorCode.DIGEST_MISMATCH,
                {
                    "session_id": session["session_id"],
                    "first_bad_part": 0,
                    "expected_digest": expected_digest,
                    "actual_digest": actual_digest,
                    "digest_algorithm": obj["digest_algorithm"],
                    "bound_version": attempt["bound_version"],
                },
                first_bad_part=0,
            )

        final_reference = self.db.storage_reference(final_path)
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE completion_attempts SET status = 'validated', "
                "actual_digest = ?, final_path = ?, final_size = ?, "
                "updated_at_ms = ? WHERE attempt_id = ?",
                (
                    actual_digest,
                    final_reference,
                    writer.size_bytes,
                    self.db.current_clock(conn),
                    attempt_id,
                ),
            )
        fault("completion_after_validation")
        writer.materialize()
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE completion_attempts SET status = 'materialized', "
                "updated_at_ms = ? WHERE attempt_id = ?",
                (self.db.current_clock(conn), attempt_id),
            )
        fault("completion_after_materialize")

        with self.db.transaction() as conn:
            current_session = self._session_row(conn, str(attempt["session_id"]))
            current_obj = self._object_row(
                conn, str(attempt["bucket"]), str(attempt["object_key"])
            )
            if int(current_session["visible_version"]) != int(attempt["bound_version"]):
                raise DomainError(
                    ErrorCode.VERSION_CONFLICT,
                    "visible version changed before final completion commit",
                    {
                        "session_id": current_session["session_id"],
                        "bound_version": attempt["bound_version"],
                        "actual_version": current_session["visible_version"],
                    },
                )
            if (
                current_obj["completion_claim_session_id"]
                != current_session["session_id"]
            ):
                raise DomainError(
                    ErrorCode.COMPLETION_PENDING,
                    "completion claim was lost before final commit",
                    {
                        "session_id": current_session["session_id"],
                        "actual_claim": current_obj["completion_claim_session_id"],
                    },
                )
            now = self.db.current_clock(conn)
            conn.execute(
                "UPDATE sessions SET phase = 'completed', decision = 'completed', "
                "decision_version = ?, decision_at_ms = ?, completion_bound_version = ?, "
                "visible_version = ?, updated_at_ms = ? WHERE session_id = ?",
                (
                    attempt["bound_version"],
                    now,
                    attempt["bound_version"],
                    attempt["bound_version"],
                    now,
                    current_session["session_id"],
                ),
            )
            conn.execute(
                "UPDATE objects SET completion_claim_session_id = NULL, "
                "completed_session_id = ?, completed_version = ?, "
                "final_digest = ?, final_path = ?, final_size = ?, "
                "completed_at_ms = ? WHERE bucket = ? AND object_key = ?",
                (
                    current_session["session_id"],
                    attempt["bound_version"],
                    actual_digest,
                    final_reference,
                    writer.size_bytes,
                    now,
                    attempt["bucket"],
                    attempt["object_key"],
                ),
            )
            conn.execute(
                "UPDATE completion_attempts SET status = 'committed', "
                "actual_digest = ?, final_path = ?, final_size = ?, "
                "updated_at_ms = ? WHERE attempt_id = ?",
                (
                    actual_digest,
                    final_reference,
                    writer.size_bytes,
                    now,
                    attempt_id,
                ),
            )
            aborted = self._abort_losers_in_connection(
                conn,
                str(attempt["bucket"]),
                str(attempt["object_key"]),
                winner_session_id=str(current_session["session_id"]),
                winner_version=int(attempt["bound_version"]),
            )
        fault("completion_after_commit")
        return {
            "ok": True,
            "session_id": current_session["session_id"],
            "bucket": current_session["bucket"],
            "object_key": current_session["object_key"],
            "status": "completed",
            "visible_version": attempt["bound_version"],
            "decision_version": attempt["bound_version"],
            "bound_version": attempt["bound_version"],
            "visible_set_digest": current_session["visible_set_digest"],
            "expected_digest": expected_digest,
            "actual_digest": actual_digest,
            "final_digest": actual_digest,
            "final_path": final_reference,
            "final_size_bytes": writer.size_bytes,
            "aborted_sessions": aborted,
            "attempt_id": attempt_id,
            "idempotent": False,
        }

    def _reject_attempt_locked(
        self,
        attempt_id: int,
        code: str,
        details: dict[str, Any],
        *,
        first_bad_part: int | None,
    ) -> None:
        with self.db.transaction() as conn:
            attempt = conn.execute(
                "SELECT * FROM completion_attempts WHERE attempt_id = ?",
                (attempt_id,),
            ).fetchone()
            if attempt is None:
                raise DomainError(ErrorCode.INTERNAL_ERROR, "attempt disappeared", {})
            conn.execute(
                "UPDATE completion_attempts SET status = 'rejected', "
                "failure_code = ?, failure_json = ?, first_bad_part = ?, "
                "updated_at_ms = ? WHERE attempt_id = ?",
                (
                    code,
                    canonical_json(details),
                    first_bad_part,
                    self.db.current_clock(conn),
                    attempt_id,
                ),
            )
            conn.execute(
                "UPDATE objects SET completion_claim_session_id = NULL "
                "WHERE bucket = ? AND object_key = ? "
                "AND completion_claim_session_id = ?",
                (
                    attempt["bucket"],
                    attempt["object_key"],
                    attempt["session_id"],
                ),
            )
            obj = conn.execute(
                "SELECT completed_session_id FROM objects "
                "WHERE bucket = ? AND object_key = ?",
                (attempt["bucket"], attempt["object_key"]),
            ).fetchone()
            cleanup_path = (
                str(attempt["final_path"])
                if attempt["final_path"] and obj["completed_session_id"] is None
                else None
            )
        if cleanup_path is not None:
            try:
                self.db.resolve_reference(cleanup_path).unlink(missing_ok=True)
            except OSError:
                pass
        messages = {
            ErrorCode.PART_MISSING: "declared parts are missing",
            ErrorCode.PART_FILE_MISSING: "a visible part file is missing",
            ErrorCode.CHECKSUM_MISMATCH: "a part checksum does not match its content",
            ErrorCode.DIGEST_MISMATCH: "concatenated object digest does not match declaration",
            ErrorCode.VERSION_CONFLICT: "visible set version no longer matches decision",
            ErrorCode.SESSION_ABORTED: "session was aborted",
            ErrorCode.SESSION_COMPLETED: "session was already completed",
            ErrorCode.COMPLETION_PENDING: "completion claim no longer belongs to session",
        }
        raise DomainError(
            code,
            messages.get(code, "completion attempt was rejected"),
            details,
        )

    def _abort_losers_locked(
        self,
        bucket: str,
        object_key: str,
        *,
        winner_session_id: str,
        winner_version: int,
    ) -> list[str]:
        with self.db.transaction() as conn:
            return self._abort_losers_in_connection(
                conn,
                bucket,
                object_key,
                winner_session_id=winner_session_id,
                winner_version=winner_version,
            )

    def _abort_losers_in_connection(
        self,
        conn: Any,
        bucket: str,
        object_key: str,
        *,
        winner_session_id: str,
        winner_version: int,
    ) -> list[str]:
        aborted: list[str] = []
        now = self.db.current_clock(conn)
        rows = conn.execute(
            "SELECT session_id, visible_version, phase FROM sessions "
            "WHERE bucket = ? AND object_key = ? AND session_id != ? "
            "ORDER BY session_id",
            (bucket, object_key, winner_session_id),
        ).fetchall()
        for row in rows:
            if row["phase"] in {"completed", "aborted"}:
                continue
            trigger = f"session:{winner_session_id}"
            reason = "another session completed the object"
            conn.execute(
                "UPDATE sessions SET phase = 'aborted', decision = 'aborted', "
                "decision_version = ?, decision_at_ms = ?, "
                "aborted_trigger = ?, aborted_reason = ?, "
                "updated_at_ms = ? WHERE session_id = ?",
                (
                    row["visible_version"],
                    now,
                    trigger,
                    reason,
                    now,
                    row["session_id"],
                ),
            )
            conn.execute(
                "INSERT INTO aborts("
                "session_id, trigger_type, trigger_source, trigger_version, "
                "reason, created_at_ms"
                ") VALUES (?, 'session_completed', ?, ?, ?, ?)",
                (
                    row["session_id"],
                    winner_session_id,
                    winner_version,
                    reason,
                    now,
                ),
            )
            aborted.append(str(row["session_id"]))
        return aborted

    def _reset_interrupted_events_locked(self, reason: str) -> list[int]:
        with self.db.transaction() as conn:
            rows = conn.execute(
                "SELECT event_id FROM events WHERE status IN ('queued', 'started') "
                "ORDER BY event_id"
            ).fetchall()
            ids = [int(row["event_id"]) for row in rows]
            for event_id in ids:
                conn.execute(
                    "UPDATE events SET status = 'pending', "
                    "disposition = 'recovered_pending', reason = ?, started = 0, "
                    "started_at_ms = NULL "
                    "WHERE event_id = ?",
                    (reason, event_id),
                )
            if ids:
                conn.execute(
                    "INSERT INTO recovery_log(action, detail_json, created_at_ms) "
                    "VALUES ('reset_interrupted_events', ?, ?)",
                    (
                        canonical_json({"event_ids": ids, "reason": reason}),
                        self.db.current_clock(conn),
                    ),
                )
            return ids

    def _remove_staging_debris_locked(self) -> list[str]:
        removed: list[str] = []
        staging = self.db.staging_root()
        if not staging.exists():
            return removed
        with self.db.transaction() as conn:
            now = self.db.current_clock(conn)
            for path in sorted(staging.iterdir()):
                if not path.is_file():
                    continue
                reference = self.db.storage_reference(path)
                try:
                    path.unlink()
                    removed.append(reference)
                    conn.execute(
                        "INSERT INTO recovery_log(action, detail_json, created_at_ms) "
                        "VALUES ('remove_staging_debris', ?, ?)",
                        (canonical_json({"path": reference}), now),
                    )
                except OSError:
                    continue
        return removed

    def _cleanup_orphans_locked(self) -> dict[str, Any]:
        scan = self._scan_orphans()
        deleted: list[dict[str, Any]] = []
        with self.db.transaction() as conn:
            now = self.db.current_clock(conn)
            for item in scan["parts"]:
                path = Path(item["absolute_path"])
                try:
                    path.unlink()
                except OSError:
                    continue
                conn.execute(
                    "INSERT INTO gc_log(path, reason, size_bytes, digest, created_at_ms) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (
                        item["path"],
                        item["reason"],
                        item["size_bytes"],
                        item["sha256"],
                        now,
                    ),
                )
                deleted.append(
                    {
                        "path": item["path"],
                        "reason": item["reason"],
                        "size_bytes": item["size_bytes"],
                        "sha256": item["sha256"],
                    }
                )
        return {"count": len(deleted), "deleted": deleted}

    def _scan_orphans(self) -> dict[str, list[dict[str, Any]]]:
        config = self.db.get_config()
        if config is None:
            return {"parts": [], "staging": []}
        with self.db.connection() as conn:
            referenced = {
                str(row["storage_path"])
                for row in conn.execute(
                    "SELECT v.storage_path FROM visible_parts v "
                    "JOIN sessions s ON s.session_id = v.session_id "
                    "WHERE s.phase != 'aborted'"
                )
            }
            aborted_refs = {
                str(row["storage_path"])
                for row in conn.execute(
                    "SELECT v.storage_path FROM visible_parts v "
                    "JOIN sessions s ON s.session_id = v.session_id "
                    "WHERE s.phase = 'aborted'"
                )
            }
            historical_refs = {
                str(row["storage_path"])
                for row in conn.execute("SELECT storage_path FROM part_versions")
            }
        parts_root = self.db.parts_root(config)
        part_orphans: list[dict[str, Any]] = []
        if parts_root.exists():
            for path in sorted(parts_root.rglob("*")):
                if not path.is_file():
                    continue
                reference = self.db.storage_reference(path)
                if reference in referenced:
                    continue
                if reference in aborted_refs:
                    reason = "aborted_session_unreferenced"
                elif reference in historical_refs:
                    reason = "superseded_version_unreferenced"
                else:
                    reason = "unreferenced_part_file"
                try:
                    digest, size = stream_digest(path, "sha256")
                except DomainError:
                    continue
                part_orphans.append(
                    {
                        "path": reference,
                        "absolute_path": str(path.resolve()),
                        "size_bytes": size,
                        "sha256": digest,
                        "reason": reason,
                    }
                )
        staging_orphans: list[dict[str, Any]] = []
        staging = self.db.staging_root()
        if staging.exists():
            for path in sorted(staging.iterdir()):
                if not path.is_file():
                    continue
                try:
                    digest, size = stream_digest(path, "sha256")
                except DomainError:
                    continue
                staging_orphans.append(
                    {
                        "path": self.db.storage_reference(path),
                        "absolute_path": str(path.resolve()),
                        "size_bytes": size,
                        "sha256": digest,
                        "reason": "staging_debris",
                    }
                )
        return {"parts": part_orphans, "staging": staging_orphans}

    def _verify_session(
        self, conn: Any, session_id: str, config: StoreConfig
    ) -> dict[str, Any]:
        session = self._session_row(conn, session_id)
        obj = self._object_row(conn, session["bucket"], session["object_key"])
        issues: list[dict[str, Any]] = []
        visible = conn.execute(
            "SELECT * FROM visible_parts WHERE session_id = ? ORDER BY part_number",
            (session_id,),
        ).fetchall()
        declared = [int(part) for part in json.loads(session["declared_parts_json"])]
        listed = {int(row["part_number"]) for row in visible}
        missing = [part for part in declared if part not in listed]
        if missing:
            issues.append(
                {
                    "session_id": session_id,
                    "code": ErrorCode.PART_MISSING,
                    "first_bad_part": missing[0],
                    "missing_parts": missing,
                    "expected_digest": obj["expected_digest"],
                    "actual_digest": None,
                }
            )
        digest = hashlib.new(str(obj["digest_algorithm"]))
        for part in visible:
            path = self.db.resolve_reference(str(part["storage_path"]))
            try:
                actual, size = stream_digest(path, str(part["checksum_algorithm"]))
            except DomainError as exc:
                issues.append(
                    {
                        "session_id": session_id,
                        "code": ErrorCode.PART_FILE_MISSING,
                        "first_bad_part": part["part_number"],
                        "part_number": part["part_number"],
                        "expected_checksum": part["checksum"],
                        "actual_digest": None,
                        "cause": exc.details,
                    }
                )
                continue
            if (
                not checksum_valid(
                    str(part["checksum"]),
                    str(part["checksum_algorithm"]),
                    actual,
                    config,
                )
                or actual != part["content_digest"]
                or size != part["size_bytes"]
            ):
                issues.append(
                    {
                        "session_id": session_id,
                        "code": ErrorCode.CHECKSUM_MISMATCH,
                        "first_bad_part": part["part_number"],
                        "part_number": part["part_number"],
                        "expected_checksum": part["checksum"],
                        "actual_digest": actual,
                    }
                )
            if not missing:
                for chunk in chunks(path):
                    digest.update(chunk)
        actual_object_digest = digest.hexdigest() if not missing else None
        if not missing and actual_object_digest != obj["expected_digest"]:
            issues.append(
                {
                    "session_id": session_id,
                    "code": ErrorCode.DIGEST_MISMATCH,
                    "first_bad_part": 0,
                    "expected_digest": obj["expected_digest"],
                    "actual_digest": actual_object_digest,
                }
            )
        report = {
            "session_id": session_id,
            "bucket": session["bucket"],
            "object_key": session["object_key"],
            "phase": session["phase"],
            "visible_version": session["visible_version"],
            "declared_parts": declared,
            "visible_parts": sorted(listed),
            "expected_digest": obj["expected_digest"],
            "actual_digest": actual_object_digest,
            "valid": not any(issue["session_id"] == session_id for issue in issues),
            "issues": [issue for issue in issues if issue["session_id"] == session_id],
        }
        return report

    def _session_row(self, conn: Any, session_id: str) -> Any:
        row = conn.execute(
            "SELECT * FROM sessions WHERE session_id = ?", (session_id,)
        ).fetchone()
        if row is None:
            raise DomainError(
                ErrorCode.UNKNOWN_SESSION,
                "session does not exist",
                {"session_id": session_id},
            )
        return row

    def _object_row(self, conn: Any, bucket: str, object_key: str) -> Any:
        row = conn.execute(
            "SELECT * FROM objects WHERE bucket = ? AND object_key = ?",
            (bucket, object_key),
        ).fetchone()
        if row is None:
            raise DomainError(
                ErrorCode.UNKNOWN_OBJECT,
                "object does not exist",
                {"bucket": bucket, "object_key": object_key},
            )
        return row

    def _event_public(self, conn: Any, event_id: int) -> dict[str, Any]:
        row = conn.execute(
            "SELECT event_id, session_id, part_number, logical_time_ms, "
            "declared_overwrite, status, disposition, reason, round_no, "
            "visible_version_before, visible_version_after, started, "
            "started_at_ms, finished_at_ms, is_direct, result_json "
            "FROM events WHERE event_id = ?",
            (event_id,),
        ).fetchone()
        if row is None:
            return {"event_id": event_id, "status": "missing"}
        data = dict(row)
        data["declared_overwrite"] = bool(data["declared_overwrite"])
        data["started"] = bool(data["started"])
        data["is_direct"] = bool(data["is_direct"])
        result_json = data.pop("result_json")
        data["result"] = json.loads(result_json) if result_json else None
        return data

    def _final_object_path(self, bucket: str, object_key: str) -> Path:
        name = sha256_text(f"{bucket}\0{object_key}")
        return self.db.objects_root() / name[:2] / f"{name}.bin"

    def _meta(self, conn: Any, key: str) -> str | None:
        row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    @staticmethod
    def _ensure_writable_directory(path: Path) -> None:
        try:
            path.mkdir(parents=True, exist_ok=True)
            probe = path / f".probe-{os.getpid()}-{uuid.uuid4().hex}"
            probe.write_bytes(b"ok")
            probe.unlink()
        except OSError as exc:
            raise DomainError(
                ErrorCode.STORAGE_DIR_INVALID,
                "runtime directory is not writable",
                {"path": str(path), "cause": str(exc)},
            ) from exc

    def _validate_parts_root(self, parts_root: Path) -> None:
        data_dir = self.db.data_dir
        staging = self.db.staging_root()
        objects = self.db.objects_root()
        forbidden = {
            "data directory": data_dir,
            "staging directory": staging,
            "object directory": objects,
        }
        for label, path in forbidden.items():
            if parts_root == path or data_dir.is_relative_to(parts_root):
                raise DomainError(
                    ErrorCode.STORAGE_DIR_INVALID,
                    f"part_temp_dir must be a dedicated directory, not overlap the {label}",
                    {"part_temp_dir": str(parts_root), "conflict": str(path)},
                )
        if parts_root.is_relative_to(staging) or parts_root.is_relative_to(objects):
            raise DomainError(
                ErrorCode.STORAGE_DIR_INVALID,
                "part_temp_dir must not be nested inside staging or object directories",
                {"part_temp_dir": str(parts_root)},
            )
