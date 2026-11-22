from __future__ import annotations

import json
import os
import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .canonical import canonical_json, row_key_text, sha256_json
from .errors import WarbiterError
from .model import (
    ArbiterConfig,
    Schema,
    Script,
    Table,
    Transaction,
    evaluate_assertion,
    normalize_transaction,
    parse_config,
    parse_schema,
)
from .storage import (
    connect,
    current_policy_version,
    database_summary,
    get_policy_version_document,
    get_state,
    initialize_database,
    is_initialized,
    next_generation,
    open_initialized,
    policy_table_map,
    set_state,
    transaction,
)


PENDING_STATUS = "committed"
ACCEPTED_STATUS = "accepted"
REJECTED_CONFLICT = "rejected_conflict"
SUSPENDED_MANUAL = "suspended_manual"
ABORTED_ASSERTION = "aborted_assertion"
SUSPENDED_ASSERTION = "suspended_assertion"
SUSPENDED_ROW = "suspended_row"
SUSPENDED_REPLAY = "suspended_replay_limit"
FINAL_STATUSES = {
    ACCEPTED_STATUS,
    REJECTED_CONFLICT,
    SUSPENDED_MANUAL,
    ABORTED_ASSERTION,
    SUSPENDED_ASSERTION,
    SUSPENDED_ROW,
    SUSPENDED_REPLAY,
}


@dataclass
class RowRuntime:
    values: dict[str, Any]
    deleted: bool = False
    version: int = 0
    last_tx_id: int | None = None

    def snapshot(self) -> dict[str, Any]:
        return {
            "values": dict(sorted(self.values.items())),
            "deleted": self.deleted,
            "version": self.version,
            "last_tx_id": self.last_tx_id,
        }


def _row_key_value(text: str) -> Any:
    return json.loads(text)


def _fault(point: str, value: int | None) -> None:
    env_name = f"WARBITER_FAULT_{point.upper()}"
    raw = os.environ.get(env_name)
    if raw is None:
        return
    try:
        threshold = int(raw)
    except ValueError:
        threshold = 1
    if value is not None and value >= threshold:
        os._exit(137)


def load_schema_from_db(connection: sqlite3.Connection) -> Schema:
    raw = json.loads(get_state(connection, "schema_doc"))
    return parse_schema(raw)


def load_config_from_db(connection: sqlite3.Connection) -> ArbiterConfig:
    raw = json.loads(get_state(connection, "config_doc"))
    return parse_config(raw)


def load_database_context(path: str) -> tuple[Schema, ArbiterConfig]:
    connection = open_initialized(path)
    try:
        return load_schema_from_db(connection), load_config_from_db(connection)
    finally:
        connection.close()


def _order_key(tx_id: int, commit_clock_ms: int) -> tuple[int, int]:
    return (commit_clock_ms, tx_id)


