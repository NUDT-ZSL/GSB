from __future__ import annotations

import json
import os
import sqlite3
from collections import defaultdict
from typing import Any

from .aggregate import aggregate_samples
from .dependencies import build_dependency_edges, invalidation_closure
from .errors import (
    BusinessError,
    E_ALREADY_INITIALIZED,
    E_BAD_SAMPLE,
    E_BAD_STAT,
    E_CLOCK_ROLLBACK,
    E_DUP_TEMPLATE,
    E_NOT_INITIALIZED,
    E_SAMPLE_CONFLICT,
    E_STAT_VERSION_CONFLICT,
    E_STAT_VERSION_ROLLBACK,
    E_VERIFY_FAILED,
)
from .profiling import build_profile
from .normalize import normalize_sql
from .samples import sample_storage_digest
from .stats import (
    Catalog,
    build_patched_snapshot,
    flatten_snapshot,
    read_catalog,
    validate_snapshot,
)
from .store import (
    SCHEMA_VERSION,
    active_version,
    all_templates,
    clear_current_profile,
    committed_version,
    complete_operation,
    current_dependency_edges,
    get_meta,
    get_meta_int,
    get_operation,
    initialize_schema,
    insert_snapshot,
    insert_template,
    list_snapshots,
    load_snapshot,
    open_state,
    operation_id,
    rebuild_dependencies,
    record_activation,
    running_operations,
    set_meta,
    snapshot_version_at_clock,
    start_operation,
    state_digest,
    state_exists,
    template_payload,
    template_row,
    upsert_current_aggregate,
    upsert_current_profile,
)
from .util import (
    FileLock,
    canonical_json,
    fault_inject,
    is_int,
    read_json_file,
    rounded_number,
    sha256_json,
    sha256_text,
    transaction,
)


