"""Filesystem + SQLite workspace for the deterministic capability ledger."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

from .atomicio import atomic_write_json
from .canonical import canonical_sha256
from .capabilities import (
    CAPABILITY_WHITELIST_VERSION,
    CHECKPOINT_FORMAT_VERSION,
    MERGE_POLICY_VERSION,
)
from .closure import build_closure_report
from .codes import (
    E_ARG,
    E_BATCH_CONFLICT,
    E_CHECKPOINT_CORRUPT,
    E_CHECKPOINT_MISSING,
    E_JSON,
    E_LEDGER_DIGEST,
    E_LOCK,
    E_NOT_FOUND,
    E_OBS_CLOCK,
    E_POLICY_MISMATCH,
    E_VERSION_REBUILD,
    OK,
)
from .errors import GuardError
from .locking import FileLock
from .merge import build_unified_view
from .parsing import (
    load_json_text,
    parse_patch_batch,
    parse_policy,
    patch_reference_rejections,
)


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS batches (
    batch_id TEXT PRIMARY KEY,
    batch_hash TEXT NOT NULL,
    logical_watermark INTEGER NOT NULL,
    first_seq INTEGER NOT NULL,
    last_seq INTEGER NOT NULL,
    payload_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS operations (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id TEXT NOT NULL,
    op_id TEXT NOT NULL,
    op_index INTEGER NOT NULL,
    fact_id TEXT NOT NULL UNIQUE,
    canonical_key TEXT NOT NULL,
    op_kind TEXT NOT NULL,
    logical_watermark INTEGER NOT NULL,
    generation INTEGER,
    effective_at_ms INTEGER,
    canonical_json TEXT NOT NULL,
    UNIQUE(batch_id, op_id)
);
CREATE TABLE IF NOT EXISTS checkpoints (
    checkpoint_id TEXT PRIMARY KEY,
    checkpoint_seq INTEGER NOT NULL,
    apply_watermark INTEGER NOT NULL,
    ledger_digest TEXT NOT NULL,
    complete INTEGER NOT NULL DEFAULT 1,
    payload_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_operations_seq ON operations(seq);
CREATE INDEX IF NOT EXISTS idx_checkpoints_seq ON checkpoints(checkpoint_seq);
"""


