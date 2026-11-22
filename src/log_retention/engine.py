from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from collections.abc import Iterable
from dataclasses import replace
from typing import Any

import numpy as np

from .config import Config
from .errors import InjectedCrash, fail
from .fixedpoint import MarginResult, compute_margin
from .ledger import Ledger
from .models import NormalizedInput
from .policy import PolicyNode, PolicyView, build_policy_view, descendant_keys
from .sources import load_normalized_input


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _fault_limit(name: str) -> int | None:
    value = os.environ.get(name)
    if value is None or value == "":
        return None
    try:
        parsed = int(value)
    except ValueError:
        return None
    return parsed if parsed >= 0 else None


def _maybe_crash(name: str, count: int) -> None:
    limit = _fault_limit(name)
    if limit is not None and count >= limit:
        raise InjectedCrash(f"injected crash at {name}={limit}")


class Engine:
    def __init__(self, config: Config):
        self.config = config
        self._ledger: Ledger | None = None

    @property
    def ledger(self) -> Ledger:
        if self._ledger is None:
            self._ledger = Ledger(self.config.ledger_db)
        return self._ledger

    def close(self) -> None:
        if self._ledger is not None:
            self._ledger.close()
            self._ledger = None

    def _resolve_clock(
        self, conn: sqlite3.Connection, requested_clock_ms: int
    ) -> tuple[int, int | None]:
        current = self.ledger.clock_watermark(conn)
        if (
            current is not None
            and requested_clock_ms < current - self.config.clock_tolerance_ms
        ):
            fail(
                "E_CLOCK_ROLLBACK",
                "logical clock rollback exceeds configured tolerance",
                requested_clock_ms=requested_clock_ms,
                watermark_ms=current,
                tolerance_ms=self.config.clock_tolerance_ms,
            )
        return max(requested_clock_ms, current or requested_clock_ms), current

    def _policy_rows(self, view: PolicyView) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for key in sorted(view.nodes):
            node = view.nodes[key]
            rows.append(
                {
                    "policy_key": key,
                    "source_version": node.policy.source_version,
                    "source_policy_id": node.policy.policy_id,
                    "instance_id": node.policy.instance_id,
                    "segment_id": node.policy.segment_id,
                    "retention_generation": node.policy.retention_generation,
                    "effective_at_ms": node.policy.effective_at_ms,
                    "raw_cutoff_ms": node.raw_cutoff_ms,
                    "relative_seconds": node.policy.relative_seconds,
                    "original_time": node.original_time,
                    "required_min_catchup_margin": node.policy.required_min_catchup_margin,
                    "parent_policy_key": node.parent_key,
                    "source_priority": node.policy.source_priority,
                    "active": node.active,
                    "selected": node.selected,
                    "effective": node.effective,
                    "tombstone": node.tombstone,
                    "invalidated": node.invalidated,
                    "invalidation_reason": node.invalidation_reason,
                    "merge_decision": node.merge_decision,
                    "merged_from": node.merged_from,
                    "conversion": node.conversion,
                    "raw": node.policy.raw,
                }
            )
        return rows

    def _conflict_rows(self, view: PolicyView) -> list[dict[str, Any]]:
        return [
            {
                "segment_id": conflict.segment_id,
                "instance_id": conflict.instance_id,
                "winning_policy_key": conflict.winner.key,
                "losing_policy_keys": [node.key for node in conflict.losers],
                "common_ancestor_policy_key": conflict.common_ancestor_key,
                "evidence": conflict.evidence,
            }
            for conflict in view.conflicts
        ]

    def _content_hash(
        self,
        *,
        kind: str,
        load_version: int,
        clock_ms: int,
        target_ms: int,
        policy_rows: list[dict[str, Any]],
        edges: list[dict[str, Any]],
        decisions: list[dict[str, Any]],
        conflicts: list[dict[str, Any]],
    ) -> str:
        return _hash(
            {
                "kind": kind,
                "load_version": load_version,
                "logical_clock_ms": clock_ms,
                "archive_target_watermark_ms": target_ms,
                "policies": policy_rows,
                "edges": edges,
                "decisions": decisions,
                "conflicts": conflicts,
            }
        )

    def _effective_policies(
        self, conn: sqlite3.Connection, view_version: int
    ) -> list[dict[str, Any]]:
        rows = conn.execute(
            "SELECT * FROM view_policies WHERE view_version = ? AND effective = 1 "
            "ORDER BY instance_id, retention_generation, policy_key",
            (view_version,),
        ).fetchall()
        return [
            {
                "policy_key": str(row["policy_key"]),
                "policy_id": str(row["source_policy_id"]),
                "instance_id": str(row["instance_id"]),
                "segment_id": row["segment_id"],
                "source_version": str(row["source_version"]),
                "retention_generation": int(row["retention_generation"]),
                "effective_at_ms": int(row["effective_at_ms"]),
                "raw_cutoff_ms": int(row["raw_cutoff_ms"]),
                "original_time_representation": json.loads(
                    str(row["original_time_json"])
                ),
                "conversion": json.loads(str(row["conversion_json"])),
                "parent_policy_key": row["parent_policy_key"],
                "merged_from": json.loads(str(row["merged_from_json"])),
            }
            for row in rows
        ]

    def _summary_from_view(
        self, conn: sqlite3.Connection, view_version: int
    ) -> dict[str, Any]:
        row = conn.execute(
            "SELECT * FROM view_versions WHERE view_version = ?", (view_version,)
        ).fetchone()
        if row is None:
            fail(
                "E_VIEW_MISSING",
                "view version does not exist",
                view_version=view_version,
            )
        edge_count = int(
            conn.execute(
                "SELECT COUNT(*) AS n FROM view_edges WHERE view_version = ?",
                (view_version,),
            ).fetchone()["n"]
        )
        return {
            "schema_version": 1,
            "command": str(row["kind"]),
            "status": str(row["status"]),
            "load_version": int(row["load_version"]),
            "view_version": int(row["view_version"]),
            "source_view_version": row["source_view_version"],
            "logical_clock_ms": int(row["logical_clock_ms"]),
            "requested_clock_ms": int(row["requested_clock_ms"]),
            "archive_target_watermark_ms": int(row["archive_target_watermark_ms"]),
            "content_hash": str(row["content_hash"]),
            "conflict_count": int(row["conflict_count"]),
            "edge_count": edge_count,
            "effective_policies": self._effective_policies(conn, view_version),
        }

    def load(self) -> dict[str, Any]:
        normalized = load_normalized_input(self.config)
        result = self.ledger.load_input(normalized, self.config.load_version)
        result = dict(result)
        result["command"] = "load"
        result["config"] = str(self.config.path)
        return result

    def merge(self) -> dict[str, Any]:
        with self.ledger.global_lock(), self.ledger.transaction() as conn:
            load_version = self.ledger.current_load_version(conn)
            clock, _watermark = self._resolve_clock(conn, self.config.injected_clock_ms)
            snapshot = self.ledger.read_snapshot(load_version, conn)
            invalidations = self.ledger.invalidations(load_version, conn)
            view = build_policy_view(
                snapshot, clock_ms=clock, invalidations=invalidations
            )
            policy_rows = self._policy_rows(view)
            conflict_rows = self._conflict_rows(view)
            fingerprint = "merge:" + _hash(
                {
                    "kind": "merge",
                    "load_version": load_version,
                    "logical_clock_ms": clock,
                    "archive_target_watermark_ms": self.config.archive_target_watermark_ms,
                    "invalidations": sorted(invalidations),
                }
            )
            existing = conn.execute(
                "SELECT view_version FROM view_versions WHERE fingerprint = ?",
                (fingerprint,),
            ).fetchone()
            if existing is not None:
                return self._summary_from_view(conn, int(existing["view_version"]))
            content_hash = self._content_hash(
                kind="merge",
                load_version=load_version,
                clock_ms=clock,
                target_ms=self.config.archive_target_watermark_ms,
                policy_rows=policy_rows,
                edges=view.edges,
                decisions=[],
                conflicts=conflict_rows,
            )
            view_version = self.ledger.allocate_view_version_and_insert(
                conn,
                kind="merge",
                source_view_version=None,
                load_version=load_version,
                logical_clock_ms=clock,
                requested_clock_ms=self.config.injected_clock_ms,
                archive_target_ms=self.config.archive_target_watermark_ms,
                operation_id=None,
                fingerprint=fingerprint,
                content_hash=content_hash,
                conflict_count=len(conflict_rows),
            )
            self.ledger.insert_view_policies(conn, view_version, policy_rows)
            self.ledger.insert_view_edges(conn, view_version, view.edges)
            self.ledger.insert_conflicts(conn, view_version, conflict_rows)
            self.ledger.set_view_pointer(conn, view_version)
            self.ledger.meta_set(conn, "logical_clock_watermark_ms", clock)
            return self._summary_from_view(conn, view_version)

    def _calculate_margin_for_instance(
        self,
        conn: sqlite3.Connection,
        *,
        snapshot: NormalizedInput,
        view: PolicyView,
        instance_id: str,
        clock_ms: int,
    ) -> tuple[int, int, str, sqlite3.Row]:
        nodes = [
            node
            for node in view.effective_nodes()
            if node.policy.instance_id == instance_id
        ]
        if not nodes:
            fail(
                "E_INSTANCE_NO_ACTIVE_POLICY",
                "instance has no effective non-tombstone policy at this clock",
                instance_id=instance_id,
                logical_clock_ms=clock_ms,
            )
        ranked = sorted(
            nodes,
            key=lambda node: (
                node.policy.retention_generation,
                node.policy.effective_at_ms,
                node.policy.source_priority,
                node.key,
            ),
            reverse=True,
        )
        chosen = ranked[0]
        declared = max(node.policy.required_min_catchup_margin for node in nodes)
        rate = self.config.replay_rates_bytes_per_sec.get(instance_id)
        if rate is None:
            fail("E_CONFIG_REPLAY_RATE", "missing replay rate", instance_id=instance_id)
        state = snapshot.replay_states[instance_id]
        lag_bytes = sum(
            txn.byte_size
            for txn in snapshot.transactions
            if txn.txn_id > state.latest_txn_id
        )
        signature = _hash(
            {
                "instance_id": instance_id,
                "policy_generation": chosen.policy.retention_generation,
                "logical_clock_ms": clock_ms,
                "lag_bytes": lag_bytes,
                "replay_rate_bytes_per_sec": rate,
                "scale_units_per_ms": self.config.fixed_scale_units_per_ms,
                "declared_min_units": declared,
            }
        )
        existing = self.ledger.find_window(
            conn,
            instance_id=instance_id,
            policy_generation=chosen.policy.retention_generation,
            logical_clock_ms=clock_ms,
            input_signature=signature,
        )
        if existing is not None:
            return (
                chosen.policy.retention_generation,
                int(existing["window_id"]),
                signature,
                existing,
            )
        carry_in = self.ledger.latest_remainder(conn, instance_id, clock_ms)
        margin = compute_margin(
            lag_bytes=lag_bytes,
            replay_rate_bytes_per_sec=rate,
            scale_units_per_ms=self.config.fixed_scale_units_per_ms,
            declared_min_units=declared,
            carry_in_units=carry_in,
        )
        window_id = self.ledger.insert_window(
            conn,
            instance_id=instance_id,
            policy_generation=chosen.policy.retention_generation,
            logical_clock_ms=clock_ms,
            input_signature=signature,
            lag_bytes=lag_bytes,
            replay_rate=rate,
            scale_units_per_ms=self.config.fixed_scale_units_per_ms,
            declared_min_units=declared,
            carry_in_units=carry_in,
            computed_margin_units=margin.computed_units,
            effective_units=margin.effective_units,
            remainder_units=margin.remainder_units,
            precision_sensitive=margin.precision_sensitive,
        )
        row = conn.execute(
            "SELECT * FROM retention_windows WHERE window_id = ?", (window_id,)
        ).fetchone()
        assert row is not None
        return chosen.policy.retention_generation, window_id, signature, row

    def _stage_instance(
        self,
        *,
        op_id: str,
        instance_id: str,
        fault_name: str | None = None,
        inject_fault: bool = True,
    ) -> bool:
        inserted = False
        with (
            self.ledger.instance_lock(instance_id),
            self.ledger.transaction() as conn,
        ):
            op = conn.execute(
                "SELECT * FROM operations WHERE op_id = ?", (op_id,)
            ).fetchone()
            if op is None:
                fail("E_OPERATION_MISSING", "operation does not exist", op_id=op_id)
            if op["status"] == "committed":
                return False
            if op["status"] not in {"staging", "finalizing"}:
                fail(
                    "E_OPERATION_STATE",
                    "operation cannot accept staged calculations",
                    op_id=op_id,
                    status=op["status"],
                )
            snapshot = self.ledger.read_snapshot(int(op["load_version"]), conn)
            view = build_policy_view(
                snapshot,
                clock_ms=int(op["effective_clock_ms"]),
                invalidations=self.ledger.invalidations(int(op["load_version"]), conn),
            )
            generation, window_id, _signature, _window = (
                self._calculate_margin_for_instance(
                    conn,
                    snapshot=snapshot,
                    view=view,
                    instance_id=instance_id,
                    clock_ms=int(op["effective_clock_ms"]),
                )
            )
            policy_key = max(
                (
                    node
                    for node in view.effective_nodes()
                    if node.policy.instance_id == instance_id
                ),
                key=lambda node: (
                    node.policy.retention_generation,
                    node.policy.effective_at_ms,
                    node.policy.source_priority,
                    node.key,
                ),
            ).key
            cursor = conn.execute(
                "INSERT OR IGNORE INTO op_calculations("
                "op_id, instance_id, policy_key, policy_generation, window_id, state) "
                "VALUES (?, ?, ?, ?, ?, 'staged')",
                (op_id, instance_id, policy_key, generation, window_id),
            )
            inserted = cursor.rowcount > 0
        if inserted and inject_fault and fault_name:
            with self.ledger.transaction() as conn:
                count = int(
                    conn.execute(
                        "SELECT COUNT(*) AS n FROM op_calculations WHERE op_id = ?",
                        (op_id,),
                    ).fetchone()["n"]
                )
            _maybe_crash(fault_name, count)
        return inserted

    @staticmethod
    def _operation_dict(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "operation_id": str(row["op_id"]),
            "kind": str(row["kind"]),
            "load_version": int(row["load_version"]),
            "source_view_version": row["source_view_version"],
            "requested_clock_ms": int(row["requested_clock_ms"]),
            "effective_clock_ms": int(row["effective_clock_ms"]),
            "archive_target_ms": int(row["archive_target_ms"]),
            "expected_instances": json.loads(str(row["expected_instances_json"])),
            "status": str(row["status"]),
            "view_version": row["view_version"],
        }

    def _cached_operation_result(
        self, conn: sqlite3.Connection, op_id: str
    ) -> dict[str, Any] | None:
        row = conn.execute(
            "SELECT result_json FROM operation_results WHERE op_id = ?", (op_id,)
        ).fetchone()
        return None if row is None else json.loads(str(row["result_json"]))

    def _save_operation_result(
        self, conn: sqlite3.Connection, op_id: str, result: dict[str, Any]
    ) -> None:
        result_json = _json(result)
        conn.execute(
            "INSERT INTO operation_results(op_id, result_json, result_hash) "
            "VALUES (?, ?, ?) ON CONFLICT(op_id) DO UPDATE SET "
            "result_json=excluded.result_json, result_hash=excluded.result_hash",
            (
                op_id,
                result_json,
                hashlib.sha256(result_json.encode("utf-8")).hexdigest(),
            ),
        )

    @staticmethod
    def _active_instances(view: PolicyView) -> list[str]:
        return sorted(
            {
                node.policy.instance_id
                for node in view.effective_nodes()
                if not node.tombstone
            }
        )

    def judge(
        self,
        *,
        requested_clock_ms: int | None = None,
        instances: Iterable[str] | None = None,
        finalize: bool = True,
        inject_fault: bool = True,
    ) -> dict[str, Any]:
        requested_clock = (
            self.config.injected_clock_ms
            if requested_clock_ms is None
            else requested_clock_ms
        )
        op_id: str
        expected: list[str]
        with self.ledger.global_lock(), self.ledger.transaction() as conn:
            load_version = self.ledger.current_load_version(conn)
            clock, _watermark = self._resolve_clock(conn, requested_clock)
            snapshot = self.ledger.read_snapshot(load_version, conn)
            invalidations = self.ledger.invalidations(load_version, conn)
            view = build_policy_view(
                snapshot, clock_ms=clock, invalidations=invalidations
            )
            expected = self._active_instances(view)
            if not expected:
                fail("E_NO_ACTIVE_INSTANCES", "no active replay instance policies")
            fingerprint = _hash(
                {
                    "kind": "judge",
                    "load_version": load_version,
                    "effective_clock_ms": clock,
                    "archive_target_ms": self.config.archive_target_watermark_ms,
                    "expected_instances": expected,
                    "invalidations": sorted(invalidations),
                }
            )
            op_row = conn.execute(
                "SELECT * FROM operations WHERE kind = 'judge' "
                "AND fingerprint = ? AND load_version = ?",
                (fingerprint, load_version),
            ).fetchone()
            if op_row is None:
                op_id = "judge-" + fingerprint[:24]
                conn.execute(
                    "INSERT INTO operations("
                    "op_id, kind, fingerprint, load_version, source_view_version, "
                    "requested_clock_ms, effective_clock_ms, archive_target_ms, "
                    "expected_instances_json, status, view_version) "
                    "VALUES (?, 'judge', ?, ?, NULL, ?, ?, ?, ?, 'staging', NULL)",
                    (
                        op_id,
                        fingerprint,
                        load_version,
                        requested_clock,
                        clock,
                        self.config.archive_target_watermark_ms,
                        _json(expected),
                    ),
                )
            else:
                op_id = str(op_row["op_id"])
                if op_row["status"] == "committed":
                    cached = self._cached_operation_result(conn, op_id)
                    if cached is not None:
                        return cached

        requested_instances = (
            sorted(set(instances)) if instances is not None else expected
        )
        unknown = sorted(set(requested_instances) - set(expected))
        if unknown:
            fail(
                "E_INSTANCE_UNKNOWN",
                "judge requested an instance with no active policy",
                instances=unknown,
                expected_instances=expected,
            )
        staged: set[str] = set()
        for instance_id in requested_instances:
            self._stage_instance(
                op_id=op_id,
                instance_id=instance_id,
                fault_name="RETENTION_FAULT_JUDGE_AFTER_STAGED",
                inject_fault=inject_fault,
            )
            staged.add(instance_id)

        with self.ledger.read_transaction() as conn:
            stage_rows = conn.execute(
                "SELECT instance_id FROM op_calculations WHERE op_id = ?", (op_id,)
            ).fetchall()
        staged_all = {str(row["instance_id"]) for row in stage_rows}
        missing = sorted(set(expected) - staged_all)
        if missing:
            if finalize and instances is None:
                fail(
                    "E_OPERATION_INCOMPLETE",
                    "judge operation is missing staged instances",
                    operation_id=op_id,
                    missing_instances=missing,
                )
            op = self._operation(op_id)
            return {
                "schema_version": 1,
                "command": "judge",
                "status": "staged",
                "operation_id": op_id,
                "load_version": op["load_version"],
                "logical_clock_ms": op["effective_clock_ms"],
                "requested_clock_ms": op["requested_clock_ms"],
                "archive_target_watermark_ms": op["archive_target_ms"],
                "expected_instances": expected,
                "staged_instances": sorted(staged_all),
                "missing_instances": missing,
            }
        if not finalize:
            op = self._operation(op_id)
            return {
                "schema_version": 1,
                "command": "judge",
                "status": "ready",
                "operation_id": op_id,
                "load_version": op["load_version"],
                "logical_clock_ms": op["effective_clock_ms"],
                "requested_clock_ms": op["requested_clock_ms"],
                "archive_target_watermark_ms": op["archive_target_ms"],
                "expected_instances": expected,
                "staged_instances": sorted(staged_all),
                "missing_instances": [],
            }
        return self._finalize_judge(op_id, inject_fault=inject_fault)

    def _operation(self, op_id: str) -> dict[str, Any]:
        row = self.ledger.conn.execute(
            "SELECT * FROM operations WHERE op_id = ?", (op_id,)
        ).fetchone()
        if row is None:
            fail("E_OPERATION_MISSING", "operation does not exist", op_id=op_id)
        return self._operation_dict(row)

    def _margin_json(self, window: sqlite3.Row) -> dict[str, Any]:
        margin = MarginResult(
            lag_bytes=int(window["lag_bytes"]),
            replay_rate_bytes_per_sec=int(window["replay_rate_bytes_per_sec"]),
            scale_units_per_ms=int(window["scale_units_per_ms"]),
            declared_min_units=int(window["declared_min_units"]),
            carry_in_units=int(window["carry_in_units"]),
            numerator=int(window["lag_bytes"])
            * 1000
            * int(window["scale_units_per_ms"])
            + int(window["carry_in_units"]),
            computed_units=int(window["computed_margin_units"]),
            remainder_units=int(window["remainder_units"]),
            effective_units=int(window["effective_units"]),
            precision_sensitive=bool(window["precision_sensitive"]),
        )
        return margin.as_dict()

    def _window_map(
        self, conn: sqlite3.Connection, op_id: str
    ) -> dict[str, sqlite3.Row]:
        rows = conn.execute(
            "SELECT c.instance_id, c.policy_key, c.policy_generation, c.state, w.* "
            "FROM op_calculations c JOIN retention_windows w ON w.window_id = c.window_id "
            "WHERE c.op_id = ? ORDER BY c.instance_id",
            (op_id,),
        ).fetchall()
        return {str(row["instance_id"]): row for row in rows}

    def _instance_state_rows(
        self, view: PolicyView, windows: dict[str, sqlite3.Row]
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for node in view.effective_nodes():
            instance_id = node.policy.instance_id
            window = windows.get(instance_id)
            if window is None:
                fail(
                    "E_INSTANCE_MARGIN_MISSING",
                    "effective policy has no staged catching-up margin",
                    instance_id=instance_id,
                    policy_key=node.key,
                )
            rows.append(
                {
                    "instance_id": instance_id,
                    "policy_key": node.key,
                    "policy_generation": int(window["policy_generation"]),
                    "cutoff_ms": node.cutoff_ms,
                    "declared_min_units": int(window["declared_min_units"]),
                    "computed_margin_units": int(window["computed_margin_units"]),
                    "margin_units": int(window["effective_units"]),
                    "carry_in_units": int(window["carry_in_units"]),
                    "remainder_units": int(window["remainder_units"]),
                    "precision_sensitive": bool(window["precision_sensitive"]),
                    "window_id": int(window["window_id"]),
                    "calc_key": str(window["input_signature"]),
                    "margin_json": self._margin_json(window),
                }
            )
        return rows

    def _instance_summaries(
        self, view: PolicyView, windows: dict[str, sqlite3.Row]
    ) -> list[dict[str, Any]]:
        by_instance: dict[str, list[PolicyNode]] = {}
        for node in view.effective_nodes():
            by_instance.setdefault(node.policy.instance_id, []).append(node)
        summaries: list[dict[str, Any]] = []
        for instance_id in sorted(by_instance):
            window = windows[instance_id]
            summaries.append(
                {
                    "instance_id": instance_id,
                    "policy_generation": int(window["policy_generation"]),
                    "policy_keys": sorted(
                        node.key for node in by_instance[instance_id]
                    ),
                    "lag_bytes": int(window["lag_bytes"]),
                    "replay_rate_bytes_per_sec": int(
                        window["replay_rate_bytes_per_sec"]
                    ),
                    "scale_units_per_ms": int(window["scale_units_per_ms"]),
                    "carry_in_units": int(window["carry_in_units"]),
                    "computed_margin_units": int(window["computed_margin_units"]),
                    "effective_margin_units": int(window["effective_units"]),
                    "remainder_units": int(window["remainder_units"]),
                    "precision_sensitive": bool(window["precision_sensitive"]),
                    "calc_key": str(window["input_signature"]),
                    "margin": self._margin_json(window),
                }
            )
        return summaries

    def _segment_stats(self, snapshot: NormalizedInput) -> dict[int, dict[str, int]]:
        txn_ids = np.asarray(
            [txn.txn_id for txn in snapshot.transactions], dtype=np.int64
        )
        commits = np.asarray(
            [txn.commit_ms for txn in snapshot.transactions], dtype=np.int64
        )
        byte_sizes = np.asarray(
            [txn.byte_size for txn in snapshot.transactions], dtype=np.int64
        )
        segments = np.asarray(
            [txn.segment_id for txn in snapshot.transactions], dtype=np.int64
        )
        stats: dict[int, dict[str, int]] = {}
        for segment_id in np.unique(segments).tolist():
            mask = segments == segment_id
            stats[int(segment_id)] = {
                "segment_min_txn": int(txn_ids[mask].min()),
                "segment_max_txn": int(txn_ids[mask].max()),
                "segment_min_commit_ms": int(commits[mask].min()),
                "segment_max_commit_ms": int(commits[mask].max()),
                "segment_bytes": int(byte_sizes[mask].sum()),
            }
        return stats

    def _decisions(
        self,
        *,
        snapshot: NormalizedInput,
        view: PolicyView,
        windows: dict[str, sqlite3.Row],
        target_ms: int,
        clock_ms: int,
    ) -> list[dict[str, Any]]:
        stats = self._segment_stats(snapshot)
        decisions: list[dict[str, Any]] = []
        for segment_id in sorted(stats):
            stat = stats[segment_id]
            segment_refs = view.segment_selected.get(segment_id, {})
            conflicts = view.conflicts_for_segment(segment_id)
            paths: list[dict[str, Any]] = []
            adjusted_values: list[int] = []
            raw_cutoffs: list[int] = []
            seen_instances: set[str] = set()
            margin_total_units = 0
            precision_sensitive = False
            for instance_id in sorted(segment_refs):
                node = segment_refs[instance_id]
                window = windows.get(instance_id)
                if window is None:
                    fail(
                        "E_INSTANCE_MARGIN_MISSING",
                        "segment references an instance without a committed margin",
                        segment_id=segment_id,
                        instance_id=instance_id,
                    )
                margin_units = int(window["effective_units"])
                margin_ms = margin_units // self.config.fixed_scale_units_per_ms
                adjusted = node.cutoff_ms - margin_ms
                adjusted_values.append(adjusted)
                raw_cutoffs.append(node.cutoff_ms)
                if instance_id not in seen_instances:
                    margin_total_units += margin_units
                    seen_instances.add(instance_id)
                instance_precision = bool(window["precision_sensitive"]) or (
                    margin_units % self.config.fixed_scale_units_per_ms != 0
                )
                precision_sensitive = precision_sensitive or instance_precision
                paths.append(
                    {
                        "path": [
                            f"segment:{segment_id}",
                            f"instance:{instance_id}",
                            f"policy:{node.key}",
                        ],
                        "segment_id": segment_id,
                        "instance_id": instance_id,
                        "policy_id": node.policy.policy_id,
                        "policy_key": node.key,
                        "source_version": node.policy.source_version,
                        "retention_generation": node.policy.retention_generation,
                        "effective_at_ms": node.policy.effective_at_ms,
                        "raw_cutoff_ms": node.cutoff_ms,
                        "original_time_representation": node.original_time,
                        "conversion": node.conversion,
                        "declared_min_units": int(window["declared_min_units"]),
                        "computed_margin_units": int(window["computed_margin_units"]),
                        "margin_units": margin_units,
                        "margin_ms": margin_ms,
                        "remainder_units": int(window["remainder_units"]),
                        "catchup_adjusted_cutoff_ms": adjusted,
                        "precision_sensitive": instance_precision,
                        "selected": True,
                    }
                )
            raw_min = min(raw_cutoffs) if raw_cutoffs else None
            conservative = min(adjusted_values) if adjusted_values else None
            max_commit = stat["segment_max_commit_ms"]
            conflict_count = len(conflicts)
            if conservative is None:
                decision = "ALERT"
                reason_code = "E_NO_RETENTION_REFERENCE"
                rule = "no active retention path references this segment"
            elif conflict_count > 0:
                decision = "ALERT"
                reason_code = "E_CONFLICT_PENDING_REBUILD"
                rule = "contradictory policies were arbitrated but invalidations await rebuild"
            elif max_commit <= conservative and max_commit <= target_ms:
                decision = "ARCHIVE"
                reason_code = "E_SAFE_AND_TARGET_REACHED"
                rule = "segment_max_commit_ms <= conservative_watermark_ms and <= archive_target_ms"
            elif max_commit <= target_ms:
                decision = "RETAIN"
                reason_code = "E_RETENTION_BLOCKS_TARGET"
                rule = "segment_max_commit_ms <= archive_target_ms but > conservative_watermark_ms"
            elif max_commit <= conservative:
                decision = "RETAIN"
                reason_code = "E_TARGET_WATERMARK_NOT_REACHED"
                rule = "segment_max_commit_ms <= conservative_watermark_ms but > archive_target_ms"
            else:
                decision = "RETAIN"
                reason_code = "E_RETENTION_AND_TARGET"
                rule = "segment_max_commit_ms > conservative_watermark_ms and > archive_target_ms"
            near_boundary = False
            if conservative is not None:
                near_boundary = near_boundary or abs(max_commit - conservative) <= 1
            near_boundary = near_boundary or abs(max_commit - target_ms) <= 1
            if decision != "ALERT" and precision_sensitive and near_boundary:
                decision = "ALERT"
                reason_code = "E_PRECISION_SENSITIVE_BOUNDARY"
                rule = (
                    "fixed-point conversion discarded a remainder and the segment is within "
                    "one millisecond of a decision boundary"
                )
            rejected_paths: list[dict[str, Any]] = []
            conflict_evidence: list[dict[str, Any]] = []
            for conflict in conflicts:
                conflict_evidence.append(conflict.as_dict())
                for loser in conflict.losers:
                    rejected_paths.append(
                        {
                            "path": [
                                f"segment:{segment_id}",
                                f"instance:{conflict.instance_id}",
                                f"policy:{loser.key}",
                            ],
                            "instance_id": conflict.instance_id,
                            "policy_id": loser.policy.policy_id,
                            "policy_key": loser.key,
                            "source_version": loser.policy.source_version,
                            "retention_generation": loser.policy.retention_generation,
                            "effective_at_ms": loser.policy.effective_at_ms,
                            "raw_cutoff_ms": loser.raw_cutoff_ms,
                            "selected": False,
                            "rejection_reason": "same_segment_instance_conflict_arbitration",
                            "winning_policy_key": conflict.winner.key,
                        }
                    )
            evidence = {
                "segment": {
                    **stat,
                    "decision": decision,
                    "reason_code": reason_code,
                },
                "logical_clock_ms": clock_ms,
                "archive_target_watermark_ms": target_ms,
                "most_conservative_retention": {
                    "raw_min_retention_cutoff_ms": raw_min,
                    "conservative_retention_watermark_ms": conservative,
                    "fixed_margin_units_total": margin_total_units,
                    "margin_deduplication": "sum over unique referencing replay instances",
                },
                "retention_paths": sorted(
                    paths, key=lambda item: (item["instance_id"], item["policy_key"])
                ),
                "rejected_paths": sorted(
                    rejected_paths,
                    key=lambda item: (item["instance_id"], item["policy_key"]),
                ),
                "conflicts": conflict_evidence,
                "precision": {
                    "precision_sensitive": precision_sensitive,
                    "near_decision_boundary": near_boundary,
                    "fixed_scale_units_per_ms": self.config.fixed_scale_units_per_ms,
                },
                "decision_rule": rule,
            }
            decision_hash = _hash(
                {
                    "segment_id": segment_id,
                    "decision": decision,
                    "reason_code": reason_code,
                    "evidence": evidence,
                }
            )
            decisions.append(
                {
                    "segment_id": segment_id,
                    "decision": decision,
                    "reason_code": reason_code,
                    "segment_min_txn": stat["segment_min_txn"],
                    "segment_max_txn": stat["segment_max_txn"],
                    "segment_min_commit_ms": stat["segment_min_commit_ms"],
                    "segment_max_commit_ms": stat["segment_max_commit_ms"],
                    "segment_bytes": stat["segment_bytes"],
                    "raw_min_retention_cutoff_ms": raw_min,
                    "conservative_retention_watermark_ms": conservative,
                    "fixed_margin_units_total": margin_total_units,
                    "precision_sensitive": precision_sensitive,
                    "conflict_count": conflict_count,
                    "evidence": evidence,
                    "decision_hash": decision_hash,
                }
            )
        return decisions

    def _build_result(
        self,
        *,
        command: str,
        status: str,
        op: dict[str, Any],
        view_version: int,
        view: PolicyView,
        windows: dict[str, sqlite3.Row],
        decisions: list[dict[str, Any]],
        content_hash: str,
    ) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "command": command,
            "status": status,
            "operation_id": op["operation_id"],
            "load_version": op["load_version"],
            "view_version": view_version,
            "logical_clock_ms": op["effective_clock_ms"],
            "requested_clock_ms": op["requested_clock_ms"],
            "archive_target_watermark_ms": op["archive_target_ms"],
            "content_hash": content_hash,
            "effective_policies": [
                {
                    "policy_key": node.key,
                    "policy_id": node.policy.policy_id,
                    "instance_id": node.policy.instance_id,
                    "segment_id": node.policy.segment_id,
                    "source_version": node.policy.source_version,
                    "retention_generation": node.policy.retention_generation,
                    "effective_at_ms": node.policy.effective_at_ms,
                    "raw_cutoff_ms": node.cutoff_ms,
                    "original_time_representation": node.original_time,
                    "conversion": node.conversion,
                    "parent_policy_key": node.parent_key,
                    "merged_from": node.merged_from,
                }
                for node in view.effective_nodes()
            ],
            "instance_retention": self._instance_summaries(view, windows),
            "segments": decisions,
            "conflicts": [conflict.as_dict() for conflict in view.conflicts],
        }

    def _finalize_judge(
        self, op_id: str, *, inject_fault: bool = True
    ) -> dict[str, Any]:
        with self.ledger.global_lock(), self.ledger.transaction() as conn:
            op_row = conn.execute(
                "SELECT * FROM operations WHERE op_id = ?", (op_id,)
            ).fetchone()
            if op_row is None:
                fail("E_OPERATION_MISSING", "operation does not exist", op_id=op_id)
            if op_row["status"] == "committed":
                cached = self._cached_operation_result(conn, op_id)
                if cached is not None:
                    return cached
            op = self._operation_dict(op_row)
            calc_instances = {
                str(row["instance_id"])
                for row in conn.execute(
                    "SELECT instance_id FROM op_calculations WHERE op_id = ?", (op_id,)
                )
            }
            missing = sorted(set(op["expected_instances"]) - calc_instances)
            if missing:
                fail(
                    "E_OPERATION_INCOMPLETE",
                    "cannot finalize judge before every instance is staged",
                    operation_id=op_id,
                    missing_instances=missing,
                )
            if op_row["status"] == "staging":
                conn.execute(
                    "UPDATE operations SET status='finalizing' WHERE op_id = ?",
                    (op_id,),
                )
        if inject_fault:
            _maybe_crash("RETENTION_FAULT_AFTER_FINALIZING", 1)
        with self.ledger.global_lock(), self.ledger.transaction() as conn:
            op_row = conn.execute(
                "SELECT * FROM operations WHERE op_id = ?", (op_id,)
            ).fetchone()
            if op_row is None:
                fail("E_OPERATION_MISSING", "operation does not exist", op_id=op_id)
            if op_row["status"] == "committed":
                cached = self._cached_operation_result(conn, op_id)
                if cached is not None:
                    return cached
            op = self._operation_dict(op_row)
            snapshot = self.ledger.read_snapshot(op["load_version"], conn)
            invalidations = self.ledger.invalidations(op["load_version"], conn)
            view = build_policy_view(
                snapshot,
                clock_ms=op["effective_clock_ms"],
                invalidations=invalidations,
            )
            windows = self._window_map(conn, op_id)
            missing_margin = sorted(set(op["expected_instances"]) - set(windows))
            if missing_margin:
                fail(
                    "E_OPERATION_INCOMPLETE",
                    "staged instances have no retention window",
                    operation_id=op_id,
                    missing_instances=missing_margin,
                )
            policy_rows = self._policy_rows(view)
            conflict_rows = self._conflict_rows(view)
            decisions = self._decisions(
                snapshot=snapshot,
                view=view,
                windows=windows,
                target_ms=op["archive_target_ms"],
                clock_ms=op["effective_clock_ms"],
            )
            content_hash = self._content_hash(
                kind="judge",
                load_version=op["load_version"],
                clock_ms=op["effective_clock_ms"],
                target_ms=op["archive_target_ms"],
                policy_rows=policy_rows,
                edges=view.edges,
                decisions=decisions,
                conflicts=conflict_rows,
            )
            view_fingerprint = "judge:" + _hash(
                {
                    "operation_id": op_id,
                    "content_hash": content_hash,
                }
            )
            view_version = self.ledger.allocate_view_version_and_insert(
                conn,
                kind="judge",
                source_view_version=None,
                load_version=op["load_version"],
                logical_clock_ms=op["effective_clock_ms"],
                requested_clock_ms=op["requested_clock_ms"],
                archive_target_ms=op["archive_target_ms"],
                operation_id=op_id,
                fingerprint=view_fingerprint,
                content_hash=content_hash,
                conflict_count=len(conflict_rows),
            )
            self.ledger.insert_view_policies(conn, view_version, policy_rows)
            self.ledger.insert_view_edges(conn, view_version, view.edges)
            self.ledger.insert_instance_states(
                conn, view_version, self._instance_state_rows(view, windows)
            )
            self.ledger.insert_decisions(conn, view_version, decisions)
            self.ledger.insert_conflicts(conn, view_version, conflict_rows)
            self.ledger.set_view_pointer(conn, view_version)
            self.ledger.meta_set(
                conn, "logical_clock_watermark_ms", op["effective_clock_ms"]
            )
            conn.execute(
                "UPDATE op_calculations SET state='committed' WHERE op_id = ?", (op_id,)
            )
            conn.execute(
                "UPDATE operations SET status='committed', view_version = ? WHERE op_id = ?",
                (view_version, op_id),
            )
            result = self._build_result(
                command="judge",
                status="committed",
                op=op,
                view_version=view_version,
                view=view,
                windows=windows,
                decisions=decisions,
                content_hash=content_hash,
            )
            self._save_operation_result(conn, op_id, result)
            return result

    def _source_conflicts(
        self, conn: sqlite3.Connection, view_version: int
    ) -> list[dict[str, Any]]:
        rows = conn.execute(
            "SELECT * FROM conflict_records WHERE view_version = ? "
            "AND resolved_in_rebuild = 0 ORDER BY conflict_id",
            (view_version,),
        ).fetchall()
        return [
            {
                "conflict_id": int(row["conflict_id"]),
                "segment_id": int(row["segment_id"]),
                "instance_id": str(row["instance_id"]),
                "winning_policy_key": str(row["winning_policy_key"]),
                "losing_policy_keys": json.loads(str(row["losing_policy_keys_json"])),
                "common_ancestor_policy_key": row["common_ancestor_policy_key"],
                "evidence": json.loads(str(row["evidence_json"])),
            }
            for row in rows
        ]

    def _rebuild_plan(
        self,
        *,
        conn: sqlite3.Connection,
        source_view_version: int,
        snapshot: NormalizedInput,
    ) -> list[dict[str, Any]]:
        source_view = conn.execute(
            "SELECT * FROM view_versions WHERE view_version = ?",
            (source_view_version,),
        ).fetchone()
        if source_view is None:
            fail(
                "E_VIEW_MISSING",
                "rebuild source view does not exist",
                view_version=source_view_version,
            )
        invalidations = self.ledger.invalidations(
            int(source_view["load_version"]), conn
        )
        policy_view = build_policy_view(
            snapshot,
            clock_ms=int(source_view["logical_clock_ms"]),
            invalidations=invalidations,
        )
        plan: list[dict[str, Any]] = []
        for conflict in self._source_conflicts(conn, source_view_version):
            winner = conflict["winning_policy_key"]
            losers: set[str] = set(conflict["losing_policy_keys"])
            for loser in list(losers):
                if loser in policy_view.nodes:
                    losers.update(descendant_keys(policy_view, loser))
            for loser in sorted(losers):
                if loser == winner or loser in invalidations:
                    continue
                node = policy_view.nodes.get(loser)
                if node is None:
                    continue
                plan.append(
                    {
                        "policy_key": loser,
                        "instance_id": node.policy.instance_id,
                        "segment_id": node.policy.segment_id,
                        "winning_policy_key": winner,
                        "common_ancestor_policy_key": conflict[
                            "common_ancestor_policy_key"
                        ],
                        "conflict_id": conflict["conflict_id"],
                        "evidence": {
                            "reason": "retention_conflict_invalidation",
                            "source_view_version": source_view_version,
                            "conflict_segment_id": conflict["segment_id"],
                            "instance_id": node.policy.instance_id,
                            "winning_policy_key": winner,
                            "invalidated_policy_key": loser,
                            "common_ancestor_policy_key": conflict[
                                "common_ancestor_policy_key"
                            ],
                            "ranking": "retention_generation,effective_at_ms,source_priority,policy_key",
                            "source_policy": node.evidence(),
                        },
                    }
                )
        return plan

    def _stage_rebuild_item(self, op_id: str, item: dict[str, Any]) -> bool:
        with self.ledger.global_lock(), self.ledger.transaction() as conn:
            op = conn.execute(
                "SELECT * FROM operations WHERE op_id = ?", (op_id,)
            ).fetchone()
            if op is None:
                fail("E_OPERATION_MISSING", "operation does not exist", op_id=op_id)
            if op["status"] == "committed":
                return False
            cursor = conn.execute(
                "INSERT OR IGNORE INTO rebuild_stages("
                "op_id, policy_key, instance_id, segment_id, winning_policy_key, "
                "common_ancestor_policy_key, conflict_id, evidence_json, state) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'staged')",
                (
                    op_id,
                    item["policy_key"],
                    item["instance_id"],
                    item["segment_id"],
                    item["winning_policy_key"],
                    item["common_ancestor_policy_key"],
                    item["conflict_id"],
                    _json(item["evidence"]),
                ),
            )
            return cursor.rowcount > 0

    def rebuild(
        self,
        *,
        requested_clock_ms: int | None = None,
        inject_fault: bool = True,
    ) -> dict[str, Any]:
        requested_clock = (
            self.config.injected_clock_ms
            if requested_clock_ms is None
            else requested_clock_ms
        )
        with self.ledger.global_lock(), self.ledger.transaction() as conn:
            source_view_version = self.ledger.current_view_version(conn)
            if source_view_version is None:
                fail("E_VIEW_REQUIRED", "rebuild requires a committed view")
            source_view = conn.execute(
                "SELECT * FROM view_versions WHERE view_version = ?",
                (source_view_version,),
            ).fetchone()
            if source_view is None:
                fail("E_VIEW_MISSING", "current view does not exist")
            load_version = int(source_view["load_version"])
            clock, _watermark = self._resolve_clock(conn, requested_clock)
            snapshot = self.ledger.read_snapshot(load_version, conn)
            plan = self._rebuild_plan(
                conn=conn,
                source_view_version=source_view_version,
                snapshot=snapshot,
            )
            if not plan:
                return {
                    "schema_version": 1,
                    "command": "rebuild",
                    "status": "no_conflicts",
                    "load_version": load_version,
                    "view_version": source_view_version,
                    "logical_clock_ms": clock,
                    "archive_target_watermark_ms": self.config.archive_target_watermark_ms,
                    "invalidated_policies": [],
                    "rebuild_records": [],
                    "clean_equivalence": {
                        "all_equal": True,
                        "reason": "no unresolved conflicts",
                    },
                }
            fingerprint = _hash(
                {
                    "kind": "rebuild",
                    "load_version": load_version,
                    "source_view_version": source_view_version,
                    "effective_clock_ms": clock,
                    "archive_target_ms": self.config.archive_target_watermark_ms,
                    "invalidated_policy_keys": sorted(
                        item["policy_key"] for item in plan
                    ),
                }
            )
            op_row = conn.execute(
                "SELECT * FROM operations WHERE kind = 'rebuild' "
                "AND fingerprint = ? AND load_version = ?",
                (fingerprint, load_version),
            ).fetchone()
            if op_row is None:
                op_id = "rebuild-" + fingerprint[:24]
                expected_instances = sorted({item["instance_id"] for item in plan})
                conn.execute(
                    "INSERT INTO operations("
                    "op_id, kind, fingerprint, load_version, source_view_version, "
                    "requested_clock_ms, effective_clock_ms, archive_target_ms, "
                    "expected_instances_json, status, view_version) "
                    "VALUES (?, 'rebuild', ?, ?, ?, ?, ?, ?, ?, 'staging', NULL)",
                    (
                        op_id,
                        fingerprint,
                        load_version,
                        source_view_version,
                        requested_clock,
                        clock,
                        self.config.archive_target_watermark_ms,
                        _json(expected_instances),
                    ),
                )
            else:
                op_id = str(op_row["op_id"])
                if op_row["status"] == "committed":
                    cached = self._cached_operation_result(conn, op_id)
                    if cached is not None:
                        return cached
        for index, item in enumerate(plan, start=1):
            inserted = self._stage_rebuild_item(op_id, item)
            if inserted and inject_fault:
                with self.ledger.read_transaction() as conn:
                    count = int(
                        conn.execute(
                            "SELECT COUNT(*) AS n FROM rebuild_stages WHERE op_id = ?",
                            (op_id,),
                        ).fetchone()["n"]
                    )
                _maybe_crash("RETENTION_FAULT_REBUILD_AFTER_STAGED", count)
        return self._finalize_rebuild(op_id, inject_fault=inject_fault)

    def _finalize_rebuild(
        self, op_id: str, *, inject_fault: bool = True
    ) -> dict[str, Any]:
        with self.ledger.global_lock(), self.ledger.transaction() as conn:
            op_row = conn.execute(
                "SELECT * FROM operations WHERE op_id = ?", (op_id,)
            ).fetchone()
            if op_row is None:
                fail("E_OPERATION_MISSING", "operation does not exist", op_id=op_id)
            if op_row["status"] == "committed":
                cached = self._cached_operation_result(conn, op_id)
                if cached is not None:
                    return cached
            stage_count = int(
                conn.execute(
                    "SELECT COUNT(*) AS n FROM rebuild_stages WHERE op_id = ?",
                    (op_id,),
                ).fetchone()["n"]
            )
            if stage_count == 0:
                fail(
                    "E_OPERATION_INCOMPLETE",
                    "rebuild operation has no staged invalidations",
                    operation_id=op_id,
                )
            if op_row["status"] == "staging":
                conn.execute(
                    "UPDATE operations SET status='finalizing' WHERE op_id = ?",
                    (op_id,),
                )
        if inject_fault:
            _maybe_crash("RETENTION_FAULT_AFTER_FINALIZING", 1)
        with self.ledger.global_lock(), self.ledger.transaction() as conn:
            op_row = conn.execute(
                "SELECT * FROM operations WHERE op_id = ?", (op_id,)
            ).fetchone()
            if op_row is None:
                fail("E_OPERATION_MISSING", "operation does not exist", op_id=op_id)
            if op_row["status"] == "committed":
                cached = self._cached_operation_result(conn, op_id)
                if cached is not None:
                    return cached
            op = self._operation_dict(op_row)
            source_view_version = op["source_view_version"]
            if source_view_version is None:
                fail("E_OPERATION_STATE", "rebuild operation has no source view")
            source_view = conn.execute(
                "SELECT * FROM view_versions WHERE view_version = ?",
                (source_view_version,),
            ).fetchone()
            if source_view is None:
                fail("E_VIEW_MISSING", "rebuild source view is missing")
            snapshot = self.ledger.read_snapshot(op["load_version"], conn)
            existing_invalidations = self.ledger.invalidations(op["load_version"], conn)
            stage_rows = conn.execute(
                "SELECT * FROM rebuild_stages WHERE op_id = ? ORDER BY policy_key",
                (op_id,),
            ).fetchall()
            if not stage_rows:
                fail("E_OPERATION_INCOMPLETE", "rebuild has no staged invalidations")
            staged_keys = {str(row["policy_key"]) for row in stage_rows}
            all_invalidations = existing_invalidations | staged_keys
            view = build_policy_view(
                snapshot,
                clock_ms=op["effective_clock_ms"],
                invalidations=all_invalidations,
            )
            if view.conflicts:
                fail(
                    "E_REBUILD_STILL_CONFLICTED",
                    "rebuild still leaves contradictory policy references",
                    conflicts=[conflict.as_dict() for conflict in view.conflicts],
                )
            windows: dict[str, sqlite3.Row] = {}
            if op["effective_clock_ms"] == int(source_view["logical_clock_ms"]):
                source_states = conn.execute(
                    "SELECT * FROM view_instance_state WHERE view_version = ? "
                    "ORDER BY instance_id, policy_generation DESC, policy_key",
                    (source_view_version,),
                ).fetchall()
                for state in source_states:
                    instance_id = str(state["instance_id"])
                    if instance_id in windows:
                        continue
                    window = conn.execute(
                        "SELECT * FROM retention_windows WHERE window_id = ?",
                        (int(state["window_id"]),),
                    ).fetchone()
                    if window is not None:
                        windows[instance_id] = window
            for instance_id in sorted(
                {node.policy.instance_id for node in view.effective_nodes()}
            ):
                if instance_id in windows:
                    continue
                _generation, _window_id, _signature, window = (
                    self._calculate_margin_for_instance(
                        conn,
                        snapshot=snapshot,
                        view=view,
                        instance_id=instance_id,
                        clock_ms=op["effective_clock_ms"],
                    )
                )
                windows[instance_id] = window
            decisions = self._decisions(
                snapshot=snapshot,
                view=view,
                windows=windows,
                target_ms=op["archive_target_ms"],
                clock_ms=op["effective_clock_ms"],
            )
            clean_snapshot = replace(
                snapshot,
                policies=[
                    policy
                    for policy in snapshot.policies
                    if policy.key not in staged_keys
                ],
            )
            clean_view = build_policy_view(
                clean_snapshot,
                clock_ms=op["effective_clock_ms"],
                invalidations=existing_invalidations,
            )
            if clean_view.conflicts:
                fail(
                    "E_REBUILD_EQUIVALENCE",
                    "clean conflict-free policy evaluation still reports conflicts",
                    conflicts=[conflict.as_dict() for conflict in clean_view.conflicts],
                )
            clean_decisions = self._decisions(
                snapshot=clean_snapshot,
                view=clean_view,
                windows=windows,
                target_ms=op["archive_target_ms"],
                clock_ms=op["effective_clock_ms"],
            )
            clean_by_segment = {
                int(row["segment_id"]): str(row["decision_hash"])
                for row in clean_decisions
            }
            equivalence = {
                "all_equal": all(
                    int(row["segment_id"]) in clean_by_segment
                    and row["decision_hash"] == clean_by_segment[int(row["segment_id"])]
                    for row in decisions
                ),
                "per_segment_decision_hash": {
                    str(row["segment_id"]): row["decision_hash"] for row in decisions
                },
                "clean_policy_evaluation": {
                    str(row["segment_id"]): clean_by_segment[int(row["segment_id"])]
                    for row in decisions
                },
                "clean_effective_policy_keys": [
                    node.key for node in clean_view.effective_nodes()
                ],
            }
            if not equivalence["all_equal"]:
                fail(
                    "E_REBUILD_EQUIVALENCE",
                    "rebuilt view differs from clean conflict-free evaluation",
                    equivalence=equivalence,
                )
            policy_rows = self._policy_rows(view)
            unresolved_rows = self._conflict_rows(view)
            content_hash = self._content_hash(
                kind="rebuild",
                load_version=op["load_version"],
                clock_ms=op["effective_clock_ms"],
                target_ms=op["archive_target_ms"],
                policy_rows=policy_rows,
                edges=view.edges,
                decisions=decisions,
                conflicts=unresolved_rows,
            )
            view_fingerprint = "rebuild:" + _hash(
                {
                    "operation_id": op_id,
                    "content_hash": content_hash,
                }
            )
            view_version = self.ledger.allocate_view_version_and_insert(
                conn,
                kind="rebuild",
                source_view_version=source_view_version,
                load_version=op["load_version"],
                logical_clock_ms=op["effective_clock_ms"],
                requested_clock_ms=op["requested_clock_ms"],
                archive_target_ms=op["archive_target_ms"],
                operation_id=op_id,
                fingerprint=view_fingerprint,
                content_hash=content_hash,
                conflict_count=0,
            )
            self.ledger.insert_view_policies(conn, view_version, policy_rows)
            self.ledger.insert_view_edges(conn, view_version, view.edges)
            self.ledger.insert_instance_states(
                conn, view_version, self._instance_state_rows(view, windows)
            )
            self.ledger.insert_decisions(conn, view_version, decisions)
            resolved_conflicts = self._source_conflicts(conn, source_view_version)
            self.ledger.insert_conflicts(
                conn, view_version, resolved_conflicts, resolved=True
            )

            conflict_groups: dict[int, dict[str, Any]] = {}
            for row in stage_rows:
                evidence = json.loads(str(row["evidence_json"]))
                conflict_id = int(row["conflict_id"])
                group = conflict_groups.setdefault(
                    conflict_id,
                    {
                        "invalidated": [],
                        "winning": str(row["winning_policy_key"]),
                        "ancestor": row["common_ancestor_policy_key"],
                        "evidence": evidence,
                    },
                )
                group["invalidated"].append(str(row["policy_key"]))
                conn.execute(
                    "INSERT OR IGNORE INTO policy_invalidations("
                    "load_version, policy_key, instance_id, segment_id, "
                    "invalidated_by_policy_key, common_ancestor_policy_key, reason, "
                    "view_version, evidence_json) "
                    "VALUES (?, ?, ?, ?, ?, ?, 'retention_conflict', ?, ?)",
                    (
                        op["load_version"],
                        str(row["policy_key"]),
                        str(row["instance_id"]),
                        row["segment_id"],
                        str(row["winning_policy_key"]),
                        row["common_ancestor_policy_key"],
                        view_version,
                        str(row["evidence_json"]),
                    ),
                )
                conn.execute(
                    "INSERT OR IGNORE INTO policy_history("
                    "load_version, policy_key, source_version, source_policy_id, "
                    "instance_id, segment_id, retention_generation, state, tombstone, "
                    "view_version, related_policy_key, reason, evidence_json) "
                    "SELECT ?, policy_key, source_version, source_policy_id, instance_id, "
                    "segment_id, retention_generation, 'invalidated', 1, ?, ?, "
                    "'retention_conflict', ? FROM view_policies "
                    "WHERE view_version = ? AND policy_key = ?",
                    (
                        op["load_version"],
                        view_version,
                        str(row["winning_policy_key"]),
                        str(row["evidence_json"]),
                        source_view_version,
                        str(row["policy_key"]),
                    ),
                )
            rebuild_records: list[dict[str, Any]] = []
            for conflict_id in sorted(conflict_groups):
                group = conflict_groups[conflict_id]
                basis = {
                    "reason": "restart_from_nearest_common_policy_ancestor",
                    "source_view_version": source_view_version,
                    "conflict_id": conflict_id,
                    "winning_policy_key": group["winning"],
                    "common_ancestor_policy_key": group["ancestor"],
                    "invalidated_policy_keys": sorted(group["invalidated"]),
                    "evaluation_clock_ms": op["effective_clock_ms"],
                }
                conn.execute(
                    "INSERT INTO rebuild_records("
                    "view_version, source_view_version, conflict_id, "
                    "invalidated_policy_keys_json, common_ancestor_policy_key, "
                    "basis_json, clean_equivalence_json, logical_clock_ms) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        view_version,
                        source_view_version,
                        conflict_id,
                        _json(sorted(group["invalidated"])),
                        group["ancestor"],
                        _json(basis),
                        _json(equivalence),
                        op["effective_clock_ms"],
                    ),
                )
                rebuild_records.append(
                    {
                        "conflict_id": conflict_id,
                        "invalidated_policy_keys": sorted(group["invalidated"]),
                        "common_ancestor_policy_key": group["ancestor"],
                        "winning_policy_key": group["winning"],
                        "basis": basis,
                    }
                )
            conn.execute(
                "UPDATE rebuild_stages SET state='committed' WHERE op_id = ?", (op_id,)
            )
            self.ledger.set_view_pointer(conn, view_version)
            self.ledger.meta_set(
                conn, "logical_clock_watermark_ms", op["effective_clock_ms"]
            )
            conn.execute(
                "UPDATE operations SET status='committed', view_version = ? WHERE op_id = ?",
                (view_version, op_id),
            )
            result = {
                "schema_version": 1,
                "command": "rebuild",
                "status": "committed",
                "operation_id": op_id,
                "load_version": op["load_version"],
                "source_view_version": source_view_version,
                "view_version": view_version,
                "logical_clock_ms": op["effective_clock_ms"],
                "requested_clock_ms": op["requested_clock_ms"],
                "archive_target_watermark_ms": op["archive_target_ms"],
                "content_hash": content_hash,
                "invalidated_policies": sorted(
                    str(row["policy_key"]) for row in stage_rows
                ),
                "rebuild_records": rebuild_records,
                "conflicts_resolved": resolved_conflicts,
                "clean_equivalence": equivalence,
                "segments": decisions,
                "effective_policies": [
                    {
                        "policy_key": node.key,
                        "policy_id": node.policy.policy_id,
                        "instance_id": node.policy.instance_id,
                        "source_version": node.policy.source_version,
                        "retention_generation": node.policy.retention_generation,
                        "effective_at_ms": node.policy.effective_at_ms,
                        "raw_cutoff_ms": node.cutoff_ms,
                        "tombstone": node.tombstone,
                    }
                    for node in view.effective_nodes()
                ],
            }
            self._save_operation_result(conn, op_id, result)
            return result

    def resume(self) -> dict[str, Any]:
        recovered: list[dict[str, Any]] = []
        with self.ledger.read_transaction() as conn:
            rows = conn.execute(
                "SELECT * FROM operations WHERE status IN ('staging', 'finalizing') "
                "ORDER BY op_id"
            ).fetchall()
            operations = [self._operation_dict(row) for row in rows]
        for op in operations:
            if op["kind"] == "judge":
                for instance_id in op["expected_instances"]:
                    self._stage_instance(
                        op_id=op["operation_id"],
                        instance_id=instance_id,
                        inject_fault=False,
                    )
                result = self._finalize_judge(op["operation_id"], inject_fault=False)
                recovered.append(
                    {
                        "operation_id": op["operation_id"],
                        "kind": "judge",
                        "view_version": result.get("view_version"),
                        "status": result.get("status"),
                    }
                )
            elif op["kind"] == "rebuild":
                with self.ledger.transaction() as conn:
                    source_view_version = op["source_view_version"]
                    if source_view_version is None:
                        fail(
                            "E_OPERATION_STATE",
                            "rebuild operation has no source view",
                            operation_id=op["operation_id"],
                        )
                    snapshot = self.ledger.read_snapshot(op["load_version"], conn)
                    plan = self._rebuild_plan(
                        conn=conn,
                        source_view_version=int(source_view_version),
                        snapshot=snapshot,
                    )
                for item in plan:
                    self._stage_rebuild_item(op["operation_id"], item)
                result = self._finalize_rebuild(op["operation_id"], inject_fault=False)
                recovered.append(
                    {
                        "operation_id": op["operation_id"],
                        "kind": "rebuild",
                        "view_version": result.get("view_version"),
                        "status": result.get("status"),
                    }
                )
        return {
            "schema_version": 1,
            "command": "resume",
            "status": "recovered" if recovered else "clean",
            "recovered_operations": recovered,
            "logical_clock_watermark_ms": self._read_watermark(),
            "view_version": self._read_current_view_version(),
        }

    def _read_watermark(self) -> int | None:
        with self.ledger.read_transaction() as conn:
            return self.ledger.clock_watermark(conn)

    def _read_current_view_version(self) -> int | None:
        with self.ledger.read_transaction() as conn:
            return self.ledger.current_view_version(conn)

    def status(self) -> dict[str, Any]:
        with self.ledger.read_transaction() as conn:
            load_version = self.ledger.current_load_version(conn)
            watermark = self.ledger.clock_watermark(conn)
            view_version = self.ledger.current_view_version(conn)
            if view_version is None:
                return {
                    "schema_version": 1,
                    "command": "status",
                    "load_version": load_version,
                    "view_version": None,
                    "logical_clock_watermark_ms": watermark,
                    "effective_policies": [],
                    "invalidated_policies": [],
                    "unresolved_conflicts": [],
                }
            view = conn.execute(
                "SELECT * FROM view_versions WHERE view_version = ?", (view_version,)
            ).fetchone()
            effective = self._effective_policies(conn, view_version)
            invalidated = [
                {
                    "policy_key": str(row["policy_key"]),
                    "invalidated_by_policy_key": str(row["invalidated_by_policy_key"]),
                    "common_ancestor_policy_key": row["common_ancestor_policy_key"],
                    "reason": str(row["reason"]),
                }
                for row in conn.execute(
                    "SELECT * FROM policy_invalidations WHERE load_version = ? "
                    "ORDER BY policy_key",
                    (load_version,),
                )
            ]
            unresolved = [
                {
                    "conflict_id": int(row["conflict_id"]),
                    "segment_id": int(row["segment_id"]),
                    "instance_id": str(row["instance_id"]),
                    "winning_policy_key": str(row["winning_policy_key"]),
                    "losing_policy_keys": json.loads(
                        str(row["losing_policy_keys_json"])
                    ),
                    "common_ancestor_policy_key": row["common_ancestor_policy_key"],
                }
                for row in conn.execute(
                    "SELECT * FROM conflict_records WHERE view_version = ? "
                    "AND resolved_in_rebuild = 0 ORDER BY conflict_id",
                    (view_version,),
                )
            ]
            margins = [
                {
                    "instance_id": str(row["instance_id"]),
                    "policy_key": str(row["policy_key"]),
                    "policy_generation": int(row["policy_generation"]),
                    "cutoff_ms": int(row["cutoff_ms"]),
                    "margin_units": int(row["margin_units"]),
                    "remainder_units": int(row["remainder_units"]),
                    "precision_sensitive": bool(row["precision_sensitive"]),
                }
                for row in conn.execute(
                    "SELECT * FROM view_instance_state WHERE view_version = ? "
                    "ORDER BY instance_id, policy_key",
                    (view_version,),
                )
            ]
            return {
                "schema_version": 1,
                "command": "status",
                "load_version": load_version,
                "view_version": view_version,
                "view_kind": str(view["kind"]),
                "logical_clock_ms": int(view["logical_clock_ms"]),
                "logical_clock_watermark_ms": watermark,
                "archive_target_watermark_ms": int(view["archive_target_watermark_ms"]),
                "content_hash": str(view["content_hash"]),
                "effective_policies": effective,
                "invalidated_policies": invalidated,
                "unresolved_conflicts": unresolved,
                "instance_margins": margins,
            }

    def verify(self) -> dict[str, Any]:
        with self.ledger.read_transaction() as conn:
            load_version = self.ledger.current_load_version(conn)
            view_version = self.ledger.current_view_version(conn)
            if view_version is None:
                fail("E_VIEW_REQUIRED", "verify requires a committed view")
            view_row = conn.execute(
                "SELECT * FROM view_versions WHERE view_version = ?", (view_version,)
            ).fetchone()
            if view_row is None:
                fail("E_VIEW_MISSING", "current view does not exist")
            snapshot = self.ledger.read_snapshot(load_version, conn)
            invalidations = self.ledger.invalidations(load_version, conn)
            rebuilt = build_policy_view(
                snapshot,
                clock_ms=int(view_row["logical_clock_ms"]),
                invalidations=invalidations,
            )
            stored_policies = {
                str(row["policy_key"]): row
                for row in conn.execute(
                    "SELECT * FROM view_policies WHERE view_version = ?",
                    (view_version,),
                )
            }
            rebuild_keys = set(rebuilt.nodes)
            if set(stored_policies) != rebuild_keys:
                fail(
                    "E_VERIFY_MISMATCH",
                    "stored policy key set differs from full policy re-evaluation",
                    stored_only=sorted(set(stored_policies) - rebuild_keys),
                    rebuilt_only=sorted(rebuild_keys - set(stored_policies)),
                )
            stored_edges: dict[tuple[int, str], str] = {}
            for row in conn.execute(
                "SELECT * FROM view_edges WHERE view_version = ? ORDER BY edge_order",
                (view_version,),
            ):
                if row["parent_type"] == "segment" and row["child_type"] == "instance":
                    attrs = json.loads(str(row["attributes_json"]))
                    stored_edges[(int(row["parent_id"]), str(row["child_id"]))] = str(
                        attrs["policy_key"]
                    )
            rebuilt_edges = {
                (segment_id, instance_id): node.key
                for segment_id, refs in rebuilt.segment_selected.items()
                for instance_id, node in refs.items()
            }
            if stored_edges != rebuilt_edges:
                fail(
                    "E_VERIFY_MISMATCH",
                    "stored segment dependency edges differ from full re-evaluation",
                    stored_edges=sorted(
                        [list(key) + [value] for key, value in stored_edges.items()]
                    ),
                    rebuilt_edges=sorted(
                        [list(key) + [value] for key, value in rebuilt_edges.items()]
                    ),
                )
            stored_conflict_keys = {
                (
                    int(row["segment_id"]),
                    str(row["instance_id"]),
                    str(row["winning_policy_key"]),
                    tuple(json.loads(str(row["losing_policy_keys_json"]))),
                )
                for row in conn.execute(
                    "SELECT * FROM conflict_records WHERE view_version = ? "
                    "AND resolved_in_rebuild = 0",
                    (view_version,),
                )
            }
            rebuilt_conflict_keys = {
                (
                    conflict.segment_id,
                    conflict.instance_id,
                    conflict.winner.key,
                    tuple(node.key for node in conflict.losers),
                )
                for conflict in rebuilt.conflicts
            }
            if stored_conflict_keys != rebuilt_conflict_keys:
                fail(
                    "E_VERIFY_MISMATCH",
                    "stored unresolved conflicts differ from full re-evaluation",
                    stored=sorted(stored_conflict_keys),
                    rebuilt=sorted(rebuilt_conflict_keys),
                )
            invalidated_in_view = {
                str(row["policy_key"])
                for row in stored_policies.values()
                if bool(row["invalidated"])
            }
            if invalidated_in_view != invalidations:
                fail(
                    "E_VERIFY_MISMATCH",
                    "policy tombstones do not match invalidation ledger",
                    view_invalidated=sorted(invalidated_in_view),
                    ledger_invalidations=sorted(invalidations),
                )
            view_kind = str(view_row["kind"])
            if view_kind == "merge":
                merge_policy_rows = self._policy_rows(rebuilt)
                merge_conflict_rows = self._conflict_rows(rebuilt)
                merge_hash = self._content_hash(
                    kind="merge",
                    load_version=load_version,
                    clock_ms=int(view_row["logical_clock_ms"]),
                    target_ms=int(view_row["archive_target_watermark_ms"]),
                    policy_rows=merge_policy_rows,
                    edges=rebuilt.edges,
                    decisions=[],
                    conflicts=merge_conflict_rows,
                )
                if merge_hash != str(view_row["content_hash"]):
                    fail(
                        "E_VERIFY_MISMATCH",
                        "merge view content hash differs from full re-evaluation",
                        stored=str(view_row["content_hash"]),
                        recomputed=merge_hash,
                    )
                return {
                    "schema_version": 1,
                    "command": "verify",
                    "status": "ok",
                    "load_version": load_version,
                    "view_version": view_version,
                    "view_kind": "merge",
                    "verified_segments": 0,
                    "verified_policies": len(stored_policies),
                    "verified_edges": len(stored_edges),
                    "unresolved_conflicts": len(stored_conflict_keys),
                    "content_hash": merge_hash,
                    "decision_hashes": {},
                }
            state_rows = conn.execute(
                "SELECT * FROM view_instance_state WHERE view_version = ? "
                "ORDER BY instance_id, policy_generation DESC, policy_key",
                (view_version,),
            ).fetchall()
            if not state_rows and rebuilt.effective_nodes():
                fail(
                    "E_VERIFY_MISMATCH",
                    "view has effective policies but no committed instance margins",
                )
            normalized_states: dict[str, dict[str, Any]] = {}
            for row in state_rows:
                instance_id = str(row["instance_id"])
                if instance_id in normalized_states:
                    continue
                margin_json = json.loads(str(row["margin_json"]))
                normalized_states[instance_id] = {
                    "instance_id": instance_id,
                    "policy_generation": int(row["policy_generation"]),
                    "effective_units": int(row["margin_units"]),
                    "declared_min_units": int(row["declared_min_units"]),
                    "computed_margin_units": int(row["computed_margin_units"]),
                    "carry_in_units": int(row["carry_in_units"]),
                    "remainder_units": int(row["remainder_units"]),
                    "precision_sensitive": bool(row["precision_sensitive"]),
                    "margin_json": margin_json,
                }
            recalced = self._decisions(
                snapshot=snapshot,
                view=rebuilt,
                windows=normalized_states,
                target_ms=int(view_row["archive_target_watermark_ms"]),
                clock_ms=int(view_row["logical_clock_ms"]),
            )
            stored_decisions = {
                int(row["segment_id"]): row
                for row in conn.execute(
                    "SELECT * FROM segment_decisions WHERE view_version = ? "
                    "ORDER BY segment_id",
                    (view_version,),
                )
            }
            recalced_by_id = {int(row["segment_id"]): row for row in recalced}
            if set(stored_decisions) != set(recalced_by_id):
                fail(
                    "E_VERIFY_MISMATCH",
                    "stored decision segment set differs from re-evaluation",
                    stored=sorted(stored_decisions),
                    rebuilt=sorted(recalced_by_id),
                )
            for segment_id in sorted(stored_decisions):
                stored = stored_decisions[segment_id]
                recomputed = recalced_by_id[segment_id]
                stored_evidence = json.loads(str(stored["evidence_json"]))
                if stored_evidence != recomputed["evidence"]:
                    fail(
                        "E_VERIFY_MISMATCH",
                        "decision evidence differs from full re-evaluation",
                        segment_id=segment_id,
                    )
                for field in (
                    "decision",
                    "reason_code",
                    "raw_min_retention_cutoff_ms",
                    "conservative_retention_watermark_ms",
                    "fixed_margin_units_total",
                    "precision_sensitive",
                    "conflict_count",
                    "decision_hash",
                ):
                    stored_value = stored[field]
                    recomputed_value = recomputed[field]
                    if field == "precision_sensitive":
                        stored_value = bool(stored_value)
                    if stored_value != recomputed_value:
                        fail(
                            "E_VERIFY_MISMATCH",
                            "decision field differs from full re-evaluation",
                            segment_id=segment_id,
                            field=field,
                            stored=stored_value,
                            recomputed=recomputed_value,
                        )
            policy_rows = self._policy_rows(rebuilt)
            conflict_rows = self._conflict_rows(rebuilt)
            content_hash = self._content_hash(
                kind=str(view_row["kind"]),
                load_version=load_version,
                clock_ms=int(view_row["logical_clock_ms"]),
                target_ms=int(view_row["archive_target_watermark_ms"]),
                policy_rows=policy_rows,
                edges=rebuilt.edges,
                decisions=recalced,
                conflicts=conflict_rows,
            )
            if content_hash != str(view_row["content_hash"]):
                fail(
                    "E_VERIFY_MISMATCH",
                    "view content hash differs from full re-evaluation",
                    stored=str(view_row["content_hash"]),
                    recomputed=content_hash,
                )
            invalidated_in_view = {
                str(row["policy_key"])
                for row in stored_policies.values()
                if bool(row["invalidated"])
            }
            return {
                "schema_version": 1,
                "command": "verify",
                "status": "ok",
                "load_version": load_version,
                "view_version": view_version,
                "view_kind": str(view_row["kind"]),
                "verified_segments": len(stored_decisions),
                "verified_policies": len(stored_policies),
                "verified_edges": len(stored_edges),
                "unresolved_conflicts": len(stored_conflict_keys),
                "content_hash": content_hash,
                "decision_hashes": {
                    str(segment_id): recalced_by_id[segment_id]["decision_hash"]
                    for segment_id in sorted(recalced_by_id)
                },
            }