class Engine:
    def __init__(self, db_path: str) -> None:
        self.db_path = os.path.abspath(db_path)
        self.lock_path = self.db_path + ".lock"

    # ------------------------------------------------------------------ load
    def load(
        self,
        sample_db_path: str,
        stats_path: str,
        *,
        window_ms: int = 1000,
        clock_tolerance_ms: int = 250,
    ) -> dict[str, Any]:
        if not is_int(window_ms) or window_ms <= 0:
            raise BusinessError(E_BAD_STAT, "window_ms must be a positive integer", {"window_ms": window_ms})
        if not is_int(clock_tolerance_ms) or clock_tolerance_ms < 0:
            raise BusinessError(
                E_BAD_STAT,
                "clock_tolerance_ms must be a non-negative integer",
                {"clock_tolerance_ms": clock_tolerance_ms},
            )
        catalog = read_catalog(sample_db_path)
        document = read_json_file(stats_path, E_BAD_STAT)
        snapshot = validate_snapshot(document, catalog, path=stats_path)
        initial_active = snapshot["version"] if snapshot["effective_at_ms"] <= 0 else 0

        if state_exists(self.db_path):
            conn = open_state(self.db_path, writable=False)
            try:
                same_digest = get_meta(conn, "sample_db_digest") == catalog.digest
                same_snapshot = False
                row = conn.execute(
                    "SELECT content_digest FROM stats_snapshots WHERE version=?", (snapshot["version"],)
                ).fetchone()
                if row is not None:
                    same_snapshot = row["content_digest"] == snapshot["content_digest"]
                if same_digest and same_snapshot:
                    return {
                        "ok": True,
                        "idempotent": True,
                        "db": self.db_path,
                        "sample_db_digest": catalog.digest,
                        "initial_stats_version": snapshot["version"],
                        "active_stats_version": get_meta_int(conn, "active_stats_version"),
                    }
                raise BusinessError(
                    E_ALREADY_INITIALIZED,
                    "state database is already initialized with a different baseline",
                    {"db": self.db_path},
                )
            finally:
                conn.close()

        with FileLock(self.lock_path):
            if state_exists(self.db_path):
                conn = open_state(self.db_path, writable=False)
                try:
                    if (
                        get_meta(conn, "sample_db_digest") == catalog.digest
                        and get_meta_int(conn, "committed_stats_version") >= snapshot["version"]
                    ):
                        return {
                            "ok": True,
                            "idempotent": True,
                            "db": self.db_path,
                            "sample_db_digest": catalog.digest,
                            "initial_stats_version": snapshot["version"],
                            "active_stats_version": get_meta_int(conn, "active_stats_version"),
                        }
                finally:
                    conn.close()
                raise BusinessError(
                    E_ALREADY_INITIALIZED,
                    "state database was initialized concurrently with a different baseline",
                    {"db": self.db_path},
                )
            parent = os.path.dirname(self.db_path)
            os.makedirs(parent, exist_ok=True)
            temp_path = self.db_path + f".init.{os.getpid()}"
            for suffix in ("", "-wal", "-shm"):
                try:
                    os.unlink(temp_path + suffix)
                except FileNotFoundError:
                    pass
            conn = sqlite3.connect(temp_path, isolation_level=None, timeout=30)
            try:
                conn.execute("PRAGMA journal_mode = DELETE")
                conn.execute("PRAGMA foreign_keys = ON")
                initialize_schema(conn)
                with transaction(conn):
                    set_meta(conn, "schema_version", SCHEMA_VERSION)
                    set_meta(conn, "sample_db_path", catalog.path)
                    set_meta(conn, "sample_db_digest", catalog.digest)
                    set_meta(conn, "window_size_ms", window_ms)
                    set_meta(conn, "clock_tolerance_ms", clock_tolerance_ms)
                    set_meta(conn, "committed_stats_version", snapshot["version"])
                    set_meta(conn, "active_stats_version", initial_active)
                    set_meta(conn, "sample_watermark", 0)
                    set_meta(conn, "clock_high_water_ms", 0)
                    set_meta(conn, "dependency_graph_version", 0)
                    set_meta(conn, "profile_generation", 0)
                    set_meta(conn, "invalidation_count", 0)
                    set_meta(conn, "last_profile_tolerance", None)
                    set_meta(conn, "last_profile_run_id", None)
                    set_meta(conn, "last_profile_clock_ms", None)
                    insert_snapshot(conn, snapshot)
                    if initial_active:
                        record_activation(conn, initial_active, 0)
            finally:
                conn.close()
            os.replace(temp_path, self.db_path)
            conn = open_state(self.db_path)
            conn.close()
        return {
            "ok": True,
            "idempotent": False,
            "db": self.db_path,
            "sample_db_digest": catalog.digest,
            "initial_stats_version": snapshot["version"],
            "active_stats_version": initial_active,
            "window_size_ms": window_ms,
        }

    # --------------------------------------------------------------- helpers
    def _open_writable(self) -> sqlite3.Connection:
        return open_state(self.db_path)

    def _catalog(self, conn: sqlite3.Connection) -> Catalog:
        path = get_meta(conn, "sample_db_path")
        if not path:
            raise BusinessError(E_NOT_INITIALIZED, "state has no sample database path", {"db": self.db_path})
        return read_catalog(path)

    def _clock(self, conn: sqlite3.Connection) -> int:
        return get_meta_int(conn, "clock_high_water_ms")

    def _tolerance(self, conn: sqlite3.Connection) -> int:
        return get_meta_int(conn, "clock_tolerance_ms")

    def _check_requested_clock(self, conn: sqlite3.Connection, now_ms: int | None) -> int:
        current = self._clock(conn)
        if now_ms is None:
            return current
        if not is_int(now_ms) or now_ms < 0:
            raise BusinessError(E_BAD_SAMPLE, "logical clock must be a non-negative integer", {"now_ms": now_ms})
        tolerance = self._tolerance(conn)
        if now_ms < current - tolerance:
            raise BusinessError(
                E_CLOCK_ROLLBACK,
                "logical clock moved backwards beyond tolerance",
                {"now_ms": now_ms, "high_water_ms": current, "tolerance_ms": tolerance},
            )
        return max(current, now_ms)

    def _window_start(self, conn: sqlite3.Connection, timestamp_ms: int) -> int:
        width = get_meta_int(conn, "window_size_ms")
        if width <= 0:
            raise BusinessError(E_NOT_INITIALIZED, "invalid sampling window width", {"window_size_ms": width})
        return (timestamp_ms // width) * width

    def _samples_with_versions(self, conn: sqlite3.Connection) -> list[dict[str, Any]]:
        snapshots = [
            (row["effective_at_ms"], row["version"])
            for row in conn.execute(
                "SELECT version, effective_at_ms FROM stats_snapshots ORDER BY effective_at_ms, version"
            )
        ]
        samples: list[dict[str, Any]] = []
        for row in conn.execute("SELECT * FROM samples ORDER BY sample_seq"):
            version = None
            for effective_at_ms, candidate in snapshots:
                if effective_at_ms <= row["collected_at_ms"]:
                    version = candidate
                else:
                    break
            samples.append(
                {
                    "sample_seq": row["sample_seq"],
                    "collected_at_ms": row["collected_at_ms"],
                    "sql": row["sql_text"],
                    "template_id": row["template_id"],
                    "duration_ms": row["duration_ms"],
                    "rows_scanned": row["rows_scanned"],
                    "rows_returned": row["rows_returned"],
                    "index_hit": row["index_hit"],
                    "session_id": row["session_id"],
                    "window_start_ms": row["window_start_ms"],
                    "sample_digest": row["sample_digest"],
                    "stats_version": version,
                }
            )
        return samples

    def _sample_fingerprint(self, conn: sqlite3.Connection) -> str:
        return sha256_json(
            [
                [row["sample_seq"], row["sample_digest"]]
                for row in conn.execute(
                    "SELECT sample_seq, sample_digest FROM samples ORDER BY sample_seq"
                )
            ]
        )

    def _diff_items(self, conn: sqlite3.Connection, old_version: int, new_version: int) -> list[dict[str, Any]]:
        def load_items(version: int) -> dict[str, dict[str, Any]]:
            if version <= 0:
                return {}
            result: dict[str, dict[str, Any]] = {}
            for row in conn.execute(
                "SELECT * FROM stats_items WHERE snapshot_version=? ORDER BY item_key", (version,)
            ):
                result[row["item_key"]] = {
                    "item_key": row["item_key"],
                    "kind": row["kind"],
                    "table": row["table_name"],
                    "column": row["column_name"],
                    "index": row["index_name"],
                    "value": json.loads(row["value_json"]),
                }
            return result

        old_items = load_items(old_version)
        new_items = load_items(new_version)
        changes: list[dict[str, Any]] = []
        for key in sorted(set(old_items) | set(new_items)):
            old = old_items.get(key)
            new = new_items.get(key)
            old_value = old["value"] if old is not None else None
            new_value = new["value"] if new is not None else None
            if old is not None and new is not None and old_value == new_value:
                continue
            base = new or old
            changes.append(
                {
                    "item_key": key,
                    "kind": base["kind"],
                    "table": base["table"],
                    "column": base["column"],
                    "index": base["index"],
                    "old_value": old_value,
                    "new_value": new_value,
                }
            )
        return changes

    def _activate_due(self, conn: sqlite3.Connection, clock_ms: int) -> list[dict[str, Any]]:
        target = snapshot_version_at_clock(conn, clock_ms)
        if target is None:
            return []
        current = active_version(conn)
        if current == target:
            return []
        pending = conn.execute(
            """
            SELECT version FROM stats_snapshots
            WHERE version > ? AND effective_at_ms <= ?
            ORDER BY effective_at_ms, version
            """,
            (current, clock_ms),
        ).fetchall()
        edges = current_dependency_edges(conn)
        activated: list[dict[str, Any]] = []
        previous = current
        for row in pending:
            version = int(row["version"])
            changes = self._diff_items(conn, previous, version)
            closure = invalidation_closure(changes, edges)
            affected: list[str] = []
            inserted_count = 0
            for template_id in sorted(closure):
                entry = closure[template_id]
                existing = conn.execute(
                    "SELECT event_id FROM invalidations WHERE snapshot_version=? AND template_id=?",
                    (version, template_id),
                ).fetchone()
                if existing is not None:
                    affected.append(template_id)
                    continue
                old_profile_id = clear_current_profile(conn, template_id)
                conn.execute(
                    """
                    INSERT INTO invalidations(
                        snapshot_version, template_id, triggers_json, paths_json,
                        old_profile_id, status, created_clock_ms, resolved_profile_id
                    ) VALUES(?, ?, ?, ?, ?, 'pending', ?, NULL)
                    """,
                    (
                        version,
                        template_id,
                        canonical_json(entry["triggers"]),
                        canonical_json(entry["propagation_paths"]),
                        old_profile_id,
                        clock_ms,
                    ),
                )
                affected.append(template_id)
                inserted_count += 1
            if inserted_count:
                count = get_meta_int(conn, "invalidation_count") + inserted_count
                set_meta(conn, "invalidation_count", count)
            set_meta(conn, "active_stats_version", version)
            record_activation(conn, version, clock_ms)
            activated.append(
                {
                    "version": version,
                    "clock_ms": clock_ms,
                    "changed_items": changes,
                    "affected_templates": affected,
                }
            )
            previous = version
        return activated

    # ----------------------------------------------------------------- ingest
    def ingest(self, samples_path: str, *, now_ms: int | None = None) -> dict[str, Any]:
        from .samples import load_and_validate_samples

        samples, batch_digest = load_and_validate_samples(samples_path)
        if not state_exists(self.db_path):
            raise BusinessError(E_NOT_INITIALIZED, "state database is not initialized", {"db": self.db_path})
        with FileLock(self.lock_path):
            conn = self._open_writable()
            try:
                self._precheck_ingest(conn, samples, now_ms)
                op_id = operation_id("ingest", batch_digest)
                existing = get_operation(conn, op_id)
                if existing is not None and existing["status"] == "completed" and existing["result_json"]:
                    return {**json.loads(existing["result_json"]), "idempotent": True}
                with transaction(conn):
                    op_id, existing = start_operation(
                        conn,
                        "ingest",
                        batch_digest,
                        {"samples": samples, "now_ms": now_ms},
                    )
                fault_inject("ingest_after_journal")
                result = self._execute_ingest(conn, op_id, {"samples": samples, "now_ms": now_ms})
                result["idempotent"] = False
                return result
            finally:
                conn.close()

    def _precheck_ingest(
        self,
        conn: sqlite3.Connection,
        samples: list[dict[str, Any]],
        now_ms: int | None,
    ) -> None:
        clock_after_request = self._check_requested_clock(conn, now_ms)
        high = self._clock(conn)
        tolerance = self._tolerance(conn)
        watermark = get_meta_int(conn, "sample_watermark")
        batch_high = high
        for sample in samples:
            window_start = self._window_start(conn, sample["collected_at_ms"])
            stored_digest = sample_storage_digest(sample, window_start)
            existing = conn.execute(
                "SELECT sample_digest FROM samples WHERE sample_seq=?", (sample["sample_seq"],)
            ).fetchone()
            if existing is not None:
                if existing["sample_digest"] != stored_digest:
                    raise BusinessError(
                        E_SAMPLE_CONFLICT,
                        "sample_seq already exists with different content",
                        {"sample_seq": sample["sample_seq"]},
                    )
                continue
            if sample["sample_seq"] > watermark and sample["collected_at_ms"] < batch_high - tolerance:
                raise BusinessError(
                    E_CLOCK_ROLLBACK,
                    "new forward sample logical clock moved backwards beyond tolerance",
                    {
                        "sample_seq": sample["sample_seq"],
                        "collected_at_ms": sample["collected_at_ms"],
                        "high_water_ms": batch_high,
                        "tolerance_ms": tolerance,
                    },
                )
            batch_high = max(batch_high, sample["collected_at_ms"])
            template = template_row(conn, sample["template_id"])
            if template is not None:
                stored_structure = json.loads(template["structure_json"])
                if (
                    template["template_sql"] != sample["template_sql"]
                    or stored_structure != sample["structure"]
                ):
                    raise BusinessError(
                        E_DUP_TEMPLATE,
                        "template_id is already bound to a different normalized structure",
                        {
                            "template_id": sample["template_id"],
                            "stored_template_sql": template["template_sql"],
                            "incoming_template_sql": sample["template_sql"],
                        },
                    )
        if clock_after_request < high - tolerance:
            raise BusinessError(
                E_CLOCK_ROLLBACK,
                "logical clock moved backwards beyond tolerance",
                {"now_ms": now_ms, "high_water_ms": high, "tolerance_ms": tolerance},
            )

    def _execute_ingest(
        self,
        conn: sqlite3.Connection,
        op_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        samples = payload["samples"]
        now_ms = payload.get("now_ms")
        with transaction(conn):
            self._precheck_ingest(conn, samples, now_ms)
            catalog = self._catalog(conn)
            high = self._clock(conn)
            accepted: list[int] = []
            duplicates: list[int] = []
            new_templates: set[str] = set()
            for sample in samples:
                window_start = self._window_start(conn, sample["collected_at_ms"])
                stored_digest = sample_storage_digest(sample, window_start)
                existing = conn.execute(
                    "SELECT sample_digest FROM samples WHERE sample_seq=?", (sample["sample_seq"],)
                ).fetchone()
                if existing is not None:
                    duplicates.append(sample["sample_seq"])
                    continue
                template = template_row(conn, sample["template_id"])
                if template is None:
                    insert_template(
                        conn,
                        sample["template_id"],
                        sample["template_sql"],
                        sample["structure"],
                        sample["sample_seq"],
                    )
                    new_templates.add(sample["template_id"])
                else:
                    stored_structure = json.loads(template["structure_json"])
                    if (
                        template["template_sql"] != sample["template_sql"]
                        or stored_structure != sample["structure"]
                    ):
                        raise BusinessError(
                            E_DUP_TEMPLATE,
                            "template_id is already bound to a different normalized structure",
                            {
                                "template_id": sample["template_id"],
                                "stored_template_sql": template["template_sql"],
                                "incoming_template_sql": sample["template_sql"],
                            },
                        )
                    if sample["sample_seq"] < template["first_seq"]:
                        conn.execute(
                            "UPDATE templates SET first_seq=? WHERE template_id=?",
                            (sample["sample_seq"], sample["template_id"]),
                        )
                conn.execute(
                    """
                    INSERT INTO samples(
                        sample_seq, collected_at_ms, sql_text, template_id, duration_ms,
                        rows_scanned, rows_returned, index_hit, session_id, window_start_ms, sample_digest
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        sample["sample_seq"],
                        sample["collected_at_ms"],
                        sample["sql"],
                        sample["template_id"],
                        sample["duration_ms"],
                        sample["rows_scanned"],
                        sample["rows_returned"],
                        sample["index_hit"],
                        sample["session_id"],
                        window_start,
                        stored_digest,
                    ),
                )
                accepted.append(sample["sample_seq"])
                high = max(high, sample["collected_at_ms"])
            if now_ms is not None:
                high = max(high, now_ms)
            new_watermark = max([get_meta_int(conn, "sample_watermark")] + accepted)
            set_meta(conn, "sample_watermark", new_watermark)
            set_meta(conn, "clock_high_water_ms", high)
            graph_version = get_meta_int(conn, "dependency_graph_version")
            if new_templates:
                templates = [template_payload(row) for row in all_templates(conn)]
                graph_version = rebuild_dependencies(conn, build_dependency_edges(templates, catalog))
            activated = self._activate_due(conn, high)
            result = {
                "ok": True,
                "op_id": op_id,
                "accepted_sample_seqs": accepted,
                "duplicate_sample_seqs": duplicates,
                "sample_watermark": new_watermark,
                "clock_high_water_ms": high,
                "window_assignments": [
                    {
                        "sample_seq": sample["sample_seq"],
                        "window_start_ms": self._window_start(conn, sample["collected_at_ms"]),
                        "collected_at_ms": sample["collected_at_ms"],
                    }
                    for sample in samples
                ],
                "dependency_graph_version": graph_version,
                "active_stats_version": active_version(conn),
                "activated_stats_versions": activated,
            }
            complete_operation(conn, op_id, result)
            return result

    # ------------------------------------------------------------------ apply
    def apply(self, patch_path: str, *, now_ms: int | None = None) -> dict[str, Any]:
        patch_document = read_json_file(patch_path, E_BAD_STAT)
        if not isinstance(patch_document, dict):
            raise BusinessError(E_BAD_STAT, "statistics patch must be an object", {"path": patch_path})
        patch_digest = sha256_json(patch_document)
        if not state_exists(self.db_path):
            raise BusinessError(E_NOT_INITIALIZED, "state database is not initialized", {"db": self.db_path})
        with FileLock(self.lock_path):
            conn = self._open_writable()
            try:
                op_id_for_patch = operation_id("apply", patch_digest)
                existing = get_operation(conn, op_id_for_patch)
                if existing is not None and existing["status"] == "completed" and existing["result_json"]:
                    return {**json.loads(existing["result_json"]), "idempotent": True}
                catalog = self._catalog(conn)
                current = committed_version(conn)
                current_snapshot = load_snapshot(conn, current)
                target = patch_document.get("target_version")
                declared_digest = patch_document.get("content_digest")
                base_version = patch_document.get("base_version")
                if is_int(target) and target < current:
                    raise BusinessError(
                        E_STAT_VERSION_ROLLBACK,
                        "statistics patch target version would roll back the current version",
                        {"target_version": target, "current_version": current},
                    )
                if not is_int(base_version):
                    raise BusinessError(
                        E_BAD_STAT,
                        "patch base_version must be a positive integer",
                        {"base_version": base_version},
                    )
                if is_int(target) and is_int(base_version) and target <= base_version:
                    raise BusinessError(
                        E_STAT_VERSION_ROLLBACK,
                        "statistics patch target version must advance its base version",
                        {"target_version": target, "base_version": base_version},
                    )
                if is_int(target) and target == current:
                    if declared_digest == current_snapshot["content_digest"]:
                        try:
                            base_snapshot = load_snapshot(conn, base_version)
                        except BusinessError as exc:
                            raise BusinessError(
                                E_STAT_VERSION_CONFLICT,
                                "statistics patch base version does not exist",
                                {"base_version": base_version, "current_version": current},
                            ) from exc
                        rebuilt, _ = build_patched_snapshot(base_snapshot, patch_document, catalog)
                        if rebuilt["content_digest"] != current_snapshot["content_digest"]:
                            raise BusinessError(
                                E_STAT_VERSION_CONFLICT,
                                "statistics patch content does not reproduce the current snapshot",
                                {"target_version": target, "current_version": current},
                            )
                        result = {
                            "ok": True,
                            "op_id": operation_id("apply", patch_digest),
                            "target_version": target,
                            "content_digest": declared_digest,
                            "committed": False,
                            "activated": False,
                            "idempotent": True,
                            "active_stats_version": active_version(conn),
                        }
                        return result
                    raise BusinessError(
                        E_STAT_VERSION_CONFLICT,
                        "statistics version already exists with different content",
                        {"target_version": target, "current_version": current},
                    )
                base_version = patch_document.get("base_version")
                if not is_int(base_version) or base_version != current:
                    raise BusinessError(
                        E_STAT_VERSION_CONFLICT,
                        "statistics patch base version does not match the current version",
                        {"base_version": base_version, "current_version": current},
                    )
                if is_int(target) and target <= base_version:
                    raise BusinessError(
                        E_STAT_VERSION_ROLLBACK,
                        "statistics patch target version must advance the base version",
                        {"target_version": target, "base_version": base_version},
                    )
                snapshot, change_info = build_patched_snapshot(current_snapshot, patch_document, catalog)
                self._check_requested_clock(conn, now_ms)
                with transaction(conn):
                    op_id, _ = start_operation(
                        conn,
                        "apply",
                        patch_digest,
                        {
                            "snapshot": snapshot,
                            "changes": change_info["changes"],
                            "now_ms": now_ms,
                            "patch_digest": patch_digest,
                        },
                    )
                fault_inject("apply_after_journal")
                result = self._execute_apply(
                    conn,
                    op_id,
                    {
                        "snapshot": snapshot,
                        "changes": change_info["changes"],
                        "now_ms": now_ms,
                        "patch_digest": patch_digest,
                    },
                )
                result["idempotent"] = False
                return result
            finally:
                conn.close()

    def _execute_apply(
        self,
        conn: sqlite3.Connection,
        op_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        snapshot = payload["snapshot"]
        now_ms = payload.get("now_ms")
        with transaction(conn):
            current = committed_version(conn)
            target = int(snapshot["version"])
            if target < current:
                raise BusinessError(
                    E_STAT_VERSION_ROLLBACK,
                    "statistics snapshot version would roll back",
                    {"target_version": target, "current_version": current},
                )
            if target == current:
                current_snapshot = load_snapshot(conn, current)
                if current_snapshot["content_digest"] != snapshot["content_digest"]:
                    raise BusinessError(
                        E_STAT_VERSION_CONFLICT,
                        "statistics version already exists with different content",
                        {"target_version": target, "current_version": current},
                    )
                result = {
                    "ok": True,
                    "op_id": op_id,
                    "target_version": target,
                    "content_digest": snapshot["content_digest"],
                    "committed": False,
                    "activated": False,
                    "active_stats_version": active_version(conn),
                }
                complete_operation(conn, op_id, result)
                return result
            high = self._check_requested_clock(conn, now_ms)
            insert_snapshot(conn, snapshot)
            set_meta(conn, "committed_stats_version", target)
            if now_ms is not None:
                set_meta(conn, "clock_high_water_ms", high)
            activated = self._activate_due(conn, high)
            result = {
                "ok": True,
                "op_id": op_id,
                "target_version": target,
                "content_digest": snapshot["content_digest"],
                "effective_at_ms": snapshot["effective_at_ms"],
                "changed_items": payload.get("changes", []),
                "committed": True,
                "activated": bool(activated),
                "active_stats_version": active_version(conn),
                "activated_stats_versions": activated,
            }
            complete_operation(conn, op_id, result)
            return result

    # ---------------------------------------------------------------- profile
    def profile(
        self,
        *,
        now_ms: int | None = None,
        tolerance: float = 5.0,
    ) -> dict[str, Any]:
        if isinstance(tolerance, bool) or not isinstance(tolerance, (int, float)) or tolerance < 0:
            raise BusinessError(E_BAD_STAT, "tolerance must be non-negative", {"tolerance": tolerance})
        if not state_exists(self.db_path):
            raise BusinessError(E_NOT_INITIALIZED, "state database is not initialized", {"db": self.db_path})
        with FileLock(self.lock_path):
            conn = self._open_writable()
            try:
                high = self._check_requested_clock(conn, now_ms)
                if now_ms is not None:
                    with transaction(conn):
                        set_meta(conn, "clock_high_water_ms", high)
                        self._activate_due(conn, high)
                active = active_version(conn)
                if active <= 0:
                    raise BusinessError(
                        E_BAD_STAT,
                        "no statistics snapshot is active at the logical clock",
                        {"clock_ms": high},
                    )
                execution_payload = {
                    "now_ms": now_ms,
                    "tolerance": rounded_number(tolerance),
                    "active_stats_version": active,
                    "sample_watermark": get_meta_int(conn, "sample_watermark"),
                    "sample_fingerprint": self._sample_fingerprint(conn),
                    "dependency_graph_version": get_meta_int(conn, "dependency_graph_version"),
                }
                request_digest = sha256_json(
                    {
                        "clock_ms": high,
                        "tolerance": execution_payload["tolerance"],
                        "active_stats_version": active,
                        "sample_watermark": execution_payload["sample_watermark"],
                        "sample_fingerprint": execution_payload["sample_fingerprint"],
                        "dependency_graph_version": execution_payload["dependency_graph_version"],
                    }
                )
                existing = get_operation(conn, operation_id("profile", request_digest))
                if existing is not None and existing["status"] == "completed" and existing["result_json"]:
                    return {**json.loads(existing["result_json"]), "idempotent": True}
                with transaction(conn):
                    op_id, _ = start_operation(conn, "profile", request_digest, execution_payload)
                fault_inject("profile_after_journal")
                result = self._execute_profile(conn, op_id, execution_payload)
                result["idempotent"] = False
                return result
            finally:
                conn.close()

    def _execute_profile(
        self,
        conn: sqlite3.Connection,
        op_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        tolerance = float(payload["tolerance"])
        now_ms = payload.get("now_ms")
        with transaction(conn):
            if now_ms is not None:
                high = self._check_requested_clock(conn, now_ms)
                set_meta(conn, "clock_high_water_ms", high)
                self._activate_due(conn, high)
            active = active_version(conn)
            if active <= 0:
                raise BusinessError(
                    E_BAD_STAT,
                    "no statistics snapshot is active at the logical clock",
                    {"clock_ms": self._clock(conn)},
                )
            snapshot = load_snapshot(conn, active)
            catalog = self._catalog(conn)
            samples = self._samples_with_versions(conn)
            active_samples: dict[str, list[dict[str, Any]]] = defaultdict(list)
            for sample in samples:
                if sample["stats_version"] == active:
                    active_samples[sample["template_id"]].append(sample)
            templates = {row["template_id"]: template_payload(row) for row in all_templates(conn)}
            conn.execute("DROP TABLE IF EXISTS temp.stage_aggregates")
            conn.execute("DROP TABLE IF EXISTS temp.stage_profiles")
            conn.execute(
                """
                CREATE TEMP TABLE stage_aggregates(
                    aggregate_id TEXT,
                    template_id TEXT,
                    stats_version INTEGER,
                    input_digest TEXT,
                    aggregate_json TEXT
                )
                """
            )
            conn.execute(
                """
                CREATE TEMP TABLE stage_profiles(
                    profile_id TEXT,
                    template_id TEXT,
                    stats_version INTEGER,
                    profile_version TEXT,
                    aggregate_id TEXT,
                    input_digest TEXT,
                    conclusion_json TEXT,
                    evidence_json TEXT,
                    profile_digest TEXT
                )
                """
            )
            computed: list[str] = []
            errors: list[dict[str, Any]] = []
            for template_id in sorted(active_samples):
                template = templates[template_id]
                try:
                    aggregate = aggregate_samples(
                        active_samples[template_id],
                        template_id,
                        template["template_sql"],
                    )
                    conclusion = build_profile(
                        template_id,
                        template["template_sql"],
                        template["structure"],
                        aggregate,
                        snapshot,
                        catalog,
                        tolerance,
                    )
                except BusinessError as exc:
                    errors.append(exc.as_dict())
                    conn.execute(
                        "DELETE FROM profile_errors WHERE template_id=? AND stats_version=?",
                        (template_id, active),
                    )
                    conn.execute(
                        """
                        INSERT INTO profile_errors(
                            template_id, stats_version, run_id, code, details_json, created_clock_ms
                        ) VALUES(?, ?, ?, ?, ?, ?)
                        """,
                        (
                            template_id,
                            active,
                            op_id,
                            exc.code,
                            canonical_json(
                                {
                                    "message": exc.message,
                                    "details": exc.details or {},
                                }
                            ),
                            self._clock(conn),
                        ),
                    )
                    continue
                aggregate_json = canonical_json(aggregate)
                aggregate_id = aggregate["aggregate_version"]
                input_digest = aggregate["sample_ids_digest"]
                profile_version = "pv_" + sha256_json(
                    {
                        "template_id": template_id,
                        "stats_version": active,
                        "aggregate_version": aggregate_id,
                        "tolerance": rounded_number(tolerance),
                    }
                )[:20]
                profile_id = "pf_" + sha256_text(f"{template_id}:{active}:{profile_version}")[:24]
                conclusion_json = canonical_json(conclusion)
                evidence = {
                    "sample_ids_digest": input_digest,
                    "sample_count": aggregate["sample_count"],
                    "sample_seq_min": aggregate["sample_seq_min"],
                    "sample_seq_max": aggregate["sample_seq_max"],
                    "windows": aggregate["windows"],
                    "tolerance": rounded_number(tolerance),
                    "selection_rule": conclusion["selection_rule"],
                }
                evidence_json = canonical_json(evidence)
                profile_digest = sha256_text(conclusion_json)
                conn.execute(
                    "INSERT INTO stage_aggregates VALUES(?, ?, ?, ?, ?)",
                    (aggregate_id, template_id, active, input_digest, aggregate_json),
                )
                conn.execute(
                    "INSERT INTO stage_profiles VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        profile_id,
                        template_id,
                        active,
                        profile_version,
                        aggregate_id,
                        input_digest,
                        conclusion_json,
                        evidence_json,
                        profile_digest,
                    ),
                )
                computed.append(template_id)
            for row in conn.execute("SELECT * FROM stage_aggregates ORDER BY aggregate_id"):
                conn.execute(
                    """
                    INSERT INTO aggregates(
                        aggregate_id, template_id, stats_version, input_digest, aggregate_json
                    ) VALUES(?, ?, ?, ?, ?)
                    ON CONFLICT(aggregate_id) DO NOTHING
                    """,
                    (
                        row["aggregate_id"],
                        row["template_id"],
                        row["stats_version"],
                        row["input_digest"],
                        row["aggregate_json"],
                    ),
                )
            for row in conn.execute("SELECT * FROM stage_profiles ORDER BY profile_id"):
                conn.execute(
                    """
                    INSERT INTO profiles(
                        profile_id, template_id, stats_version, profile_version, aggregate_id,
                        input_digest, conclusion_json, evidence_json, profile_digest,
                        created_graph_version
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(profile_id) DO NOTHING
                    """,
                    (
                        row["profile_id"],
                        row["template_id"],
                        row["stats_version"],
                        row["profile_version"],
                        row["aggregate_id"],
                        row["input_digest"],
                        row["conclusion_json"],
                        row["evidence_json"],
                        row["profile_digest"],
                        get_meta_int(conn, "dependency_graph_version"),
                    ),
                )
                upsert_current_aggregate(conn, row["template_id"], row["aggregate_id"])
                upsert_current_profile(conn, row["template_id"], row["profile_id"])
                conn.execute(
                    """
                    UPDATE invalidations
                    SET status='recomputed', resolved_profile_id=?
                    WHERE snapshot_version<=? AND template_id=? AND status='pending'
                    """,
                    (row["profile_id"], active, row["template_id"]),
                )
                conn.execute(
                    "DELETE FROM profile_errors WHERE template_id=?",
                    (row["template_id"],),
                )
            generation = get_meta_int(conn, "profile_generation") + 1
            set_meta(conn, "profile_generation", generation)
            set_meta(conn, "last_profile_tolerance", rounded_number(tolerance))
            set_meta(conn, "last_profile_run_id", op_id)
            set_meta(conn, "last_profile_clock_ms", self._clock(conn))
            pending = conn.execute(
                "SELECT COUNT(*) AS n FROM invalidations WHERE status='pending' AND snapshot_version<=?",
                (active,),
            ).fetchone()["n"]
            profiles_output = []
            for row in conn.execute(
                """
                SELECT c.template_id, p.profile_version, p.stats_version, p.profile_digest,
                       p.conclusion_json
                FROM current_profiles c
                JOIN profiles p ON p.profile_id=c.profile_id
                ORDER BY c.template_id
                """
            ):
                profiles_output.append(
                    {
                        "template_id": row["template_id"],
                        "profile_version": row["profile_version"],
                        "stats_version": row["stats_version"],
                        "profile_digest": row["profile_digest"],
                        "conclusion": json.loads(row["conclusion_json"]),
                    }
                )
            result = {
                "ok": not errors,
                "partial": bool(computed and errors),
                "op_id": op_id,
                "active_stats_version": active,
                "profile_generation": generation,
                "profiled_templates": computed,
                "profiles": profiles_output,
                "pending_invalidations": int(pending),
                "errors": errors,
                "clock_high_water_ms": self._clock(conn),
            }
            if errors:
                result["error"] = errors[0]
            complete_operation(conn, op_id, result)
            return result

    # ----------------------------------------------------------------- resume
    def resume(self) -> dict[str, Any]:
        if not state_exists(self.db_path):
            raise BusinessError(E_NOT_INITIALIZED, "state database is not initialized", {"db": self.db_path})
        resumed: list[dict[str, Any]] = []
        with FileLock(self.lock_path):
            conn = self._open_writable()
            try:
                for row in running_operations(conn):
                    payload = json.loads(row["input_json"])
                    op_type = row["op_type"]
                    if op_type == "ingest":
                        result = self._execute_ingest(conn, row["op_id"], payload)
                    elif op_type == "apply":
                        result = self._execute_apply(conn, row["op_id"], payload)
                    elif op_type == "profile":
                        result = self._execute_profile(conn, row["op_id"], payload)
                    else:
                        raise BusinessError(
                            E_BAD_SAMPLE,
                            "cannot resume unknown operation type",
                            {"op_id": row["op_id"], "op_type": op_type},
                        )
                    resumed.append(
                        {
                            "op_id": row["op_id"],
                            "op_type": op_type,
                            "result": result,
                        }
                    )
                return {
                    "ok": True,
                    "resumed_count": len(resumed),
                    "resumed": resumed,
                }
            finally:
                conn.close()

    # ----------------------------------------------------------------- status
    def status(self) -> dict[str, Any]:
        if not state_exists(self.db_path):
            raise BusinessError(E_NOT_INITIALIZED, "state database is not initialized", {"db": self.db_path})
        conn = open_state(self.db_path, writable=False)
        try:
            conn.execute("BEGIN")
            payload = self._status_payload(conn)
            conn.execute("ROLLBACK")
            return payload
        finally:
            conn.close()

    def _status_payload(self, conn: sqlite3.Connection) -> dict[str, Any]:
        active = active_version(conn)
        committed = committed_version(conn)
        samples = self._samples_with_versions(conn)
        sample_counts: dict[str, int] = defaultdict(int)
        active_counts: dict[str, int] = defaultdict(int)
        for sample in samples:
            sample_counts[sample["template_id"]] += 1
            if sample["stats_version"] == active:
                active_counts[sample["template_id"]] += 1
        invalidation_counts: dict[str, int] = defaultdict(int)
        pending_counts: dict[str, int] = defaultdict(int)
        invalidation_events = []
        for row in conn.execute(
            "SELECT * FROM invalidations ORDER BY snapshot_version, template_id"
        ):
            invalidation_counts[row["template_id"]] += 1
            if row["status"] == "pending":
                pending_counts[row["template_id"]] += 1
            invalidation_events.append(
                {
                    "snapshot_version": row["snapshot_version"],
                    "template_id": row["template_id"],
                    "status": row["status"],
                    "triggers": json.loads(row["triggers_json"]),
                    "propagation_paths": json.loads(row["paths_json"]),
                    "old_profile_id": row["old_profile_id"],
                    "resolved_profile_id": row["resolved_profile_id"],
                    "created_clock_ms": row["created_clock_ms"],
                }
            )
        current_profiles = {
            row["template_id"]: row
            for row in conn.execute(
                """
                SELECT c.template_id, p.*, a.aggregate_json
                FROM current_profiles c
                JOIN profiles p ON p.profile_id = c.profile_id
                JOIN aggregates a ON a.aggregate_id = p.aggregate_id
                ORDER BY c.template_id
                """
            )
        }
        latest_errors = {
            row["template_id"]: row
            for row in conn.execute(
                """
                SELECT e.* FROM profile_errors e
                JOIN (
                    SELECT template_id, MAX(error_id) AS max_id
                    FROM profile_errors GROUP BY template_id
                ) m ON m.max_id = e.error_id
                """
            )
        }
        templates = []
        for row in conn.execute("SELECT * FROM templates ORDER BY template_id"):
            template_id = row["template_id"]
            profile = current_profiles.get(template_id)
            pending = pending_counts.get(template_id, 0)
            if profile is not None:
                profile_status = "ready"
                profile_version = profile["profile_version"]
                profile_snapshot = profile["stats_version"]
                profile_digest = profile["profile_digest"]
                conclusion = json.loads(profile["conclusion_json"])
                selected_path = conclusion.get("selected_path")
                aggregate = json.loads(profile["aggregate_json"])
                sample_count = aggregate["sample_count"]
            else:
                profile_status = "pending" if pending else "no_profile"
                profile_version = None
                profile_snapshot = None
                profile_digest = None
                selected_path = None
                sample_count = 0
            error = latest_errors.get(template_id)
            templates.append(
                {
                    "template_id": template_id,
                    "template_sql": row["template_sql"],
                    "profile_status": profile_status,
                    "profile_version": profile_version,
                    "profile_stats_version": profile_snapshot,
                    "profile_digest": profile_digest,
                    "selected_path": selected_path,
                    "profile_sample_count": sample_count,
                    "reused_from_previous_snapshot": bool(profile and profile_snapshot != active),
                    "total_sample_count": sample_counts.get(template_id, 0),
                    "active_snapshot_sample_count": active_counts.get(template_id, 0),
                    "invalidation_count": invalidation_counts.get(template_id, 0),
                    "pending_invalidation_count": pending,
                    "last_error": (
                        {
                            "code": error["code"],
                            "details": json.loads(error["details_json"]),
                        }
                        if error is not None
                        else None
                    ),
                }
            )
        stats_versions = []
        activations = {
            row["snapshot_version"]: row["activated_clock_ms"]
            for row in conn.execute("SELECT * FROM stats_activations")
        }
        for row in conn.execute("SELECT * FROM stats_snapshots ORDER BY version"):
            stats_versions.append(
                {
                    "version": row["version"],
                    "collected_at_ms": row["collected_at_ms"],
                    "effective_at_ms": row["effective_at_ms"],
                    "content_digest": row["content_digest"],
                    "activated_clock_ms": activations.get(row["version"]),
                    "active": row["version"] == active,
                }
            )
        total_invalidations = sum(invalidation_counts.values())
        total_pending = sum(pending_counts.values())
        payload = {
            "ok": True,
            "active_stats_version": active,
            "committed_stats_version": committed,
            "sample_watermark": get_meta_int(conn, "sample_watermark"),
            "sample_count": len(samples),
            "logical_clock_ms": self._clock(conn),
            "clock_tolerance_ms": self._tolerance(conn),
            "window_size_ms": get_meta_int(conn, "window_size_ms"),
            "dependency_graph_version": get_meta_int(conn, "dependency_graph_version"),
            "profile_generation": get_meta_int(conn, "profile_generation"),
            "invalidations": {
                "total": total_invalidations,
                "pending": total_pending,
                "events": invalidation_events,
            },
            "stats_versions": stats_versions,
            "templates": templates,
            "state_digest": state_digest(conn),
        }
        payload["status_digest"] = sha256_json({key: value for key, value in payload.items() if key != "state_digest"})
        return payload

    # ----------------------------------------------------------------- verify
    def verify(self) -> dict[str, Any]:
        if not state_exists(self.db_path):
            raise BusinessError(E_NOT_INITIALIZED, "state database is not initialized", {"db": self.db_path})
        checks: list[dict[str, Any]] = []

        def check(name: str, condition: bool, details: dict[str, Any] | None = None) -> None:
            checks.append({"name": name, "ok": bool(condition), "details": details or {}})

        conn = open_state(self.db_path, writable=False)
        try:
            conn.execute("BEGIN")
            integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
            check("sqlite_integrity", integrity == "ok", {"result": integrity})
            running = running_operations(conn)
            check("no_running_operations", not running, {"running": [row["op_id"] for row in running]})
            catalog = self._catalog(conn)
            check(
                "sample_database_digest",
                catalog.digest == get_meta(conn, "sample_db_digest"),
                {"declared": get_meta(conn, "sample_db_digest"), "actual": catalog.digest},
            )
            snapshots = list_snapshots(conn)
            previous_version = 0
            previous_effective = -1
            snapshot_checks_ok = True
            snapshot_details: dict[str, Any] = {}
            for row in snapshots:
                snapshot = load_snapshot(conn, row["version"])
                try:
                    validate_snapshot(snapshot, catalog)
                    snapshot_valid = True
                except BusinessError as exc:
                    snapshot_valid = False
                    snapshot_details[str(row["version"])] = exc.as_dict()
                if row["version"] <= previous_version or row["effective_at_ms"] < previous_effective:
                    snapshot_checks_ok = False
                    snapshot_details.setdefault("ordering", []).append(
                        {
                            "version": row["version"],
                            "effective_at_ms": row["effective_at_ms"],
                            "previous_version": previous_version,
                            "previous_effective_at_ms": previous_effective,
                        }
                    )
                snapshot_checks_ok = snapshot_checks_ok and snapshot_valid
                previous_version = row["version"]
                previous_effective = row["effective_at_ms"]
            check("statistics_chain", snapshot_checks_ok, snapshot_details)
            committed = committed_version(conn)
            check(
                "committed_pointer",
                bool(snapshots) and snapshots[-1]["version"] == committed,
                {"committed": committed, "latest": snapshots[-1]["version"] if snapshots else None},
            )
            expected_active = snapshot_version_at_clock(conn, self._clock(conn)) or 0
            check(
                "active_pointer_by_logical_clock",
                active_version(conn) == expected_active,
                {"active": active_version(conn), "expected": expected_active},
            )
            sample_issues: list[dict[str, Any]] = []
            previous_seq = 0
            expected_window = get_meta_int(conn, "window_size_ms")
            for row in conn.execute("SELECT * FROM samples ORDER BY sample_seq"):
                if row["sample_seq"] <= previous_seq:
                    sample_issues.append({"sample_seq": row["sample_seq"], "reason": "sequence not increasing"})
                if row["window_start_ms"] != (row["collected_at_ms"] // expected_window) * expected_window:
                    sample_issues.append({"sample_seq": row["sample_seq"], "reason": "window mismatch"})
                template = template_row(conn, row["template_id"])
                if template is None:
                    sample_issues.append({"sample_seq": row["sample_seq"], "reason": "missing template"})
                previous_seq = row["sample_seq"]
            watermark = get_meta_int(conn, "sample_watermark")
            check(
                "sample_watermark",
                watermark == previous_seq,
                {"watermark": watermark, "max_sample_seq": previous_seq},
            )
            check("sample_windows", not sample_issues, {"issues": sample_issues})
            templates = [template_payload(row) for row in all_templates(conn)]
            expected_edges = build_dependency_edges(templates, catalog)
            actual_edges = current_dependency_edges(conn)
            check(
                "dependency_graph",
                expected_edges == actual_edges,
                {"expected_edge_count": len(expected_edges), "actual_edge_count": len(actual_edges)},
            )
            profile_issues: list[dict[str, Any]] = []
            normalization_issues: list[dict[str, Any]] = []
            for row in conn.execute("SELECT * FROM templates ORDER BY template_id"):
                try:
                    normalized = normalize_sql(row["template_sql"])
                except BusinessError as exc:
                    normalization_issues.append(
                        {"template_id": row["template_id"], "reason": exc.code}
                    )
                    continue
                if normalized.template_id != row["template_id"]:
                    normalization_issues.append(
                        {
                            "template_id": row["template_id"],
                            "reason": "template id mismatch",
                            "actual": normalized.template_id,
                        }
                    )
                if canonical_json(normalized.structure) != row["structure_json"]:
                    normalization_issues.append(
                        {"template_id": row["template_id"], "reason": "structure bytes differ"}
                    )
            check("template_normalization", not normalization_issues, {"issues": normalization_issues})
            stats_item_issues: list[dict[str, Any]] = []
            for row in snapshots:
                snapshot = load_snapshot(conn, row["version"])
                expected_items = {
                    item["item_key"]: item["value"] for item in flatten_snapshot(snapshot)
                }
                actual_items = {
                    item["item_key"]: json.loads(item["value_json"])
                    for item in conn.execute(
                        "SELECT item_key, value_json FROM stats_items WHERE snapshot_version=?",
                        (row["version"],),
                    )
                }
                if expected_items != actual_items:
                    stats_item_issues.append(
                        {
                            "version": row["version"],
                            "expected_count": len(expected_items),
                            "actual_count": len(actual_items),
                        }
                    )
            check("statistics_items", not stats_item_issues, {"issues": stats_item_issues})
            all_sample_rows = self._samples_with_versions(conn)
            grouped: dict[tuple[str, int], list[dict[str, Any]]] = defaultdict(list)
            for sample in all_sample_rows:
                if sample["stats_version"] is not None:
                    grouped[(sample["template_id"], sample["stats_version"])].append(sample)
            for row in conn.execute(
                """
                SELECT c.template_id, p.*, a.aggregate_json
                FROM current_profiles c
                JOIN profiles p ON p.profile_id=c.profile_id
                JOIN aggregates a ON a.aggregate_id=p.aggregate_id
                ORDER BY c.template_id
                """
            ):
                conclusion_json = row["conclusion_json"]
                if sha256_text(conclusion_json) != row["profile_digest"]:
                    profile_issues.append(
                        {"template_id": row["template_id"], "reason": "profile digest mismatch"}
                    )
                template = template_row(conn, row["template_id"])
                if template is None:
                    profile_issues.append(
                        {"template_id": row["template_id"], "reason": "missing template"}
                    )
                    continue
                group = grouped.get((row["template_id"], row["stats_version"]), [])
                if not group:
                    profile_issues.append(
                        {
                            "template_id": row["template_id"],
                            "stats_version": row["stats_version"],
                            "reason": "profile has no matching assigned samples",
                        }
                    )
                    continue
                aggregate = aggregate_samples(
                    group,
                    row["template_id"],
                    template["template_sql"],
                )
                if canonical_json(aggregate) != row["aggregate_json"]:
                    profile_issues.append(
                        {"template_id": row["template_id"], "reason": "aggregate bytes differ"}
                    )
                evidence = {
                    "sample_ids_digest": aggregate["sample_ids_digest"],
                    "sample_count": aggregate["sample_count"],
                    "sample_seq_min": aggregate["sample_seq_min"],
                    "sample_seq_max": aggregate["sample_seq_max"],
                    "windows": aggregate["windows"],
                    "tolerance": float(json.loads(conclusion_json)["tolerance"]),
                    "selection_rule": json.loads(conclusion_json)["selection_rule"],
                }
                if canonical_json(evidence) != row["evidence_json"]:
                    profile_issues.append(
                        {"template_id": row["template_id"], "reason": "profile evidence bytes differ"}
                    )
                snapshot = load_snapshot(conn, row["stats_version"])
                try:
                    conclusion = build_profile(
                        row["template_id"],
                        template["template_sql"],
                        json.loads(template["structure_json"]),
                        aggregate,
                        snapshot,
                        catalog,
                        float(json.loads(conclusion_json)["tolerance"]),
                    )
                    if canonical_json(conclusion) != conclusion_json:
                        profile_issues.append(
                            {"template_id": row["template_id"], "reason": "profile bytes differ"}
                        )
                except BusinessError as exc:
                    profile_issues.append(
                        {"template_id": row["template_id"], "reason": exc.code, "details": exc.details}
                    )
            check("current_profiles", not profile_issues, {"issues": profile_issues})
            sample_digest_issues: list[dict[str, Any]] = []
            for row in conn.execute("SELECT * FROM samples ORDER BY sample_seq"):
                sample_record = {
                    "sample_seq": row["sample_seq"],
                    "collected_at_ms": row["collected_at_ms"],
                    "sql": row["sql_text"],
                    "template_id": row["template_id"],
                    "duration_ms": row["duration_ms"],
                    "rows_scanned": row["rows_scanned"],
                    "rows_returned": row["rows_returned"],
                    "index_hit": row["index_hit"],
                    "session_id": row["session_id"],
                }
                if sample_storage_digest(sample_record, row["window_start_ms"]) != row["sample_digest"]:
                    sample_digest_issues.append(
                        {"sample_seq": row["sample_seq"], "reason": "sample digest mismatch"}
                    )
            check("sample_digests", not sample_digest_issues, {"issues": sample_digest_issues})
            invalidation_issues: list[dict[str, Any]] = []
            for event in conn.execute("SELECT * FROM invalidations ORDER BY snapshot_version, template_id"):
                triggers = json.loads(event["triggers_json"])
                paths = json.loads(event["paths_json"])
                if not triggers or not paths:
                    invalidation_issues.append(
                        {"snapshot_version": event["snapshot_version"], "template_id": event["template_id"], "reason": "empty trigger or path"}
                    )
                if event["status"] not in {"pending", "recomputed"}:
                    invalidation_issues.append(
                        {"snapshot_version": event["snapshot_version"], "template_id": event["template_id"], "reason": "bad status"}
                    )
            check("invalidation_events", not invalidation_issues, {"issues": invalidation_issues})
            status_payload = self._status_payload(conn)
            state_hash = state_digest(conn)
            check(
                "status_state_digest",
                status_payload["state_digest"] == state_hash,
                {"expected": state_hash, "actual": status_payload["state_digest"]},
            )
            failures = [item for item in checks if not item["ok"]]
            payload = {
                "ok": not failures,
                "checks": checks,
                "failed_checks": [item["name"] for item in failures],
                "state_digest": state_hash,
                "status_digest": status_payload["status_digest"],
                "status": status_payload,
            }
            if failures:
                payload["error"] = {
                    "code": E_VERIFY_FAILED,
                    "message": "persisted state failed verification",
                }
            conn.execute("ROLLBACK")
            return payload
        finally:
            conn.close()
