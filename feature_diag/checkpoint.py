from __future__ import annotations

import copy
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .config import FeatureConfig
from .engine import (
    Computation,
    FeaturePlan,
    FitContext,
    build_feature_plan,
    compose_computation,
    encode_feature,
    estimate_resident_bytes,
    fit_context,
    validate_rows,
)
from .errors import (
    E_CHECKPOINT_CORRUPT,
    EngineError,
    bad_input,
    param_error,
    resource_error,
)
from .sample import rows_digest
from .util import canonical_json, sha256_json, stable_float


class SimulatedHardStop(Exception):
    def __init__(self, checkpoint_version: int) -> None:
        super().__init__("simulated external forced termination")
        self.checkpoint_version = checkpoint_version


@dataclass
class CheckpointState:
    meta: dict[str, Any]
    latest: dict[str, Any] | None
    history: list[dict[str, Any]]


class CheckpointStore:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.path), timeout=0, isolation_level=None)
        connection.execute("PRAGMA busy_timeout=0")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        return connection

    def _initialize(self) -> None:
        connection = self._connect()
        try:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS run_meta (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    run_signature TEXT NOT NULL,
                    status TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    last_version INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS checkpoints (
                    version INTEGER PRIMARY KEY,
                    payload_json TEXT NOT NULL,
                    checksum TEXT NOT NULL
                );
                """
            )
        finally:
            connection.close()

    def reset(self, run_signature: str, status: str = "running", reason: str = "") -> None:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("DELETE FROM checkpoints")
            connection.execute("DELETE FROM run_meta")
            connection.execute(
                "INSERT INTO run_meta(singleton, run_signature, status, reason, last_version) VALUES (1, ?, ?, ?, 0)",
                (run_signature, status, reason),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def ensure_meta(self, run_signature: str) -> dict[str, Any]:
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT run_signature, status, reason, last_version FROM run_meta WHERE singleton=1"
            ).fetchone()
            if row is None:
                connection.execute(
                    "INSERT INTO run_meta(singleton, run_signature, status, reason, last_version) VALUES (1, ?, ?, ?, 0)",
                    (run_signature, "running", "", 0),
                )
                connection.commit()
                row = (run_signature, "running", "", 0)
        finally:
            connection.close()
        if row[0] != run_signature:
            raise EngineError(
                E_CHECKPOINT_CORRUPT,
                "checkpoint run signature does not match the requested input/config",
                {
                    "field": "run_signature",
                    "expected": run_signature,
                    "actual": row[0],
                },
            )
        return {
            "run_signature": str(row[0]),
            "status": str(row[1]),
            "reason": str(row[2]),
            "last_version": int(row[3]),
        }

    def set_status(self, status: str, reason: str) -> None:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "UPDATE run_meta SET status=?, reason=? WHERE singleton=1",
                (status, reason),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def commit(self, payload: dict[str, Any]) -> tuple[int, str]:
        version = int(payload["version"])
        payload_json = canonical_json(payload)
        checksum = sha256_json(payload)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            meta = connection.execute(
                "SELECT last_version FROM run_meta WHERE singleton=1"
            ).fetchone()
            if meta is None:
                raise EngineError(E_CHECKPOINT_CORRUPT, "checkpoint run metadata is missing", {"field": "run_meta"})
            expected_version = int(meta[0]) + 1
            if version != expected_version:
                raise EngineError(
                    E_CHECKPOINT_CORRUPT,
                    "checkpoint version is not monotonic",
                    {"field": "version", "expected": expected_version, "actual": version},
                )
            duplicate = connection.execute("SELECT 1 FROM checkpoints WHERE version=?", (version,)).fetchone()
            if duplicate is not None:
                raise EngineError(
                    E_CHECKPOINT_CORRUPT,
                    "checkpoint version already exists",
                    {"field": "version", "value": version},
                )
            connection.execute(
                "INSERT INTO checkpoints(version, payload_json, checksum) VALUES (?, ?, ?)",
                (version, payload_json, checksum),
            )
            connection.execute(
                "UPDATE run_meta SET last_version=?, status='running', reason='' WHERE singleton=1",
                (version,),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return version, checksum

    def load(self) -> CheckpointState:
        connection = self._connect()
        try:
            meta_row = connection.execute(
                "SELECT run_signature, status, reason, last_version FROM run_meta WHERE singleton=1"
            ).fetchone()
            rows = connection.execute(
                "SELECT version, payload_json, checksum FROM checkpoints ORDER BY version ASC"
            ).fetchall()
        finally:
            connection.close()
        if meta_row is None:
            raise EngineError(E_CHECKPOINT_CORRUPT, "checkpoint run metadata is missing", {"field": "run_meta"})
        meta = {
            "run_signature": str(meta_row[0]),
            "status": str(meta_row[1]),
            "reason": str(meta_row[2]),
            "last_version": int(meta_row[3]),
        }
        history: list[dict[str, Any]] = []
        latest: dict[str, Any] | None = None
        previous_version = 0
        for version, payload_json, checksum in rows:
            version = int(version)
            if version != previous_version + 1:
                raise EngineError(
                    E_CHECKPOINT_CORRUPT,
                    "checkpoint version chain is not monotonic",
                    {"field": "version", "previous": previous_version, "actual": version},
                )
            try:
                payload = json.loads(payload_json)
            except Exception as error:
                raise EngineError(
                    E_CHECKPOINT_CORRUPT,
                    "checkpoint payload cannot be parsed",
                    {"field": "payload_json", "version": version, "error": str(error)},
                ) from None
            actual_checksum = sha256_json(payload)
            if actual_checksum != checksum:
                raise EngineError(
                    E_CHECKPOINT_CORRUPT,
                    "checkpoint checksum mismatch",
                    {
                        "field": "checksum",
                        "version": version,
                        "expected": checksum,
                        "actual": actual_checksum,
                    },
                )
            if payload.get("version") != version:
                raise EngineError(
                    E_CHECKPOINT_CORRUPT,
                    "checkpoint payload version mismatch",
                    {"field": "payload.version", "expected": version, "actual": payload.get("version")},
                )
            history.append(
                {
                    "version": version,
                    "checksum": str(checksum),
                    "batch_index": payload.get("batch_index"),
                    "row_cursor": payload.get("row_cursor"),
                }
            )
            latest = payload
            previous_version = version
        if meta["last_version"] != previous_version:
            raise EngineError(
                E_CHECKPOINT_CORRUPT,
                "checkpoint metadata version does not match the last committed checkpoint",
                {"field": "last_version", "meta": meta["last_version"], "checkpoint": previous_version},
            )
        return CheckpointState(meta=meta, latest=latest, history=history)


def _resolve_batch_size(config: FeatureConfig, requested: int) -> tuple[int, list[dict[str, Any]]]:
    if isinstance(requested, bool) or not isinstance(requested, int) or requested < 1:
        raise param_error("batch size must be a positive integer", field="batch_size")
    maximum = int(config.limits["max_batch_rows"])
    if requested <= maximum:
        return requested, []
    if config.resource_policy["on_batch_rows"] == "degrade_serial":
        return maximum, [
            {
                "category": "batch_rows_serialized",
                "resource": "batch_rows",
                "requested": requested,
                "applied_limit": maximum,
                "result_unchanged": True,
            }
        ]
    raise resource_error(
        "requested batch size exceeds max_batch_rows",
        resource="batch_rows",
        limit=maximum,
        requested=requested,
    )


def _validate_latest(
    latest: dict[str, Any],
    *,
    rows: list[dict[str, Any]],
    plan: FeaturePlan,
    context: FitContext,
) -> list[dict[str, Any]]:
    encoded_rows = latest.get("encoded_rows")
    feature_ids = latest.get("feature_ids")
    if not isinstance(encoded_rows, list) or not isinstance(feature_ids, list):
        raise EngineError(E_CHECKPOINT_CORRUPT, "checkpoint encoded state is malformed", {"field": "encoded_rows"})
    if feature_ids != plan.ids:
        raise EngineError(
            E_CHECKPOINT_CORRUPT,
            "checkpoint feature plan does not match the current config",
            {"field": "feature_ids"},
        )
    if latest.get("context_digest") != context.digest:
        raise EngineError(
            E_CHECKPOINT_CORRUPT,
            "checkpoint fit context does not match the current input/config",
            {"field": "context_digest"},
        )
    expected_prefix = [row["row_id"] for row in rows[: len(encoded_rows)]]
    actual_ids = [entry.get("row_id") for entry in encoded_rows]
    if actual_ids != expected_prefix:
        raise EngineError(
            E_CHECKPOINT_CORRUPT,
            "checkpoint row prefix does not match deterministic input order",
            {"field": "encoded_rows.row_id"},
        )
    if latest.get("row_cursor") != len(encoded_rows):
        raise EngineError(
            E_CHECKPOINT_CORRUPT,
            "checkpoint row cursor does not match its encoded row count",
            {"field": "row_cursor"},
        )
    for entry in encoded_rows:
        values = entry.get("values")
        if not isinstance(values, list) or len(values) != len(feature_ids):
            raise EngineError(
                E_CHECKPOINT_CORRUPT,
                "checkpoint row vector is malformed",
                {"field": "encoded_rows.values", "row_id": entry.get("row_id")},
            )
        for value in values:
            try:
                stable_float(float(value))
            except Exception:
                raise EngineError(
                    E_CHECKPOINT_CORRUPT,
                    "checkpoint row vector contains a non-finite value",
                    {"field": "encoded_rows.values", "row_id": entry.get("row_id")},
                ) from None
    return encoded_rows


def run_checkpointed_batches(
    rows: list[dict[str, Any]],
    config: FeatureConfig,
    checkpoint_path: Path,
    *,
    requested_batch_size: int,
    reset: bool,
    hard_stop_after_batches: int | None = None,
    graceful_stop_after_batches: int | None = None,
    graceful_requested: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    batch_size, degradations = _resolve_batch_size(config, requested_batch_size)
    sorted_rows = validate_rows(rows, config)
    plan = build_feature_plan(config)
    context = fit_context(config, sorted_rows)
    needed_bytes = estimate_resident_bytes(len(sorted_rows), len(plan.features))
    if needed_bytes > config.limits["max_resident_bytes"]:
        raise resource_error(
            "resident encoded matrix exceeds max_resident_bytes",
            resource="encoding_matrix",
            limit=config.limits["max_resident_bytes"],
            needed=needed_bytes,
        )
    signature = sha256_json(
        {
            "config_digest": config.semantic_digest,
            "input_digest": rows_digest(sorted_rows),
            "batch_size": batch_size,
            "feature_ids": plan.ids,
            "context_digest": context.digest,
        }
    )
    store = CheckpointStore(checkpoint_path)
    if reset:
        store.reset(signature, status="running", reason="")
    status_before = store.ensure_meta(signature)
    state = store.load()
    latest = state.latest
    encoded_rows: list[dict[str, Any]] = []
    if latest is not None:
        encoded_rows = _validate_latest(latest, rows=sorted_rows, plan=plan, context=context)
    prior_status = status_before["status"]
    if prior_status == "running":
        resume_reason = "forced_termination_recovery" if latest is not None else "fresh_run"
    elif prior_status == "graceful_stop":
        resume_reason = "after_graceful_stop"
    elif prior_status == "completed":
        resume_reason = "already_completed"
    else:
        resume_reason = prior_status
    cursor = len(encoded_rows)
    batch_index = int(latest.get("batch_index", 0)) if latest is not None else 0
    committed_batches_reused = batch_index
    rows_reused = cursor
    recomputed_row_ids: list[str] = []
    encoded_feature_ids: dict[str, set[str]] = {}
    new_batches = 0
    checkpoint_versions = [int(item["version"]) for item in state.history]
    last_commit_version = state.meta["last_version"]
    final_status = prior_status
    stop_reason = state.meta["reason"]
    if prior_status != "completed":
        store.set_status("running", "")
        while cursor < len(sorted_rows):
            batch = sorted_rows[cursor : cursor + batch_size]
            encoded_batch: list[dict[str, Any]] = []
            for row in batch:
                values = [encode_feature(feature, row, context) for feature in plan.features]
                encoded_batch.append({"row_id": row["row_id"], "values": values})
                recomputed_row_ids.append(row["row_id"])
                for feature in plan.features:
                    encoded_feature_ids.setdefault(feature["id"], set()).add(row["row_id"])
            encoded_rows.extend(encoded_batch)
            cursor += len(batch)
            batch_index += 1
            new_batches += 1
            version = last_commit_version + 1
            payload = {
                "version": version,
                "run_signature": signature,
                "batch_index": batch_index,
                "batch_size": batch_size,
                "row_cursor": cursor,
                "total_rows": len(sorted_rows),
                "feature_ids": plan.ids,
                "context_digest": context.digest,
                "encoded_rows": copy.deepcopy(encoded_rows),
            }
            version, checksum = store.commit(payload)
            checkpoint_versions.append(version)
            last_commit_version = version
            state.meta["last_version"] = version
            if hard_stop_after_batches is not None and new_batches >= hard_stop_after_batches:
                raise SimulatedHardStop(version)
            should_graceful = (
                graceful_stop_after_batches is not None
                and new_batches >= graceful_stop_after_batches
            ) or (graceful_requested is not None and graceful_requested())
            if should_graceful:
                final_status = "graceful_stop"
                stop_reason = "graceful_signal_after_committed_batch"
                store.set_status(final_status, stop_reason)
                return {
                    "status": final_status,
                    "reason": stop_reason,
                    "resume_reason": resume_reason,
                    "checkpoint_version": version,
                    "checkpoint_versions": checkpoint_versions,
                    "committed_batches_reused": committed_batches_reused,
                    "rows_reused_from_checkpoint": rows_reused,
                    "recomputed_row_ids": sorted(recomputed_row_ids),
                    "completed_batches_this_run": new_batches,
                    "row_cursor": cursor,
                    "total_rows": len(sorted_rows),
                    "uncommitted_state_discarded": True,
                    "degradations": degradations,
                    "selection_available": False,
                }
        final_status = "completed"
        stop_reason = "all_batches_committed"
        store.set_status(final_status, stop_reason)
    encodings = {
        entry["row_id"]: {
            feature_id: stable_float(value)
            for feature_id, value in zip(plan.ids, entry["values"])
        }
        for entry in encoded_rows
    }
    computation = compose_computation(
        sorted_rows,
        config,
        encodings,
        plan=plan,
        context=context,
        computed_feature_keys=[],
        reused_feature_keys=[],
        recomputed_row_ids=recomputed_row_ids,
        reused_row_ids=[row["row_id"] for row in sorted_rows[:rows_reused]],
        affected_feature_ids=sorted(encoded_feature_ids),
    )
    return {
        "status": final_status,
        "reason": stop_reason,
        "resume_reason": resume_reason,
        "checkpoint_version": checkpoint_versions[-1] if checkpoint_versions else 0,
        "checkpoint_versions": checkpoint_versions,
        "committed_batches_reused": committed_batches_reused,
        "rows_reused_from_checkpoint": rows_reused,
        "recomputed_row_ids": sorted(recomputed_row_ids),
        "completed_batches_this_run": new_batches,
        "row_cursor": cursor,
        "total_rows": len(sorted_rows),
        "uncommitted_state_discarded": True,
        "degradations": degradations,
        "selection_available": True,
        "selection": computation.summary(),
        "computation": computation,
    }
