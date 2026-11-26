from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any, Iterable

from .engine import (
    audit_attempts,
    directory_path,
    path_intersects,
    resolve_quota,
    simulate,
    subtree_and_ancestor_closure,
)
from .errors import fail
from .inputs import load_dirs, load_events, load_patch, load_v1, load_v2
from .models import Attempt, AuditDecision, Directory, Rule, WriteEvent
from .rules import changed_rule_keys, merge_rule_candidates, rule_dir_id
from .storage import (
    connect,
    ensure_empty,
    get_meta,
    get_watermark,
    initialize,
    require_initialized,
    set_meta,
    set_watermark,
    transaction,
)
from .util import (
    canonical_json,
    file_lock,
    normalize_version,
    strict_int,
)


def _row_to_directory(row: sqlite3.Row) -> Directory:
    try:
        ext_quotas = json.loads(row["ext_quotas"] or "{}")
    except json.JSONDecodeError:
        ext_quotas = {}
    return Directory(
        dir_id=str(row["dir_id"]),
        parent_id=None if row["parent_id"] is None else str(row["parent_id"]),
        base_quota=None if row["base_quota"] is None else int(row["base_quota"]),
        ext_quotas={str(k): int(v) for k, v in ext_quotas.items()},
        name=None if row["name"] is None else str(row["name"]),
        tombstoned=bool(row["tombstoned"]),
    )


def _load_runtime_dirs(conn: sqlite3.Connection) -> dict[str, Directory]:
    rows = conn.execute(
        "SELECT dir_id, parent_id, base_quota, ext_quotas, name, tombstoned "
        "FROM dirs ORDER BY dir_id"
    ).fetchall()
    return {str(row["dir_id"]): _row_to_directory(row) for row in rows}


def _load_runtime_events(
    conn: sqlite3.Connection, *, processed_only: bool = False
) -> list[WriteEvent]:
    if processed_only:
        rows = conn.execute(
            "SELECT e.seq, e.dir_id, e.file_key, e.bytes, e.digest, e.clock_ms, "
            "e.content_b64 FROM write_events e "
            "JOIN processed_events p ON p.seq=e.seq ORDER BY e.seq"
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT seq, dir_id, file_key, bytes, digest, clock_ms, content_b64 "
            "FROM write_events ORDER BY seq"
        ).fetchall()
    return [
        WriteEvent(
            seq=int(row["seq"]),
            dir_id=str(row["dir_id"]),
            file_key=str(row["file_key"]),
            bytes=int(row["bytes"]),
            digest=str(row["digest"]),
            clock_ms=int(row["clock_ms"]),
            content_b64=row["content_b64"],
        )
        for row in rows
    ]