class Workspace:
    def __init__(self, root: str | Path, *, lock_timeout_ms: int = 0) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.db_path = self.root / "guard.db"
        self.snapshot_path = self.root / "checkpoint.json"
        self.lock_path = self.root / ".guard.lock"
        self.lock_timeout_ms = lock_timeout_ms
        with self._connect() as conn:
            conn.executescript(SCHEMA_SQL)
            conn.commit()

    def _connect(self):
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        return closing(conn)

    def _lock(self, timeout_ms: int | None = None) -> FileLock:
        return FileLock(
            self.lock_path,
            self.lock_timeout_ms if timeout_ms is None else timeout_ms,
        )

    @staticmethod
    def _meta_get(conn: sqlite3.Connection, key: str) -> str | None:
        row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return None if row is None else str(row["value"])

    @staticmethod
    def _meta_set(conn: sqlite3.Connection, key: str, value: str) -> None:
        conn.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    def _stored_versions(self, conn: sqlite3.Connection) -> dict[str, Any]:
        raw = self._meta_get(conn, "versions_json")
        if raw:
            return json.loads(raw)
        return {
            "checkpoint_format_version": self._meta_get(conn, "checkpoint_format_version"),
            "capability_whitelist_version": self._meta_get(
                conn, "capability_whitelist_version"
            ),
            "merge_policy_version": self._meta_get(conn, "merge_policy_version"),
        }

    def _supported_versions(self, policy: dict[str, Any]) -> dict[str, Any]:
        declared = policy.get("versions", {}) if isinstance(policy, dict) else {}
        current = {
            "checkpoint_format_version": CHECKPOINT_FORMAT_VERSION,
            "capability_whitelist_version": CAPABILITY_WHITELIST_VERSION,
            "merge_policy_version": MERGE_POLICY_VERSION,
        }
        for key, expected in current.items():
            if key in declared and declared[key] != expected:
                raise GuardError(
                    E_VERSION_REBUILD,
                    f"{key} changed; the workspace must be rebuilt from source inputs",
                    details={
                        "field": key,
                        "declared": declared[key],
                        "supported": expected,
                        "rebuild_required": True,
                    },
                )
        return current

    def is_initialized(self) -> bool:
        with self._connect() as conn:
            return self._meta_get(conn, "policy_json") is not None

    def initialize_policy(
        self,
        policy: dict[str, Any],
        *,
        force: bool = False,
    ) -> dict[str, Any]:
        if not isinstance(policy, dict):
            raise GuardError(E_ARG, "policy must be a parsed JSON object")
        versions = self._supported_versions(policy)
        policy_digest = canonical_sha256(policy)
        with self._lock():
            with self._connect() as conn:
                existing = self._meta_get(conn, "policy_json")
                if existing is not None:
                    existing_digest = self._meta_get(conn, "policy_digest")
                    if existing_digest == policy_digest:
                        return {
                            "status": "already_initialized",
                            "policy_digest": policy_digest,
                        }
                    if not force:
                        raise GuardError(
                            E_POLICY_MISMATCH,
                            "workspace already contains a different policy; use --rebuild",
                            details={"existing_digest": existing_digest},
                        )

                with conn:
                    self._meta_set(conn, "policy_json", json.dumps(policy, sort_keys=True))
                    self._meta_set(conn, "policy_digest", policy_digest)
                    self._meta_set(conn, "versions_json", json.dumps(versions, sort_keys=True))
                    for key, value in versions.items():
                        self._meta_set(conn, key, json.dumps(value))
                    checkpoint = self._insert_checkpoint_locked(
                        conn, policy, reason="initialize"
                    )
                atomic_write_json(self.snapshot_path, checkpoint)
                return {
                    "status": "initialized",
                    "policy_digest": policy_digest,
                    "checkpoint_id": checkpoint["checkpoint_id"],
                }

    def initialize_policy_file(self, path: str | Path) -> dict[str, Any]:
        payload, error = self._read_json_file(path)
        if error:
            return {"status": "rejected", "rejections": [error]}
        policy = parse_policy(payload)
        result = self.initialize_policy(policy)
        result["policy_rejections"] = policy["rejections"]
        return result

    @staticmethod
    def _read_json_file(path: str | Path) -> tuple[Any, dict[str, Any] | None]:
        try:
            text = Path(path).read_text(encoding="utf-8")
        except OSError as exc:
            return None, {
                "code": E_NOT_FOUND,
                "source": "file",
                "index": -1,
                "message": f"cannot read JSON input: {exc}",
                "path": str(path),
            }
        return load_json_text(text)

    def _load_policy(self, conn: sqlite3.Connection) -> dict[str, Any]:
        raw = self._meta_get(conn, "policy_json")
        if raw is None:
            raise GuardError(
                E_CHECKPOINT_MISSING,
                "workspace has no policy ledger; pass --policy for the first import",
            )
        return json.loads(raw)

    def list_operations(self, conn: sqlite3.Connection | None = None) -> list[dict[str, Any]]:
        if conn is not None:
            rows = conn.execute(
                "SELECT canonical_json FROM operations ORDER BY seq ASC"
            ).fetchall()
            return [json.loads(row["canonical_json"]) for row in rows]
        with self._connect() as local_conn:
            rows = local_conn.execute(
                "SELECT canonical_json FROM operations ORDER BY seq ASC"
            ).fetchall()
            return [json.loads(row["canonical_json"]) for row in rows]

    @staticmethod
    def _stable_operations(operations: list[dict[str, Any]]) -> list[dict[str, Any]]:
        stable: list[dict[str, Any]] = []
        for operation in operations:
            item = {
                key: value
                for key, value in operation.items()
                if key not in {"seq", "apply_watermark"}
            }
            stable.append(item)
        stable.sort(key=lambda item: str(item["fact_id"]))
        return stable

    def ledger_digest(
        self,
        policy: dict[str, Any],
        operations: list[dict[str, Any]],
    ) -> str:
        return canonical_sha256(
            {
                "policy_digest": canonical_sha256(policy),
                "operations": self._stable_operations(operations),
            }
        )

    def _insert_checkpoint_locked(
        self,
        conn: sqlite3.Connection,
        policy: dict[str, Any],
        *,
        reason: str,
    ) -> dict[str, Any]:
        seq_row = conn.execute("SELECT COALESCE(MAX(seq), 0) AS seq FROM operations").fetchone()
        checkpoint_seq = int(seq_row["seq"])
        rows = conn.execute(
            "SELECT canonical_json FROM operations WHERE seq <= ? ORDER BY seq ASC",
            (checkpoint_seq,),
        ).fetchall()
        operations = [json.loads(row["canonical_json"]) for row in rows]
        digest = self.ledger_digest(policy, operations)

        batch_rows = conn.execute(
            "SELECT batch_id, logical_watermark FROM batches ORDER BY batch_id ASC"
        ).fetchall()
        applied_batches = sorted(row["batch_id"] for row in batch_rows)
        applied_watermarks = sorted(int(row["logical_watermark"]) for row in batch_rows)
        versions = self._stored_versions(conn)
        try:
            view = build_unified_view(
                policy,
                operations,
                at_ms=policy.get("observation_clock_ms"),
            )
            unified_view: dict[str, Any] | None = {
                "evaluation_clock_ms": view["evaluation_clock_ms"],
                "evaluation_complete": view["evaluation_complete"],
                "conflicts": view["conflicts"],
                "grants": view["grants"],
                "processes": view["processes"],
            }
        except GuardError:
            unified_view = None

        checkpoint_id = f"cp-{checkpoint_seq}"
        payload = {
            "kind": "sandbox_guard_checkpoint",
            "checkpoint_id": checkpoint_id,
            "reason": reason,
            **versions,
            "checkpoint_seq": checkpoint_seq,
            "apply_watermark": checkpoint_seq,
            "logical_max_watermark": max(applied_watermarks, default=0),
            "applied_batches": applied_batches,
            "applied_watermarks": applied_watermarks,
            "operation_count": len(operations),
            "ledger_digest": digest,
            "unified_view": unified_view,
        }
        payload_json = json.dumps(payload, sort_keys=True, ensure_ascii=False)
        conn.execute(
            "INSERT INTO checkpoints(checkpoint_id, checkpoint_seq, apply_watermark, "
            "ledger_digest, complete, payload_json) VALUES(?, ?, ?, ?, 1, ?) "
            "ON CONFLICT(checkpoint_id) DO UPDATE SET "
            "checkpoint_seq=excluded.checkpoint_seq, apply_watermark=excluded.apply_watermark, "
            "ledger_digest=excluded.ledger_digest, complete=1, payload_json=excluded.payload_json",
            (
                checkpoint_id,
                checkpoint_seq,
                checkpoint_seq,
                digest,
                payload_json,
            ),
        )
        return payload

    def create_checkpoint(self, timeout_ms: int | None = None) -> dict[str, Any]:
        with self._lock(timeout_ms):
            with self._connect() as conn:
                policy = self._load_policy(conn)
                with conn:
                    payload = self._insert_checkpoint_locked(
                        conn, policy, reason="manual"
                    )
            atomic_write_json(self.snapshot_path, payload)
            return payload

    def import_batch_payload(
        self,
        payload: Any,
        *,
        default_batch_id: str | None = None,
        timeout_ms: int | None = None,
        extra_resolvable_grant_keys: set[str] | None = None,
    ) -> dict[str, Any]:
        parsed = parse_patch_batch(payload, default_batch_id=default_batch_id)

        with self._lock(timeout_ms):
            with self._connect() as conn:
                policy = self._load_policy(conn)
                batch_id = parsed["batch_id"]
                operations = self.list_operations(conn)
                known_grants = {item["canonical_key"] for item in policy.get("grants", [])}
                known_grants.update(op["canonical_key"] for op in operations)
                known_grants.update(extra_resolvable_grant_keys or set())
                known_profiles = {
                    item["canonical_key"]
                    for item in policy.get("profiles", [])
                    if item.get("valid", True)
                }
                reference_rejections = patch_reference_rejections(
                    parsed,
                    known_grants,
                    known_profiles,
                )
                rejections = list(parsed["rejections"]) + reference_rejections
                if not parsed["ok"] or reference_rejections:
                    return {
                        "status": "rejected",
                        "batch_id": parsed.get("batch_id"),
                        "watermark": parsed.get("watermark"),
                        "rejections": sorted(
                            rejections,
                            key=lambda item: (
                                str(item.get("source", "")),
                                int(item.get("index", -1)),
                                str(item.get("code", "")),
                                str(item.get("canonical_key", "")),
                            ),
                        ),
                        "applied_operations": [],
                    }

                batch_hash = canonical_sha256(payload)
                existing = conn.execute(
                    "SELECT * FROM batches WHERE batch_id=?",
                    (batch_id,),
                ).fetchone()
                if existing is not None:
                    if existing["batch_hash"] == batch_hash:
                        ops = self.list_operations(conn)
                        matching = [op for op in ops if op.get("batch_id") == batch_id]
                        return {
                            "status": "duplicate",
                            "batch_id": batch_id,
                            "watermark": parsed["watermark"],
                            "rejections": [],
                            "applied_operations": [
                                {
                                    "op_id": op["op_id"],
                                    "canonical_key": op["canonical_key"],
                                    "apply_watermark": op.get("apply_watermark", op.get("seq")),
                                    "logical_watermark": op["logical_watermark"],
                                }
                                for op in matching
                            ],
                        }
                    rejection_item = {
                        "code": E_BATCH_CONFLICT,
                        "source": "patch",
                        "index": -1,
                        "message": "batch_id was already applied with different content",
                        "canonical_key": batch_id,
                    }
                    return {
                        "status": "rejected",
                        "batch_id": batch_id,
                        "rejections": [rejection_item],
                        "applied_operations": [],
                    }

                applied: list[dict[str, Any]] = []
                with conn:
                    first_seq: int | None = None
                    last_seq = 0
                    for operation in parsed["operations"]:
                        canonical_before = json.dumps(operation, sort_keys=True)
                        cursor = conn.execute(
                            "INSERT INTO operations(batch_id, op_id, op_index, fact_id, "
                            "canonical_key, op_kind, logical_watermark, generation, "
                            "effective_at_ms, canonical_json) "
                            "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                            (
                                batch_id,
                                operation["op_id"],
                                operation["op_index"],
                                operation["fact_id"],
                                operation["canonical_key"],
                                operation["op_kind"],
                                parsed["watermark"],
                                operation["generation"],
                                operation["effective_at_ms"],
                                canonical_before,
                            ),
                        )
                        seq = int(cursor.lastrowid)
                        if first_seq is None:
                            first_seq = seq
                        last_seq = seq
                        operation["seq"] = seq
                        operation["apply_watermark"] = seq
                        operation["logical_watermark"] = parsed["watermark"]
                        conn.execute(
                            "UPDATE operations SET canonical_json=? WHERE seq=?",
                            (json.dumps(operation, sort_keys=True), seq),
                        )
                        applied.append(
                            {
                                "op_id": operation["op_id"],
                                "canonical_key": operation["canonical_key"],
                                "op_kind": operation["op_kind"],
                                "apply_watermark": seq,
                                "logical_watermark": parsed["watermark"],
                            }
                        )

                    conn.execute(
                        "INSERT INTO batches(batch_id, batch_hash, logical_watermark, "
                        "first_seq, last_seq, payload_json) VALUES(?, ?, ?, ?, ?, ?)",
                        (
                            batch_id,
                            batch_hash,
                            parsed["watermark"],
                            first_seq or last_seq,
                            last_seq,
                            json.dumps(payload, sort_keys=True),
                        ),
                    )
                    checkpoint = self._insert_checkpoint_locked(
                        conn, policy, reason=f"batch:{batch_id}"
                    )

                atomic_write_json(self.snapshot_path, checkpoint)
                return {
                    "status": "applied",
                    "batch_id": batch_id,
                    "watermark": parsed["watermark"],
                    "rejections": [],
                    "applied_operations": applied,
                    "checkpoint_id": checkpoint["checkpoint_id"],
                    "ledger_digest": checkpoint["ledger_digest"],
                }

    def import_batch_file(
        self,
        path: str | Path,
        *,
        timeout_ms: int | None = None,
        extra_resolvable_grant_keys: set[str] | None = None,
    ) -> dict[str, Any]:
        path = Path(path)
        payload, error = self._read_json_file(path)
        if error is not None:
            return {
                "status": "rejected",
                "batch_id": path.stem,
                "path": str(path),
                "rejections": [error],
                "applied_operations": [],
            }
        result = self.import_batch_payload(
            payload,
            default_batch_id=path.stem,
            timeout_ms=timeout_ms,
            extra_resolvable_grant_keys=extra_resolvable_grant_keys,
        )
        result["path"] = str(path)
        return result

    def unified_view(self, at_ms: int | None = None) -> dict[str, Any]:
        with self._connect() as conn:
            policy = self._load_policy(conn)
            operations = self.list_operations(conn)
        evaluation_clock = at_ms
        if evaluation_clock is None:
            evaluation_clock = policy.get(
                "default_evaluation_clock_ms",
                policy.get("observation_clock_ms"),
            )
        return build_unified_view(policy, operations, at_ms=evaluation_clock)

    def closure_report(
        self,
        *,
        explicit_keys: list[str] | None = None,
        include_all_events: bool = False,
        at_ms: int | None = None,
    ) -> dict[str, Any]:
        with self._connect() as conn:
            policy = self._load_policy(conn)
            operations = self.list_operations(conn)
        evaluation_clock = at_ms
        if evaluation_clock is None:
            evaluation_clock = policy.get(
                "default_evaluation_clock_ms",
                policy.get("observation_clock_ms"),
            )
        view = build_unified_view(policy, operations, at_ms=evaluation_clock)
        return build_closure_report(
            policy,
            operations,
            view,
            explicit_keys=explicit_keys,
            include_all_events=include_all_events,
        )

    def query_process(self, selector: str, at_ms: int | None = None) -> dict[str, Any]:
        view = self.unified_view(at_ms=at_ms)
        process_map = view["process_map"]
        matches: list[dict[str, Any]] = []
        if selector in process_map:
            matches.append(process_map[selector])
        elif selector.isdigit() and f"process:{selector}" in process_map:
            matches.append(process_map[f"process:{selector}"])
        else:
            profile_key = selector if selector.startswith("profile:") else f"profile:{selector}"
            matches = [
                item
                for item in view["processes"]
                if item["profile_key"] == profile_key
            ]
        if not matches:
            return {
                "ok": False,
                "code": E_NOT_FOUND,
                "message": "no in-ledger process or profile matches selector",
                "selector": selector,
                "evaluation_clock_ms": view["evaluation_clock_ms"],
            }
        return {
            "ok": True,
            "code": OK,
            "selector": selector,
            "evaluation_clock_ms": view["evaluation_clock_ms"],
            "processes": matches,
            "conflicts": view["conflicts"],
            "grants": view["grants"],
        }

    def _residue_files(self) -> list[Path]:
        return sorted(self.root.glob(".checkpoint.json.tmp-*"))

    def verify_checkpoint(self, *, repair: bool = False) -> dict[str, Any]:
        if repair:
            return self.recover()

        result: dict[str, Any] = {
            "ok": True,
            "code": OK,
            "workspace": str(self.root),
            "checkpoint_path": str(self.snapshot_path),
            "residue_files": [path.name for path in self._residue_files()],
            "checks": {},
        }
        if result["residue_files"]:
            result["ok"] = False
            result["code"] = E_CHECKPOINT_CORRUPT

        if not self.db_path.exists():
            result.update(ok=False, code=E_CHECKPOINT_MISSING)
            return result

        with self._connect() as conn:
            if self._meta_get(conn, "policy_json") is None:
                result.update(ok=False, code=E_CHECKPOINT_MISSING)
                return result
            versions = self._stored_versions(conn)
            result["versions"] = versions
            version_ok = all(
                versions.get(key) == value
                for key, value in {
                    "checkpoint_format_version": CHECKPOINT_FORMAT_VERSION,
                    "capability_whitelist_version": CAPABILITY_WHITELIST_VERSION,
                    "merge_policy_version": MERGE_POLICY_VERSION,
                }.items()
            )
            result["checks"]["versions"] = version_ok
            if not version_ok:
                result.update(ok=False, code=E_VERSION_REBUILD, rebuild_required=True)
                return result

            row = conn.execute(
                "SELECT * FROM checkpoints WHERE complete=1 "
                "ORDER BY checkpoint_seq DESC LIMIT 1"
            ).fetchone()
            if row is None:
                result.update(ok=False, code=E_CHECKPOINT_MISSING)
                return result
            policy = self._load_policy(conn)
            rows = conn.execute(
                "SELECT canonical_json FROM operations WHERE seq <= ? ORDER BY seq",
                (row["checkpoint_seq"],),
            ).fetchall()
            operations = [json.loads(item["canonical_json"]) for item in rows]
            digest = self.ledger_digest(policy, operations)
            digest_ok = digest == row["ledger_digest"]
            result["checks"]["sqlite_digest"] = digest_ok
            result["checkpoint_id"] = row["checkpoint_id"]
            result["ledger_digest"] = digest
            if not digest_ok:
                result.update(ok=False, code=E_LEDGER_DIGEST)

        snapshot_ok = False
        snapshot_error: str | None = None
        snapshot_payload: dict[str, Any] | None = None
        if self.snapshot_path.exists():
            try:
                snapshot_payload = json.loads(self.snapshot_path.read_text(encoding="utf-8"))
                snapshot_ok = (
                    isinstance(snapshot_payload, dict)
                    and snapshot_payload.get("ledger_digest") == digest
                    and snapshot_payload.get("checkpoint_id") == row["checkpoint_id"]
                )
                if snapshot_payload and not all(
                    snapshot_payload.get(key) == value
                    for key, value in versions.items()
                ):
                    snapshot_ok = False
                    snapshot_error = E_VERSION_REBUILD
            except (OSError, json.JSONDecodeError) as exc:
                snapshot_error = str(exc)
        else:
            snapshot_error = "snapshot missing"
        result["checks"]["snapshot_file"] = snapshot_ok
        if not snapshot_ok and result["code"] == OK:
            result["code"] = (
                E_VERSION_REBUILD if snapshot_error == E_VERSION_REBUILD else E_SNAPSHOT_CORRUPT
            )
            result["ok"] = False
            result["snapshot_error"] = snapshot_error
        return result

    def recover(self, timeout_ms: int | None = None) -> dict[str, Any]:
        with self._lock(timeout_ms):
            residue: list[dict[str, Any]] = []
            for path in self._residue_files():
                size = path.stat().st_size if path.exists() else 0
                residue.append({"path": path.name, "size": size, "action": "removed_temp"})
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass

            with self._connect() as conn:
                if self._meta_get(conn, "policy_json") is None:
                    return {
                        "ok": True,
                        "code": OK,
                        "status": "empty",
                        "residue_files": residue,
                    }
                versions = self._stored_versions(conn)
                current = {
                    "checkpoint_format_version": CHECKPOINT_FORMAT_VERSION,
                    "capability_whitelist_version": CAPABILITY_WHITELIST_VERSION,
                    "merge_policy_version": MERGE_POLICY_VERSION,
                }
                if versions != current:
                    raise GuardError(
                        E_VERSION_REBUILD,
                        "ledger versions do not match this build; rebuild from source inputs",
                        details={"stored": versions, "current": current},
                    )

                policy = self._load_policy(conn)
                rows = conn.execute(
                    "SELECT * FROM checkpoints ORDER BY checkpoint_seq DESC"
                ).fetchall()
                chosen: sqlite3.Row | None = None
                chosen_payload: dict[str, Any] | None = None
                invalid_checkpoints: list[dict[str, Any]] = []
                for row in rows:
                    try:
                        payload = json.loads(row["payload_json"])
                    except json.JSONDecodeError:
                        invalid_checkpoints.append(
                            {"checkpoint_id": row["checkpoint_id"], "reason": "payload_json"}
                        )
                        continue
                    op_rows = conn.execute(
                        "SELECT canonical_json FROM operations WHERE seq <= ? ORDER BY seq",
                        (row["checkpoint_seq"],),
                    ).fetchall()
                    operations = [json.loads(item["canonical_json"]) for item in op_rows]
                    digest = self.ledger_digest(policy, operations)
                    if digest != row["ledger_digest"] or digest != payload.get("ledger_digest"):
                        invalid_checkpoints.append(
                            {"checkpoint_id": row["checkpoint_id"], "reason": "digest_mismatch"}
                        )
                        continue
                    if any(payload.get(k) != v for k, v in current.items()):
                        raise GuardError(
                            E_VERSION_REBUILD,
                            "checkpoint versions changed; rebuild required",
                            details={"checkpoint_id": row["checkpoint_id"]},
                        )
                    chosen = row
                    chosen_payload = payload
                    break

                if chosen is None or chosen_payload is None:
                    raise GuardError(
                        E_CHECKPOINT_MISSING,
                        "no complete SQLite checkpoint could be validated",
                        details={"invalid_checkpoints": invalid_checkpoints},
                    )

                later_rows = conn.execute(
                    "SELECT canonical_json FROM operations WHERE seq > ? ORDER BY seq",
                    (chosen["checkpoint_seq"],),
                ).fetchall()
                replayed_operations = []
                for row in later_rows:
                    operation = json.loads(row["canonical_json"])
                    replayed_operations.append(operation["fact_id"])
                all_operations = self.list_operations(conn)
                build_unified_view(
                    policy,
                    all_operations,
                    at_ms=policy.get("observation_clock_ms"),
                )
                head_seq_row = conn.execute(
                    "SELECT COALESCE(MAX(seq), 0) AS seq FROM operations"
                ).fetchone()
                head_seq = int(head_seq_row["seq"])
                replayed = len(replayed_operations)
                final_payload = chosen_payload
                checkpoint_advanced = False
                if head_seq > int(chosen["checkpoint_seq"]):
                    with conn:
                        final_payload = self._insert_checkpoint_locked(
                            conn,
                            policy,
                            reason="recovery-replay",
                        )
                    checkpoint_advanced = True

                snapshot_status = "complete"
                snapshot_error: str | None = None
                if self.snapshot_path.exists():
                    try:
                        snapshot = json.loads(self.snapshot_path.read_text(encoding="utf-8"))
                        if (
                            not isinstance(snapshot, dict)
                            or snapshot.get("checkpoint_id") != chosen["checkpoint_id"]
                            or snapshot.get("ledger_digest") != chosen["ledger_digest"]
                        ):
                            snapshot_status = "restored"
                            snapshot_error = "snapshot checkpoint/digest mismatch"
                        elif any(snapshot.get(k) != v for k, v in current.items()):
                            raise GuardError(
                                E_VERSION_REBUILD,
                                "snapshot version differs from ledger; refusing to mix versions",
                            details={
                                "rebuild_required": True,
                                "snapshot_versions": {
                                    k: snapshot.get(k) for k in current
                                },
                            },
                            )
                    except json.JSONDecodeError as exc:
                        snapshot_status = "restored"
                        snapshot_error = f"snapshot JSON was incomplete: {exc.msg}"
                else:
                    snapshot_status = "restored"
                    snapshot_error = "snapshot missing"

                if checkpoint_advanced:
                    snapshot_status = "restored"
                    if snapshot_error is None:
                        snapshot_error = "checkpoint advanced after replaying committed operations"
                if snapshot_status == "restored":
                    atomic_write_json(self.snapshot_path, final_payload)

                return {
                    "ok": True,
                    "code": OK,
                    "status": (
                        "recovered"
                        if snapshot_status == "restored" or residue or replayed
                        else "complete"
                    ),
                    "checkpoint_id": final_payload["checkpoint_id"],
                    "checkpoint_seq": final_payload["checkpoint_seq"],
                    "last_complete_checkpoint_id": chosen["checkpoint_id"],
                    "ledger_digest": final_payload["ledger_digest"],
                    "snapshot_status": snapshot_status,
                    "snapshot_error": snapshot_error,
                    "residue_files": residue,
                    "invalid_checkpoints": invalid_checkpoints,
                    "replayed_operations": replayed_operations,
                    "replayed_operation_count": replayed,
                }

    @classmethod
    def rebuild_fresh(
        cls,
        root: str | Path,
        policy: dict[str, Any],
        patch_paths: list[str | Path] | None = None,
        *,
        lock_timeout_ms: int = 0,
    ) -> dict[str, Any]:
        root_path = Path(root)
        root_path.mkdir(parents=True, exist_ok=True)
        for pattern in ("guard.db", "guard.db-wal", "guard.db-shm", "checkpoint.json"):
            for path in root_path.glob(pattern):
                path.unlink(missing_ok=True)
        for path in root_path.glob(".checkpoint.json.tmp-*"):
            path.unlink(missing_ok=True)
        workspace = cls(root_path, lock_timeout_ms=lock_timeout_ms)
        init = workspace.initialize_policy(policy)
        results = []
        planned_keys: set[str] = set()
        for patch_path in patch_paths or []:
            payload, error = cls._read_json_file(patch_path)
            if error is not None:
                continue
            parsed = parse_patch_batch(
                payload,
                default_batch_id=Path(patch_path).stem,
            )
            if parsed.get("ok"):
                planned_keys.update(
                    op["canonical_key"]
                    for op in parsed.get("operations", [])
                    if op.get("op_kind") == "add_grant"
                )
        for patch_path in patch_paths or []:
            results.append(
                workspace.import_batch_file(
                    patch_path,
                    extra_resolvable_grant_keys=planned_keys,
                )
            )
        return {"workspace": workspace, "initialize": init, "imports": results}


def parse_and_initialize_policy_file(workspace: Workspace, path: str | Path) -> dict[str, Any]:
    payload, error = Workspace._read_json_file(path)
    if error:
        return {"status": "rejected", "rejections": [error]}
    policy = parse_policy(payload)
    init = workspace.initialize_policy(policy)
    init["policy_rejections"] = policy["rejections"]
    return init


__all__ = ["Workspace", "parse_and_initialize_policy_file"]