def _load_transactions(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = connection.execute(
        """
        SELECT tx_id, tenant, start_clock_ms, commit_clock_ms, isolation,
               payload_json, payload_hash, script_hash, policy_version, status,
               status_detail, adjudication_policy_version, created_generation
        FROM tx
        ORDER BY commit_clock_ms, tx_id
        """
    ).fetchall()
    result: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        item["row_key"] = None
        result.append(item)
    return result


def _load_writes(connection: sqlite3.Connection) -> dict[int, list[dict[str, Any]]]:
    result: dict[int, list[dict[str, Any]]] = defaultdict(list)
    rows = connection.execute(
        """
        SELECT tx_id, tenant, table_name, row_key_json, final_kind,
               whole_row, coverage_json, values_json
        FROM tx_writes
        ORDER BY tx_id, tenant, table_name, row_key_json
        """
    ).fetchall()
    for row in rows:
        item = dict(row)
        item["row_key"] = _row_key_value(item["row_key_json"])
        item["coverage"] = json.loads(item["coverage_json"])
        item["values"] = json.loads(item["values_json"])
        item["whole_row"] = bool(item["whole_row"])
        result[int(item["tx_id"])].append(item)
    return result


def _load_assertions(connection: sqlite3.Connection) -> dict[int, list[dict[str, Any]]]:
    result: dict[int, list[dict[str, Any]]] = defaultdict(list)
    rows = connection.execute(
        """
        SELECT tx_id, op_index, tenant, table_name, row_key_json,
               assertion_text, recorded_observation_json
        FROM tx_assertions
        ORDER BY tx_id, op_index
        """
    ).fetchall()
    for row in rows:
        item = dict(row)
        item["row_key"] = _row_key_value(item["row_key_json"])
        item["assertion"] = json.loads(item["assertion_text"])
        item["recorded_observation"] = (
            json.loads(item["recorded_observation_json"])
            if item["recorded_observation_json"]
            else None
        )
        result[int(item["tx_id"])].append(item)
    return result


def _apply_final_row(
    state: dict[tuple[str, str, str], RowRuntime],
    final_row: dict[str, Any],
    table: Table,
    policy: dict[str, Any],
    tx_id: int,
) -> None:
    key = (
        final_row["tenant"],
        final_row["table_name"],
        final_row["row_key_json"],
    )
    runtime = state.get(key)
    if runtime is None:
        runtime = RowRuntime(values={})
        state[key] = runtime
    merge_columns = {
        column
        for column, strategy in policy.get("column_strategies", {}).items()
        if strategy == "column_merge"
    }
    values = dict(final_row["values"])
    if final_row["final_kind"] == "delete":
        runtime.values = {}
        runtime.deleted = True
    elif final_row["final_kind"] == "insert":
        if runtime.deleted:
            runtime.values = dict(values)
            runtime.deleted = False
        else:
            for column in table.column_names:
                if column in values:
                    runtime.values[column] = values[column]
                elif column in merge_columns:
                    continue
                else:
                    runtime.values.pop(column, None)
            runtime.deleted = False
    else:
        if runtime.deleted or runtime.version == 0:
            runtime.values = {table.primary_key: _row_key_value(final_row["row_key_json"])}
            runtime.deleted = False
        runtime.values.update(values)
        if final_row.get("whole_row"):
            for column in table.non_pk_columns:
                if column not in values and column not in merge_columns:
                    runtime.values.pop(column, None)
    runtime.version += 1
    runtime.last_tx_id = tx_id


def _rebuild_commit_log_locked(connection: sqlite3.Connection) -> None:
    rows = connection.execute(
        "SELECT tx_id, commit_clock_ms, script_hash FROM tx ORDER BY commit_clock_ms, tx_id"
    ).fetchall()
    connection.execute("DELETE FROM commit_log")
    for index, row in enumerate(rows, start=1):
        connection.execute(
            """
            INSERT INTO commit_log(seq, tx_id, commit_clock_ms, order_key, script_hash)
            VALUES(?, ?, ?, ?, ?)
            """,
            (
                index,
                row["tx_id"],
                row["commit_clock_ms"],
                canonical_json([row["commit_clock_ms"], row["tx_id"]]),
                row["script_hash"],
            ),
        )
    set_state(connection, "commit_seq_high_water", str(len(rows)))
    clock = max((row["commit_clock_ms"] for row in rows), default=0)
    set_state(connection, "clock_high_water", str(clock))


def _existing_conflict_keys(connection: sqlite3.Connection) -> set[tuple[Any, ...]]:
    rows = connection.execute(
        """
        SELECT left_tx_id, right_tx_id, tenant, table_name, row_key_json
        FROM conflicts
        """
    ).fetchall()
    return {
        (
            int(row["left_tx_id"]),
            int(row["right_tx_id"]),
            row["tenant"],
            row["table_name"],
            row["row_key_json"],
        )
        for row in rows
    }


def _detect_conflicts_locked(
    connection: sqlite3.Connection,
    schema: Schema,
    config: ArbiterConfig,
    policy_version: int,
) -> int:
    transactions = _load_transactions(connection)
    writes = _load_writes(connection)
    by_row: dict[tuple[str, str, str], list[tuple[dict[str, Any], list[dict[str, Any]]]]] = (
        defaultdict(list)
    )
    for tx in transactions:
        for write in writes.get(int(tx["tx_id"]), []):
            by_row[
                (write["tenant"], write["table_name"], write["row_key_json"])
            ].append((tx, write))
    existing = _existing_conflict_keys(connection)
    new_conflicts: list[tuple[int, dict[str, Any]]] = []
    for row_key in sorted(by_row):
        entries = sorted(
            by_row[row_key],
            key=lambda item: _order_key(int(item[0]["tx_id"]), int(item[0]["commit_clock_ms"])),
        )
        for left_index in range(len(entries)):
            left_tx, left_write = entries[left_index]
            left_coverage = set(left_write["coverage"])
            for right_index in range(left_index + 1, len(entries)):
                right_tx, right_write = entries[right_index]
                key = (
                    int(left_tx["tx_id"]),
                    int(right_tx["tx_id"]),
                    row_key[0],
                    row_key[1],
                    row_key[2],
                )
                if key in existing:
                    continue
                intersection = sorted(left_coverage & set(right_write["coverage"]))
                concurrent = (
                    int(right_tx["start_clock_ms"])
                    <= int(left_tx["commit_clock_ms"]) + config.conflict_tolerance_ms
                )
                if not concurrent:
                    kind = "serialized"
                elif intersection:
                    kind = "true"
                else:
                    kind = "pseudo"
                cursor = connection.execute(
                    """
                    INSERT INTO conflicts(
                        generation, left_tx_id, right_tx_id, tenant, table_name,
                        row_key_json, kind, columns_json, policy_version, status
                    )
                    VALUES(0, ?, ?, ?, ?, ?, ?, ?, ?, 'pending')
                    """,
                    (
                        int(left_tx["tx_id"]),
                        int(right_tx["tx_id"]),
                        row_key[0],
                        row_key[1],
                        row_key[2],
                        kind,
                        canonical_json(intersection),
                        policy_version,
                    ),
                )
                conflict_id = int(cursor.lastrowid)
                skeleton = {
                    "schema": "warbiter-conflict-evidence-v1",
                    "conflict_id": conflict_id,
                    "kind": kind,
                    "tenant": row_key[0],
                    "table": row_key[1],
                    "row_key": _row_key_value(row_key[2]),
                    "left_tx_id": int(left_tx["tx_id"]),
                    "right_tx_id": int(right_tx["tx_id"]),
                    "left_commit_clock_ms": int(left_tx["commit_clock_ms"]),
                    "right_commit_clock_ms": int(right_tx["commit_clock_ms"]),
                    "contended_columns": intersection,
                    "left_write_columns": left_write["coverage"],
                    "right_write_columns": right_write["coverage"],
                    "policy_version": policy_version,
                    "decision": "pending",
                }
                skeleton_text = canonical_json(skeleton)
                connection.execute(
                    """
                    UPDATE conflicts
                    SET evidence_json=?, evidence_hash=?
                    WHERE conflict_id=?
                    """,
                    (skeleton_text, sha256_json(skeleton), conflict_id),
                )
                existing.add(key)
                new_conflicts.append((conflict_id, skeleton))
    if new_conflicts:
        generation = next_generation(connection, "detection_generation")
        ids = [conflict_id for conflict_id, _ in new_conflicts]
        connection.executemany(
            "UPDATE conflicts SET generation=? WHERE conflict_id=?",
            [(generation, conflict_id) for conflict_id in ids],
        )
        _refresh_pending_digest_locked(connection)
    else:
        generation = int(get_state(connection, "detection_generation"))
    return len(new_conflicts)


def _refresh_pending_digest_locked(connection: sqlite3.Connection) -> str:
    rows = connection.execute(
        """
        SELECT left_tx_id, right_tx_id, tenant, table_name, row_key_json,
               kind, policy_version
        FROM conflicts
        WHERE status='pending'
        ORDER BY right_tx_id, left_tx_id, tenant, table_name, row_key_json
        """
    ).fetchall()
    payload = [dict(row) for row in rows]
    digest = sha256_json(payload)
    set_state(connection, "pending_conflict_digest", digest)
    return digest


def detect_conflicts(path: str) -> dict[str, Any]:
    connection = open_initialized(path)
    try:
        schema = load_schema_from_db(connection)
        config = load_config_from_db(connection)
        with transaction(connection):
            policy_version = current_policy_version(connection)
            inserted = _detect_conflicts_locked(
                connection, schema, config, policy_version
            )
            total = connection.execute("SELECT COUNT(*) AS n FROM conflicts").fetchone()[
                "n"
            ]
            pending = connection.execute(
                "SELECT COUNT(*) AS n FROM conflicts WHERE status='pending'"
            ).fetchone()["n"]
        return {
            "detected": inserted,
            "conflicts_total": int(total),
            "conflicts_pending": int(pending),
            "pending_conflict_digest": get_state(
                connection, "pending_conflict_digest"
            ),
        }
    finally:
        connection.close()


def apply_script(
    path: str,
    script: Script,
    schema: Schema,
    config: ArbiterConfig,
    *,
    do_resolve: bool = False,
    fault_after: int | None = None,
) -> dict[str, Any]:
    if path != ":memory:":
        target = Path(path)
        if not target.exists():
            initialize_database(path, schema, config)
        else:
            connection_check = connect(path)
            try:
                initialized = is_initialized(connection_check)
            finally:
                connection_check.close()
            if not initialized:
                initialize_database(path, schema, config)
    connection = open_initialized(path)
    inserted_tx: list[int] = []
    skipped_tx: list[int] = []
    try:
        with transaction(connection):
            stored_schema = load_schema_from_db(connection)
            if sha256_json(stored_schema.physical_document()) != sha256_json(
                schema.physical_document()
            ):
                raise WarbiterError(
                    "E_SCHEMA_CHANGED",
                    "script schema does not match initialized database",
                )
            if sha256_json(stored_schema.policy_document()) != sha256_json(
                schema.policy_document()
            ):
                raise WarbiterError(
                    "E_POLICY_CHANGED",
                    "policy differs from the current version; use init or policy",
                )
            stored_policy = current_policy_version(connection)
            finalized_rows = connection.execute(
                """
                SELECT tx_id, commit_clock_ms FROM tx
                WHERE status <> 'committed'
                ORDER BY commit_clock_ms DESC, tx_id DESC LIMIT 1
                """
            ).fetchone()
            finalized_order = (
                (int(finalized_rows["commit_clock_ms"]), int(finalized_rows["tx_id"]))
                if finalized_rows is not None
                else None
            )
            new_transactions: list[Transaction] = []
            for tx in sorted(
                script.transactions,
                key=lambda item: _order_key(item.tx_id, item.commit_clock_ms),
            ):
                existing = connection.execute(
                    "SELECT payload_hash FROM tx WHERE tx_id=?", (tx.tx_id,)
                ).fetchone()
                if existing is not None:
                    if existing["payload_hash"] == tx.payload_hash:
                        skipped_tx.append(tx.tx_id)
                        continue
                    raise WarbiterError(
                        "E_TX_CONFLICT",
                        f"transaction {tx.tx_id} already exists with a different payload",
                        {"tx_id": tx.tx_id},
                    )
                if finalized_order is not None and _order_key(
                    tx.tx_id, tx.commit_clock_ms
                ) <= finalized_order:
                    raise WarbiterError(
                        "E_COMMIT_ORDER",
                        "new transaction would precede an already adjudicated transaction",
                        {
                            "tx_id": tx.tx_id,
                            "commit_order": list(
                                _order_key(tx.tx_id, tx.commit_clock_ms)
                            ),
                            "finalized_high_water": list(finalized_order),
                        },
                    )
                new_transactions.append(tx)
            if new_transactions:
                now_generation = int(get_state(connection, "detection_generation"))
                connection.execute(
                    """
                    INSERT OR IGNORE INTO scripts(
                        script_hash, payload_json, tx_count, inserted_count,
                        skipped_count, created_generation
                    )
                    VALUES(?, ?, ?, 0, 0, ?)
                    """,
                    (
                        script.script_hash,
                        canonical_json(script.document),
                        len(script.transactions),
                        now_generation,
                    ),
                )
                for tx in new_transactions:
                    normalized = normalize_transaction(tx, stored_schema)
                    connection.execute(
                        """
                        INSERT INTO tx(
                            tx_id, tenant, start_clock_ms, commit_clock_ms,
                            isolation, payload_json, payload_hash, script_hash,
                            policy_version, status, status_detail,
                            adjudication_policy_version, created_generation
                        )
                        VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, 'committed', '',
                               NULL, ?)
                        """,
                        (
                            tx.tx_id,
                            tx.tenant,
                            tx.start_clock_ms,
                            tx.commit_clock_ms,
                            tx.isolation,
                            canonical_json(tx.payload),
                            tx.payload_hash,
                            script.script_hash,
                            stored_policy,
                            now_generation,
                        ),
                    )
                    for final_row in normalized.final_rows:
                        connection.execute(
                            """
                            INSERT INTO tx_writes(
                                tx_id, tenant, table_name, row_key_json,
                                final_kind, whole_row, coverage_json, values_json
                            )
                            VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                final_row.tx_id,
                                final_row.tenant,
                                final_row.table,
                                final_row.row_key_text,
                                final_row.final_kind,
                                1 if final_row.whole_row else 0,
                                canonical_json(list(final_row.coverage)),
                                canonical_json(final_row.values),
                            ),
                        )
                    for audit in normalized.audit_rows:
                        connection.execute(
                            """
                            INSERT INTO tx_audit(
                                tx_id, op_index, op_type, tenant, table_name,
                                row_key_json, column_name, before_json, after_json,
                                assertion_text, recorded_observation_json
                            )
                            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                audit["tx_id"],
                                audit["op_index"],
                                audit["op_type"],
                                audit["tenant"],
                                audit["table"],
                                audit["row_key_text"],
                                audit["column_name"],
                                canonical_json(audit["before"])
                                if audit["before"] is not None
                                else None,
                                canonical_json(audit["after"])
                                if audit["after"] is not None
                                else None,
                                audit["assertion"],
                                audit["observation"],
                            ),
                        )
                    for assertion in normalized.assertions:
                        connection.execute(
                            """
                            INSERT INTO tx_assertions(
                                tx_id, op_index, tenant, table_name, row_key_json,
                                assertion_text, recorded_observation_json
                            )
                            VALUES(?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                assertion["tx_id"],
                                assertion["op_index"],
                                assertion["tenant"],
                                assertion["table"],
                                assertion["row_key_text"],
                                assertion["assertion_text"],
                                canonical_json(assertion["recorded_observation"])
                                if assertion["recorded_observation"] is not None
                                else None,
                            ),
                        )
                    inserted_tx.append(tx.tx_id)
                    _fault("apply", len(inserted_tx))
                connection.execute(
                    """
                    UPDATE scripts
                    SET inserted_count=?, skipped_count=?
                    WHERE script_hash=?
                    """,
                    (len(new_transactions), len(skipped_tx), script.script_hash),
                )
            _rebuild_commit_log_locked(connection)
            detected = _detect_conflicts_locked(
                connection, stored_schema, config, stored_policy
            )
        summary = database_summary(connection)
    finally:
        connection.close()
    result = {
        "script_hash": script.script_hash,
        "inserted": sorted(inserted_tx),
        "skipped_idempotent": sorted(skipped_tx),
        "conflicts_detected": detected,
        "database": summary,
    }
    if do_resolve:
        result["resolution"] = resolve_pending(path)
    return result


def _get_runtime(
    state: dict[tuple[str, str, str], RowRuntime],
    key: tuple[str, str, str],
) -> RowRuntime:
    runtime = state.get(key)
    if runtime is None:
        runtime = RowRuntime(values={})
        state[key] = runtime
    return runtime


def _capture_snapshot(
    state: dict[tuple[str, str, str], RowRuntime],
    key: tuple[str, str, str],
) -> dict[str, Any]:
    runtime = state.get(key)
    if runtime is None:
        return {
            "values": {},
            "deleted": False,
            "version": 0,
            "last_tx_id": None,
        }
    return runtime.snapshot()


def _column_strategy(
    table_policy: dict[str, Any],
    right_write: dict[str, Any],
    column: str,
) -> tuple[str, str | None]:
    if right_write["final_kind"] == "delete":
        return str(table_policy["row_strategy"]), None
    column_strategies = table_policy.get("column_strategies", {})
    strategy = column_strategies.get(column, table_policy["row_strategy"])
    if strategy == "column_merge":
        return "column_merge", str(table_policy.get("same_column_merge", "last_write_wins"))
    return str(strategy), None


def _evaluate_conflict_policy(
    table_policy: dict[str, Any],
    right_write: dict[str, Any],
    columns: Iterable[str],
) -> dict[str, Any]:
    decisions: list[dict[str, Any]] = []
    for column in sorted(columns):
        strategy, fallback = _column_strategy(table_policy, right_write, column)
        effective = fallback if strategy == "column_merge" else strategy
        decisions.append(
            {
                "column": column,
                "applicable_strategy": strategy,
                "same_column_fallback": fallback,
                "effective_strategy": effective,
            }
        )
    manual = any(item["effective_strategy"] == "manual_review" for item in decisions)
    reject = any(item["effective_strategy"] == "reject_rollback" for item in decisions)
    return {
        "manual": manual,
        "reject": reject,
        "columns": decisions,
    }


def _decode_row_key(text: str) -> Any:
    return json.loads(text)


def _assertion_evidence_payload(
    assertion: dict[str, Any],
    snapshot: dict[str, Any],
    *,
    tx_id: int,
    action: str,
    clock: int,
) -> dict[str, Any]:
    result = bool(assertion["result"])
    return {
        "schema": "warbiter-assertion-evidence-v1",
        "tx_id": tx_id,
        "op_index": int(assertion["op_index"]),
        "tenant": assertion["tenant"],
        "table": assertion["table_name"],
        "row_key": _decode_row_key(assertion["row_key_json"]),
        "assertion_text": assertion["assertion_text"],
        "recorded_observation": assertion["recorded_observation"],
        "record_version": int(snapshot["version"]),
        "record_values": snapshot["values"],
        "record_deleted": bool(snapshot["deleted"]),
        "record_last_tx_id": snapshot["last_tx_id"],
        "result": result,
        "action": action,
        "evaluated_at_clock": clock,
        "basis": "committed_state_at_arbitration",
        "recorded_observation_used": False,
    }


def _conflict_decision_for_status(status: str) -> str:
    return {
        ACCEPTED_STATUS: "later_accepted",
        REJECTED_CONFLICT: "later_rejected",
        SUSPENDED_MANUAL: "later_suspended_manual",
        ABORTED_ASSERTION: "assertion_aborted",
        SUSPENDED_ASSERTION: "assertion_suspended",
        SUSPENDED_ROW: "row_suspended",
        SUSPENDED_REPLAY: "replay_limit",
    }.get(status, "inactive_prior")


def _build_conflict_evidence(
    connection: sqlite3.Connection,
    conflict: dict[str, Any],
    tx_by_id: dict[int, dict[str, Any]],
    writes_by_tx: dict[int, list[dict[str, Any]]],
    snapshots: dict[int, dict[tuple[str, str, str], dict[str, Any]]],
    assertion_results: dict[int, list[dict[str, Any]]],
    schema: Schema,
    config: ArbiterConfig,
    clock: int,
) -> dict[str, Any]:
    left_id = int(conflict["left_tx_id"])
    right_id = int(conflict["right_tx_id"])
    left_tx = tx_by_id[left_id]
    right_tx = tx_by_id[right_id]
    key = (
        conflict["tenant"],
        conflict["table_name"],
        conflict["row_key_json"],
    )
    table = schema.get_table(key[0], key[1])
    if table is None:
        raise WarbiterError("E_DB", "conflict refers to unknown table")
    left_write = next(
        item for item in writes_by_tx[left_id] if (
            item["tenant"],
            item["table_name"],
            item["row_key_json"],
        )
        == key
    )
    right_write = next(
        item for item in writes_by_tx[right_id] if (
            item["tenant"],
            item["table_name"],
            item["row_key_json"],
        )
        == key
    )
    left_snapshot_pair = snapshots[left_id][key]
    right_snapshot_pair = snapshots[right_id][key]
    left_snapshot = left_snapshot_pair["before"]
    right_snapshot = right_snapshot_pair["before"]
    policy_version = int(conflict["policy_version"])
    policy_document = get_policy_version_document(connection, policy_version)
    policy_map = policy_table_map(policy_document)
    table_policy = policy_map.get((key[0], key[1]), table.policy.as_dict())
    columns = json.loads(conflict["columns_json"])
    policy_evaluation = _evaluate_conflict_policy(table_policy, right_write, columns)
    right_status = right_tx["status"]
    left_status = left_tx["status"]
    kind = conflict["kind"]
    if kind == "pseudo":
        decision = "safe_merge"
        winner_tx_id: int | None = None
    elif kind == "serialized":
        decision = "serialized_no_conflict"
        winner_tx_id = right_id if right_status == ACCEPTED_STATUS else left_id
    elif right_status == ACCEPTED_STATUS:
        decision = (
            "later_accepted"
            if left_status == ACCEPTED_STATUS
            else "later_accepted_inactive_prior"
        )
        winner_tx_id = right_id
    else:
        decision = _conflict_decision_for_status(right_status)
        winner_tx_id = left_id if left_status == ACCEPTED_STATUS else None
    left_after = left_write["values"] if left_write["final_kind"] != "delete" else {}
    right_after = right_write["values"] if right_write["final_kind"] != "delete" else {}
    column_evidence: list[dict[str, Any]] = []
    rejected_candidates: set[str] = set()
    allowed = list(table_policy.get("allowed_strategies", []))
    for decision_item in policy_evaluation["columns"]:
        column = decision_item["column"]
        for candidate in allowed:
            if candidate not in {
                decision_item["applicable_strategy"],
                decision_item["effective_strategy"],
            }:
                rejected_candidates.add(candidate)
        column_evidence.append(
            {
                **decision_item,
                "left_before": left_snapshot["values"].get(column),
                "left_after": left_after.get(column),
                "right_before": right_snapshot["values"].get(column),
                "right_after": right_after.get(column),
                "winner_tx_id": winner_tx_id,
                "candidate_strategies": allowed,
                "rejected_candidates": [
                    candidate
                    for candidate in allowed
                    if candidate
                    not in {
                        decision_item["applicable_strategy"],
                        decision_item["effective_strategy"],
                    }
                ],
            }
        )
    assertion_items: list[dict[str, Any]] = []
    for assertion in assertion_results.get(right_id, []):
        action = assertion["action"]
        payload = _assertion_evidence_payload(
            assertion,
            assertion["snapshot"],
            tx_id=right_id,
            action=action,
            clock=clock,
        )
        assertion_items.append(payload)
    basis = {
        "intervals_overlap": kind != "serialized",
        "write_columns_overlap": bool(columns),
        "column_sets_disjoint": kind == "pseudo",
        "conflict_tolerance_ms": config.conflict_tolerance_ms,
        "left_interval": [
            int(left_tx["start_clock_ms"]),
            int(left_tx["commit_clock_ms"]),
        ],
        "right_interval": [
            int(right_tx["start_clock_ms"]),
            int(right_tx["commit_clock_ms"]),
        ],
    }
    evidence = {
        "schema": "warbiter-conflict-evidence-v1",
        "conflict_id": int(conflict["conflict_id"]),
        "detected_generation": int(conflict["generation"]),
        "policy_version": policy_version,
        "tenant": key[0],
        "table": key[1],
        "row_key": _decode_row_key(key[2]),
        "left": {
            "tx_id": left_id,
            "status": left_status,
            "commit_clock_ms": int(left_tx["commit_clock_ms"]),
            "write_columns": left_write["coverage"],
            "whole_row": bool(left_write["whole_row"]),
            "before_values": left_snapshot["values"],
            "before_version": left_snapshot["version"],
            "after_values": left_after,
        },
        "right": {
            "tx_id": right_id,
            "status": right_status,
            "commit_clock_ms": int(right_tx["commit_clock_ms"]),
            "write_columns": right_write["coverage"],
            "whole_row": bool(right_write["whole_row"]),
            "before_values": right_snapshot["values"],
            "before_version": right_snapshot["version"],
            "after_values": right_after,
        },
        "kind": kind,
        "contended_columns": columns,
        "detection_basis": basis,
        "applicable_policy": {
            "policy_version": policy_version,
            "table_policy": table_policy,
            "columns": policy_evaluation["columns"],
        },
        "column_evidence": column_evidence,
        "rejected_candidate_strategies": sorted(rejected_candidates),
        "assertion_evidence": assertion_items,
        "decision": decision,
        "winner_tx_id": winner_tx_id,
        "resolved_at_clock": clock,
    }
    return evidence


def _persist_row_state_locked(
    connection: sqlite3.Connection,
    state: dict[tuple[str, str, str], RowRuntime],
) -> None:
    connection.execute("DELETE FROM row_state")
    for key in sorted(state):
        runtime = state[key]
        connection.execute(
            """
            INSERT INTO row_state(
                tenant, table_name, row_key_json, values_json, deleted,
                version, last_tx_id
            )
            VALUES(?, ?, ?, ?, ?, ?, ?)
            """,
            (
                key[0],
                key[1],
                key[2],
                canonical_json(runtime.values),
                1 if runtime.deleted else 0,
                runtime.version,
                runtime.last_tx_id,
            ),
        )


def _persist_effective_writes_locked(
    connection: sqlite3.Connection,
    transactions: list[dict[str, Any]],
    writes_by_tx: dict[int, list[dict[str, Any]]],
    schema: Schema,
) -> None:
    conflict_rows = connection.execute(
        """
        SELECT conflict_id, left_tx_id, right_tx_id, tenant, table_name,
               row_key_json, columns_json
        FROM conflicts
        ORDER BY conflict_id
        """
    ).fetchall()
    conflicts_by_tx_row: dict[tuple[int, str, str, str], list[int]] = defaultdict(list)
    for row in conflict_rows:
        key = (
            int(row["right_tx_id"]),
            row["tenant"],
            row["table_name"],
            row["row_key_json"],
        )
        conflicts_by_tx_row[key].append(int(row["conflict_id"]))
        left_key = (
            int(row["left_tx_id"]),
            row["tenant"],
            row["table_name"],
            row["row_key_json"],
        )
        conflicts_by_tx_row[left_key].append(int(row["conflict_id"]))
    connection.execute("DELETE FROM effective_writes")
    event_seq = 0
    for tx in transactions:
        if tx["status"] != ACCEPTED_STATUS:
            continue
        for final_row in writes_by_tx.get(int(tx["tx_id"]), []):
            table = schema.get_table(final_row["tenant"], final_row["table_name"])
            if table is None:
                raise WarbiterError("E_DB", "write refers to unknown table")
            key = (
                int(tx["tx_id"]),
                final_row["tenant"],
                final_row["table_name"],
                final_row["row_key_json"],
            )
            conflict_ids = sorted(set(conflicts_by_tx_row.get(key, [])))
            if final_row["final_kind"] == "delete":
                for column in table.column_names:
                    event_seq += 1
                    connection.execute(
                        """
                        INSERT INTO effective_writes(
                            event_seq, tx_id, tenant, table_name, row_key_json,
                            column_name, action, value_json, conflict_ids_json
                        )
                        VALUES(?, ?, ?, ?, ?, ?, 'delete', NULL, ?)
                        """,
                        (
                            event_seq,
                            int(tx["tx_id"]),
                            final_row["tenant"],
                            final_row["table_name"],
                            final_row["row_key_json"],
                            column,
                            canonical_json(conflict_ids),
                        ),
                    )
            else:
                for column, value in sorted(final_row["values"].items()):
                    event_seq += 1
                    connection.execute(
                        """
                        INSERT INTO effective_writes(
                            event_seq, tx_id, tenant, table_name, row_key_json,
                            column_name, action, value_json, conflict_ids_json
                        )
                        VALUES(?, ?, ?, ?, ?, ?, 'set', ?, ?)
                        """,
                        (
                            event_seq,
                            int(tx["tx_id"]),
                            final_row["tenant"],
                            final_row["table_name"],
                            final_row["row_key_json"],
                            column,
                            canonical_json(value),
                            canonical_json(conflict_ids),
                        ),
                    )


def _ledger_document(connection: sqlite3.Connection) -> dict[str, Any]:
    rows = connection.execute(
        """
        SELECT c.*, l.commit_clock_ms AS left_clock, r.commit_clock_ms AS right_clock
        FROM conflicts c
        JOIN tx l ON l.tx_id = c.left_tx_id
        JOIN tx r ON r.tx_id = c.right_tx_id
        ORDER BY r.commit_clock_ms, r.tx_id, l.commit_clock_ms, l.tx_id,
                 c.tenant, c.table_name, c.row_key_json
        """
    ).fetchall()
    items: list[dict[str, Any]] = []
    for row in rows:
        items.append(
            {
                "conflict_id": int(row["conflict_id"]),
                "generation": int(row["generation"]),
                "left_tx_id": int(row["left_tx_id"]),
                "right_tx_id": int(row["right_tx_id"]),
                "tenant": row["tenant"],
                "table": row["table_name"],
                "row_key": _decode_row_key(row["row_key_json"]),
                "kind": row["kind"],
                "columns": json.loads(row["columns_json"]),
                "policy_version": int(row["policy_version"]),
                "status": row["status"],
                "replay_count": int(row["replay_count"]),
                "decision": row["decision"],
                "evidence_hash": row["evidence_hash"],
                "evidence": json.loads(row["evidence_json"]),
            }
        )
    return {"schema": "warbiter-ledger-v1", "entries": items}


def _refresh_ledger_locked(
    connection: sqlite3.Connection,
    config: ArbiterConfig,
) -> dict[str, Any]:
    document = _ledger_document(connection)
    digest = sha256_json(document)
    generation = next_generation(connection, "resolution_generation")
    created_clock = int(get_state(connection, "clock_high_water"))
    connection.execute(
        """
        INSERT INTO ledger_versions(generation, ledger_json, digest, created_clock)
        VALUES(?, ?, ?, ?)
        """,
        (generation, canonical_json(document), digest, created_clock),
    )
    set_state(connection, "ledger_pointer_generation", str(generation))
    cutoff = generation - config.ledger_retention_generations
    if cutoff > 0:
        connection.execute(
            "DELETE FROM ledger_versions WHERE generation <= ?", (cutoff,)
        )
    return {
        "ledger_generation": generation,
        "ledger_digest": digest,
        "ledger_entries": len(document["entries"]),
    }


def _status_counts(connection: sqlite3.Connection) -> dict[str, Any]:
    tx_counts = {
        row["status"]: int(row["n"])
        for row in connection.execute(
            "SELECT status, COUNT(*) AS n FROM tx GROUP BY status ORDER BY status"
        ).fetchall()
    }
    conflict_counts = {
        row["status"]: int(row["n"])
        for row in connection.execute(
            "SELECT status, COUNT(*) AS n FROM conflicts GROUP BY status ORDER BY status"
        ).fetchall()
    }
    kind_counts = {
        row["kind"]: int(row["n"])
        for row in connection.execute(
            "SELECT kind, COUNT(*) AS n FROM conflicts GROUP BY kind ORDER BY kind"
        ).fetchall()
    }
    decision_counts = {
        row["decision"]: int(row["n"])
        for row in connection.execute(
            """
            SELECT decision, COUNT(*) AS n FROM conflicts
            WHERE decision <> ''
            GROUP BY decision ORDER BY decision
            """
        ).fetchall()
    }
    return {
        "transactions": tx_counts,
        "conflicts_by_status": conflict_counts,
        "conflicts_by_kind": kind_counts,
        "conflicts_by_decision": decision_counts,
    }


def resolve_pending(path: str) -> dict[str, Any]:
    connection = open_initialized(path)
    try:
        schema = load_schema_from_db(connection)
        config = load_config_from_db(connection)
        with transaction(connection):
            connection.execute(
                "UPDATE conflicts SET status='pending' WHERE status='resolving'"
            )
            current_policy = current_policy_version(connection)
            inserted_conflicts = _detect_conflicts_locked(
                connection, schema, config, current_policy
            )
            pending_count = connection.execute(
                "SELECT COUNT(*) AS n FROM conflicts WHERE status='pending'"
            ).fetchone()["n"]
            committed_count = connection.execute(
                "SELECT COUNT(*) AS n FROM tx WHERE status='committed'"
            ).fetchone()["n"]
            if pending_count == 0 and committed_count == 0:
                summary = database_summary(connection)
                return {
                    "changed": False,
                    "resolved_conflicts": 0,
                    "detected_conflicts": inserted_conflicts,
                    "database": summary,
                    "status_counts": _status_counts(connection),
                }
            transactions = _load_transactions(connection)
            tx_by_id = {int(tx["tx_id"]): tx for tx in transactions}
            writes_by_tx = _load_writes(connection)
            assertions_by_tx = _load_assertions(connection)
            pending_conflicts = [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT * FROM conflicts
                    WHERE status='pending'
                    ORDER BY right_tx_id, left_tx_id, conflict_id
                    """
                ).fetchall()
            ]
            pending_by_right: dict[int, list[dict[str, Any]]] = defaultdict(list)
            for conflict in pending_conflicts:
                pending_by_right[int(conflict["right_tx_id"])].append(conflict)
            suspensions = {
                (row["tenant"], row["table_name"], row["row_key_json"]): dict(row)
                for row in connection.execute(
                    "SELECT * FROM suspensions ORDER BY tenant, table_name, row_key_json"
                ).fetchall()
            }
            state: dict[tuple[str, str, str], RowRuntime] = {}
            snapshots: dict[
                int, dict[tuple[str, str, str], dict[str, Any]]
            ] = defaultdict(dict)
            assertion_results: dict[int, list[dict[str, Any]]] = defaultdict(list)
            policy_cache: dict[
                int, dict[tuple[str, str], dict[str, Any]]
            ] = {}
            processed = 0
            for tx in transactions:
                tx_id = int(tx["tx_id"])
                touched_keys = [
                    (
                        final_row["tenant"],
                        final_row["table_name"],
                        final_row["row_key_json"],
                    )
                    for final_row in writes_by_tx.get(tx_id, [])
                ]
                before = {
                    key: _capture_snapshot(state, key) for key in touched_keys
                }
                status = tx["status"]
                if status == ACCEPTED_STATUS:
                    accepted_policy_version = int(
                        tx["adjudication_policy_version"] or tx["policy_version"]
                    )
                    policy_map = policy_cache.get(accepted_policy_version)
                    if policy_map is None:
                        policy_map = policy_table_map(
                            get_policy_version_document(
                                connection, accepted_policy_version
                            )
                        )
                        policy_cache[accepted_policy_version] = policy_map
                    for final_row in writes_by_tx.get(tx_id, []):
                        table = schema.get_table(
                            final_row["tenant"], final_row["table_name"]
                        )
                        if table is None:
                            raise WarbiterError(
                                "E_DB", "write refers to unknown table"
                            )
                        table_policy = policy_map.get(
                            (final_row["tenant"], final_row["table_name"]),
                            table.policy.as_dict(),
                        )
                        _apply_final_row(
                            state, final_row, table, table_policy, tx_id
                        )
                if status not in FINAL_STATUSES:
                    if status != PENDING_STATUS:
                        raise WarbiterError(
                            "E_DB", f"unknown transaction status {status}"
                        )
                    processed += 1
                    status = PENDING_STATUS
                    detail_parts: list[str] = []
                    current_clock = int(get_state(connection, "clock_high_water"))
                    assertion_failure = False
                    assertion_manual = False
                    for assertion in assertions_by_tx.get(tx_id, []):
                        key = (
                            assertion["tenant"],
                            assertion["table_name"],
                            assertion["row_key_json"],
                        )
                        assertion_snapshot = before.get(
                            key, _capture_snapshot(state, key)
                        )
                        result = evaluate_assertion(
                            assertion["assertion"],
                            assertion_snapshot["values"]
                            if not assertion_snapshot["deleted"]
                            else {},
                        )
                        action = (
                            "none"
                            if result
                            else (
                                "manual_review"
                                if config.assertion_failure_action == "manual_review"
                                else "rollback"
                            )
                        )
                        record = {
                            **assertion,
                            "result": result,
                            "action": action,
                            "snapshot": assertion_snapshot,
                            "evaluated_at_clock": current_clock,
                        }
                        assertion_results[tx_id].append(record)
                        if not result:
                            assertion_failure = True
                            if action == "manual_review":
                                assertion_manual = True
                    if assertion_failure:
                        status = (
                            SUSPENDED_ASSERTION
                            if assertion_manual
                            else ABORTED_ASSERTION
                        )
                        detail_parts.append("assertion_re_evaluation_failed")
                    suspended_keys = [
                        key for key in touched_keys if key in suspensions
                    ]
                    if status == PENDING_STATUS and suspended_keys:
                        status = SUSPENDED_ROW
                        detail_parts.append("row_already_suspended")
                    manual_conflict_rows: list[tuple[tuple[str, str, str], int]] = []
                    if status == PENDING_STATUS:
                        conflict_manual = False
                        conflict_reject = False
                        for conflict in pending_by_right.get(tx_id, []):
                            if int(conflict["replay_count"]) + 1 > config.max_conflict_replay:
                                status = SUSPENDED_REPLAY
                                detail_parts.append("conflict_replay_limit")
                                manual_conflict_rows.append(
                                    (
                                        (
                                            conflict["tenant"],
                                            conflict["table_name"],
                                            conflict["row_key_json"],
                                        ),
                                        int(conflict["conflict_id"]),
                                    )
                                )
                                break
                            left_tx = tx_by_id[int(conflict["left_tx_id"])]
                            if left_tx["status"] != ACCEPTED_STATUS:
                                continue
                            if conflict["kind"] != "true":
                                continue
                            policy_document = get_policy_version_document(
                                connection, int(conflict["policy_version"])
                            )
                            policy_map = policy_table_map(policy_document)
                            table = schema.get_table(
                                conflict["tenant"], conflict["table_name"]
                            )
                            if table is None:
                                raise WarbiterError(
                                    "E_DB", "conflict refers to unknown table"
                                )
                            table_policy = policy_map.get(
                                (conflict["tenant"], conflict["table_name"]),
                                table.policy.as_dict(),
                            )
                            right_write = next(
                                item
                                for item in writes_by_tx[tx_id]
                                if item["table_name"] == conflict["table_name"]
                                and item["row_key_json"] == conflict["row_key_json"]
                            )
                            evaluation = _evaluate_conflict_policy(
                                table_policy,
                                right_write,
                                json.loads(conflict["columns_json"]),
                            )
                            if evaluation["manual"]:
                                conflict_manual = True
                                manual_conflict_rows.append(
                                    (
                                        (
                                            conflict["tenant"],
                                            conflict["table_name"],
                                            conflict["row_key_json"],
                                        ),
                                        int(conflict["conflict_id"]),
                                    )
                                )
                            elif evaluation["reject"]:
                                conflict_reject = True
                        if status == PENDING_STATUS:
                            if conflict_manual:
                                status = SUSPENDED_MANUAL
                                detail_parts.append("manual_review")
                            elif conflict_reject:
                                status = REJECTED_CONFLICT
                                detail_parts.append("reject_rollback")
                            else:
                                status = ACCEPTED_STATUS
                    if status == SUSPENDED_ASSERTION:
                        for assertion in assertion_results[tx_id]:
                            if not assertion["result"]:
                                key = (
                                    assertion["tenant"],
                                    assertion["table_name"],
                                    assertion["row_key_json"],
                                )
                                suspensions.setdefault(
                                    key,
                                    {
                                        "tenant": key[0],
                                        "table_name": key[1],
                                        "row_key_json": key[2],
                                        "conflict_id": None,
                                        "tx_id": tx_id,
                                        "reason": "assertion_manual_review",
                                        "assertion_text": assertion["assertion_text"],
                                    },
                                )
                    for key, conflict_id in manual_conflict_rows:
                        suspensions.setdefault(
                            key,
                            {
                                "tenant": key[0],
                                "table_name": key[1],
                                "row_key_json": key[2],
                                "conflict_id": conflict_id,
                                "tx_id": tx_id,
                                "reason": (
                                    "replay_limit"
                                    if status == SUSPENDED_REPLAY
                                    else "manual_review"
                                ),
                                "assertion_text": None,
                            },
                        )
                    connection.execute(
                        """
                        UPDATE tx
                        SET status=?, status_detail=?, adjudication_policy_version=?
                        WHERE tx_id=?
                        """,
                        (
                            status,
                            ";".join(detail_parts),
                            int(tx["policy_version"]),
                            tx_id,
                        ),
                    )
                    tx["status"] = status
                    tx["adjudication_policy_version"] = int(tx["policy_version"])
                    if status == ACCEPTED_STATUS:
                        policy_map = policy_cache.get(int(tx["policy_version"]))
                        if policy_map is None:
                            policy_map = policy_table_map(
                                get_policy_version_document(
                                    connection, int(tx["policy_version"])
                                )
                            )
                            policy_cache[int(tx["policy_version"])] = policy_map
                        for final_row in writes_by_tx.get(tx_id, []):
                            table = schema.get_table(
                                final_row["tenant"], final_row["table_name"]
                            )
                            if table is None:
                                raise WarbiterError("E_DB", "write refers to unknown table")
                            table_policy = policy_map.get(
                                (final_row["tenant"], final_row["table_name"]),
                                table.policy.as_dict(),
                            )
                            _apply_final_row(
                                state, final_row, table, table_policy, tx_id
                            )
                    _fault("resolve", processed)
                after = {
                    key: _capture_snapshot(state, key) for key in touched_keys
                }
                snapshots[tx_id] = {
                    key: {"before": before[key], "after": after[key]}
                    for key in touched_keys
                }
            assert_clock = int(get_state(connection, "clock_high_water"))
            for tx_id, results in assertion_results.items():
                for assertion in results:
                    snapshot = assertion["snapshot"]
                    payload = _assertion_evidence_payload(
                        assertion,
                        snapshot,
                        tx_id=tx_id,
                        action=assertion["action"],
                        clock=assert_clock,
                    )
                    payload_hash = sha256_json(payload)
                    connection.execute(
                        """
                        INSERT INTO assertion_evidence(
                            tx_id, op_index, assertion_text, result, action,
                            record_version, record_values_json,
                            recorded_observation_json, evaluated_at_clock,
                            evidence_json, evidence_hash
                        )
                        VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(tx_id, op_index) DO UPDATE SET
                            assertion_text=excluded.assertion_text,
                            result=excluded.result,
                            action=excluded.action,
                            record_version=excluded.record_version,
                            record_values_json=excluded.record_values_json,
                            recorded_observation_json=excluded.recorded_observation_json,
                            evaluated_at_clock=excluded.evaluated_at_clock,
                            evidence_json=excluded.evidence_json,
                            evidence_hash=excluded.evidence_hash
                        """,
                        (
                            tx_id,
                            int(assertion["op_index"]),
                            assertion["assertion_text"],
                            1 if assertion["result"] else 0,
                            assertion["action"],
                            int(snapshot["version"]),
                            canonical_json(snapshot["values"]),
                            canonical_json(assertion["recorded_observation"])
                            if assertion["recorded_observation"] is not None
                            else None,
                            int(assertion["evaluated_at_clock"]),
                            canonical_json(payload),
                            payload_hash,
                        ),
                    )
            resolved_conflicts = 0
            for conflict in pending_conflicts:
                right_id = int(conflict["right_tx_id"])
                right_tx = tx_by_id[right_id]
                if (
                    int(conflict["replay_count"]) + 1
                    > config.max_conflict_replay
                ):
                    conflict_status = "blocked"
                    decision = "replay_limit"
                else:
                    conflict_status = "resolved"
                    decision = _conflict_decision_for_status(right_tx["status"])
                    if conflict["kind"] == "pseudo":
                        decision = "safe_merge"
                    elif conflict["kind"] == "serialized":
                        decision = "serialized_no_conflict"
                    elif right_tx["status"] == ACCEPTED_STATUS:
                        left_tx = tx_by_id[int(conflict["left_tx_id"])]
                        decision = (
                            "later_accepted"
                            if left_tx["status"] == ACCEPTED_STATUS
                            else "later_accepted_inactive_prior"
                        )
                evidence = _build_conflict_evidence(
                    connection,
                    conflict,
                    tx_by_id,
                    writes_by_tx,
                    snapshots,
                    assertion_results,
                    schema,
                    config,
                    assert_clock,
                )
                evidence["decision"] = decision
                evidence_text = canonical_json(evidence)
                connection.execute(
                    """
                    UPDATE conflicts
                    SET status=?, replay_count=replay_count+1, decision=?,
                        evidence_json=?, evidence_hash=?
                    WHERE conflict_id=?
                    """,
                    (
                        conflict_status,
                        decision,
                        evidence_text,
                        sha256_json(evidence),
                        int(conflict["conflict_id"]),
                    ),
                )
                resolved_conflicts += 1
            connection.execute("DELETE FROM suspensions")
            for key in sorted(suspensions):
                suspension = suspensions[key]
                connection.execute(
                    """
                    INSERT INTO suspensions(
                        tenant, table_name, row_key_json, conflict_id, tx_id,
                        reason, assertion_text, created_generation
                    )
                    VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        suspension["tenant"],
                        suspension["table_name"],
                        suspension["row_key_json"],
                        suspension.get("conflict_id"),
                        suspension["tx_id"],
                        suspension["reason"],
                        suspension.get("assertion_text"),
                        int(get_state(connection, "detection_generation")),
                    ),
                )
            _persist_row_state_locked(connection, state)
            _persist_effective_writes_locked(
                connection, transactions, writes_by_tx, schema
            )
            _refresh_pending_digest_locked(connection)
            ledger_summary = _refresh_ledger_locked(connection, config)
            summary = database_summary(connection)
            counts = _status_counts(connection)
            return {
                "changed": True,
                "detected_conflicts": inserted_conflicts,
                "resolved_conflicts": resolved_conflicts,
                "ledger": ledger_summary,
                "database": summary,
                "status_counts": counts,
            }
    finally:
        connection.close()


def list_conflicts(path: str) -> dict[str, Any]:
    detection = detect_conflicts(path)
    connection = open_initialized(path)
    try:
        rows = connection.execute(
            """
            SELECT c.*, l.commit_clock_ms AS left_clock, r.commit_clock_ms AS right_clock
            FROM conflicts c
            JOIN tx l ON l.tx_id = c.left_tx_id
            JOIN tx r ON r.tx_id = c.right_tx_id
            ORDER BY r.commit_clock_ms, r.tx_id, l.commit_clock_ms, l.tx_id,
                     c.tenant, c.table_name, c.row_key_json
            """
        ).fetchall()
        conflicts = []
        for row in rows:
            conflicts.append(
                {
                    "conflict_id": int(row["conflict_id"]),
                    "tenant": row["tenant"],
                    "table": row["table_name"],
                    "row_key": _decode_row_key(row["row_key_json"]),
                    "left_tx_id": int(row["left_tx_id"]),
                    "right_tx_id": int(row["right_tx_id"]),
                    "left_commit_clock_ms": int(row["left_clock"]),
                    "right_commit_clock_ms": int(row["right_clock"]),
                    "kind": row["kind"],
                    "columns": json.loads(row["columns_json"]),
                    "policy_version": int(row["policy_version"]),
                    "status": row["status"],
                    "replay_count": int(row["replay_count"]),
                    "decision": row["decision"],
                    "evidence_hash": row["evidence_hash"],
                    "evidence": json.loads(row["evidence_json"]),
                }
            )
        counts = _status_counts(connection)
        return {
            "schema": "warbiter-conflicts-v1",
            "detection": detection,
            "counts": counts,
            "conflicts": conflicts,
        }
    finally:
        connection.close()


def status_report(path: str) -> dict[str, Any]:
    connection = open_initialized(path)
    try:
        schema = load_schema_from_db(connection)
        row_rows = connection.execute(
            """
            SELECT tenant, table_name, row_key_json, values_json, deleted,
                   version, last_tx_id
            FROM row_state
            ORDER BY tenant, table_name, row_key_json
            """
        ).fetchall()
        suspensions = {
            (row["tenant"], row["table_name"], row["row_key_json"]): dict(row)
            for row in connection.execute(
                "SELECT * FROM suspensions ORDER BY tenant, table_name, row_key_json"
            ).fetchall()
        }
        by_table: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
        for row in row_rows:
            key = (row["tenant"], row["table_name"], row["row_key_json"])
            suspension = suspensions.get(key)
            by_table[(row["tenant"], row["table_name"])].append(
                {
                    "row_key": _decode_row_key(row["row_key_json"]),
                    "values": json.loads(row["values_json"]),
                    "deleted": bool(row["deleted"]),
                    "version": int(row["version"]),
                    "last_tx_id": int(row["last_tx_id"])
                    if row["last_tx_id"] is not None
                    else None,
                    "suspended": suspension is not None,
                    "suspension": (
                        {
                            "reason": suspension["reason"],
                            "tx_id": int(suspension["tx_id"]),
                            "conflict_id": int(suspension["conflict_id"])
                            if suspension["conflict_id"] is not None
                            else None,
                            "assertion_text": suspension["assertion_text"],
                        }
                        if suspension is not None
                        else None
                    ),
                }
            )
        tables = []
        for table in sorted(schema.tables, key=lambda item: (item.tenant, item.name)):
            key = (table.tenant, table.name)
            tables.append(
                {
                    "tenant": table.tenant,
                    "table": table.name,
                    "primary_key": table.primary_key,
                    "rows": by_table.get(key, []),
                    "suspension_count": sum(
                        1 for item in by_table.get(key, []) if item["suspended"]
                    ),
                }
            )
        final_rows_digest = sha256_json(tables)
        summary = database_summary(connection)
        return {
            "schema": "warbiter-status-v1",
            "engine": summary,
            "counts": _status_counts(connection),
            "tables": tables,
            "final_rows_digest": final_rows_digest,
        }
    finally:
        connection.close()


def resume(path: str) -> dict[str, Any]:
    connection = open_initialized(path)
    try:
        with transaction(connection):
            reset = connection.execute(
                "UPDATE conflicts SET status='pending' WHERE status='resolving'"
            ).rowcount
            before_log = [
                (
                    int(row["seq"]),
                    int(row["tx_id"]),
                    int(row["commit_clock_ms"]),
                    row["order_key"],
                )
                for row in connection.execute(
                    """
                    SELECT seq, tx_id, commit_clock_ms, order_key
                    FROM commit_log ORDER BY seq
                    """
                ).fetchall()
            ]
            before_tx = connection.execute("SELECT COUNT(*) AS n FROM tx").fetchone()["n"]
            _rebuild_commit_log_locked(connection)
            after_log = [
                (
                    int(row["seq"]),
                    int(row["tx_id"]),
                    int(row["commit_clock_ms"]),
                    row["order_key"],
                )
                for row in connection.execute(
                    """
                    SELECT seq, tx_id, commit_clock_ms, order_key
                    FROM commit_log ORDER BY seq
                    """
                ).fetchall()
            ]
        resolution = resolve_pending(path)
        return {
            "schema": "warbiter-resume-v1",
            "recovering_conflicts_reset": int(reset),
            "commit_log_rebuilt": before_log != after_log,
            "committed_transactions": int(before_tx),
            "resolution": resolution,
        }
    finally:
        connection.close()


def update_policy(path: str, schema: Schema, config: ArbiterConfig) -> dict[str, Any]:
    previous_version: int | None = None
    if Path(path).exists():
        connection = connect(path)
        try:
            if is_initialized(connection):
                previous_version = current_policy_version(connection)
        finally:
            connection.close()
    initialized = initialize_database(path, schema, config)
    connection = open_initialized(path)
    try:
        document = get_policy_version_document(
            connection, int(initialized["policy_version"])
        )
        return {
            "schema": "warbiter-policy-v1",
            "previous_policy_version": previous_version,
            "current_policy_version": int(initialized["policy_version"]),
            "policy_digest": sha256_json(document),
            "changed": previous_version != int(initialized["policy_version"]),
        }
    finally:
        connection.close()


def _expected_conflict_rows(
    connection: sqlite3.Connection,
    schema: Schema,
    config: ArbiterConfig,
) -> dict[tuple[Any, ...], dict[str, Any]]:
    transactions = _load_transactions(connection)
    writes = _load_writes(connection)
    by_row: dict[tuple[str, str, str], list[tuple[dict[str, Any], dict[str, Any]]]] = (
        defaultdict(list)
    )
    for tx in transactions:
        for write in writes.get(int(tx["tx_id"]), []):
            by_row[
                (write["tenant"], write["table_name"], write["row_key_json"])
            ].append((tx, write))
    expected: dict[tuple[Any, ...], dict[str, Any]] = {}
    for row_key in sorted(by_row):
        entries = sorted(
            by_row[row_key],
            key=lambda item: _order_key(
                int(item[0]["tx_id"]), int(item[0]["commit_clock_ms"])
            ),
        )
        for left_index in range(len(entries)):
            left_tx, left_write = entries[left_index]
            for right_index in range(left_index + 1, len(entries)):
                right_tx, right_write = entries[right_index]
                columns = sorted(
                    set(left_write["coverage"]) & set(right_write["coverage"])
                )
                concurrent = (
                    int(right_tx["start_clock_ms"])
                    <= int(left_tx["commit_clock_ms"]) + config.conflict_tolerance_ms
                )
                kind = (
                    "serialized"
                    if not concurrent
                    else ("true" if columns else "pseudo")
                )
                key = (
                    int(left_tx["tx_id"]),
                    int(right_tx["tx_id"]),
                    row_key[0],
                    row_key[1],
                    row_key[2],
                )
                expected[key] = {
                    "kind": kind,
                    "columns": columns,
                }
    return expected


def _materialize_accepted(
    connection: sqlite3.Connection,
    schema: Schema,
) -> dict[tuple[str, str, str], RowRuntime]:
    transactions = _load_transactions(connection)
    writes = _load_writes(connection)
    state: dict[tuple[str, str, str], RowRuntime] = {}
    policy_cache: dict[int, dict[tuple[str, str], dict[str, Any]]] = {}
    for tx in transactions:
        if tx["status"] != ACCEPTED_STATUS:
            continue
        version = int(tx["adjudication_policy_version"] or tx["policy_version"])
        policy_map = policy_cache.get(version)
        if policy_map is None:
            policy_map = policy_table_map(
                get_policy_version_document(connection, version)
            )
            policy_cache[version] = policy_map
        for final_row in writes.get(int(tx["tx_id"]), []):
            table = schema.get_table(final_row["tenant"], final_row["table_name"])
            if table is None:
                raise WarbiterError("E_DB", "write refers to unknown table")
            table_policy = policy_map.get(
                (final_row["tenant"], final_row["table_name"]),
                table.policy.as_dict(),
            )
            _apply_final_row(
                state, final_row, table, table_policy, int(tx["tx_id"])
            )
    return state


def _effective_write_rows(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = connection.execute(
        """
        SELECT event_seq, tx_id, tenant, table_name, row_key_json, column_name,
               action, value_json, conflict_ids_json
        FROM effective_writes ORDER BY event_seq
        """
    ).fetchall()
    return [
        {
            "event_seq": int(row["event_seq"]),
            "tx_id": int(row["tx_id"]),
            "tenant": row["tenant"],
            "table": row["table_name"],
            "row_key": _decode_row_key(row["row_key_json"]),
            "column": row["column_name"],
            "action": row["action"],
            "value": json.loads(row["value_json"])
            if row["value_json"] is not None
            else None,
            "conflict_ids": json.loads(row["conflict_ids_json"]),
        }
        for row in rows
    ]


def verify(path: str) -> dict[str, Any]:
    connection = open_initialized(path)
    try:
        schema = load_schema_from_db(connection)
        config = load_config_from_db(connection)
        failures: list[dict[str, Any]] = []
        checks: dict[str, bool] = {}

        transactions = _load_transactions(connection)
        tx_by_id = {int(tx["tx_id"]): tx for tx in transactions}
        writes = _load_writes(connection)
        expected_order = sorted(
            transactions,
            key=lambda item: (int(item["commit_clock_ms"]), int(item["tx_id"])),
        )
        log_rows = connection.execute(
            "SELECT seq, tx_id, commit_clock_ms, order_key FROM commit_log ORDER BY seq"
        ).fetchall()
        checks["commit_log_order"] = (
            len(log_rows) == len(expected_order)
            and all(
                int(row["tx_id"]) == int(expected_order[index]["tx_id"])
                and int(row["commit_clock_ms"])
                == int(expected_order[index]["commit_clock_ms"])
                for index, row in enumerate(log_rows)
            )
        )
        if not checks["commit_log_order"]:
            failures.append({"check": "commit_log_order", "detail": "commit order drift"})

        expected_conflicts = _expected_conflict_rows(connection, schema, config)
        actual_rows = connection.execute(
            """
            SELECT conflict_id, left_tx_id, right_tx_id, tenant, table_name,
                   row_key_json, kind, columns_json, policy_version, status,
                   replay_count, decision, evidence_json, evidence_hash
            FROM conflicts
            ORDER BY conflict_id
            """
        ).fetchall()
        actual_keys: set[tuple[Any, ...]] = set()
        checks["conflict_evidence"] = True
        checks["adjudication_complete"] = True
        for row in actual_rows:
            key = (
                int(row["left_tx_id"]),
                int(row["right_tx_id"]),
                row["tenant"],
                row["table_name"],
                row["row_key_json"],
            )
            actual_keys.add(key)
            expected = expected_conflicts.get(key)
            if expected is None:
                checks["conflict_evidence"] = False
                failures.append(
                    {"check": "conflict_evidence", "detail": f"unexpected conflict {key}"}
                )
                continue
            columns = json.loads(row["columns_json"])
            if (
                expected["kind"] != row["kind"]
                or expected["columns"] != columns
            ):
                checks["conflict_evidence"] = False
                failures.append(
                    {
                        "check": "conflict_evidence",
                        "detail": f"kind/columns mismatch for conflict {row['conflict_id']}",
                    }
                )
            if row["status"] in {"pending", "resolving", "blocked"}:
                checks["adjudication_complete"] = False
            try:
                evidence = json.loads(row["evidence_json"])
            except json.JSONDecodeError:
                checks["conflict_evidence"] = False
                failures.append(
                    {
                        "check": "conflict_evidence",
                        "detail": f"invalid evidence JSON {row['conflict_id']}",
                    }
                )
                continue
            if sha256_json(evidence) != row["evidence_hash"]:
                checks["conflict_evidence"] = False
                failures.append(
                    {
                        "check": "conflict_evidence",
                        "detail": f"evidence hash mismatch {row['conflict_id']}",
                    }
                )
            if evidence.get("decision") != row["decision"]:
                checks["conflict_evidence"] = False
                failures.append(
                    {
                        "check": "conflict_evidence",
                        "detail": f"decision mismatch {row['conflict_id']}",
                    }
                )
            if evidence.get("policy_version") != int(row["policy_version"]):
                checks["conflict_evidence"] = False
                failures.append(
                    {
                        "check": "conflict_evidence",
                        "detail": f"policy version mismatch {row['conflict_id']}",
                    }
                )
        if actual_keys != set(expected_conflicts):
            checks["conflict_candidates_complete"] = False
            failures.append(
                {
                    "check": "conflict_candidates_complete",
                    "missing": sorted(set(expected_conflicts) - actual_keys),
                }
            )
        else:
            checks["conflict_candidates_complete"] = True

        assertion_rows = connection.execute(
            """
            SELECT ae.*, t.status AS tx_status
            FROM assertion_evidence ae
            JOIN tx t ON t.tx_id = ae.tx_id
            ORDER BY ae.tx_id, ae.op_index
            """
        ).fetchall()
        checks["assertion_re_evaluation"] = True
        for row in assertion_rows:
            try:
                assertion = json.loads(row["assertion_text"])
                values = json.loads(row["record_values_json"])
                result = evaluate_assertion(assertion, values)
                evidence = json.loads(row["evidence_json"])
            except Exception as exc:
                checks["assertion_re_evaluation"] = False
                failures.append(
                    {
                        "check": "assertion_re_evaluation",
                        "detail": f"unreadable assertion evidence {row['tx_id']}/{row['op_index']}: {exc}",
                    }
                )
                continue
            if bool(row["result"]) != bool(result):
                checks["assertion_re_evaluation"] = False
                failures.append(
                    {
                        "check": "assertion_re_evaluation",
                        "detail": f"result mismatch {row['tx_id']}/{row['op_index']}",
                    }
                )
            if sha256_json(evidence) != row["evidence_hash"]:
                checks["assertion_re_evaluation"] = False
                failures.append(
                    {
                        "check": "assertion_re_evaluation",
                        "detail": f"evidence hash mismatch {row['tx_id']}/{row['op_index']}",
                    }
                )
            if result and row["tx_status"] in {
                ABORTED_ASSERTION,
                SUSPENDED_ASSERTION,
            }:
                checks["assertion_re_evaluation"] = False
                failures.append(
                    {
                        "check": "assertion_re_evaluation",
                        "detail": f"tx {row['tx_id']} aborted despite passing assertion",
                    }
                )
            if not result and row["tx_status"] not in {
                ABORTED_ASSERTION,
                SUSPENDED_ASSERTION,
            }:
                checks["assertion_re_evaluation"] = False
                failures.append(
                    {
                        "check": "assertion_re_evaluation",
                        "detail": f"tx {row['tx_id']} did not adopt assertion failure action",
                    }
                )

        replay_state = _materialize_accepted(connection, schema)
        persisted_state: dict[tuple[str, str, str], RowRuntime] = {}
        for row in connection.execute(
            """
            SELECT tenant, table_name, row_key_json, values_json, deleted,
                   version, last_tx_id
            FROM row_state ORDER BY tenant, table_name, row_key_json
            """
        ).fetchall():
            persisted_state[
                (row["tenant"], row["table_name"], row["row_key_json"])
            ] = RowRuntime(
                values=json.loads(row["values_json"]),
                deleted=bool(row["deleted"]),
                version=int(row["version"]),
                last_tx_id=int(row["last_tx_id"])
                if row["last_tx_id"] is not None
                else None,
            )
        canonical_replay = {
            key: value.snapshot() for key, value in sorted(replay_state.items())
        }
        canonical_persisted = {
            key: value.snapshot() for key, value in sorted(persisted_state.items())
        }
        checks["final_rows_match_replay"] = canonical_replay == canonical_persisted
        if not checks["final_rows_match_replay"]:
            failures.append(
                {
                    "check": "final_rows_match_replay",
                    "detail": "materialized rows differ from full accepted-write replay",
                }
            )

        accepted_events: set[tuple[str, str, str, str, int]] = set()
        expected_event_count = 0
        for tx in transactions:
            if tx["status"] != ACCEPTED_STATUS:
                continue
            tx_id = int(tx["tx_id"])
            for final_row in writes.get(tx_id, []):
                table = schema.get_table(
                    final_row["tenant"], final_row["table_name"]
                )
                if table is None:
                    continue
                columns = (
                    table.column_names
                    if final_row["final_kind"] == "delete"
                    else tuple(final_row["values"])
                )
                for column in columns:
                    accepted_events.add(
                        (
                            final_row["tenant"],
                            final_row["table_name"],
                            final_row["row_key_json"],
                            column,
                            tx_id,
                        )
                    )
                    expected_event_count += 1
        effective_rows = _effective_write_rows(connection)
        checks["effective_write_provenance"] = (
            len(effective_rows) == expected_event_count
        )
        for effective in effective_rows:
            key = (
                effective["tenant"],
                effective["table"],
                row_key_text(effective["row_key"]),
                effective["column"],
                effective["tx_id"],
            )
            if key not in accepted_events:
                checks["effective_write_provenance"] = False
                failures.append(
                    {
                        "check": "effective_write_provenance",
                        "detail": f"uncovered effective write {effective['event_seq']}",
                    }
                )

        ledger_document = _ledger_document(connection)
        ledger_digest = sha256_json(ledger_document)
        pointer = int(get_state(connection, "ledger_pointer_generation"))
        ledger_row = connection.execute(
            "SELECT digest FROM ledger_versions WHERE generation=?", (pointer,)
        ).fetchone()
        checks["ledger_digest"] = (
            (ledger_row is not None and ledger_row["digest"] == ledger_digest)
            or (ledger_row is None and not transactions and not actual_rows)
        )
        if not checks["ledger_digest"]:
            failures.append(
                {"check": "ledger_digest", "detail": "current ledger digest mismatch"}
            )
        expected_pending_digest = sha256_json(
            [
                {
                    "left_tx_id": int(row["left_tx_id"]),
                    "right_tx_id": int(row["right_tx_id"]),
                    "tenant": row["tenant"],
                    "table_name": row["table_name"],
                    "row_key_json": row["row_key_json"],
                    "kind": row["kind"],
                    "policy_version": int(row["policy_version"]),
                }
                for row in connection.execute(
                    """
                    SELECT left_tx_id, right_tx_id, tenant, table_name,
                           row_key_json, kind, policy_version
                    FROM conflicts WHERE status='pending'
                    ORDER BY right_tx_id, left_tx_id, tenant, table_name, row_key_json
                    """
                ).fetchall()
            ]
        )
        checks["pending_conflict_digest"] = (
            expected_pending_digest
            == get_state(connection, "pending_conflict_digest")
        )
        if not checks["pending_conflict_digest"]:
            failures.append(
                {
                    "check": "pending_conflict_digest",
                    "detail": "pending conflict digest mismatch",
                }
            )
        ok = all(checks.values()) and not failures
        result = {
            "schema": "warbiter-verify-v1",
            "ok": ok,
            "checks": checks,
            "failures": failures,
            "counts": _status_counts(connection),
            "ledger_digest": ledger_digest,
            "final_rows_digest": sha256_json(
                [
                    {
                        "tenant": key[0],
                        "table": key[1],
                        "row_key_json": key[2],
                        **value.snapshot(),
                    }
                    for key, value in sorted(persisted_state.items())
                ]
            ),
        }
        if not ok:
            raise WarbiterError(
                "E_VERIFY_FAILED",
                "verification failed",
                result,
            )
        return result
    finally:
        connection.close()