def _load_unified(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    rows = conn.execute(
        "SELECT rule_key, quota_bytes, effective_at_ms, generation, source, "
        "decision, conversion, source_version FROM rules_unified ORDER BY rule_key"
    ).fetchall()
    for row in rows:
        conversion = None
        if row["conversion"]:
            try:
                conversion = json.loads(row["conversion"])
            except json.JSONDecodeError:
                conversion = None
        result[str(row["rule_key"])] = {
            "rule_key": str(row["rule_key"]),
            "quota_bytes": int(row["quota_bytes"]),
            "effective_at_ms": int(row["effective_at_ms"]),
            "generation": int(row["generation"]),
            "source": str(row["source"]),
            "source_version": row["source_version"],
            "decision": str(row["decision"]),
            "conversion": conversion,
        }
    return result


def _load_tombstones(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("SELECT rule_key FROM rule_tombstones").fetchall()
    return {str(row[0]) for row in rows}


def _load_v1_rules(conn: sqlite3.Connection) -> list[Rule]:
    manifest = get_meta(conn, "v1_manifest")
    if manifest is None:
        return []
    rows = conn.execute(
        "SELECT rule_key, quota_bytes, effective_at_ms, generation, conversion, "
        "manifest_version FROM rules_v1 WHERE manifest_version=? ORDER BY rule_key",
        (manifest,),
    ).fetchall()
    rules: list[Rule] = []
    for row in rows:
        conversion = None
        if row["conversion"]:
            try:
                conversion = json.loads(row["conversion"])
            except json.JSONDecodeError:
                conversion = None
        rules.append(
            Rule(
                rule_key=str(row["rule_key"]),
                quota_bytes=int(row["quota_bytes"]),
                effective_at_ms=int(row["effective_at_ms"]),
                generation=int(row["generation"]),
                source="v1",
                source_version=str(row["manifest_version"]),
                conversion=conversion,
            )
        )
    return rules


def _load_v2_rules(conn: sqlite3.Connection) -> list[Rule]:
    version = get_meta(conn, "current_v2_snapshot")
    if version is None:
        return []
    rows = conn.execute(
        "SELECT rule_key, quota_bytes, effective_at_ms, generation "
        "FROM rules_v2_versions WHERE version=? ORDER BY rule_key",
        (version,),
    ).fetchall()
    return [
        Rule(
            rule_key=str(row["rule_key"]),
            quota_bytes=int(row["quota_bytes"]),
            effective_at_ms=int(row["effective_at_ms"]),
            generation=int(row["generation"]),
            source="v2",
            source_version=version,
        )
        for row in rows
    ]


def _load_patch_rules(conn: sqlite3.Connection) -> list[Rule]:
    rows = conn.execute(
        "SELECT r.patch_id, r.rule_key, r.quota_bytes, r.effective_at_ms, r.generation "
        "FROM rules_patch r JOIN patches p ON p.patch_id=r.patch_id "
        "WHERE p.applied=1 ORDER BY r.patch_id, r.rule_key"
    ).fetchall()
    return [
        Rule(
            rule_key=str(row["rule_key"]),
            quota_bytes=int(row["quota_bytes"]),
            effective_at_ms=int(row["effective_at_ms"]),
            generation=int(row["generation"]),
            source="patch",
            source_version=str(row["patch_id"]),
        )
        for row in rows
    ]


def _public_audit(decision: AuditDecision) -> dict[str, Any]:
    return {
        "seq": decision.seq,
        "dir_id": decision.dir_id,
        "file_key": decision.file_key,
        "bytes": decision.bytes,
        "digest": decision.digest,
        "clock_ms": decision.clock_ms,
        "status": decision.status,
        "fail_dir": decision.fail_dir,
        "reason": decision.reason,
        "deducted_nodes": decision.deducted_nodes,
        "deducted_bytes": decision.deducted_bytes,
        "attempts": [attempt.to_dict() for attempt in decision.attempts],
    }


class QuotaDiagnostic:
    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)

    def _connect(self) -> sqlite3.Connection:
        return connect(self.db_path)

    def _require_rules_reference_dirs(
        self, rules: Iterable[Rule], dir_ids: set[str]
    ) -> None:
        for rule in rules:
            directory = rule_dir_id(rule.rule_key)
            if directory is not None and directory not in dir_ids:
                raise fail(
                    "E_RULE_UNKNOWN_DIR",
                    "rule references an unknown directory",
                    rule_key=rule.rule_key,
                    source=rule.source,
                    source_version=rule.source_version,
                )

    def load(
        self,
        *,
        dirs_path: str | Path,
        writes_path: str | Path,
        v1_path: str | Path,
        v2_path: str | Path,
    ) -> dict[str, Any]:
        dirs = load_dirs(dirs_path)
        dir_ids = {directory.dir_id for directory in dirs}
        events = load_events(writes_path, dir_ids)
        v1 = load_v1(v1_path)
        v2 = load_v2(v2_path)
        self._require_rules_reference_dirs(v1.rules, dir_ids)
        self._require_rules_reference_dirs(v2.rules, dir_ids)

        with file_lock(self.db_path, exclusive=True):
            conn = self._connect()
            try:
                initialize(conn)
                with transaction(conn):
                    ensure_empty(conn)
                    for directory in dirs:
                        conn.execute(
                            "INSERT INTO dirs(dir_id, parent_id, base_quota, ext_quotas, "
                            "name, tombstoned) VALUES(?, ?, ?, ?, ?, 0)",
                            (
                                directory.dir_id,
                                directory.parent_id,
                                directory.base_quota,
                                canonical_json(directory.ext_quotas),
                                directory.display_name,
                            ),
                        )
                    conn.executemany(
                        "INSERT INTO write_events(seq, dir_id, file_key, bytes, digest, "
                        "clock_ms, content_b64) VALUES(?, ?, ?, ?, ?, ?, ?)",
                        [
                            (
                                event.seq,
                                event.dir_id,
                                event.file_key,
                                event.bytes,
                                event.digest,
                                event.clock_ms,
                                event.content_b64,
                            )
                            for event in events
                        ],
                    )
                    set_meta(conn, "v1_manifest", v1.manifest_version)
                    set_meta(conn, "current_v2_snapshot", v2.version)
                    for rule in v1.rules:
                        conn.execute(
                            "INSERT INTO rules_v1(manifest_version, rule_key, quota_bytes, "
                            "effective_at_ms, generation, conversion) "
                            "VALUES(?, ?, ?, ?, ?, ?)",
                            (
                                v1.manifest_version,
                                rule.rule_key,
                                rule.quota_bytes,
                                rule.effective_at_ms,
                                rule.generation,
                                canonical_json(rule.conversion)
                                if rule.conversion is not None
                                else None,
                            ),
                        )
                        conn.execute(
                            "INSERT INTO rules_raw(source, rule_key, quota_bytes, "
                            "effective_at_ms, generation, conversion, source_version) "
                            "VALUES('v1', ?, ?, ?, ?, ?, ?)",
                            (
                                rule.rule_key,
                                rule.quota_bytes,
                                rule.effective_at_ms,
                                rule.generation,
                                canonical_json(rule.conversion)
                                if rule.conversion is not None
                                else None,
                                v1.manifest_version,
                            ),
                        )
                    conn.executemany(
                        "INSERT INTO rules_v2_versions(version, rule_key, quota_bytes, "
                        "effective_at_ms, generation) VALUES(?, ?, ?, ?, ?)",
                        [
                            (
                                v2.version,
                                rule.rule_key,
                                rule.quota_bytes,
                                rule.effective_at_ms,
                                rule.generation,
                            )
                            for rule in v2.rules
                        ],
                    )
                    snapshots = [
                        (
                            v2.version,
                            "initial_load",
                            canonical_json(v2.signature()),
                            1,
                        )
                    ]
                    conn.executemany(
                        "INSERT INTO snapshots(version, kind, detail, applied) "
                        "VALUES(?, ?, ?, ?)",
                        snapshots,
                    )
                    set_watermark(conn, "ledger_last_committed_seq", 0)
                    set_watermark(conn, "audit_watermark", 0)
                    set_watermark(conn, "logical_clock_ms", 0)
                    _, merged, rules_version = self._merge_rules_tx(conn)
                return {
                    "loaded": {
                        "dirs": len(dirs),
                        "events": len(events),
                        "v1_rules": len(v1.rules),
                        "v2_rules": len(v2.rules),
                    },
                    "snapshot": {
                        "affected": [],
                        "kind": "initial_load",
                        "previous": None,
                        "version": v2.version,
                    },
                    "rules_version": rules_version,
                    "unified_rules": len(merged),
                }
            finally:
                conn.close()

    def _rules_version_tx(self, conn: sqlite3.Connection) -> str:
        v1_version = get_meta(conn, "v1_manifest") or "v1-none"
        v2_version = get_meta(conn, "current_v2_snapshot") or "v2-none"
        base = f"{v1_version}+{v2_version}"
        rows = conn.execute(
            "SELECT patch_id FROM patches WHERE applied=1 "
            "ORDER BY COALESCE(apply_order, 0), patch_id"
        ).fetchall()
        if rows:
            return base + "#" + ",".join(str(row[0]) for row in rows)
        return base

    def _merge_rules_tx(
        self, conn: sqlite3.Connection
    ) -> tuple[
        dict[str, dict[str, Any]], dict[str, dict[str, Any]], str
    ]:
        before = _load_unified(conn)
        v1_rules = _load_v1_rules(conn)
        v2_rules = _load_v2_rules(conn)
        patch_rules = _load_patch_rules(conn)
        tombstones = _load_tombstones(conn)
        merged = merge_rule_candidates(
            [v1_rules, v2_rules, patch_rules], tombstones=tombstones
        )
        conn.execute("DELETE FROM rules_unified")
        for rule_key in sorted(merged):
            item = merged[rule_key]
            conn.execute(
                "INSERT INTO rules_unified(rule_key, quota_bytes, effective_at_ms, "
                "generation, source, decision, conversion, source_version) "
                "VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    rule_key,
                    item["quota_bytes"],
                    item["effective_at_ms"],
                    item["generation"],
                    item["source"],
                    item["decision"],
                    canonical_json(item["conversion"])
                    if item["conversion"] is not None
                    else None,
                    item.get("source_version"),
                ),
            )
        rules_version = self._rules_version_tx(conn)
        set_meta(conn, "current_rules_version", rules_version)
        return before, merged, rules_version

    def merge(self) -> dict[str, Any]:
        with file_lock(self.db_path, exclusive=True):
            conn = self._connect()
            try:
                require_initialized(conn)
                with transaction(conn):
                    before, merged, rules_version = self._merge_rules_tx(conn)
                changed = changed_rule_keys(before, merged)
                return {
                    "rules_version": rules_version,
                    "changed_rule_keys": changed,
                    "unified": {key: merged[key] for key in sorted(merged)},
                }
            finally:
                conn.close()

    def _apply_event_tx(
        self,
        conn: sqlite3.Connection,
        event: WriteEvent,
        dirs: dict[str, Directory],
        unified: dict[str, dict[str, Any]],
        rules_version: str,
    ) -> AuditDecision:
        path = directory_path(event.dir_id, dirs)
        attempts: list[Attempt] = []
        failure: Attempt | None = None
        for directory in path:
            key = (directory.dir_id, event.file_key)
            row = conn.execute(
                "SELECT consumed FROM ledger WHERE dir_id=? AND file_key=?",
                key,
            ).fetchone()
            before = 0 if row is None else int(row["consumed"])
            quota, reason = resolve_quota(
                directory, event.file_key, event.clock_ms, unified
            )
            available = None if quota is None else quota - before
            attempt = Attempt(
                dir_id=directory.dir_id,
                file_key=event.file_key,
                bytes=event.bytes,
                quota=quota,
                consumed_before=before,
                available_before=available,
                status="OK",
            )
            if quota is None:
                attempt.status = "FAIL"
                attempt.reason = reason or "no_quota_for_file_key"
                attempts.append(attempt)
                failure = attempt
                break
            if available is None or available < event.bytes:
                attempt.status = "FAIL"
                attempt.reason = "quota_exceeded"
                attempts.append(attempt)
                failure = attempt
                break
            attempts.append(attempt)

        if failure is not None:
            for attempt in attempts:
                if attempt.status == "OK":
                    attempt.rolled_back = True
            decision = AuditDecision(
                seq=event.seq,
                dir_id=event.dir_id,
                file_key=event.file_key,
                bytes=event.bytes,
                digest=event.digest,
                clock_ms=event.clock_ms,
                status="FAIL",
                fail_dir=failure.dir_id,
                reason=failure.reason,
                deducted_nodes=0,
                deducted_bytes=0,
                attempts=attempts,
            )
        else:
            for attempt in attempts:
                key = (attempt.dir_id, event.file_key)
                row = conn.execute(
                    "SELECT consumed FROM ledger WHERE dir_id=? AND file_key=?",
                    key,
                ).fetchone()
                before = 0 if row is None else int(row["consumed"])
                used = before + event.bytes
                quota = int(attempt.quota or 0)
                conn.execute(
                    "INSERT INTO ledger(dir_id, file_key, consumed, remaining, "
                    "rules_version, quota, updated_seq) VALUES(?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(dir_id, file_key) DO UPDATE SET "
                    "consumed=excluded.consumed, remaining=excluded.remaining, "
                    "rules_version=excluded.rules_version, quota=excluded.quota, "
                    "updated_seq=excluded.updated_seq",
                    (
                        attempt.dir_id,
                        event.file_key,
                        used,
                        max(0, quota - used),
                        rules_version,
                        quota,
                        event.seq,
                    ),
                )
            decision = AuditDecision(
                seq=event.seq,
                dir_id=event.dir_id,
                file_key=event.file_key,
                bytes=event.bytes,
                digest=event.digest,
                clock_ms=event.clock_ms,
                status="OK",
                deducted_nodes=len(attempts),
                deducted_bytes=len(attempts) * event.bytes,
                attempts=attempts,
            )

        conn.execute(
            "UPDATE audit SET is_current=0 WHERE seq=? AND is_current=1",
            (event.seq,),
        )
        conn.execute(
            "INSERT INTO audit(seq, dir_id, file_key, bytes, clock_ms, rules_version, "
            "status, fail_dir, reason, deducted_nodes, digest, deducted_bytes, "
            "attempts, is_current) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1) "
            "ON CONFLICT(seq, rules_version) DO UPDATE SET "
            "dir_id=excluded.dir_id, file_key=excluded.file_key, bytes=excluded.bytes, "
            "clock_ms=excluded.clock_ms, status=excluded.status, "
            "fail_dir=excluded.fail_dir, reason=excluded.reason, "
            "deducted_nodes=excluded.deducted_nodes, digest=excluded.digest, "
            "deducted_bytes=excluded.deducted_bytes, attempts=excluded.attempts, "
            "is_current=1",
            (
                decision.seq,
                decision.dir_id,
                decision.file_key,
                decision.bytes,
                decision.clock_ms,
                rules_version,
                decision.status,
                decision.fail_dir,
                decision.reason,
                decision.deducted_nodes,
                decision.digest,
                decision.deducted_bytes,
                decision.attempts_json(),
            ),
        )
        conn.execute(
            "INSERT INTO processed_events(seq, rules_version) VALUES(?, ?)",
            (event.seq, rules_version),
        )
        last_seq = max(
            get_watermark(conn, "ledger_last_committed_seq"), event.seq
        )
        audit_seq = max(get_watermark(conn, "audit_watermark"), event.seq)
        logical_clock = max(
            get_watermark(conn, "logical_clock_ms"), event.clock_ms
        )
        set_watermark(conn, "ledger_last_committed_seq", last_seq)
        set_watermark(conn, "audit_watermark", audit_seq)
        set_watermark(conn, "logical_clock_ms", logical_clock)
        return decision

    def _refresh_ledger_windows_tx(
        self,
        conn: sqlite3.Connection,
        *,
        clock_ms: int,
        dirs: dict[str, Directory],
        unified: dict[str, dict[str, Any]],
        rules_version: str,
    ) -> None:
        rows = conn.execute(
            "SELECT dir_id, file_key, consumed, quota FROM ledger"
        ).fetchall()
        for row in rows:
            directory = dirs.get(str(row["dir_id"]))
            if directory is None:
                continue
            quota, _ = resolve_quota(
                directory, str(row["file_key"]), clock_ms, unified
            )
            consumed = int(row["consumed"])
            stored_quota = int(row["quota"]) if row["quota"] is not None else None
            effective_quota = stored_quota if quota is None else quota
            remaining = (
                0
                if effective_quota is None
                else max(0, int(effective_quota) - consumed)
            )
            conn.execute(
                "UPDATE ledger SET remaining=?, quota=?, rules_version=? "
                "WHERE dir_id=? AND file_key=?",
                (
                    remaining,
                    effective_quota,
                    rules_version,
                    row["dir_id"],
                    row["file_key"],
                ),
            )

    def write(
        self,
        *,
        block_size: int = 25,
        start_seq: int | None = None,
        end_seq: int | None = None,
        crash_at_seq: int | None = None,
    ) -> dict[str, Any]:
        block_size = strict_int(block_size, "block_size", minimum=1)
        if block_size > 5000:
            raise fail(
                "E_SCHEMA",
                "block_size is too large",
                block_size=block_size,
                maximum=5000,
            )
        if start_seq is not None:
            start_seq = strict_int(start_seq, "start_seq", minimum=1)
        if end_seq is not None:
            end_seq = strict_int(end_seq, "end_seq", minimum=1)
        if crash_at_seq is None:
            raw_crash = os.environ.get("QUOTA_DIAG_CRASH_AT_SEQ")
            if raw_crash:
                crash_at_seq = strict_int(int(raw_crash), "crash_at_seq", minimum=1)

        processed = 0
        with file_lock(self.db_path, exclusive=False):
            conn = self._connect()
            try:
                require_initialized(conn)
                while True:
                    pending: list[WriteEvent] = []
                    with transaction(conn):
                        # The pending set must be selected after BEGIN IMMEDIATE
                        # so concurrent writers cannot reserve the same seq.
                        pending = self._pending_events_tx(
                            conn,
                            start_seq=start_seq,
                            end_seq=end_seq,
                            limit=block_size,
                        )
                        if pending:
                            rules_version = get_meta(conn, "current_rules_version") or ""
                            if not rules_version:
                                raise fail(
                                    "E_RULES_NOT_MERGED",
                                    "rules must be merged before writing",
                                )
                            # Keep the merged view fixed for the whole write block.
                            unified = _load_unified(conn)
                            dirs = _load_runtime_dirs(conn)
                            for event in pending:
                                if event.dir_id not in dirs:
                                    raise fail(
                                        "E_EVENT_UNKNOWN_DIR",
                                        "write event references an unknown directory",
                                        seq=event.seq,
                                        dir_id=event.dir_id,
                                    )
                                self._apply_event_tx(
                                    conn, event, dirs, unified, rules_version
                                )
                                processed += 1
                                if (
                                    crash_at_seq is not None
                                    and event.seq == crash_at_seq
                                ):
                                    # Simulates SIGKILL in the middle of the block.
                                    os._exit(137)
                            block_clock = max(event.clock_ms for event in pending)
                            self._refresh_ledger_windows_tx(
                                conn,
                                clock_ms=block_clock,
                                dirs=dirs,
                                unified=unified,
                                rules_version=rules_version,
                            )
                    if not pending:
                        break
                watermark = get_watermark(conn, "ledger_last_committed_seq")
                rules_version = get_meta(conn, "current_rules_version") or ""
                return {
                    "ledger_watermark_seq": watermark,
                    "processed": processed,
                    "rules_version": rules_version,
                }
            finally:
                conn.close()

    def _pending_events_tx(
        self,
        conn: sqlite3.Connection,
        *,
        start_seq: int | None,
        end_seq: int | None,
        limit: int,
    ) -> list[WriteEvent]:
        clauses = ["p.seq IS NULL"]
        params: list[Any] = []
        if start_seq is not None:
            clauses.append("e.seq >= ?")
            params.append(start_seq)
        if end_seq is not None:
            clauses.append("e.seq <= ?")
            params.append(end_seq)
        params.append(limit)
        rows = conn.execute(
            "SELECT e.seq, e.dir_id, e.file_key, e.bytes, e.digest, e.clock_ms, "
            "e.content_b64 FROM write_events e "
            "LEFT JOIN processed_events p ON p.seq=e.seq "
            f"WHERE {' AND '.join(clauses)} ORDER BY e.seq LIMIT ?",
            params,
        ).fetchall()
        return [
            WriteEvent(
                seq=int(row["seq"]),
                dir_id=str(row["dir_id"]),
                file_key=str(row["file_key"]),
                bytes=int(row["bytes"]),
                digest=str(row["digest"]),
                clock_ms=int(row["clock_ms"]),
                content_b64=row["content_b64"],
            )
            for row in rows
        ]

    def resume(self, *, block_size: int = 25) -> dict[str, Any]:
        with file_lock(self.db_path, exclusive=False):
            conn = self._connect()
            try:
                require_initialized(conn)
                row = conn.execute(
                    "SELECT MIN(e.seq) FROM write_events e "
                    "LEFT JOIN processed_events p ON p.seq=e.seq WHERE p.seq IS NULL"
                ).fetchone()
                resumed_from = None if row is None else row[0]
            finally:
                conn.close()
        result = self.write(block_size=block_size)
        result["resumed_from_seq"] = resumed_from
        return result

    def _mark_invalidations_tx(
        self,
        conn: sqlite3.Connection,
        *,
        marker: str,
        affected: dict[str, str],
    ) -> None:
        for dir_id in sorted(affected):
            conn.execute(
                "INSERT INTO invalidation(dir_id, patch_id, reason, recomputed) "
                "VALUES(?, ?, ?, 0) ON CONFLICT(dir_id, patch_id) DO UPDATE SET "
                "reason=excluded.reason",
                (dir_id, marker, affected[dir_id]),
            )

    def _actual_dependency_changes(
        self,
        *,
        old_dirs: dict[str, Directory],
        old_unified: dict[str, dict[str, Any]],
        new_dirs: dict[str, Directory],
        new_unified: dict[str, dict[str, Any]],
        events: list[WriteEvent],
    ) -> set[str]:
        """Find directories whose persisted conclusion really changes.

        The ancestor/subtree closure is the operational invalidation scope.
        Shared ancestors can additionally change sibling subtree decisions
        (for example, a child now succeeds and consumes the root).  This
        deterministic old/new simulation captures that cascade without
        persisting a recomputation for untouched directories.
        """

        if not events:
            return set()
        old_result = simulate(old_dirs, events, old_unified)
        new_result = simulate(new_dirs, events, new_unified)
        changed_dirs: set[str] = set()

        old_ledger = {
            key: (
                int(row["consumed"]),
                int(row["remaining"]),
                row.get("quota"),
            )
            for key, row in old_result.ledger.items()
        }
        new_ledger = {
            key: (
                int(row["consumed"]),
                int(row["remaining"]),
                row.get("quota"),
            )
            for key, row in new_result.ledger.items()
        }
        for key in set(old_ledger) | set(new_ledger):
            if old_ledger.get(key) != new_ledger.get(key):
                changed_dirs.add(key[0])

        old_audit = {decision.seq: decision for decision in old_result.audit}
        new_audit = {decision.seq: decision for decision in new_result.audit}
        decision_fields = (
            "status",
            "fail_dir",
            "reason",
            "deducted_nodes",
            "deducted_bytes",
        )
        for seq in sorted(set(old_audit) | set(new_audit)):
            before = old_audit.get(seq)
            after = new_audit.get(seq)
            changed = before is None or after is None
            if before is not None and after is not None:
                changed = any(
                    getattr(before, field) != getattr(after, field)
                    for field in decision_fields
                )
            if not changed:
                continue
            event = after or before
            assert event is not None
            for directory in directory_path(event.dir_id, new_dirs):
                changed_dirs.add(directory.dir_id)
            if before is not None:
                for directory in directory_path(before.dir_id, old_dirs):
                    changed_dirs.add(directory.dir_id)
        return changed_dirs

    def _recompute_tx(
        self,
        conn: sqlite3.Connection,
        *,
        affected: dict[str, str],
        marker: str,
    ) -> dict[str, Any]:
        if not affected:
            return {"recomputed_dirs": 0, "recomputed_audits": 0}
        affected_dirs = set(affected)
        dirs = _load_runtime_dirs(conn)
        events = _load_runtime_events(conn, processed_only=True)
        unified = _load_unified(conn)
        rules_version = get_meta(conn, "current_rules_version") or ""
        result = simulate(dirs, events, unified)

        placeholders = ",".join("?" for _ in affected_dirs)
        conn.execute(
            f"DELETE FROM ledger WHERE dir_id IN ({placeholders})",
            sorted(affected_dirs),
        )
        for key in sorted(result.ledger):
            row = result.ledger[key]
            if row["dir_id"] not in affected_dirs:
                continue
            conn.execute(
                "INSERT INTO ledger(dir_id, file_key, consumed, remaining, "
                "rules_version, quota, updated_seq) VALUES(?, ?, ?, ?, ?, ?, ?)",
                (
                    row["dir_id"],
                    row["file_key"],
                    row["consumed"],
                    row["remaining"],
                    rules_version,
                    row["quota"],
                    row["updated_seq"],
                ),
            )

        event_by_seq = {event.seq: event for event in events}
        recomputed_audits = 0
        for decision in result.audit:
            event = event_by_seq.get(decision.seq)
            if event is None:
                continue
            path = directory_path(event.dir_id, dirs)
            if not path_intersects(path, affected_dirs):
                continue
            conn.execute(
                "UPDATE audit SET is_current=0 WHERE seq=? AND is_current=1",
                (decision.seq,),
            )
            conn.execute(
                "INSERT INTO audit(seq, dir_id, file_key, bytes, clock_ms, "
                "rules_version, status, fail_dir, reason, deducted_nodes, digest, "
                "deducted_bytes, attempts, is_current) "
                "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1) "
                "ON CONFLICT(seq, rules_version) DO UPDATE SET "
                "dir_id=excluded.dir_id, file_key=excluded.file_key, bytes=excluded.bytes, "
                "clock_ms=excluded.clock_ms, status=excluded.status, "
                "fail_dir=excluded.fail_dir, reason=excluded.reason, "
                "deducted_nodes=excluded.deducted_nodes, digest=excluded.digest, "
                "deducted_bytes=excluded.deducted_bytes, attempts=excluded.attempts, "
                "is_current=1",
                (
                    decision.seq,
                    decision.dir_id,
                    decision.file_key,
                    decision.bytes,
                    decision.clock_ms,
                    rules_version,
                    decision.status,
                    decision.fail_dir,
                    decision.reason,
                    decision.deducted_nodes,
                    decision.digest,
                    decision.deducted_bytes,
                    decision.attempts_json(),
                ),
            )
            recomputed_audits += 1
        conn.execute(
            "UPDATE invalidation SET recomputed=1 WHERE patch_id=?",
            (marker,),
        )
        return {
            "recomputed_dirs": len(affected_dirs),
            "recomputed_audits": recomputed_audits,
        }

    def patch(self, *, patch_path: str | Path) -> dict[str, Any]:
        with file_lock(self.db_path, exclusive=True):
            conn = self._connect()
            try:
                require_initialized(conn)
                old_dirs = _load_runtime_dirs(conn)
                payload = load_patch(patch_path, set(old_dirs))
                patch_id = str(payload["patch_id"])
                with transaction(conn):
                    existing = conn.execute(
                        "SELECT applied, payload FROM patches WHERE patch_id=?",
                        (patch_id,),
                    ).fetchone()
                    if existing is not None and int(existing["applied"]) == 1:
                        if str(existing["payload"]) != canonical_json(payload):
                            raise fail(
                                "E_PATCH_CONFLICT",
                                "same patch ID has different content",
                                patch_id=patch_id,
                            )
                        rows = conn.execute(
                            "SELECT dir_id FROM invalidation WHERE patch_id=? "
                            "ORDER BY dir_id",
                            (patch_id,),
                        ).fetchall()
                        return {
                            "applied": True,
                            "duplicate": True,
                            "patch_id": patch_id,
                            "affected": [str(row[0]) for row in rows],
                        }
                    if existing is None:
                        order_row = conn.execute(
                            "SELECT COALESCE(MAX(apply_order), 0) + 1 FROM patches"
                        ).fetchone()
                        conn.execute(
                            "INSERT INTO patches(patch_id, payload, applied, apply_order) "
                            "VALUES(?, ?, 0, ?)",
                            (patch_id, canonical_json(payload), int(order_row[0])),
                        )
                    direct_reasons: dict[str, str] = {}
                    cli_ops = payload["ops"]
                    for op in cli_ops:
                        kind = op["op"]
                        if kind == "rule_change":
                            rule_key = str(op["rule_key"])
                            conn.execute(
                                "INSERT INTO rules_patch(patch_id, rule_key, quota_bytes, "
                                "effective_at_ms, generation) VALUES(?, ?, ?, ?, ?) "
                                "ON CONFLICT(patch_id, rule_key) DO UPDATE SET "
                                "quota_bytes=excluded.quota_bytes, "
                                "effective_at_ms=excluded.effective_at_ms, "
                                "generation=excluded.generation",
                                (
                                    patch_id,
                                    rule_key,
                                    int(op["quota_bytes"]),
                                    int(op["effective_at_ms"]),
                                    int(op["generation"]),
                                ),
                            )
                            conn.execute(
                                "DELETE FROM rule_tombstones WHERE rule_key=?",
                                (rule_key,),
                            )
                            directory = rule_dir_id(rule_key)
                            if directory is not None:
                                direct_reasons[directory] = f"rule_change:{rule_key}"
                        elif kind == "rename":
                            dir_id = str(op["dir_id"])
                            conn.execute(
                                "UPDATE dirs SET name=? WHERE dir_id=?",
                                (str(op["new_name"]), dir_id),
                            )
                            direct_reasons[dir_id] = (
                                f"rename:{dir_id}->{op['new_name']}"
                            )
                        elif kind == "tombstone":
                            rule_key = op.get("rule_key")
                            if rule_key is not None:
                                rule_key = str(rule_key)
                                conn.execute(
                                    "INSERT INTO rule_tombstones(rule_key, patch_id) "
                                    "VALUES(?, ?) ON CONFLICT(rule_key) DO UPDATE SET "
                                    "patch_id=excluded.patch_id",
                                    (rule_key, patch_id),
                                )
                                directory = rule_dir_id(rule_key)
                                if directory is not None:
                                    direct_reasons[directory] = (
                                        f"tombstone:{rule_key}"
                                    )
                            dir_id = op.get("dir_id")
                            if dir_id is not None:
                                dir_id = str(dir_id)
                                conn.execute(
                                    "UPDATE dirs SET tombstoned=1 WHERE dir_id=?",
                                    (dir_id,),
                                )
                                direct_reasons[dir_id] = f"tombstone:dir:{dir_id}"

                    conn.execute(
                        "UPDATE patches SET applied=1 WHERE patch_id=?",
                        (patch_id,),
                    )
                    before, merged, rules_version = self._merge_rules_tx(conn)
                    changed = changed_rule_keys(before, merged)
                    roots = set(direct_reasons)
                    for rule_key in changed:
                        directory = rule_dir_id(rule_key)
                        if directory is not None:
                            roots.add(directory)
                            direct_reasons.setdefault(
                                directory, f"rule_change:{rule_key}"
                            )
                    new_dirs = _load_runtime_dirs(conn)
                    affected = subtree_and_ancestor_closure(
                        roots, new_dirs, direct_reasons
                    )
                    events = _load_runtime_events(conn, processed_only=True)
                    actual = self._actual_dependency_changes(
                        old_dirs=old_dirs,
                        old_unified=before,
                        new_dirs=new_dirs,
                        new_unified=merged,
                        events=events,
                    )
                    for dir_id in sorted(actual):
                        affected.setdefault(dir_id, "dependency_cascade")
                    marker = f"patch:{patch_id}"
                    self._mark_invalidations_tx(
                        conn, marker=marker, affected=affected
                    )
                    recompute = self._recompute_tx(
                        conn, affected=affected, marker=marker
                    )
                    set_meta(conn, "current_rules_version", rules_version)
                return {
                    "affected": sorted(affected),
                    "applied": True,
                    "patch_id": patch_id,
                    "recompute": recompute,
                    "rules_version": rules_version,
                }
            finally:
                conn.close()

    def _current_v2_manifest(
        self, conn: sqlite3.Connection
    ) -> tuple[str | None, dict[str, Any] | None]:
        version = get_meta(conn, "current_v2_snapshot")
        if version is None:
            return None, None
        row = conn.execute(
            "SELECT detail FROM snapshots WHERE version=?",
            (version,),
        ).fetchone()
        if row is None:
            return version, None
        try:
            detail = json.loads(row["detail"])
        except json.JSONDecodeError:
            detail = None
        return version, detail if isinstance(detail, dict) else None

    def snapshot(self, *, v2_path: str | Path) -> dict[str, Any]:
        incoming = load_v2(v2_path)
        with file_lock(self.db_path, exclusive=True):
            conn = self._connect()
            try:
                require_initialized(conn)
                dirs = _load_runtime_dirs(conn)
                self._require_rules_reference_dirs(incoming.rules, set(dirs))
                with transaction(conn):
                    current_version, current_detail = self._current_v2_manifest(conn)
                    if current_version is None:
                        kind = "initial_load"
                        previous = None
                    else:
                        current_key = normalize_version(current_version)
                        incoming_key = normalize_version(incoming.version)
                        if incoming_key < current_key:
                            raise fail(
                                "E_VERSION_ROLLBACK",
                                "rule snapshot version rollback is rejected",
                                current_version=current_version,
                                incoming_version=incoming.version,
                            )
                        same_signature = (
                            current_detail is not None
                            and current_detail
                            == json.loads(canonical_json(incoming.signature()))
                        )
                        if incoming.version == current_version:
                            if same_signature:
                                return {
                                    "affected": [],
                                    "kind": "no_op",
                                    "previous": current_version,
                                    "version": current_version,
                                }
                            raise fail(
                                "E_SNAPSHOT_CONFLICT",
                                "same snapshot version has different content",
                                current_version=current_version,
                                incoming_version=incoming.version,
                                current_signature=current_detail,
                                incoming_signature=incoming.signature(),
                            )
                        previous = current_version
                        old_keys = {
                            str(row[0])
                            for row in conn.execute(
                                "SELECT rule_key FROM rules_v2_versions "
                                "WHERE version=?",
                                (current_version,),
                            ).fetchall()
                        }
                        current_min_generation = (
                            int(current_detail.get("min_generation", 0))
                            if current_detail is not None
                            else 0
                        )
                        if (
                            set(old_keys).issubset(set(incoming.keys))
                            and incoming.min_generation
                            >= current_min_generation
                        ):
                            kind = "incremental_append"
                        else:
                            kind = "full_replace"

                    old_unified = _load_unified(conn)
                    conn.execute(
                        "CREATE TEMP TABLE IF NOT EXISTS rules_v2_staging("
                        "version TEXT, rule_key TEXT, quota_bytes INTEGER, "
                        "effective_at_ms INTEGER, generation INTEGER)"
                    )
                    conn.execute("DELETE FROM rules_v2_staging")
                    conn.executemany(
                        "INSERT INTO rules_v2_staging(version, rule_key, quota_bytes, "
                        "effective_at_ms, generation) VALUES(?, ?, ?, ?, ?)",
                        [
                            (
                                incoming.version,
                                rule.rule_key,
                                rule.quota_bytes,
                                rule.effective_at_ms,
                                rule.generation,
                            )
                            for rule in incoming.rules
                        ],
                    )
                    conn.execute(
                        "INSERT INTO rules_v2_versions(version, rule_key, quota_bytes, "
                        "effective_at_ms, generation) "
                        "SELECT version, rule_key, quota_bytes, effective_at_ms, generation "
                        "FROM rules_v2_staging WHERE version=?",
                        (incoming.version,),
                    )
                    set_meta(conn, "current_v2_snapshot", incoming.version)
                    conn.execute(
                        "INSERT INTO snapshots(version, kind, detail, applied) "
                        "VALUES(?, ?, ?, 1) ON CONFLICT(version) DO UPDATE SET "
                        "kind=excluded.kind, detail=excluded.detail, applied=1",
                        (
                            incoming.version,
                            kind,
                            canonical_json(incoming.signature()),
                        ),
                    )
                    _, merged, rules_version = self._merge_rules_tx(conn)
                    changed = changed_rule_keys(old_unified, merged)
                    reasons: dict[str, str] = {}
                    for rule_key in changed:
                        directory = rule_dir_id(rule_key)
                        if directory is not None:
                            reasons[directory] = f"snapshot_rule_changed:{rule_key}"
                    affected = subtree_and_ancestor_closure(
                        reasons.keys(), dirs, reasons
                    )
                    events = _load_runtime_events(conn, processed_only=True)
                    actual = self._actual_dependency_changes(
                        old_dirs=dirs,
                        old_unified=old_unified,
                        new_dirs=dirs,
                        new_unified=merged,
                        events=events,
                    )
                    for dir_id in sorted(actual):
                        affected.setdefault(dir_id, "dependency_cascade")
                    marker = f"snapshot:{incoming.version}"
                    self._mark_invalidations_tx(
                        conn, marker=marker, affected=affected
                    )
                    recompute = self._recompute_tx(
                        conn, affected=affected, marker=marker
                    )
                    set_meta(conn, "current_rules_version", rules_version)
                return {
                    "affected": sorted(affected),
                    "kind": kind,
                    "previous": previous,
                    "recompute": recompute,
                    "rules_version": rules_version,
                    "version": incoming.version,
                }
            finally:
                conn.close()

    def status(self) -> dict[str, Any]:
        with file_lock(self.db_path, exclusive=False):
            conn = self._connect()
            try:
                require_initialized(conn)
                counts = {
                    "audit_rows": int(
                        conn.execute(
                            "SELECT COUNT(*) FROM audit WHERE is_current=1"
                        ).fetchone()[0]
                    ),
                    "audit_versions": int(
                        conn.execute("SELECT COUNT(*) FROM audit").fetchone()[0]
                    ),
                    "dirs": int(conn.execute("SELECT COUNT(*) FROM dirs").fetchone()[0]),
                    "ledger_rows": int(
                        conn.execute("SELECT COUNT(*) FROM ledger").fetchone()[0]
                    ),
                    "processed_events": int(
                        conn.execute("SELECT COUNT(*) FROM processed_events").fetchone()[0]
                    ),
                    "rules_unified": int(
                        conn.execute("SELECT COUNT(*) FROM rules_unified").fetchone()[0]
                    ),
                    "write_events": int(
                        conn.execute("SELECT COUNT(*) FROM write_events").fetchone()[0]
                    ),
                }
                counts["pending_events"] = (
                    counts["write_events"] - counts["processed_events"]
                )
                meta = {
                    "current_rules_version": get_meta(
                        conn, "current_rules_version"
                    ),
                    "current_v2_snapshot": get_meta(
                        conn, "current_v2_snapshot"
                    ),
                    "v1_manifest": get_meta(conn, "v1_manifest"),
                }
                watermarks = {
                    "audit_watermark": get_watermark(conn, "audit_watermark"),
                    "ledger_last_committed_seq": get_watermark(
                        conn, "ledger_last_committed_seq"
                    ),
                    "logical_clock_ms": get_watermark(conn, "logical_clock_ms"),
                }
                snapshots = [
                    {
                        "applied": int(row["applied"]),
                        "kind": str(row["kind"]),
                        "version": str(row["version"]),
                    }
                    for row in conn.execute(
                        "SELECT version, kind, applied FROM snapshots ORDER BY version"
                    ).fetchall()
                ]
                invalidations = [
                    {
                        "dir_id": str(row["dir_id"]),
                        "patch_id": str(row["patch_id"]),
                        "reason": str(row["reason"]),
                        "recomputed": int(row["recomputed"]),
                    }
                    for row in conn.execute(
                        "SELECT dir_id, patch_id, reason, recomputed "
                        "FROM invalidation ORDER BY patch_id, dir_id"
                    ).fetchall()
                ]
                patches = [
                    {
                        "applied": int(row["applied"]),
                        "apply_order": row["apply_order"],
                        "patch_id": str(row["patch_id"]),
                    }
                    for row in conn.execute(
                        "SELECT patch_id, applied, apply_order FROM patches "
                        "ORDER BY COALESCE(apply_order, 0), patch_id"
                    ).fetchall()
                ]
                return {
                    "counts": counts,
                    "invalidations": invalidations,
                    "meta": meta,
                    "patches": patches,
                    "snapshots": snapshots,
                    "watermarks": watermarks,
                }
            finally:
                conn.close()

    def verify(self) -> dict[str, Any]:
        with file_lock(self.db_path, exclusive=False):
            conn = self._connect()
            try:
                require_initialized(conn)
                dirs = _load_runtime_dirs(conn)
                events = _load_runtime_events(conn, processed_only=True)
                unified = _load_unified(conn)
                result = simulate(dirs, events, unified)
                processed_count = len(events)
                total_events = int(
                    conn.execute("SELECT COUNT(*) FROM write_events").fetchone()[0]
                )

                persisted_audit: dict[int, sqlite3.Row] = {}
                for row in conn.execute(
                    "SELECT * FROM audit WHERE is_current=1 ORDER BY seq"
                ).fetchall():
                    seq = int(row["seq"])
                    if seq in persisted_audit:
                        persisted_audit[seq] = row
                    else:
                        persisted_audit[seq] = row

                mismatches: list[dict[str, Any]] = []
                for decision in result.audit:
                    row = persisted_audit.get(decision.seq)
                    if row is None:
                        mismatches.append(
                            {
                                "kind": "missing_audit",
                                "seq": decision.seq,
                            }
                        )
                        continue
                    expected = {
                        "dir_id": decision.dir_id,
                        "file_key": decision.file_key,
                        "bytes": decision.bytes,
                        "digest": decision.digest,
                        "clock_ms": decision.clock_ms,
                        "status": decision.status,
                        "fail_dir": decision.fail_dir,
                        "reason": decision.reason,
                        "deducted_nodes": decision.deducted_nodes,
                        "deducted_bytes": decision.deducted_bytes,
                    }
                    actual = {
                        "dir_id": str(row["dir_id"]),
                        "file_key": str(row["file_key"]),
                        "bytes": int(row["bytes"]),
                        "digest": row["digest"],
                        "clock_ms": int(row["clock_ms"]),
                        "status": str(row["status"]),
                        "fail_dir": row["fail_dir"],
                        "reason": row["reason"],
                        "deducted_nodes": int(row["deducted_nodes"]),
                        "deducted_bytes": int(row["deducted_bytes"]),
                    }
                    for field in expected:
                        if expected[field] != actual[field]:
                            mismatches.append(
                                {
                                    "kind": "audit_mismatch",
                                    "seq": decision.seq,
                                    "field": field,
                                    "expected": expected[field],
                                    "actual": actual[field],
                                }
                            )

                persisted_ledger = {
                    (str(row["dir_id"]), str(row["file_key"])): {
                        "dir_id": str(row["dir_id"]),
                        "file_key": str(row["file_key"]),
                        "consumed": int(row["consumed"]),
                        "remaining": int(row["remaining"]),
                        "quota": None if row["quota"] is None else int(row["quota"]),
                    }
                    for row in conn.execute(
                        "SELECT dir_id, file_key, consumed, remaining, quota "
                        "FROM ledger"
                    ).fetchall()
                }
                for key in sorted(set(persisted_ledger) | set(result.ledger)):
                    expected_row = result.ledger.get(key)
                    actual_row = persisted_ledger.get(key)
                    if expected_row is None or actual_row is None:
                        mismatches.append(
                            {
                                "kind": "ledger_row_missing",
                                "dir_id": key[0],
                                "file_key": key[1],
                                "in_expected": expected_row is not None,
                                "in_actual": actual_row is not None,
                            }
                        )
                        continue
                    for field in ("consumed", "remaining", "quota"):
                        if expected_row[field] != actual_row[field]:
                            mismatches.append(
                                {
                                    "kind": "ledger_mismatch",
                                    "dir_id": key[0],
                                    "file_key": key[1],
                                    "field": field,
                                    "expected": expected_row[field],
                                    "actual": actual_row[field],
                                }
                            )
                if processed_count != total_events:
                    mismatches.append(
                        {
                            "kind": "unprocessed_events",
                            "processed": processed_count,
                            "total": total_events,
                        }
                    )

                audit_rows = [
                    {
                        "seq": int(row["seq"]),
                        "dir_id": str(row["dir_id"]),
                        "file_key": str(row["file_key"]),
                        "bytes": int(row["bytes"]),
                        "digest": row["digest"],
                        "clock_ms": int(row["clock_ms"]),
                        "rules_version": str(row["rules_version"]),
                        "status": str(row["status"]),
                        "fail_dir": row["fail_dir"],
                        "reason": row["reason"],
                        "deducted_nodes": int(row["deducted_nodes"]),
                        "deducted_bytes": int(row["deducted_bytes"]),
                        "attempts": audit_attempts(str(row["attempts"])),
                    }
                    for row in persisted_audit.values()
                ]
                audit_rows.sort(key=lambda item: item["seq"])
                ledger_rows = [
                    persisted_ledger[key] for key in sorted(persisted_ledger)
                ]
                ledger_total = sum(row["consumed"] for row in ledger_rows)
                stream_total = sum(
                    row["deducted_bytes"]
                    for row in audit_rows
                    if row["status"] == "OK"
                )
                if ledger_total != stream_total:
                    mismatches.append(
                        {
                            "kind": "reconciliation_mismatch",
                            "ledger_total_consumed": ledger_total,
                            "stream_total_deducted": stream_total,
                        }
                    )
                summary = {
                    "fail": sum(1 for row in audit_rows if row["status"] == "FAIL"),
                    "ok": sum(1 for row in audit_rows if row["status"] == "OK"),
                    "total": len(audit_rows),
                }
                return {
                    "audit": audit_rows,
                    "audit_summary": summary,
                    "consistent": not mismatches,
                    "ledger": ledger_rows,
                    "mismatches": mismatches,
                    "reconciliation": {
                        "ledger_total_consumed": ledger_total,
                        "stream_total_deducted": stream_total,
                    },
                }
            finally:
                conn.close()
