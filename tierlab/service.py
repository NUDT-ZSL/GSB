from __future__ import annotations

import json
import os
import sqlite3
from decimal import Decimal
from pathlib import Path
from typing import Any

from .candidates import (
    choose_observation,
    evaluate_object,
    sort_candidates,
)
from .errors import BusinessError, ErrorCode
from .models import (
    COLD,
    HOT,
    Candidate,
    FailureSpec,
    Manifest,
    Observation,
    StorageObject,
    TierConfig,
    canonical_json,
    decimal_text,
    stable_digest,
)
from .parsing import config_from_dict
from .scoring import check_clock
from .storage import (
    connect_database,
    database_exists,
    file_lock,
    layer_capacity,
    layer_reserved,
    layer_used,
    reconcile_ledger,
    transaction,
)

MAX_RESULT_ITEMS = 1000


def _maybe_crash(point: str) -> None:
    if os.environ.get("TIERLAB_CRASH_AT") == point:
        os._exit(137)


def _object_from_row(row: sqlite3.Row) -> StorageObject:
    return StorageObject(
        bucket=row["bucket"],
        key=row["key"],
        size_bytes=int(row["size_bytes"]),
        last_access_clock=int(row["last_access_clock"]),
        access_count=int(row["access_count"]),
        heat_weight=Decimal(row["heat_weight"]),
        declared_layer=row["declared_layer"],
        migratable=bool(row["migratable"]),
        current_layer=row["current_layer"],
        migration_state=row["migration_state"],
        failure_count=int(row["failure_count"]),
        backoff_until_clock=int(row["backoff_until_clock"]),
        version=int(row["version"]),
        last_round=int(row["last_round"]),
        last_outcome=row["last_outcome"],
    )


def _config_from_row(row: sqlite3.Row) -> TierConfig:
    return config_from_dict(json.loads(row["config_json"]))


def _observation_summary(observation: Observation) -> dict[str, Any]:
    return {
        "mode": observation.mode,
        "truth_source": observation.truth_source,
        "population_count": observation.population_count,
        "observed_count": observation.observed_count,
        "candidate_count": observation.candidate_count,
        "candidate_bytes": observation.candidate_bytes,
        "excluded": observation.excluded,
        "sampling_summary": observation.sampling_summary,
        "observations": observation.observations[:MAX_RESULT_ITEMS],
        "observations_truncated": len(observation.observations)
        > MAX_RESULT_ITEMS,
    }


def _manifest_digest(manifest: Manifest, config: TierConfig, clock: int) -> str:
    object_rows = [
        {
            "key": obj.key,
            "size_bytes": obj.size_bytes,
            "last_access_clock": obj.last_access_clock,
            "access_count": obj.access_count,
            "heat_weight": decimal_text(obj.heat_weight),
            "declared_layer": obj.declared_layer,
            "migratable": obj.migratable,
        }
        for obj in sorted(manifest.objects, key=lambda item: item.key)
    ]
    return stable_digest(
        {
            "bucket": manifest.bucket,
            "clock": clock,
            "config": config.to_dict(),
            "objects": object_rows,
        }
    )


def _request_digest(
    bucket: str,
    round_number: int,
    clock: int,
    requested_mode: str | None,
    failure_spec: FailureSpec,
) -> str:
    return stable_digest(
        {
            "bucket": bucket,
            "round": round_number,
            "clock": clock,
            "requested_mode": requested_mode,
            "failure_spec": failure_spec.to_dict(),
        }
    )


def _ledger_dict(used: dict[str, int], capacity: dict[str, int]) -> dict[str, Any]:
    return {
        layer: {
            "used_bytes": used.get(layer, 0),
            "capacity_bytes": capacity.get(layer, 0),
        }
        for layer in (HOT, COLD)
    }


class Workbench:
    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)

    def _require_database(self) -> None:
        if not database_exists(self.db_path):
            raise BusinessError(
                ErrorCode.DB_NOT_FOUND,
                "workspace database does not exist",
                {"path": self.db_path},
            )

    def load(
        self,
        manifest: Manifest,
        config: TierConfig,
        clock: int,
    ) -> dict[str, Any]:
        if clock < 0:
            raise BusinessError(
                ErrorCode.INVALID_CLOCK,
                "injected logical clock must be >= 0",
            )
        initial_used = {HOT: 0, COLD: 0}
        for obj in manifest.objects:
            if obj.last_access_clock > clock:
                raise BusinessError(
                    ErrorCode.CLOCK_BEFORE_ACCESS,
                    "initial logical clock precedes an object's last access clock",
                    {
                        "key": obj.key,
                        "last_access_clock": obj.last_access_clock,
                        "injected_clock": clock,
                    },
                )
            initial_used[obj.current_layer] += obj.size_bytes
        for layer in (HOT, COLD):
            if initial_used[layer] > config.capacity(layer):
                raise BusinessError(
                    ErrorCode.INITIAL_CAPACITY_EXCEEDED,
                    "declared initial placement exceeds a layer capacity",
                    {
                        "layer": layer,
                        "used_bytes": initial_used[layer],
                        "capacity_bytes": config.capacity(layer),
                    },
                )

        config_json = canonical_json(config.to_dict())
        config_digest = stable_digest(config.to_dict())
        load_digest = _manifest_digest(manifest, config, clock)

        with file_lock(self.db_path, exclusive=True):
            connection = connect_database(self.db_path, create=True)
            try:
                with transaction(connection):
                    existing = connection.execute(
                        "SELECT bucket FROM buckets WHERE bucket = ?",
                        (manifest.bucket,),
                    ).fetchone()
                    if existing is not None:
                        raise BusinessError(
                            ErrorCode.BUCKET_EXISTS,
                            "bucket already exists in this workspace database",
                            {"bucket": manifest.bucket},
                        )
                    connection.execute(
                        """
                        INSERT INTO buckets(
                            bucket, config_json, config_digest, load_digest,
                            clock_watermark, clock_tolerance, round_watermark,
                            mode_last, decision_json
                        ) VALUES (?, ?, ?, ?, ?, ?, 0, NULL, NULL)
                        """,
                        (
                            manifest.bucket,
                            config_json,
                            config_digest,
                            load_digest,
                            clock,
                            config.clock_tolerance,
                        ),
                    )
                    connection.executemany(
                        """
                        INSERT INTO layers(bucket, layer, capacity_bytes, used_bytes)
                        VALUES (?, ?, ?, ?)
                        """,
                        [
                            (
                                manifest.bucket,
                                HOT,
                                config.hot_capacity_bytes,
                                initial_used[HOT],
                            ),
                            (
                                manifest.bucket,
                                COLD,
                                config.cold_capacity_bytes,
                                initial_used[COLD],
                            ),
                        ],
                    )
                    connection.executemany(
                        """
                        INSERT INTO objects(
                            bucket, key, size_bytes, last_access_clock,
                            access_count, heat_weight, declared_layer,
                            current_layer, migratable, migration_state,
                            failure_count, backoff_until_clock, version,
                            last_round, last_outcome
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', 0, 0, 0, 0, NULL)
                        """,
                        [
                            (
                                manifest.bucket,
                                obj.key,
                                obj.size_bytes,
                                obj.last_access_clock,
                                obj.access_count,
                                decimal_text(obj.heat_weight),
                                obj.declared_layer,
                                obj.current_layer,
                                1 if obj.migratable else 0,
                            )
                            for obj in manifest.objects
                        ],
                    )
            finally:
                connection.close()

        return {
            "ok": True,
            "command": "load",
            "bucket": manifest.bucket,
            "object_count": len(manifest.objects),
            "clock_watermark": clock,
            "config_digest": config_digest,
            "load_digest": load_digest,
            "ledger": _ledger_dict(
                initial_used,
                {HOT: config.hot_capacity_bytes, COLD: config.cold_capacity_bytes},
            ),
            "truth_source": "validated manifest and config; no wall clock was read",
        }

    def migrate(
        self,
        *,
        bucket: str,
        clock: int,
        round_number: int | None,
        requested_mode: str | None,
        failure_spec: FailureSpec,
    ) -> dict[str, Any]:
        self._require_database()
        with file_lock(self.db_path, exclusive=True):
            connection = connect_database(self.db_path)
            try:
                bucket_row = self._get_bucket(connection, bucket)
                config = _config_from_row(bucket_row)
                if requested_mode not in (None, "auto", "exact", "sampled"):
                    raise BusinessError(
                        ErrorCode.INVALID_MODE,
                        "requested mode must be auto, exact, or sampled",
                        {"value": requested_mode},
                    )
                resolved_round = (
                    int(bucket_row["round_watermark"]) + 1
                    if round_number is None
                    else round_number
                )
                if resolved_round <= 0:
                    raise BusinessError(
                        ErrorCode.ROUND_SEQUENCE,
                        "round number must be > 0",
                    )
                request_digest = _request_digest(
                    bucket,
                    resolved_round,
                    clock,
                    requested_mode or config.mode,
                    failure_spec,
                )
                existing = connection.execute(
                    "SELECT * FROM batches WHERE bucket = ? AND round = ?",
                    (bucket, resolved_round),
                ).fetchone()
                if existing is not None:
                    if existing["request_digest"] != request_digest:
                        raise BusinessError(
                            ErrorCode.ROUND_CONFLICT,
                            "round already exists with a different request",
                            {"bucket": bucket, "round": resolved_round},
                        )
                    return self._finalize_batch(
                        connection,
                        bucket,
                        resolved_round,
                    )

                with transaction(connection):
                    active = connection.execute(
                        """
                        SELECT round FROM batches
                        WHERE bucket = ? AND status = 'in_flight'
                        ORDER BY round LIMIT 1
                        """,
                        (bucket,),
                    ).fetchone()
                    if active is not None:
                        raise BusinessError(
                            ErrorCode.RESUME_REQUIRED,
                            "an in-flight batch must be resumed before a new round",
                            {"bucket": bucket, "round": int(active["round"])},
                        )
                    expected_round = int(bucket_row["round_watermark"]) + 1
                    if resolved_round != expected_round:
                        raise BusinessError(
                            ErrorCode.ROUND_SEQUENCE,
                            "new rounds must advance the persisted watermark by one",
                            {
                                "bucket": bucket,
                                "requested_round": resolved_round,
                                "expected_round": expected_round,
                            },
                        )
                    effective_clock = check_clock(
                        int(bucket_row["clock_watermark"]),
                        clock,
                        int(bucket_row["clock_tolerance"]),
                    )
                    objects = self._load_objects(connection, bucket)
                    observation, decision = choose_observation(
                        objects,
                        effective_clock,
                        config,
                        bucket=bucket,
                        round_number=resolved_round,
                        requested_mode=requested_mode,
                    )
                    decision.metrics["observed_candidate_count"] = (
                        observation.candidate_count
                    )
                    decision.metrics["observed_candidate_bytes"] = (
                        observation.candidate_bytes
                    )
                    plan = self._build_plan(
                        connection,
                        bucket,
                        config,
                        observation,
                        failure_spec,
                    )
                    self._persist_plan(
                        connection,
                        bucket,
                        resolved_round,
                        clock,
                        effective_clock,
                        request_digest,
                        observation,
                        decision.to_dict(),
                        plan,
                    )
                    _maybe_crash("during_reserve")
                _maybe_crash("after_reserve")
                return self._finalize_batch(
                    connection,
                    bucket,
                    resolved_round,
                )
            finally:
                connection.close()

    def _get_bucket(
        self,
        connection: sqlite3.Connection,
        bucket: str,
    ) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM buckets WHERE bucket = ?",
            (bucket,),
        ).fetchone()
        if row is None:
            raise BusinessError(
                ErrorCode.BUCKET_NOT_FOUND,
                "bucket is not loaded in this workspace",
                {"bucket": bucket},
            )
        return row

    def _load_objects(
        self,
        connection: sqlite3.Connection,
        bucket: str,
    ) -> list[StorageObject]:
        rows = connection.execute(
            "SELECT * FROM objects WHERE bucket = ? ORDER BY key",
            (bucket,),
        ).fetchall()
        return [_object_from_row(row) for row in rows]

    def _build_plan(
        self,
        connection: sqlite3.Connection,
        bucket: str,
        config: TierConfig,
        observation: Observation,
        failure_spec: FailureSpec,
    ) -> list[dict[str, Any]]:
        candidates = sort_candidates(
            observation.candidates,
            config.hot_threshold,
        )
        used = layer_used(connection, bucket)
        capacity = layer_capacity(connection, bucket)
        reserved = layer_reserved(connection, bucket)
        local_reserved = {HOT: 0, COLD: 0}
        capacity_blocked = {HOT: False, COLD: False}
        remaining_budget = config.round_byte_budget
        budget_exhausted = False
        started = 0
        plan: list[dict[str, Any]] = []

        for order_index, candidate in enumerate(candidates, start=1):
            size = candidate.obj.size_bytes
            target = candidate.target_layer
            item: dict[str, Any] = {
                "candidate": candidate,
                "order_index": order_index,
                "start_order": None,
                "state": "abandoned",
                "planned_outcome": "abandoned",
                "outcome": None,
                "reason": None,
                "error_code": None,
            }
            if budget_exhausted:
                item["outcome"] = "abandoned_budget"
                item["reason"] = "round_byte_budget_exhausted"
            elif size > remaining_budget:
                budget_exhausted = True
                item["outcome"] = "abandoned_budget"
                item["reason"] = "round_byte_budget_exhausted"
            elif (
                capacity_blocked[target]
                or used[target]
                + reserved[target]
                + local_reserved[target]
                + size
                > capacity[target]
            ):
                capacity_blocked[target] = True
                item["outcome"] = "abandoned_capacity"
                item["reason"] = f"{target}_capacity_exhausted"
            elif started >= config.migration_concurrency:
                item["state"] = "not_started"
                item["planned_outcome"] = "not_started"
                item["outcome"] = "not_started_within_budget"
                item["reason"] = "migration_concurrency_limit"
            else:
                started += 1
                item["start_order"] = started
                item["state"] = "reserved"
                item["planned_outcome"] = (
                    "failure"
                    if failure_spec.should_fail(candidate.obj.key, started)
                    else "success"
                )
                remaining_budget -= size
                local_reserved[target] += size
            plan.append(item)
        return plan

    def _persist_plan(
        self,
        connection: sqlite3.Connection,
        bucket: str,
        round_number: int,
        request_clock: int,
        effective_clock: int,
        request_digest: str,
        observation: Observation,
        decision: dict[str, Any],
        plan: list[dict[str, Any]],
    ) -> None:
        selected_mode = decision["selected_mode"]
        connection.execute(
            """
            INSERT INTO batches(
                bucket, round, request_clock, effective_clock, request_digest,
                mode, status, decision_json, observation_json, result_json
            ) VALUES (?, ?, ?, ?, ?, ?, 'in_flight', ?, ?, NULL)
            """,
            (
                bucket,
                round_number,
                request_clock,
                effective_clock,
                request_digest,
                selected_mode,
                canonical_json(decision),
                canonical_json(_observation_summary(observation)),
            ),
        )

        item_rows: list[tuple[Any, ...]] = []
        inflight_rows: list[tuple[Any, ...]] = []
        selected_updates: list[tuple[Any, ...]] = []
        deferred_updates: list[tuple[Any, ...]] = []
        abandoned_updates: list[tuple[Any, ...]] = []
        for item in plan:
            candidate: Candidate = item["candidate"]
            obj = candidate.obj
            item_rows.append(
                (
                    bucket,
                    round_number,
                    obj.key,
                    obj.version,
                    obj.size_bytes,
                    obj.current_layer,
                    candidate.target_layer,
                    decimal_text(candidate.score),
                    item["order_index"],
                    item["start_order"],
                    item["planned_outcome"],
                    item["state"],
                    item["outcome"],
                    item["reason"],
                    item["error_code"],
                )
            )
            if item["state"] == "reserved":
                inflight_rows.append(
                    (
                        bucket,
                        round_number,
                        obj.key,
                        obj.version,
                        obj.current_layer,
                        candidate.target_layer,
                        obj.size_bytes,
                        "reserved",
                    )
                )
                selected_updates.append((round_number, bucket, obj.key))
            elif item["state"] == "not_started":
                deferred_updates.append(
                    (
                        round_number,
                        item["outcome"],
                        bucket,
                        obj.key,
                    )
                )
            else:
                abandoned_updates.append(
                    (
                        round_number,
                        item["outcome"],
                        bucket,
                        obj.key,
                    )
                )

        connection.executemany(
            """
            INSERT INTO batch_items(
                bucket, round, key, object_version, size_bytes,
                source_layer, target_layer, score, order_index, start_order,
                planned_outcome, state, outcome, reason, error_code
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            item_rows,
        )
        connection.executemany(
            """
            INSERT INTO inflight(
                bucket, round, key, object_version, source_layer,
                target_layer, size_bytes, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            inflight_rows,
        )
        connection.executemany(
            """
            UPDATE objects
            SET migration_state = 'migrating', last_round = ?
            WHERE bucket = ? AND key = ?
            """,
            selected_updates,
        )
        connection.executemany(
            """
            UPDATE objects
            SET migration_state = 'pending', last_round = ?, last_outcome = ?
            WHERE bucket = ? AND key = ?
            """,
            deferred_updates,
        )
        connection.executemany(
            """
            UPDATE objects
            SET migration_state = 'abandoned', last_round = ?, last_outcome = ?
            WHERE bucket = ? AND key = ?
            """,
            abandoned_updates,
        )

    def _finalize_batch(
        self,
        connection: sqlite3.Connection,
        bucket: str,
        round_number: int,
    ) -> dict[str, Any]:
        with transaction(connection):
            batch = connection.execute(
                "SELECT * FROM batches WHERE bucket = ? AND round = ?",
                (bucket, round_number),
            ).fetchone()
            if batch is None:
                raise BusinessError(
                    ErrorCode.ROUND_CONFLICT,
                    "batch does not exist",
                    {"bucket": bucket, "round": round_number},
                )
            if batch["status"] == "completed":
                return json.loads(batch["result_json"])
            bucket_row = self._get_bucket(connection, bucket)
            config = _config_from_row(bucket_row)
            before = layer_used(connection, bucket)
            items = connection.execute(
                """
                SELECT * FROM batch_items
                WHERE bucket = ? AND round = ?
                ORDER BY order_index
                """,
                (bucket, round_number),
            ).fetchall()
            recovered_committed = 0
            conflicts = 0
            crash_once = False
            for item in items:
                if item["state"] != "reserved":
                    continue
                obj_row = connection.execute(
                    "SELECT * FROM objects WHERE bucket = ? AND key = ?",
                    (bucket, item["key"]),
                ).fetchone()
                if obj_row is None:
                    raise BusinessError(
                        ErrorCode.RECOVERY_CONFLICT,
                        "in-flight object no longer exists",
                        {"bucket": bucket, "key": item["key"]},
                    )
                obj = _object_from_row(obj_row)
                if (
                    obj.current_layer == item["target_layer"]
                    and obj.version == int(item["object_version"]) + 1
                ):
                    connection.execute(
                        """
                        UPDATE inflight SET status = 'committed'
                        WHERE bucket = ? AND round = ? AND key = ?
                          AND object_version = ?
                        """,
                        (
                            bucket,
                            round_number,
                            item["key"],
                            item["object_version"],
                        ),
                    )
                    connection.execute(
                        """
                        UPDATE batch_items
                        SET state = 'committed', outcome = 'started_success',
                            reason = 'recovered_committed_observation',
                            error_code = NULL
                        WHERE bucket = ? AND round = ? AND key = ?
                        """,
                        (bucket, round_number, item["key"]),
                    )
                    connection.execute(
                        """
                        UPDATE objects
                        SET migration_state = 'migrated', failure_count = 0,
                            backoff_until_clock = 0,
                            last_outcome = 'started_success'
                        WHERE bucket = ? AND key = ?
                        """,
                        (bucket, item["key"]),
                    )
                    recovered_committed += 1
                    continue
                if (
                    obj.version != int(item["object_version"])
                    or obj.current_layer != item["source_layer"]
                ):
                    connection.execute(
                        """
                        UPDATE inflight SET status = 'conflict'
                        WHERE bucket = ? AND round = ? AND key = ?
                          AND object_version = ?
                        """,
                        (
                            bucket,
                            round_number,
                            item["key"],
                            item["object_version"],
                        ),
                    )
                    connection.execute(
                        """
                        UPDATE batch_items
                        SET state = 'conflict', outcome = 'recovery_conflict',
                            reason = 'object_version_or_layer_changed',
                            error_code = ?
                        WHERE bucket = ? AND round = ? AND key = ?
                        """,
                        (
                            ErrorCode.RECOVERY_CONFLICT,
                            bucket,
                            round_number,
                            item["key"],
                        ),
                    )
                    connection.execute(
                        """
                        UPDATE objects
                        SET migration_state = 'pending',
                            last_outcome = 'recovery_conflict'
                        WHERE bucket = ? AND key = ?
                        """,
                        (bucket, item["key"]),
                    )
                    conflicts += 1
                    continue

                if item["planned_outcome"] == "success":
                    cursor = connection.execute(
                        """
                        UPDATE objects
                        SET current_layer = ?, version = version + 1,
                            migration_state = 'migrated', failure_count = 0,
                            backoff_until_clock = 0,
                            last_outcome = 'started_success'
                        WHERE bucket = ? AND key = ? AND version = ?
                          AND current_layer = ?
                        """,
                        (
                            item["target_layer"],
                            bucket,
                            item["key"],
                            item["object_version"],
                            item["source_layer"],
                        ),
                    )
                    if cursor.rowcount != 1:
                        raise BusinessError(
                            ErrorCode.RECOVERY_CONFLICT,
                            "conditional success update lost a race",
                            {"bucket": bucket, "key": item["key"]},
                        )
                    connection.execute(
                        """
                        UPDATE inflight SET status = 'committed'
                        WHERE bucket = ? AND round = ? AND key = ?
                          AND object_version = ?
                        """,
                        (
                            bucket,
                            round_number,
                            item["key"],
                            item["object_version"],
                        ),
                    )
                    connection.execute(
                        """
                        UPDATE batch_items
                        SET state = 'committed', outcome = 'started_success',
                            reason = NULL, error_code = NULL
                        WHERE bucket = ? AND round = ? AND key = ?
                        """,
                        (bucket, round_number, item["key"]),
                    )
                else:
                    new_failures = int(obj.failure_count) + 1
                    delay = config.backoff.delay_for(new_failures)
                    deadline = int(batch["effective_clock"]) + delay
                    retry_limit = (
                        new_failures >= config.backoff.max_consecutive_failures
                    )
                    new_state = "abandoned" if retry_limit else "failed"
                    outcome = (
                        "abandoned_retry_limit"
                        if retry_limit
                        else "started_failed"
                    )
                    connection.execute(
                        """
                        UPDATE objects
                        SET migration_state = ?, failure_count = ?,
                            backoff_until_clock = ?, last_outcome = ?
                        WHERE bucket = ? AND key = ?
                        """,
                        (
                            new_state,
                            new_failures,
                            deadline,
                            outcome,
                            bucket,
                            item["key"],
                        ),
                    )
                    connection.execute(
                        """
                        UPDATE inflight SET status = 'released'
                        WHERE bucket = ? AND round = ? AND key = ?
                          AND object_version = ?
                        """,
                        (
                            bucket,
                            round_number,
                            item["key"],
                            item["object_version"],
                        ),
                    )
                    connection.execute(
                        """
                        UPDATE batch_items
                        SET state = ?, outcome = ?,
                            reason = 'injected_or_runtime_migration_failure',
                            error_code = 'E_INJECTED_MIGRATION_FAILURE'
                        WHERE bucket = ? AND round = ? AND key = ?
                        """,
                        (
                            "abandoned" if retry_limit else "released",
                            outcome,
                            bucket,
                            round_number,
                            item["key"],
                        ),
                    )
                if not crash_once:
                    _maybe_crash("during_finalize")
                    crash_once = True

            computed = reconcile_ledger(connection, bucket)
            capacity = layer_capacity(connection, bucket)
            for layer in (HOT, COLD):
                if computed[layer] > capacity[layer]:
                    raise BusinessError(
                        ErrorCode.CAPACITY_LEDGER_VIOLATION,
                        "final ledger exceeds layer capacity",
                        {
                            "bucket": bucket,
                            "layer": layer,
                            "used_bytes": computed[layer],
                            "capacity_bytes": capacity[layer],
                        },
                    )
            result = self._build_batch_result(
                connection,
                batch,
                before,
                computed,
                recovered_committed,
                conflicts,
            )
            connection.execute(
                """
                UPDATE batches
                SET status = 'completed', result_json = ?
                WHERE bucket = ? AND round = ?
                """,
                (canonical_json(result), bucket, round_number),
            )
            connection.execute(
                """
                UPDATE buckets
                SET clock_watermark = MAX(clock_watermark, ?),
                    round_watermark = MAX(round_watermark, ?),
                    mode_last = ?,
                    decision_json = ?
                WHERE bucket = ?
                """,
                (
                    int(batch["effective_clock"]),
                    round_number,
                    batch["mode"],
                    batch["decision_json"],
                    bucket,
                ),
            )
            return result

    def _build_batch_result(
        self,
        connection: sqlite3.Connection,
        batch: sqlite3.Row,
        ledger_before: dict[str, int],
        ledger_after: dict[str, int],
        recovered_committed: int,
        conflicts: int,
    ) -> dict[str, Any]:
        rows = connection.execute(
            """
            SELECT * FROM batch_items
            WHERE bucket = ? AND round = ?
            ORDER BY order_index
            """,
            (batch["bucket"], batch["round"]),
        ).fetchall()
        aggregate: dict[tuple[str, str, str, str], dict[str, Any]] = {}
        for row in rows:
            key = (
                row["state"],
                row["outcome"] or "",
                row["reason"] or "",
                row["error_code"] or "",
            )
            entry = aggregate.setdefault(
                key,
                {
                    "state": row["state"],
                    "outcome": row["outcome"],
                    "reason": row["reason"],
                    "error_code": row["error_code"],
                    "count": 0,
                    "bytes": 0,
                },
            )
            entry["count"] += 1
            entry["bytes"] += int(row["size_bytes"])
        outcomes = sorted(
            aggregate.values(),
            key=lambda item: (
                item["state"],
                item["outcome"] or "",
                item["reason"] or "",
                item["error_code"] or "",
            ),
        )
        items = [
            {
                "key": row["key"],
                "object_version": int(row["object_version"]),
                "start_order": (
                    None if row["start_order"] is None else int(row["start_order"])
                ),
                "size_bytes": int(row["size_bytes"]),
                "source_layer": row["source_layer"],
                "target_layer": row["target_layer"],
                "score": row["score"],
                "planned_outcome": row["planned_outcome"],
                "state": row["state"],
                "outcome": row["outcome"],
                "reason": row["reason"],
                "error_code": row["error_code"],
            }
            for row in rows[:MAX_RESULT_ITEMS]
        ]
        observation = json.loads(batch["observation_json"])
        decision = json.loads(batch["decision_json"])
        return {
            "ok": True,
            "batch_id": f"{batch['bucket']}:{batch['round']}",
            "bucket": batch["bucket"],
            "round": int(batch["round"]),
            "request_clock": int(batch["request_clock"]),
            "effective_clock": int(batch["effective_clock"]),
            "mode": batch["mode"],
            "mode_decision": decision,
            "observation": observation,
            "candidate_count": len(rows),
            "candidate_bytes": sum(int(row["size_bytes"]) for row in rows),
            "started_count": sum(
                1 for row in rows if row["start_order"] is not None
            ),
            "success_count": sum(
                1 for row in rows if row["outcome"] == "started_success"
            ),
            "failed_count": sum(
                1 for row in rows if row["outcome"] == "started_failed"
            ),
            "abandoned_count": sum(
                1
                for row in rows
                if row["state"] in ("abandoned", "conflict")
                or row["outcome"] == "abandoned_retry_limit"
            ),
            "not_started_count": sum(
                1
                for row in rows
                if row["outcome"] == "not_started_within_budget"
            ),
            "outcomes": outcomes,
            "ledger_before": {
                HOT: ledger_before.get(HOT, 0),
                COLD: ledger_before.get(COLD, 0),
            },
            "ledger_after": {
                HOT: ledger_after.get(HOT, 0),
                COLD: ledger_after.get(COLD, 0),
            },
            "recovery": {
                "recovered_committed_observations": recovered_committed,
                "conflicts": conflicts,
            },
            "items": items,
            "items_truncated": len(rows) > MAX_RESULT_ITEMS,
            "candidate_audit_rows": len(rows),
            "truth_source": observation["truth_source"],
        }

    def resume(self, bucket: str | None = None) -> dict[str, Any]:
        self._require_database()
        resumed: list[dict[str, Any]] = []
        with file_lock(self.db_path, exclusive=True):
            connection = connect_database(self.db_path)
            try:
                if bucket is None:
                    rows = connection.execute(
                        """
                        SELECT bucket, round FROM batches
                        WHERE status = 'in_flight'
                        ORDER BY bucket, round
                        """
                    ).fetchall()
                else:
                    self._get_bucket(connection, bucket)
                    rows = connection.execute(
                        """
                        SELECT bucket, round FROM batches
                        WHERE bucket = ? AND status = 'in_flight'
                        ORDER BY round
                        """,
                        (bucket,),
                    ).fetchall()
                for row in rows:
                    result = self._finalize_batch(
                        connection,
                        row["bucket"],
                        int(row["round"]),
                    )
                    resumed.append(
                        {
                            "batch_id": result["batch_id"],
                            "mode": result["mode"],
                            "success_count": result["success_count"],
                            "failed_count": result["failed_count"],
                            "abandoned_count": result["abandoned_count"],
                            "ledger_after": result["ledger_after"],
                        }
                    )
                repaired = self._repair_orphan_migrating(connection, bucket)
                buckets = (
                    [bucket]
                    if bucket is not None
                    else [
                        item["bucket"]
                        for item in connection.execute(
                            "SELECT bucket FROM buckets ORDER BY bucket"
                        ).fetchall()
                    ]
                )
                with transaction(connection):
                    for name in buckets:
                        reconcile_ledger(connection, name)
            finally:
                connection.close()
        return {
            "ok": True,
            "command": "resume",
            "resumed_count": len(resumed),
            "repaired_orphan_migrating": repaired,
            "resumed_batches": resumed,
            "truth_source": "persisted batches and in-flight reservations",
        }

    def _repair_orphan_migrating(
        self,
        connection: sqlite3.Connection,
        bucket: str | None,
    ) -> int:
        with transaction(connection):
            if bucket is None:
                cursor = connection.execute(
                    """
                    UPDATE objects AS o
                    SET migration_state = 'pending',
                        last_outcome = 'recovered_orphan_pending'
                    WHERE migration_state = 'migrating'
                      AND NOT EXISTS (
                          SELECT 1 FROM inflight AS i
                          WHERE i.bucket = o.bucket AND i.key = o.key
                            AND i.status = 'reserved'
                      )
                    """
                )
            else:
                cursor = connection.execute(
                    """
                    UPDATE objects AS o
                    SET migration_state = 'pending',
                        last_outcome = 'recovered_orphan_pending'
                    WHERE o.bucket = ? AND migration_state = 'migrating'
                      AND NOT EXISTS (
                          SELECT 1 FROM inflight AS i
                          WHERE i.bucket = o.bucket AND i.key = o.key
                            AND i.status = 'reserved'
                      )
                    """,
                    (bucket,),
                )
            return cursor.rowcount

    def status(self, bucket: str | None = None) -> dict[str, Any]:
        self._require_database()
        with file_lock(self.db_path, exclusive=False):
            connection = connect_database(self.db_path)
            try:
                with transaction(connection, immediate=False):
                    names = self._bucket_names(connection, bucket)
                    result = [
                        self._bucket_status(connection, name) for name in names
                    ]
            finally:
                connection.close()
        return {
            "ok": True,
            "command": "status",
            "buckets": result,
            "truth_source": (
                "SQLite persisted state evaluated with the persisted injected "
                "logical clock; no wall clock is read"
            ),
        }

    def _bucket_names(
        self,
        connection: sqlite3.Connection,
        bucket: str | None,
    ) -> list[str]:
        if bucket is not None:
            self._get_bucket(connection, bucket)
            return [bucket]
        return [
            row["bucket"]
            for row in connection.execute(
                "SELECT bucket FROM buckets ORDER BY bucket"
            ).fetchall()
        ]

    def _bucket_status(
        self,
        connection: sqlite3.Connection,
        bucket: str,
    ) -> dict[str, Any]:
        bucket_row = self._get_bucket(connection, bucket)
        config = _config_from_row(bucket_row)
        clock = int(bucket_row["clock_watermark"])
        used = layer_used(connection, bucket)
        capacity = layer_capacity(connection, bucket)
        reserved = layer_reserved(connection, bucket)
        objects = self._load_objects(connection, bucket)
        object_views: list[dict[str, Any]] = []
        backoff_views: list[dict[str, Any]] = []
        for obj in objects:
            evaluation = evaluate_object(obj, clock, config)
            view = {
                "key": obj.key,
                "size_bytes": obj.size_bytes,
                "last_access_clock": obj.last_access_clock,
                "access_count": obj.access_count,
                "heat_weight": decimal_text(obj.heat_weight),
                "heat_score": decimal_text(evaluation.score),
                "declared_layer": obj.declared_layer,
                "current_layer": obj.current_layer,
                "target_layer": evaluation.target_layer,
                "tier_change": evaluation.direction,
                "migratable": obj.migratable,
                "migration_state": obj.migration_state,
                "failure_count": obj.failure_count,
                "backoff_until_clock": obj.backoff_until_clock,
                "version": obj.version,
                "last_round": obj.last_round,
                "last_outcome": obj.last_outcome,
                "eligible_candidate": evaluation.eligible,
                "exclusion_reason": evaluation.exclusion_reason,
            }
            object_views.append(view)
            if obj.backoff_until_clock > 0:
                backoff_views.append(
                    {
                        "key": obj.key,
                        "failure_count": obj.failure_count,
                        "backoff_until_clock": obj.backoff_until_clock,
                        "migration_state": obj.migration_state,
                    }
                )
        inflight_rows = connection.execute(
            """
            SELECT key, object_version, source_layer, target_layer,
                   size_bytes, status
            FROM inflight
            WHERE bucket = ? AND status = 'reserved'
            ORDER BY key, object_version
            """,
            (bucket,),
        ).fetchall()
        last_batch = connection.execute(
            """
            SELECT round, status, mode, decision_json, result_json
            FROM batches WHERE bucket = ?
            ORDER BY round DESC LIMIT 1
            """,
            (bucket,),
        ).fetchone()
        last_batch_view = None
        if last_batch is not None:
            last_result = (
                json.loads(last_batch["result_json"])
                if last_batch["result_json"]
                else None
            )
            last_batch_view = {
                "round": int(last_batch["round"]),
                "status": last_batch["status"],
                "mode": last_batch["mode"],
                "mode_decision": json.loads(last_batch["decision_json"]),
                "summary": (
                    None
                    if last_result is None
                    else {
                        "candidate_count": last_result["candidate_count"],
                        "started_count": last_result["started_count"],
                        "success_count": last_result["success_count"],
                        "failed_count": last_result["failed_count"],
                        "abandoned_count": last_result["abandoned_count"],
                        "not_started_count": last_result["not_started_count"],
                        "ledger_after": last_result["ledger_after"],
                    }
                ),
            }
        layers = {}
        for layer in (HOT, COLD):
            layers[layer] = {
                "capacity_bytes": capacity[layer],
                "used_bytes": used[layer],
                "reserved_bytes": reserved[layer],
                "available_bytes": max(
                    0,
                    capacity[layer] - used[layer] - reserved[layer],
                ),
            }
        return {
            "bucket": bucket,
            "clock_watermark": clock,
            "clock_tolerance": int(bucket_row["clock_tolerance"]),
            "round_watermark": int(bucket_row["round_watermark"]),
            "mode": (
                bucket_row["mode_last"]
                or (last_batch["mode"] if last_batch is not None else "none")
            ),
            "last_mode_decision": (
                json.loads(bucket_row["decision_json"])
                if bucket_row["decision_json"] is not None
                else (
                    json.loads(last_batch["decision_json"])
                    if last_batch is not None
                    else None
                )
            ),
            "config": config.to_dict(),
            "layers": layers,
            "ledger_total_bytes": used[HOT] + used[COLD],
            "objects_total_bytes": sum(obj.size_bytes for obj in objects),
            "inflight": [
                {
                    "key": row["key"],
                    "object_version": int(row["object_version"]),
                    "source_layer": row["source_layer"],
                    "target_layer": row["target_layer"],
                    "size_bytes": int(row["size_bytes"]),
                    "status": row["status"],
                }
                for row in inflight_rows
            ],
            "backoff": sorted(
                backoff_views,
                key=lambda item: (item["backoff_until_clock"], item["key"]),
            ),
            "last_batch": last_batch_view,
            "objects": sorted(object_views, key=lambda item: item["key"]),
        }

    def verify(self, bucket: str | None = None) -> dict[str, Any]:
        self._require_database()
        violations: list[dict[str, Any]] = []
        summaries: list[dict[str, Any]] = []
        with file_lock(self.db_path, exclusive=False):
            connection = connect_database(self.db_path)
            try:
                with transaction(connection, immediate=False):
                    names = self._bucket_names(connection, bucket)
                    for name in names:
                        summary = self._verify_bucket(
                            connection,
                            name,
                            violations,
                        )
                        summaries.append(summary)
            finally:
                connection.close()
        violations.sort(
            key=lambda item: (
                item.get("bucket", ""),
                item.get("code", ""),
                item.get("layer", ""),
                item.get("object_key", ""),
            )
        )
        return {
            "ok": not violations,
            "command": "verify",
            "violation_count": len(violations),
            "violations": violations,
            "buckets": summaries,
            "truth_source": (
                "object-level persisted layer aggregation compared with layer "
                "ledgers, reservations, batch states and logical clock"
            ),
        }

    def _verify_bucket(
        self,
        connection: sqlite3.Connection,
        bucket: str,
        violations: list[dict[str, Any]],
    ) -> dict[str, Any]:
        bucket_row = self._get_bucket(connection, bucket)
        config = _config_from_row(bucket_row)
        used = layer_used(connection, bucket)
        capacity = layer_capacity(connection, bucket)
        computed = {"hot": 0, "cold": 0}
        object_rows = connection.execute(
            "SELECT key, size_bytes, current_layer, migration_state, version FROM objects WHERE bucket = ?",
            (bucket,),
        ).fetchall()
        for row in object_rows:
            computed[row["current_layer"]] += int(row["size_bytes"])
        for layer in (HOT, COLD):
            if used[layer] != computed[layer]:
                violations.append(
                    {
                        "bucket": bucket,
                        "code": "E_LEDGER_MISMATCH",
                        "layer": layer,
                        "recorded_bytes": used[layer],
                        "computed_bytes": computed[layer],
                    }
                )
            if used[layer] > capacity[layer]:
                violations.append(
                    {
                        "bucket": bucket,
                        "code": "E_CAPACITY_EXCEEDED",
                        "layer": layer,
                        "used_bytes": used[layer],
                        "capacity_bytes": capacity[layer],
                    }
                )
        total_bytes = sum(int(row["size_bytes"]) for row in object_rows)
        if used[HOT] + used[COLD] != total_bytes:
            violations.append(
                {
                    "bucket": bucket,
                    "code": "E_TOTAL_LEDGER_MISMATCH",
                    "recorded_bytes": used[HOT] + used[COLD],
                    "computed_bytes": total_bytes,
                }
            )

        reserved = layer_reserved(connection, bucket)
        for layer in (HOT, COLD):
            if used[layer] + reserved[layer] > capacity[layer]:
                violations.append(
                    {
                        "bucket": bucket,
                        "code": "E_RESERVED_CAPACITY_EXCEEDED",
                        "layer": layer,
                        "used_bytes": used[layer],
                        "reserved_bytes": reserved[layer],
                        "capacity_bytes": capacity[layer],
                    }
                )

        active_inflight = connection.execute(
            """
            SELECT i.*, o.version AS current_version,
                   o.current_layer AS current_layer,
                   o.migration_state AS migration_state
            FROM inflight i
            JOIN objects o ON o.bucket = i.bucket AND o.key = i.key
            WHERE i.bucket = ? AND i.status = 'reserved'
            ORDER BY i.key
            """,
            (bucket,),
        ).fetchall()
        for row in active_inflight:
            if (
                int(row["current_version"]) != int(row["object_version"])
                or row["current_layer"] != row["source_layer"]
                or row["migration_state"] != "migrating"
            ):
                violations.append(
                    {
                        "bucket": bucket,
                        "code": "E_INFLIGHT_STATE_MISMATCH",
                        "object_key": row["key"],
                    }
                )

        in_flight_batches = connection.execute(
            "SELECT COUNT(*) AS n FROM batches WHERE bucket = ? AND status = 'in_flight'",
            (bucket,),
        ).fetchone()["n"]
        if int(in_flight_batches) > 0:
            violations.append(
                {
                    "bucket": bucket,
                    "code": "E_IN_FLIGHT_BATCH",
                    "batch_count": int(in_flight_batches),
                }
            )

        reserved_items_in_completed = connection.execute(
            """
            SELECT COUNT(*) AS n
            FROM batch_items i
            JOIN batches b ON b.bucket = i.bucket AND b.round = i.round
            WHERE i.bucket = ? AND b.status = 'completed'
              AND i.state = 'reserved'
            """,
            (bucket,),
        ).fetchone()["n"]
        if int(reserved_items_in_completed) > 0:
            violations.append(
                {
                    "bucket": bucket,
                    "code": "E_COMPLETED_BATCH_HAS_RESERVED_ITEM",
                    "item_count": int(reserved_items_in_completed),
                }
            )

        max_access = connection.execute(
            "SELECT COALESCE(MAX(last_access_clock), 0) AS c FROM objects WHERE bucket = ?",
            (bucket,),
        ).fetchone()["c"]
        if int(max_access) > int(bucket_row["clock_watermark"]):
            violations.append(
                {
                    "bucket": bucket,
                    "code": "E_CLOCK_WATERMARK_BEHIND_ACCESS",
                    "clock_watermark": int(bucket_row["clock_watermark"]),
                    "max_last_access_clock": int(max_access),
                }
            )

        completed_after_watermark = connection.execute(
            """
            SELECT COUNT(*) AS n FROM batches
            WHERE bucket = ? AND status = 'completed' AND round > ?
            """,
            (bucket, int(bucket_row["round_watermark"])),
        ).fetchone()["n"]
        if int(completed_after_watermark) > 0:
            violations.append(
                {
                    "bucket": bucket,
                    "code": "E_ROUND_WATERMARK_BEHIND_BATCH",
                    "batch_count": int(completed_after_watermark),
                }
            )

        return {
            "bucket": bucket,
            "object_count": len(object_rows),
            "total_bytes": total_bytes,
            "clock_watermark": int(bucket_row["clock_watermark"]),
            "round_watermark": int(bucket_row["round_watermark"]),
            "layers": {
                layer: {
                    "capacity_bytes": capacity[layer],
                    "recorded_used_bytes": used[layer],
                    "computed_used_bytes": computed[layer],
                    "reserved_bytes": reserved[layer],
                    "consistent": used[layer] == computed[layer],
                }
                for layer in (HOT, COLD)
            },
            "active_inflight_count": len(active_inflight),
            "in_flight_batch_count": int(in_flight_batches),
            "capacity_policy": {
                "hot_capacity_bytes": config.hot_capacity_bytes,
                "cold_capacity_bytes": config.cold_capacity_bytes,
            },
        }
