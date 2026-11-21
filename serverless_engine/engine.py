"""Deterministic cold-start, concurrency, invalidation, and recovery engine."""

from __future__ import annotations

import json
import hashlib
import os
import sqlite3
from pathlib import Path
from typing import Any, Iterable

import numpy as np

from .errors import bad
from .models import (
    CallSpec,
    FunctionManifest,
    FunctionSpec,
    LayerManifest,
    LayerSpec,
    Observation,
    SnapshotMerge,
)
from .snapshots import merge_snapshots
from .store import Store, snapshot_commit_payload
from .util import (
    as_nonnegative_int,
    as_positive_int,
    atomic_write_json,
    canonical_json,
    read_json,
    require_list,
    require_object,
    require_string,
    sha256_json,
    state_file_lock,
)
from .validation import (
    affected_functions,
    effective_layers,
    layer_closure,
    manifest_payload,
    parse_function_manifest,
    parse_layer_manifest,
    parse_trace,
)


STATE_UNINITIALIZED = "UNINITIALIZED"
STATE_INITIALIZING = "INITIALIZING"
STATE_WARMED = "WARMED"
STATE_EXPIRED = "EXPIRED"
STATE_INVALIDATED = "INVALIDATED"
ALL_STATES = {
    STATE_UNINITIALIZED,
    STATE_INITIALIZING,
    STATE_WARMED,
    STATE_EXPIRED,
    STATE_INVALIDATED,
}

PRECISION_TOLERANCE = 1e-9


def _current_layer_versions(layers: LayerManifest) -> dict[str, int]:
    return {name: layers.layers[name].version for name in sorted(layers.layers)}


def _init_profile(
    function: FunctionSpec, layers: LayerManifest
) -> tuple[str, int, list[dict[str, Any]], dict[str, int], str]:
    bound_versions = {
        name: layers.layers[name].version
        for name in effective_layers(function, layers.layers)
    }
    entries: list[dict[str, Any]] = []
    declared_total = 0
    for name in sorted(bound_versions):
        layer = layers.layers[name]
        params = function.layer_params.get(name)
        if params is None:
            env = dict(layer.env)
            init_ms = layer.default_init_ms
        else:
            env = dict(params["env"])
            init_ms = int(params["init_logical_ms"])
        declared_total += init_ms
        entries.append(
            {
                "name": name,
                "version": layer.version,
                "digest": layer.digest,
                "env": env,
                "init_logical_ms": init_ms,
                "direct": name in function.layer_refs,
            }
        )
    signature = sha256_json(
        {
            "function": function.name,
            "runtime": function.runtime,
            "memory_mib": function.memory_mib,
            "init_base_ms": function.init_base_ms,
            "layers": entries,
        }
    )
    jitter = int(signature.split(":", 1)[1][:8], 16) % 17
    duration = function.init_base_ms + declared_total + jitter
    cache_key = "init:" + signature.split(":", 1)[1]
    return signature, duration, entries, bound_versions, cache_key


def _tombstoned_layers(function: FunctionSpec, layers: LayerManifest) -> list[str]:
    return [
        name
        for name in effective_layers(function, layers.layers)
        if layers.layers[name].tombstone
    ]


def _call_key(call_seq: int, config_version: int) -> str:
    return f"{call_seq}:{config_version}"


def _instance_id(function: str, generation: int, instance_seq: int) -> str:
    return f"inst-{function}-g{generation:06d}-{instance_seq:06d}"


def _candidate_score(
    row: sqlite3.Row, now: int, ttl_ms: int
) -> tuple[float, list[dict[str, Any]]]:
    age = np.float64(max(0, now - int(row["ready_clock_ms"] or now)))
    ttl = np.float64(max(1, ttl_ms))
    recency = np.clip(np.float64(1.0) - age / ttl, 0.0, 1.0)
    free = np.float64(
        max(0, int(row["capacity"]) - int(row["active_occupancy"]))
    ) / np.float64(max(1, int(row["capacity"])))
    weighted_recency = np.float64(0.75) * recency
    weighted_free = np.float64(0.25) * free
    score = np.float64(weighted_recency + weighted_free)
    epsilon = float(np.finfo(np.float64).eps)
    chain = [
        {
            "stage": "recency",
            "formula": "clip(1 - (now - ready)/ttl, 0, 1)",
            "inputs": {
                "now": now,
                "ready": int(row["ready_clock_ms"] or now),
                "ttl": ttl_ms,
            },
            "error_bound": epsilon * 2.0,
        },
        {
            "stage": "free_capacity",
            "formula": "(capacity - active_occupancy)/capacity",
            "inputs": {
                "capacity": int(row["capacity"]),
                "active_occupancy": int(row["active_occupancy"]),
            },
            "error_bound": epsilon,
        },
        {
            "stage": "weighted_score",
            "formula": "0.75*recency + 0.25*free_capacity",
            "inputs": {"recency": round(float(recency), 12), "free": round(float(free), 12)},
            "error_bound": epsilon * 8.0,
        },
    ]
    return round(float(score), 12), chain


class Engine:
    def __init__(self, state_dir: str | Path):
        self.store = Store(state_dir)
        self.state_dir = Path(state_dir)

    # ------------------------------------------------------------------
    # load
    # ------------------------------------------------------------------
    def load(
        self,
        functions_path: str,
        layers_path: str,
        snapshot_v1_path: str,
        snapshot_v2_index_path: str,
        *,
        config_version: int = 1,
        snapshot_version: int = 1,
    ) -> dict[str, Any]:
        if config_version <= 0 or snapshot_version <= 0:
            raise bad("E_BAD_VERSION", "versions must be positive integers")
        layer_payload = read_json(layers_path)
        function_payload = read_json(functions_path)
        layer_manifest = parse_layer_manifest(layer_payload)
        function_manifest = parse_function_manifest(function_payload, layer_manifest)
        merged = merge_snapshots(
            snapshot_v1_path,
            snapshot_v2_index_path,
            set(function_manifest.functions),
        )
        payload = manifest_payload(function_manifest, layer_manifest)
        config_hash = sha256_json(payload)
        snapshot_payload = snapshot_commit_payload(merged)
        snapshot_hash = sha256_json(snapshot_payload)

        with state_file_lock(self.state_dir):
            self.store.initialize()
            with self.store.transaction() as connection:
                pointer = connection.execute(
                    "SELECT * FROM pointers WHERE singleton = 1"
                ).fetchone()
                if pointer is not None:
                    if config_version < pointer["config_version"]:
                        raise bad(
                            "E_VERSION_ROLLBACK",
                            "config version is older than the committed pointer",
                            "$.config_version",
                        )
                    if snapshot_version < pointer["snapshot_version"]:
                        raise bad(
                            "E_VERSION_ROLLBACK",
                            "snapshot version is older than the committed pointer",
                            "$.snapshot_version",
                        )
                    current_config = connection.execute(
                        "SELECT manifest_hash FROM config_commits WHERE version = ?",
                        (pointer["config_version"],),
                    ).fetchone()
                    current_snapshot = connection.execute(
                        "SELECT payload_hash FROM snapshot_commits WHERE version = ?",
                        (pointer["snapshot_version"],),
                    ).fetchone()
                    if (
                        config_version == pointer["config_version"]
                        and snapshot_version == pointer["snapshot_version"]
                        and current_config is not None
                        and current_config["manifest_hash"] == config_hash
                        and current_snapshot is not None
                        and current_snapshot["payload_hash"] == snapshot_hash
                    ):
                        result = self._load_result(
                            connection,
                            function_manifest,
                            layer_manifest,
                            merged,
                            idempotent=True,
                        )
                    else:
                        raise bad(
                            "E_STATE_EXISTS",
                            "engine state already contains a different committed configuration",
                            str(self.state_dir),
                        )
                else:
                    Store.insert_config(
                        connection,
                        version=config_version,
                        function_manifest=function_manifest,
                        layer_manifest=layer_manifest,
                        payload=payload,
                        source="load",
                        committed_clock_ms=0,
                        revision=1,
                    )
                    Store.insert_snapshot(
                        connection,
                        version=snapshot_version,
                        merge=merged,
                        committed_clock_ms=0,
                    )
                    max_generation = max(
                        [
                            obs.warm_generation
                            for obs in merged.observations.values()
                        ]
                        + [0]
                    )
                    connection.execute(
                        """
                        INSERT INTO pointers(
                            singleton, config_version, snapshot_version,
                            logical_clock_ms, call_watermark, patch_version,
                            instance_seq, warm_generation_seq, revision
                        ) VALUES (1, ?, ?, 0, 0, 0, 0, ?, 1)
                        """,
                        (config_version, snapshot_version, max_generation),
                    )
                    warm_count, final_clock = self._create_warm_pool(
                        connection,
                        function_manifest,
                        layer_manifest,
                        merged.observations,
                        config_version,
                        snapshot_version,
                    )
                    Store.touch_pointer(connection, logical_clock_ms=final_clock)
                    result = self._load_result(
                        connection,
                        function_manifest,
                        layer_manifest,
                        merged,
                        idempotent=False,
                    )
                    result["warm_instances_created"] = warm_count

        artifacts = {
            "merged_snapshots.json": {
                "observations": [
                    {
                        "function": obs.function,
                        "source": obs.source,
                        "absolute_init_ms": obs.absolute_init_ms,
                        "observed_clock_ms": obs.observed_clock_ms,
                        "warm_generation": obs.warm_generation,
                        "warm_hit": obs.warm_hit,
                        "hit_watermark": obs.hit_watermark,
                        "scale_num": obs.scale_num,
                        "scale_den": obs.scale_den,
                        "rounding": obs.rounding,
                        "conversion_error_bound_ms": obs.conversion_error_bound_ms,
                        "selection_reason": obs.selection_reason,
                    }
                    for obs in sorted(
                        merged.observations.values(), key=lambda item: item.function
                    )
                ]
            },
            "snapshot_migration.json": {"migrations": list(merged.migrations)},
        }
        paths = []
        for name, artifact_payload in artifacts.items():
            path = self.store.artifacts_dir / name
            atomic_write_json(path, artifact_payload)
            paths.append("artifacts/" + name)
        result["artifacts"] = paths
        return result

    def _load_result(
        self,
        connection: sqlite3.Connection,
        function_manifest: FunctionManifest,
        layer_manifest: LayerManifest,
        merged: SnapshotMerge,
        *,
        idempotent: bool,
    ) -> dict[str, Any]:
        pointer = Store.pointer(connection)
        instance_count = connection.execute(
            "SELECT COUNT(*) AS n FROM instances"
        ).fetchone()["n"]
        return {
            "ok": True,
            "command": "load",
            "idempotent": idempotent,
            "config_version": pointer["config_version"],
            "snapshot_version": pointer["snapshot_version"],
            "logical_clock_ms": pointer["logical_clock_ms"],
            "global_concurrency_budget": function_manifest.global_concurrency_budget,
            "functions": len(function_manifest.functions),
            "layers": len(layer_manifest.layers),
            "warm_pool_instances": instance_count,
            "warm_instances_created": instance_count if idempotent else None,
            "merged_observations": len(merged.observations),
            "migrations": len(merged.migrations),
        }

    def _create_warm_pool(
        self,
        connection: sqlite3.Connection,
        function_manifest: FunctionManifest,
        layer_manifest: LayerManifest,
        observations: dict[str, Observation],
        config_version: int,
        snapshot_version: int,
    ) -> tuple[int, int]:
        pointer = Store.pointer(connection)
        instance_seq = int(pointer["instance_seq"])
        final_clock = int(pointer["logical_clock_ms"])
        created = 0
        for function_name in sorted(function_manifest.functions):
            function = function_manifest.functions[function_name]
            if function.warm_pool_target <= 0:
                continue
            if _tombstoned_layers(function, layer_manifest):
                continue
            observation = observations.get(function_name)
            generation = (
                observation.warm_generation
                if observation is not None and observation.warm_generation > 0
                else 1
            )
            signature, duration, breakdown, bound_versions, cache_key = _init_profile(
                function, layer_manifest
            )
            self._ensure_cache(
                connection,
                cache_key=cache_key,
                function_name=function_name,
                bound_versions=bound_versions,
                signature=signature,
                duration_ms=duration,
                config_version=config_version,
                snapshot_version=snapshot_version,
                clock_ms=0,
            )
            for ordinal in range(function.warm_pool_target):
                instance_seq += 1
                instance_id = _instance_id(function_name, generation, instance_seq)
                ready = duration
                expires = ready + function.warm_ttl_ms
                decision_basis = {
                    "kind": "warm_pool",
                    "function": function_name,
                    "generation": generation,
                    "bound_versions": bound_versions,
                    "init_signature": signature,
                    "init_duration_ms": duration,
                    "duration_model": "integer_ms_v1",
                    "duration_breakdown": breakdown,
                    "duration_error_bound_ms": 0.0,
                    "warm_pool_ordinal": ordinal,
                }
                connection.execute(
                    """
                    INSERT INTO instances(
                        instance_id, function_name, state, bound_versions_json,
                        warm_generation, active_occupancy, capacity,
                        created_clock_ms, init_started_clock_ms, ready_clock_ms,
                        expires_clock_ms, invalidated_clock_ms,
                        invalidation_source, pending_invalidation, config_version,
                        snapshot_version, init_cache_key, init_duration_ms,
                        init_error_bound_ms, decision_basis_json, warm_pool
                    ) VALUES (?, ?, ?, ?, ?, 0, ?, 0, 0, ?, ?, NULL, NULL, 0,
                              ?, ?, ?, ?, 0.0, ?, 1)
                    """,
                    (
                        instance_id,
                        function_name,
                        STATE_WARMED,
                        canonical_json(bound_versions),
                        generation,
                        function.max_concurrency,
                        ready,
                        expires,
                        config_version,
                        snapshot_version,
                        cache_key,
                        duration,
                        canonical_json(decision_basis),
                    ),
                )
                self._event(
                    connection,
                    instance_id,
                    None,
                    0,
                    "INIT_STARTED",
                    STATE_UNINITIALIZED,
                    STATE_INITIALIZING,
                    "warm pool initialization",
                    {"cache_key": cache_key, "duration_ms": duration},
                )
                self._event(
                    connection,
                    instance_id,
                    None,
                    ready,
                    "INIT_COMPLETED",
                    STATE_INITIALIZING,
                    STATE_WARMED,
                    "warm pool ready",
                    {"expires_clock_ms": expires},
                )
                final_clock = max(final_clock, ready)
                created += 1
        Store.touch_pointer(
            connection,
            instance_seq=instance_seq,
            logical_clock_ms=final_clock,
        )
        return created, final_clock

    def _ensure_cache(
        self,
        connection: sqlite3.Connection,
        *,
        cache_key: str,
        function_name: str,
        bound_versions: dict[str, int],
        signature: str,
        duration_ms: int,
        config_version: int,
        snapshot_version: int,
        clock_ms: int,
    ) -> bool:
        row = connection.execute(
            "SELECT * FROM init_cache WHERE cache_key = ?", (cache_key,)
        ).fetchone()
        if row is not None:
            if row["cache_state"] == "VALID":
                return True
            raise bad(
                "E_INIT_CACHE_INVALID",
                f"initialization cache {cache_key} is invalid for the same layer content",
                f"cache:{cache_key}",
            )
        connection.execute(
            """
            INSERT INTO init_cache(
                cache_key, function_name, bound_versions_json, digest_signature,
                init_duration_ms, error_bound_ms, cache_state, created_clock_ms,
                invalidated_clock_ms, invalidation_source, config_version,
                snapshot_version
            ) VALUES (?, ?, ?, ?, ?, 0.0, 'VALID', ?, NULL, NULL, ?, ?)
            """,
            (
                cache_key,
                function_name,
                canonical_json(bound_versions),
                signature,
                duration_ms,
                clock_ms,
                config_version,
                snapshot_version,
            ),
        )
        return False

    def _event(
        self,
        connection: sqlite3.Connection,
        instance_id: str,
        call_key: str | None,
        clock_ms: int,
        kind: str,
        from_state: str | None,
        to_state: str | None,
        reason: str,
        details: dict[str, Any],
    ) -> int:
        cursor = connection.execute(
            """
            INSERT INTO instance_events(
                instance_id, call_key, clock_ms, kind, from_state, to_state,
                reason, details_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                instance_id,
                call_key,
                clock_ms,
                kind,
                from_state,
                to_state,
                reason,
                canonical_json(details),
            ),
        )
        event_seq = int(cursor.lastrowid)
        connection.execute(
            "UPDATE instances SET last_event_seq = ? WHERE instance_id = ?",
            (event_seq, instance_id),
        )
        return event_seq

    # ------------------------------------------------------------------
    # invoke
    # ------------------------------------------------------------------
    def invoke_trace(
        self,
        trace_path: str,
        *,
        failpoint: str | None = None,
    ) -> dict[str, Any]:
        if not self.store.exists():
            raise bad("E_NOT_LOADED", "engine state does not exist", str(self.state_dir))
        raw_trace = read_json(trace_path)
        trace_hash = sha256_json(raw_trace)
        job_id = "invoke:" + trace_hash.split(":", 1)[1]
        with state_file_lock(self.state_dir):
            with self.store.read_snapshot() as connection:
                pointer = Store.pointer(connection)
                function_manifest, _, _ = Store.load_config(
                    connection, pointer["config_version"]
                )
            trace = parse_trace(raw_trace, function_manifest)
            with self.store.transaction() as connection:
                existing_job = connection.execute(
                    "SELECT * FROM recovery_jobs WHERE job_id = ?", (job_id,)
                ).fetchone()
                if existing_job is None:
                    connection.execute(
                        """
                        INSERT INTO recovery_jobs(
                            job_id, kind, payload_json, state, created_clock_ms,
                            updated_clock_ms, attempts, result_json
                        ) VALUES (?, 'invoke', ?, 'RUNNING', 0, 0, 1, NULL)
                        """,
                        (job_id, canonical_json(raw_trace)),
                    )
                else:
                    connection.execute(
                        """
                        UPDATE recovery_jobs
                        SET state = 'RUNNING', attempts = attempts + 1
                        WHERE job_id = ?
                        """,
                        (job_id,),
                    )

            call_keys: list[str] = []
            for call in trace.calls:
                call_result, start_phase, call_key = self._process_call(call)
                call_keys.append(call_key)
                if failpoint == "init-started" and start_phase == STATE_INITIALIZING:
                    os._exit(137)
            self._drain_calls(call_keys)
            with self.store.transaction() as connection:
                results = self._results_for_keys(connection, call_keys)
                connection.execute(
                    """
                    UPDATE recovery_jobs
                    SET state = 'DONE', updated_clock_ms = ?,
                        result_json = ?
                    WHERE job_id = ?
                    """,
                    (
                        Store.pointer(connection)["logical_clock_ms"],
                        canonical_json({"call_keys": call_keys}),
                        job_id,
                    ),
                )
                pointer = Store.pointer(connection)
                return {
                    "ok": True,
                    "command": "invoke",
                    "trace_hash": trace_hash,
                    "config_version": pointer["config_version"],
                    "snapshot_version": pointer["snapshot_version"],
                    "logical_clock_ms": pointer["logical_clock_ms"],
                    "call_watermark": pointer["call_watermark"],
                    "calls": results,
                }

    def _process_call(
        self, call: CallSpec
    ) -> tuple[dict[str, Any] | None, str | None, str]:
        with self.store.transaction() as connection:
            pointer = Store.pointer(connection)
            config_version = int(pointer["config_version"])
            snapshot_version = int(pointer["snapshot_version"])
            call_key = _call_key(call.seq, config_version)
            request_hash = sha256_json(
                {
                    "seq": call.seq,
                    "function": call.function,
                    "arrival_clock_ms": call.arrival_ms,
                    "occupancy": call.occupancy,
                    "duration_ms": call.duration_ms,
                    "wait_budget_ms": call.wait_budget_ms,
                    "cancel_after_ms": call.cancel_after_ms,
                    "config_version": config_version,
                    "snapshot_version": snapshot_version,
                }
            )
            existing = connection.execute(
                "SELECT * FROM calls WHERE call_key = ?", (call_key,)
            ).fetchone()
            if existing is not None:
                if existing["request_hash"] != request_hash:
                    raise bad(
                        "E_IDEMPOTENCY_CONFLICT",
                        "same call sequence/version was submitted with different content",
                        f"call:{call.seq}",
                    )
                if existing["phase"] == "DONE":
                    return json.loads(existing["result_json"]), existing["phase"], call_key
                return None, existing["phase"], call_key
            if call.seq <= pointer["call_watermark"]:
                raise bad(
                    "E_CALL_OUT_OF_ORDER",
                    f"call seq {call.seq} is not greater than watermark {pointer['call_watermark']}",
                    "call.seq",
                )
            function_manifest, layer_manifest, _ = Store.load_config(
                connection, config_version
            )
            function = function_manifest.functions[call.function]
            effective_arrival = max(int(pointer["logical_clock_ms"]), call.arrival_ms)
            self._tick(connection, effective_arrival)
            pointer = Store.pointer(connection)
            connection.execute(
                """
                INSERT INTO calls(
                    call_key, call_seq, function_name, occupancy,
                    arrival_clock_ms, effective_arrival_clock_ms, duration_ms,
                    wait_budget_ms, deadline_clock_ms, cancel_clock_ms,
                    phase, start_mode,
                    cache_hit, outcome, reason_code, reason, instance_id,
                    config_version, snapshot_version, request_hash,
                    committed_clock_ms, init_started_clock_ms, started_clock_ms,
                    finish_clock_ms, completed_clock_ms, result_json,
                    decision_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'PENDING', NULL, NULL,
                          NULL, NULL, NULL, NULL, ?, ?, ?, ?, NULL, NULL, NULL,
                          NULL, NULL, NULL)
                """,
                (
                    call_key,
                    call.seq,
                    call.function,
                    call.occupancy,
                    call.arrival_ms,
                    effective_arrival,
                    call.duration_ms,
                    call.wait_budget_ms,
                    effective_arrival + call.wait_budget_ms,
                    (
                        effective_arrival + call.cancel_after_ms
                        if call.cancel_after_ms is not None
                        else None
                    ),
                    config_version,
                    snapshot_version,
                    request_hash,
                    int(pointer["logical_clock_ms"]),
                ),
            )
            Store.touch_pointer(
                connection,
                call_watermark=call.seq,
                logical_clock_ms=int(pointer["logical_clock_ms"]),
            )
            self._dispatch_pending(connection, int(pointer["logical_clock_ms"]))
            row = connection.execute(
                "SELECT * FROM calls WHERE call_key = ?", (call_key,)
            ).fetchone()
            if row["phase"] == "DONE":
                return json.loads(row["result_json"]), row["phase"], call_key
            return None, row["phase"], call_key

    def _drain_calls(self, call_keys: Iterable[str]) -> None:
        keys = tuple(call_keys)
        if not keys:
            return
        placeholders = ",".join("?" for _ in keys)
        for _ in range(10000):
            with self.store.transaction() as connection:
                rows = connection.execute(
                    f"SELECT call_key, phase FROM calls WHERE call_key IN ({placeholders})",
                    keys,
                ).fetchall()
                if all(row["phase"] == "DONE" for row in rows) and len(rows) == len(keys):
                    return
                pointer = Store.pointer(connection)
                current = int(pointer["logical_clock_ms"])
                due = self._due_times(connection)
                future = [value for value in due if value is not None]
                if not future:
                    raise bad(
                        "E_DEADLOCK",
                        "unresolved calls have no reachable deterministic event",
                    )
                next_clock = min(future)
                if next_clock < current:
                    next_clock = current
                self._tick(connection, next_clock)
                if next_clock == current and all(
                    row["phase"] != "DONE" for row in rows
                ):
                    self._dispatch_pending(connection, current)
        raise bad("E_EVENT_LIMIT", "deterministic event loop exceeded its safety limit")

    def _results_for_keys(
        self, connection: sqlite3.Connection, call_keys: Iterable[str]
    ) -> list[dict[str, Any]]:
        keys = list(call_keys)
        results = []
        for key in keys:
            row = connection.execute(
                "SELECT result_json FROM calls WHERE call_key = ?", (key,)
            ).fetchone()
            if row is None or row["result_json"] is None:
                raise bad("E_INCOMPLETE", f"call {key} has no committed result")
            results.append(json.loads(row["result_json"]))
        return results

    def _due_times(self, connection: sqlite3.Connection) -> list[int]:
        times: list[int] = []
        for row in connection.execute(
            "SELECT ready_clock_ms FROM instances WHERE state = ? AND ready_clock_ms IS NOT NULL",
            (STATE_INITIALIZING,),
        ):
            times.append(int(row["ready_clock_ms"]))
        for row in connection.execute(
            "SELECT phase, deadline_clock_ms, finish_clock_ms, cancel_clock_ms FROM calls "
            "WHERE phase IN ('PENDING', 'INITIALIZING', 'RUNNING')"
        ):
            if row["phase"] == "PENDING":
                times.append(int(row["deadline_clock_ms"]))
            elif row["phase"] == "INITIALIZING":
                times.append(int(row["finish_clock_ms"]))
                if int(row["deadline_clock_ms"]) < int(row["finish_clock_ms"]):
                    times.append(int(row["deadline_clock_ms"]))
                if row["cancel_clock_ms"] is not None:
                    times.append(int(row["cancel_clock_ms"]))
            elif row["phase"] == "RUNNING" and row["finish_clock_ms"] is not None:
                times.append(int(row["finish_clock_ms"]))
                if row["cancel_clock_ms"] is not None:
                    times.append(int(row["cancel_clock_ms"]))
        for row in connection.execute(
            "SELECT expires_clock_ms FROM concurrency_leases WHERE released = 0"
        ):
            times.append(int(row["expires_clock_ms"]))
        for row in connection.execute(
            "SELECT expires_clock_ms FROM instances WHERE state = ? AND active_occupancy = 0 AND expires_clock_ms IS NOT NULL",
            (STATE_WARMED,),
        ):
            times.append(int(row["expires_clock_ms"]))
        return sorted(set(times))

    def _tick(self, connection: sqlite3.Connection, target_clock: int) -> None:
        guard = 0
        while True:
            guard += 1
            if guard > 10000:
                raise bad("E_EVENT_LIMIT", "logical clock event loop exceeded its safety limit")
            pointer = Store.pointer(connection)
            now = int(pointer["logical_clock_ms"])
            target = max(now, int(target_clock))
            due = [value for value in self._due_times(connection) if value <= target]
            if not due:
                if target != now:
                    Store.touch_pointer(connection, logical_clock_ms=target)
                return
            event_clock = max(now, min(due))
            self._process_at(connection, event_clock)
            Store.touch_pointer(connection, logical_clock_ms=max(event_clock, now))

    def _process_at(self, connection: sqlite3.Connection, now: int) -> None:
        pointer = Store.pointer(connection)
        config_version = int(pointer["config_version"])
        snapshot_version = int(pointer["snapshot_version"])
        function_manifest, layer_manifest, _ = Store.load_config(
            connection, config_version
        )
        current_versions = _current_layer_versions(layer_manifest)

        cancellations = connection.execute(
            """
            SELECT * FROM calls
            WHERE phase IN ('INITIALIZING', 'RUNNING')
              AND cancel_clock_ms IS NOT NULL AND cancel_clock_ms <= ?
            ORDER BY call_seq, call_key
            """,
            (now,),
        ).fetchall()
        for call in cancellations:
            instance = connection.execute(
                "SELECT * FROM instances WHERE instance_id = ?",
                (call["instance_id"],),
            ).fetchone()
            self._abort_active_call(connection, call, instance, now)

        initializing_calls = connection.execute(
            """
            SELECT * FROM calls
            WHERE phase = 'INITIALIZING' AND deadline_clock_ms < finish_clock_ms
              AND deadline_clock_ms <= ? AND phase = 'INITIALIZING'
            ORDER BY call_seq, call_key
            """,
            (now,),
        ).fetchall()
        for call in initializing_calls:
            instance = connection.execute(
                "SELECT * FROM instances WHERE instance_id = ?",
                (call["instance_id"],),
            ).fetchone()
            self._fail_after_start(
                connection,
                call,
                instance,
                now,
                "E_INIT_DEADLINE",
                "initialization exceeded the call waiting budget and was actively aborted",
            )

        instances_ready = connection.execute(
            "SELECT * FROM instances WHERE state = ? AND ready_clock_ms <= ? ORDER BY instance_id",
            (STATE_INITIALIZING, now),
        ).fetchall()
        for instance in instances_ready:
            self._complete_instance(connection, instance, now, current_versions)

        call_rows = connection.execute(
            """
            SELECT * FROM calls
            WHERE phase = 'INITIALIZING' AND finish_clock_ms IS NOT NULL
              AND finish_clock_ms <= ?
            ORDER BY call_seq, call_key
            """,
            (now,),
        ).fetchall()
        for call in call_rows:
            instance = connection.execute(
                "SELECT * FROM instances WHERE instance_id = ?",
                (call["instance_id"],),
            ).fetchone()
            if instance is None:
                self._fail_after_start(
                    connection,
                    call,
                    None,
                    now,
                    "E_INSTANCE_MISSING",
                    "initialized instance disappeared before call start",
                )
                continue
            self._start_running_call(connection, call, instance, now)

        running = connection.execute(
            """
            SELECT * FROM calls
            WHERE phase = 'RUNNING' AND finish_clock_ms <= ?
            ORDER BY call_seq, call_key
            """,
            (now,),
        ).fetchall()
        for call in running:
            self._finish_call(connection, call, now)

        expired = connection.execute(
            """
            SELECT * FROM instances
            WHERE state = ? AND active_occupancy = 0
              AND expires_clock_ms IS NOT NULL AND expires_clock_ms <= ?
            ORDER BY instance_id
            """,
            (STATE_WARMED, now),
        ).fetchall()
        for instance in expired:
            connection.execute(
                "UPDATE instances SET state = ?, invalidated_clock_ms = NULL WHERE instance_id = ?",
                (STATE_EXPIRED, instance["instance_id"]),
            )
            self._event(
                connection,
                instance["instance_id"],
                None,
                now,
                "EXPIRED",
                STATE_WARMED,
                STATE_EXPIRED,
                "warm TTL elapsed",
                {"expires_clock_ms": instance["expires_clock_ms"]},
            )

        self._dispatch_pending(connection, now)

    def _abort_active_call(
        self,
        connection: sqlite3.Connection,
        call: sqlite3.Row,
        instance: sqlite3.Row | None,
        now: int,
    ) -> None:
        if instance is not None:
            remaining = max(0, int(instance["active_occupancy"]) - int(call["occupancy"]))
            if call["phase"] == "INITIALIZING":
                connection.execute(
                    """
                    UPDATE instances
                    SET active_occupancy = ?, state = ?, invalidated_clock_ms = ?,
                        invalidation_source = 'explicit_cancel'
                    WHERE instance_id = ?
                    """,
                    (remaining, STATE_INVALIDATED, now, instance["instance_id"]),
                )
                self._event(
                    connection,
                    instance["instance_id"],
                    call["call_key"],
                    now,
                    "CALL_ABANDONED_ACTIVE",
                    instance["state"],
                    STATE_INVALIDATED,
                    "call was explicitly abandoned during initialization",
                    {"cancel_clock_ms": call["cancel_clock_ms"]},
                )
            else:
                connection.execute(
                    "UPDATE instances SET active_occupancy = ? WHERE instance_id = ?",
                    (remaining, instance["instance_id"]),
                )
                self._event(
                    connection,
                    instance["instance_id"],
                    call["call_key"],
                    now,
                    "CALL_ABANDONED_ACTIVE",
                    instance["state"],
                    instance["state"],
                    "running call was explicitly abandoned",
                    {"cancel_clock_ms": call["cancel_clock_ms"]},
                )
        connection.execute(
            """
            UPDATE concurrency_leases
            SET released = 1, release_clock_ms = ?
            WHERE call_key = ? AND released = 0
            """,
            (now, call["call_key"]),
        )
        result = self._call_result(
            call,
            outcome="ABANDONED_ACTIVE",
            reason_code="E_ACTIVE_CANCEL",
            reason="call was actively abandoned by an explicit cancel_after_ms directive",
            completed_clock_ms=now,
        )
        connection.execute(
            """
            UPDATE calls
            SET phase = 'DONE', outcome = 'ABANDONED_ACTIVE',
                reason_code = 'E_ACTIVE_CANCEL', reason = ?,
                completed_clock_ms = ?, result_json = ?
            WHERE call_key = ?
            """,
            (
                "call was actively abandoned by an explicit cancel_after_ms directive",
                now,
                canonical_json(result),
                call["call_key"],
            ),
        )

    def _complete_instance(
        self,
        connection: sqlite3.Connection,
        instance: sqlite3.Row,
        now: int,
        current_versions: dict[str, int],
    ) -> None:
        pointer = Store.pointer(connection)
        function_manifest, layer_manifest, _ = Store.load_config(
            connection, int(pointer["config_version"])
        )
        function = function_manifest.functions[instance["function_name"]]
        bound_versions = json.loads(instance["bound_versions_json"])
        function_versions = {
            name: current_versions[name]
            for name in effective_layers(function, layer_manifest.layers)
        }
        invalid = (
            bool(instance["pending_invalidation"])
            or bound_versions != function_versions
            or bool(_tombstoned_layers(function, layer_manifest))
        )
        if invalid:
            connection.execute(
                """
                UPDATE instances
                SET state = ?, invalidated_clock_ms = ?,
                    invalidation_source = COALESCE(invalidation_source, 'version_mismatch'),
                    pending_invalidation = 0
                WHERE instance_id = ?
                """,
                (STATE_INVALIDATED, now, instance["instance_id"]),
            )
            self._event(
                connection,
                instance["instance_id"],
                None,
                now,
                "INIT_COMPLETED_INVALIDATED",
                STATE_INITIALIZING,
                STATE_INVALIDATED,
                "initialization completed against an obsolete layer binding",
                {
                    "bound_versions": bound_versions,
                    "current_versions": function_versions,
                    "pending_invalidation": bool(instance["pending_invalidation"]),
                },
            )
            return
        expires = now + function.warm_ttl_ms
        connection.execute(
            """
            UPDATE instances
            SET state = ?, ready_clock_ms = ?, expires_clock_ms = ?,
                pending_invalidation = 0
            WHERE instance_id = ?
            """,
            (STATE_WARMED, now, expires, instance["instance_id"]),
        )
        self._event(
            connection,
            instance["instance_id"],
            None,
            now,
            "INIT_COMPLETED",
            STATE_INITIALIZING,
            STATE_WARMED,
            "deterministic initialization completed",
            {"expires_clock_ms": expires, "bound_versions": bound_versions},
        )

    def _start_running_call(
        self,
        connection: sqlite3.Connection,
        call: sqlite3.Row,
        instance: sqlite3.Row,
        now: int,
    ) -> None:
        finish = now + int(call["duration_ms"])
        connection.execute(
            """
            UPDATE calls
            SET phase = 'RUNNING', started_clock_ms = ?, finish_clock_ms = ?
            WHERE call_key = ?
            """,
            (now, finish, call["call_key"]),
        )
        connection.execute(
            """
            UPDATE concurrency_leases SET expires_clock_ms = ?
            WHERE call_key = ? AND released = 0
            """,
            (finish, call["call_key"]),
        )
        self._event(
            connection,
            instance["instance_id"],
            call["call_key"],
            now,
            "CALL_STARTED",
            instance["state"],
            instance["state"],
            "initialized instance began serving the call",
            {"finish_clock_ms": finish},
        )

    def _finish_call(
        self, connection: sqlite3.Connection, call: sqlite3.Row, now: int
    ) -> None:
        instance = connection.execute(
            "SELECT * FROM instances WHERE instance_id = ?", (call["instance_id"],)
        ).fetchone()
        if instance is not None:
            remaining = max(0, int(instance["active_occupancy"]) - int(call["occupancy"]))
            connection.execute(
                "UPDATE instances SET active_occupancy = ? WHERE instance_id = ?",
                (remaining, instance["instance_id"]),
            )
        connection.execute(
            """
            UPDATE concurrency_leases
            SET released = 1, release_clock_ms = ?
            WHERE call_key = ? AND released = 0
            """,
            (now, call["call_key"]),
        )
        outcome = (
            "REUSED_WARM"
            if call["start_mode"] == "REUSED_WARM"
            else "COLD_START_SUCCEEDED"
        )
        reason = (
            "reused a matching warmed instance"
            if outcome == "REUSED_WARM"
            else "cold start completed and served the call"
        )
        result = self._call_result(
            call,
            outcome=outcome,
            reason_code=None,
            reason=reason,
            completed_clock_ms=now,
        )
        connection.execute(
            """
            UPDATE calls
            SET phase = 'DONE', outcome = ?, reason = ?, completed_clock_ms = ?,
                result_json = ?
            WHERE call_key = ?
            """,
            (
                outcome,
                reason,
                now,
                canonical_json(result),
                call["call_key"],
            ),
        )

    def _fail_after_start(
        self,
        connection: sqlite3.Connection,
        call: sqlite3.Row,
        instance: sqlite3.Row | None,
        now: int,
        reason_code: str,
        reason: str,
    ) -> None:
        if instance is not None:
            remaining = max(0, int(instance["active_occupancy"]) - int(call["occupancy"]))
            connection.execute(
                """
                UPDATE instances
                SET active_occupancy = ?, state = ?, invalidated_clock_ms = ?,
                    invalidation_source = ?
                WHERE instance_id = ?
                """,
                (
                    remaining,
                    STATE_INVALIDATED,
                    now,
                    reason_code,
                    instance["instance_id"],
                ),
            )
            self._event(
                connection,
                instance["instance_id"],
                call["call_key"],
                now,
                "INIT_ABORTED",
                instance["state"],
                STATE_INVALIDATED,
                reason,
                {"reason_code": reason_code},
            )
        connection.execute(
            """
            UPDATE concurrency_leases
            SET released = 1, release_clock_ms = ?
            WHERE call_key = ? AND released = 0
            """,
            (now, call["call_key"]),
        )
        result = self._call_result(
            call,
            outcome="FAILED_AFTER_START",
            reason_code=reason_code,
            reason=reason,
            completed_clock_ms=now,
        )
        connection.execute(
            """
            UPDATE calls
            SET phase = 'DONE', outcome = ?, reason_code = ?, reason = ?,
                completed_clock_ms = ?, result_json = ?
            WHERE call_key = ?
            """,
            (
                "FAILED_AFTER_START",
                reason_code,
                reason,
                now,
                canonical_json(result),
                call["call_key"],
            ),
        )

    def _dispatch_pending(self, connection: sqlite3.Connection, now: int) -> None:
        pointer = Store.pointer(connection)
        config_version = int(pointer["config_version"])
        snapshot_version = int(pointer["snapshot_version"])
        function_manifest, layer_manifest, _ = Store.load_config(
            connection, config_version
        )
        budget = function_manifest.global_concurrency_budget
        current_versions = _current_layer_versions(layer_manifest)
        pending = connection.execute(
            "SELECT * FROM calls WHERE phase = 'PENDING' ORDER BY call_seq, call_key"
        ).fetchall()
        for call in pending:
            function = function_manifest.functions[call["function_name"]]
            global_used = connection.execute(
                "SELECT COALESCE(SUM(occupancy), 0) AS n FROM concurrency_leases WHERE released = 0"
            ).fetchone()["n"]
            function_used = connection.execute(
                """
                SELECT COALESCE(SUM(occupancy), 0) AS n
                FROM concurrency_leases
                WHERE released = 0 AND function_name = ?
                """,
                (function.name,),
            ).fetchone()["n"]
            occupancy = int(call["occupancy"])
            tombstoned = _tombstoned_layers(function, layer_manifest)
            if tombstoned:
                self._fail_pre_start(
                    connection,
                    call,
                    now,
                    "E_LAYER_TOMBSTONED",
                    f"function depends on tombstoned layer(s): {', '.join(tombstoned)}",
                )
                continue
            if (
                int(function_used) + occupancy > function.max_concurrency
                or int(global_used) + occupancy > budget
            ):
                if now >= int(call["deadline_clock_ms"]):
                    if int(function_used) + occupancy > function.max_concurrency:
                        code = "E_FUNCTION_CONCURRENCY"
                        reason = "function max_concurrency budget exhausted before start"
                    else:
                        code = "E_GLOBAL_BUDGET"
                        reason = "global concurrency budget exhausted before start"
                    self._abandon_pending(connection, call, now, code, reason)
                continue

            candidates = connection.execute(
                """
                SELECT * FROM instances
                WHERE function_name = ? AND state = ? AND pending_invalidation = 0
                  AND expires_clock_ms > ?
                  AND active_occupancy + ? <= capacity
                ORDER BY warm_generation DESC, ready_clock_ms DESC, instance_id
                """,
                (function.name, STATE_WARMED, now, occupancy),
            ).fetchall()
            usable = []
            for candidate in candidates:
                function_versions = {
                    name: current_versions[name]
                    for name in effective_layers(function, layer_manifest.layers)
                }
                if json.loads(candidate["bound_versions_json"]) == function_versions:
                    usable.append(candidate)
            if usable:
                scored = []
                for candidate in usable:
                    score, chain = _candidate_score(candidate, now, function.warm_ttl_ms)
                    scored.append((score, candidate, chain))
                scored.sort(
                    key=lambda entry: (
                        -entry[0],
                        -int(entry[1]["warm_generation"]),
                        -int(entry[1]["ready_clock_ms"] or 0),
                        entry[1]["instance_id"],
                    )
                )
                chosen_score, chosen, chosen_chain = scored[0]
                if len(scored) >= 2:
                    second_score, second, second_chain = scored[1]
                    difference = abs(chosen_score - second_score)
                    sensitive = difference < PRECISION_TOLERANCE
                    connection.execute(
                        """
                        INSERT INTO precision_notes(
                            note_key, kind, clock_ms, candidate_a, candidate_b,
                            score_a, score_b, difference, tolerance,
                            precision_sensitive, source_chain_json
                        ) VALUES (?, 'instance_selection', ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            call["call_key"],
                            now,
                            chosen["instance_id"],
                            second["instance_id"],
                            chosen_score,
                            second_score,
                            difference,
                            PRECISION_TOLERANCE,
                            1 if sensitive else 0,
                            canonical_json(
                                {
                                    "candidate_a": chosen_chain,
                                    "candidate_b": second_chain,
                                    "tie_break": "warm_generation, ready_clock_ms, instance_id",
                                }
                            ),
                        ),
                    )
                    decision = {
                        "kind": "warm_reuse",
                        "candidate": chosen["instance_id"],
                        "score": chosen_score,
                        "runner_up": second["instance_id"],
                        "score_difference": difference,
                        "precision_sensitive": sensitive,
                        "concurrency_accounting_error_bound": 0.0,
                        "error_source_chain": (
                            {
                                "candidate_a": chosen_chain,
                                "candidate_b": second_chain,
                                "tolerance": PRECISION_TOLERANCE,
                                "decision_use": "deterministic tie-break after binary64 score comparison",
                            }
                            if sensitive
                            else None
                        ),
                    }
                else:
                    decision = {
                        "kind": "warm_reuse",
                        "candidate": chosen["instance_id"],
                        "score": chosen_score,
                        "runner_up": None,
                        "score_difference": None,
                        "precision_sensitive": False,
                        "concurrency_accounting_error_bound": 0.0,
                        "error_source_chain": None,
                    }
                self._start_warm_call(
                    connection,
                    call,
                    chosen,
                    function,
                    now,
                    decision,
                )
                continue

            self._start_cold_call(
                connection,
                call,
                function,
                layer_manifest,
                config_version,
                snapshot_version,
                now,
            )

    def _start_warm_call(
        self,
        connection: sqlite3.Connection,
        call: sqlite3.Row,
        instance: sqlite3.Row,
        function: FunctionSpec,
        now: int,
        decision: dict[str, Any],
    ) -> None:
        occupancy = int(call["occupancy"])
        finish = now + int(call["duration_ms"])
        lease_id = "lease:" + call["call_key"]
        connection.execute(
            """
            INSERT INTO concurrency_leases(
                lease_id, call_key, function_name, occupancy, acquired_clock_ms,
                expires_clock_ms, released, release_clock_ms
            ) VALUES (?, ?, ?, ?, ?, ?, 0, NULL)
            """,
            (lease_id, call["call_key"], function.name, occupancy, now, finish),
        )
        connection.execute(
            "UPDATE instances SET active_occupancy = active_occupancy + ? WHERE instance_id = ?",
            (occupancy, instance["instance_id"]),
        )
        connection.execute(
            """
            UPDATE calls
            SET phase = 'RUNNING', start_mode = 'REUSED_WARM', cache_hit = 1,
                instance_id = ?, started_clock_ms = ?, finish_clock_ms = ?,
                decision_json = ?, outcome = NULL, reason_code = NULL
            WHERE call_key = ?
            """,
            (
                instance["instance_id"],
                now,
                finish,
                canonical_json(decision),
                call["call_key"],
            ),
        )
        self._event(
            connection,
            instance["instance_id"],
            call["call_key"],
            now,
            "CALL_REUSED",
            STATE_WARMED,
            STATE_WARMED,
            "warmed instance matched current layer versions",
            decision,
        )

    def _start_cold_call(
        self,
        connection: sqlite3.Connection,
        call: sqlite3.Row,
        function: FunctionSpec,
        layer_manifest: LayerManifest,
        config_version: int,
        snapshot_version: int,
        now: int,
    ) -> None:
        slots = connection.execute(
            """
            SELECT COALESCE(SUM(occupancy), 0) AS n
            FROM concurrency_leases WHERE released = 0
            """
        ).fetchone()["n"]
        budget = Store.load_config(connection, config_version)[0].global_concurrency_budget
        if int(slots) + int(call["occupancy"]) > budget:
            if now >= int(call["deadline_clock_ms"]):
                self._abandon_pending(
                    connection,
                    call,
                    now,
                    "E_GLOBAL_BUDGET",
                    "global concurrency budget exhausted before start",
                )
            return
        signature, duration, breakdown, bound_versions, cache_key = _init_profile(
            function, layer_manifest
        )
        cache_hit = self._ensure_cache(
            connection,
            cache_key=cache_key,
            function_name=function.name,
            bound_versions=bound_versions,
            signature=signature,
            duration_ms=duration,
            config_version=config_version,
            snapshot_version=snapshot_version,
            clock_ms=now,
        )
        pointer = Store.pointer(connection)
        generation = int(pointer["warm_generation_seq"]) + 1
        instance_seq = int(pointer["instance_seq"]) + 1
        instance_id = _instance_id(function.name, generation, instance_seq)
        ready = now + duration
        decision = {
            "kind": "cold_start",
            "generation": generation,
            "init_signature": signature,
            "init_duration_ms": duration,
            "duration_model": "integer_ms_v1",
            "duration_breakdown": breakdown,
            "duration_error_bound_ms": 0.0,
            "cache_key": cache_key,
            "cache_hit": cache_hit,
            "concurrency_slots_before": int(slots),
            "concurrency_slots_after": int(slots) + int(call["occupancy"]),
            "concurrency_accounting_error_bound": 0.0,
        }
        connection.execute(
            """
            INSERT INTO instances(
                instance_id, function_name, state, bound_versions_json,
                warm_generation, active_occupancy, capacity,
                created_clock_ms, init_started_clock_ms, ready_clock_ms,
                expires_clock_ms, invalidated_clock_ms, invalidation_source,
                pending_invalidation, config_version, snapshot_version,
                init_cache_key, init_duration_ms, init_error_bound_ms,
                decision_basis_json, warm_pool
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, 0,
                      ?, ?, ?, ?, 0.0, ?, 0)
            """,
            (
                instance_id,
                function.name,
                STATE_INITIALIZING,
                canonical_json(bound_versions),
                generation,
                int(call["occupancy"]),
                function.max_concurrency,
                now,
                now,
                ready,
                config_version,
                snapshot_version,
                cache_key,
                duration,
                canonical_json(decision),
            ),
        )
        lease_id = "lease:" + call["call_key"]
        connection.execute(
            """
            INSERT INTO concurrency_leases(
                lease_id, call_key, function_name, occupancy, acquired_clock_ms,
                expires_clock_ms, released, release_clock_ms
            ) VALUES (?, ?, ?, ?, ?, ?, 0, NULL)
            """,
            (
                lease_id,
                call["call_key"],
                function.name,
                int(call["occupancy"]),
                now,
                ready,
            ),
        )
        connection.execute(
            """
            UPDATE calls
            SET phase = 'INITIALIZING', start_mode = 'COLD_START', cache_hit = ?,
                instance_id = ?, init_started_clock_ms = ?, finish_clock_ms = ?,
                decision_json = ?, outcome = NULL, reason_code = NULL
            WHERE call_key = ?
            """,
            (
                1 if cache_hit else 0,
                instance_id,
                now,
                ready,
                canonical_json(decision),
                call["call_key"],
            ),
        )
        Store.touch_pointer(
            connection,
            instance_seq=instance_seq,
            warm_generation_seq=generation,
        )
        self._event(
            connection,
            instance_id,
            call["call_key"],
            now,
            "INIT_STARTED",
            STATE_UNINITIALIZED,
            STATE_INITIALIZING,
            "cold start began under a reserved concurrency lease",
            decision,
        )

    def _abandon_pending(
        self,
        connection: sqlite3.Connection,
        call: sqlite3.Row,
        now: int,
        code: str,
        reason: str,
    ) -> None:
        result = self._call_result(
            call,
            outcome="BUDGET_NOT_STARTED",
            reason_code=code,
            reason=reason,
            completed_clock_ms=now,
        )
        connection.execute(
            """
            UPDATE calls
            SET phase = 'DONE', outcome = 'BUDGET_NOT_STARTED',
                reason_code = ?, reason = ?, completed_clock_ms = ?,
                result_json = ?
            WHERE call_key = ?
            """,
            (code, reason, now, canonical_json(result), call["call_key"]),
        )

    def _fail_pre_start(
        self,
        connection: sqlite3.Connection,
        call: sqlite3.Row,
        now: int,
        code: str,
        reason: str,
    ) -> None:
        result = self._call_result(
            call,
            outcome="FAILED_PRE_START",
            reason_code=code,
            reason=reason,
            completed_clock_ms=now,
        )
        connection.execute(
            """
            UPDATE calls
            SET phase = 'DONE', outcome = 'FAILED_PRE_START',
                reason_code = ?, reason = ?, completed_clock_ms = ?,
                result_json = ?
            WHERE call_key = ?
            """,
            (code, reason, now, canonical_json(result), call["call_key"]),
        )

    def _call_result(
        self,
        call: sqlite3.Row,
        *,
        outcome: str,
        reason_code: str | None,
        reason: str,
        completed_clock_ms: int,
    ) -> dict[str, Any]:
        decision = json.loads(call["decision_json"]) if call["decision_json"] else None
        if decision is not None and decision.get("kind") == "cold_start":
            duration_error = float(decision.get("duration_error_bound_ms", 0.0))
        else:
            duration_error = 0.0
        return {
            "ok": True,
            "call_seq": int(call["call_seq"]),
            "function": call["function_name"],
            "phase": "DONE",
            "outcome": outcome,
            "reason_code": reason_code,
            "reason": reason,
            "instance_id": call["instance_id"],
            "start_mode": call["start_mode"],
            "cache_hit": bool(call["cache_hit"]) if call["cache_hit"] is not None else None,
            "occupancy": int(call["occupancy"]),
            "arrival_clock_ms": int(call["arrival_clock_ms"]),
            "effective_arrival_clock_ms": int(call["effective_arrival_clock_ms"]),
            "init_started_clock_ms": (
                int(call["init_started_clock_ms"])
                if call["init_started_clock_ms"] is not None
                else None
            ),
            "started_clock_ms": (
                int(call["started_clock_ms"])
                if call["started_clock_ms"] is not None
                else None
            ),
            "finish_clock_ms": (
                int(call["finish_clock_ms"])
                if call["finish_clock_ms"] is not None
                else None
            ),
            "completed_clock_ms": completed_clock_ms,
            "config_version": int(call["config_version"]),
            "snapshot_version": int(call["snapshot_version"]),
            "decision": decision,
            "error_bounds": {
                "init_duration_ms": duration_error,
                "concurrency_occupancy": 0.0,
            },
        }

    # ------------------------------------------------------------------
    # layer patches
    # ------------------------------------------------------------------
    def apply_patch(
        self, patch_path: str, *, failpoint: str | None = None
    ) -> dict[str, Any]:
        if not self.store.exists():
            raise bad("E_NOT_LOADED", "engine state does not exist", str(self.state_dir))
        payload = read_json(patch_path)
        with state_file_lock(self.state_dir):
            with self.store.read_snapshot() as connection:
                pointer = Store.pointer(connection)
                root = require_object(payload, "$", "E_BAD_PATCH")
                patch_id = require_string(
                    root.get("patch_id"), "$.patch_id", "E_BAD_PATCH"
                )
                patch_hash = sha256_json(payload)
                existing = connection.execute(
                    "SELECT * FROM patches WHERE patch_id = ?", (patch_id,)
                ).fetchone()
                if existing is not None:
                    if existing["patch_hash"] != patch_hash:
                        raise bad(
                            "E_IDEMPOTENCY_CONFLICT",
                            "patch id was reused with different content",
                            f"patch:{patch_id}",
                        )
                    return json.loads(existing["result_json"])
                function_manifest, layer_manifest, config_payload = Store.load_config(
                    connection, pointer["config_version"]
                )
            prepared = self._prepare_patch(
                payload,
                pointer["config_version"],
                function_manifest,
                layer_manifest,
                config_payload,
            )
            with self.store.transaction() as connection:
                job_id = "apply:" + prepared["patch_id"]
                connection.execute(
                    """
                    INSERT INTO recovery_jobs(
                        job_id, kind, payload_json, state, created_clock_ms,
                        updated_clock_ms, attempts, result_json
                    ) VALUES (?, 'apply_patch', ?, 'RUNNING', ?, ?, 1, NULL)
                    ON CONFLICT(job_id) DO UPDATE SET
                        payload_json = excluded.payload_json,
                        state = 'RUNNING',
                        attempts = recovery_jobs.attempts + 1
                    """,
                    (
                        job_id,
                        canonical_json(payload),
                        int(pointer["logical_clock_ms"]),
                        int(pointer["logical_clock_ms"]),
                    ),
                )
            result = self._apply_prepared(prepared, job_id=job_id, failpoint=failpoint)
            atomic_write_json(
                self.store.artifacts_dir / "last_patch.json",
                result,
            )
            if failpoint == "after-commit":
                os._exit(137)
            return result

    def _prepare_patch(
        self,
        payload: Any,
        current_config_version: int,
        function_manifest: FunctionManifest,
        layer_manifest: LayerManifest,
        config_payload: dict[str, Any],
    ) -> dict[str, Any]:
        root = require_object(payload, "$", "E_BAD_PATCH")
        patch_id = require_string(root.get("patch_id"), "$.patch_id", "E_BAD_PATCH")
        base_version = as_positive_int(
            root.get("base_config_version"),
            "$.base_config_version",
            "E_BAD_PATCH",
        )
        if base_version < current_config_version:
            raise bad(
                "E_VERSION_ROLLBACK",
                "patch base_config_version is older than the committed config",
                "$.base_config_version",
            )
        if base_version > current_config_version:
            raise bad(
                "E_PATCH_BASE_UNKNOWN",
                "patch base_config_version is newer than the committed config",
                "$.base_config_version",
            )
        clock_ms = as_nonnegative_int(
            root.get("declared_clock_ms"), "$.declared_clock_ms", "E_BAD_PATCH"
        )
        operations = require_list(root.get("operations"), "$.operations", "E_BAD_PATCH")
        if not operations:
            raise bad("E_BAD_PATCH", "patch must contain at least one operation", "$.operations")
        old_layers = {
            name: {
                "name": layer.name,
                "digest": layer.digest,
                "version": layer.version,
                "default_init_ms": layer.default_init_ms,
                "tombstone": layer.tombstone,
                "env": dict(layer.env),
                "depends_on": list(layer.depends_on),
                "referenced_by": list(layer.referenced_by),
            }
            for name, layer in layer_manifest.layers.items()
        }
        new_layers = {name: dict(value) for name, value in old_layers.items()}
        changed: set[str] = set()
        trigger_sources: list[dict[str, Any]] = []
        seen_ops: set[str] = set()
        for index, raw_op in enumerate(operations):
            path = f"$.operations[{index}]"
            op_obj = require_object(raw_op, path, "E_BAD_PATCH")
            op = require_string(op_obj.get("op"), f"{path}.op", "E_BAD_PATCH")
            if op not in {"add", "modify", "delete", "tombstone"}:
                raise bad(
                    "E_BAD_PATCH",
                    "op must be one of add, modify, delete, tombstone",
                    f"{path}.op",
                )
            name = require_string(op_obj.get("layer"), f"{path}.layer", "E_BAD_PATCH")
            if name in seen_ops:
                raise bad(
                    "E_BAD_PATCH",
                    f"layer appears more than once in the same patch: {name}",
                    f"{path}.layer",
                )
            seen_ops.add(name)
            existing = new_layers.get(name)
            if op == "add":
                if existing is not None:
                    raise bad(
                        "E_BAD_PATCH",
                        f"cannot add existing layer: {name}",
                        f"{path}.layer",
                    )
                existing = {
                    "name": name,
                    "digest": None,
                    "version": 0,
                    "default_init_ms": 0,
                    "tombstone": False,
                    "env": {},
                    "depends_on": [],
                    "referenced_by": [],
                }
            elif existing is None:
                raise bad(
                    "E_BAD_PATCH",
                    f"cannot {op} missing layer: {name}",
                    f"{path}.layer",
                )
            version = as_positive_int(
                op_obj.get("version"), f"{path}.version", "E_BAD_PATCH"
            )
            if version <= int(existing["version"]):
                raise bad(
                    "E_BAD_PATCH",
                    f"layer version must increase monotonically: {name} "
                    f"{existing['version']} -> {version}",
                    f"{path}.version",
                )
            digest = op_obj.get("digest", existing["digest"])
            digest = require_string(digest, f"{path}.digest", "E_BAD_PATCH")
            if not digest.startswith("sha256:"):
                raise bad(
                    "E_BAD_PATCH",
                    "digest must start with sha256:",
                    f"{path}.digest",
                )
            default_init_ms = as_nonnegative_int(
                op_obj.get("default_init_ms", existing["default_init_ms"]),
                f"{path}.default_init_ms",
                "E_BAD_PATCH",
            )
            env_raw = require_object(op_obj.get("env", existing["env"]), f"{path}.env", "E_BAD_PATCH")
            env: dict[str, str] = {}
            for key in sorted(env_raw):
                if not isinstance(env_raw[key], str):
                    raise bad(
                        "E_BAD_PATCH",
                        "environment variable values must be strings",
                        f"{path}.env.{key}",
                    )
                env[key] = env_raw[key]
            depends_raw = require_list(
                op_obj.get("depends_on", existing["depends_on"]),
                f"{path}.depends_on",
                "E_BAD_PATCH",
            )
            depends = []
            for dep_index, dependency in enumerate(depends_raw):
                dep_name = require_string(
                    dependency, f"{path}.depends_on[{dep_index}]", "E_BAD_PATCH"
                )
                if dep_name not in depends:
                    depends.append(dep_name)
            tombstone_raw = op_obj.get("tombstone", existing["tombstone"])
            if not isinstance(tombstone_raw, bool):
                raise bad(
                    "E_BAD_PATCH", "tombstone must be boolean", f"{path}.tombstone"
                )
            tombstone = True if op in {"delete", "tombstone"} else tombstone_raw
            new_layers[name] = {
                "name": name,
                "digest": digest,
                "version": version,
                "default_init_ms": default_init_ms,
                "tombstone": tombstone,
                "env": env,
                "depends_on": sorted(depends),
                "referenced_by": existing["referenced_by"],
            }
            changed.add(name)
            trigger_sources.append(
                {
                    "operation": op,
                    "layer": name,
                    "old_version": int(existing["version"]),
                    "new_version": version,
                    "old_digest": existing["digest"],
                    "new_digest": digest,
                    "tombstone": tombstone,
                }
            )

        new_layer_payload = {"layers": [new_layers[name] for name in sorted(new_layers)]}
        new_layer_manifest = parse_layer_manifest(new_layer_payload)
        new_function_payload = {
            "global_concurrency_budget": function_manifest.global_concurrency_budget,
            "functions": config_payload["functions"],
        }
        new_function_manifest = parse_function_manifest(
            new_function_payload, new_layer_manifest
        )
        new_config_payload = manifest_payload(
            new_function_manifest, new_layer_manifest
        )

        old_closure = layer_closure(changed, layer_manifest.layers)
        new_closure = layer_closure(changed, new_layer_manifest.layers)
        affected = sorted(
            set(
                affected_functions(old_closure, function_manifest.functions, layer_manifest.layers)
            )
            | set(
                affected_functions(
                    new_closure,
                    new_function_manifest.functions,
                    new_layer_manifest.layers,
                )
            )
        )
        return {
            "payload": payload,
            "patch_id": patch_id,
            "patch_hash": sha256_json(payload),
            "base_config_version": base_version,
            "clock_ms": clock_ms,
            "changed_layers": sorted(changed),
            "closure_layers": sorted(old_closure | new_closure),
            "trigger_sources": trigger_sources,
            "affected_functions": affected,
            "function_manifest": new_function_manifest,
            "layer_manifest": new_layer_manifest,
            "config_payload": new_config_payload,
        }

    def _apply_prepared(
        self,
        prepared: dict[str, Any],
        *,
        job_id: str,
        failpoint: str | None,
    ) -> dict[str, Any]:
        with self.store.transaction() as connection:
            pointer = Store.pointer(connection)
            existing_patch = connection.execute(
                "SELECT * FROM patches WHERE patch_id = ?",
                (prepared["patch_id"],),
            ).fetchone()
            if existing_patch is not None:
                if existing_patch["patch_hash"] != prepared["patch_hash"]:
                    raise bad(
                        "E_IDEMPOTENCY_CONFLICT",
                        "patch id was reused with different content",
                        f"patch:{prepared['patch_id']}",
                    )
                self._mark_job_done(
                    connection,
                    job_id,
                    Store.pointer(connection)["logical_clock_ms"],
                    json.loads(existing_patch["result_json"]),
                )
                return json.loads(existing_patch["result_json"])
            if int(pointer["config_version"]) != int(prepared["base_config_version"]):
                raise bad(
                    "E_PATCH_BASE_STALE",
                    "committed config changed before the patch could be applied",
                    "$.base_config_version",
                )
            target_clock = max(
                int(pointer["logical_clock_ms"]), int(prepared["clock_ms"])
            )
            self._tick(connection, target_clock)
            pointer = Store.pointer(connection)
            config_version = int(pointer["config_version"]) + 1
            patch_version = int(pointer["patch_version"]) + 1
            trigger_json = canonical_json(prepared["trigger_sources"])
            affected_instances: list[str] = []
            affected_cache: list[str] = []
            first_instance: str | None = None
            if prepared["affected_functions"]:
                placeholders = ",".join("?" for _ in prepared["affected_functions"])
                instance_rows = connection.execute(
                    f"""
                    SELECT * FROM instances
                    WHERE function_name IN ({placeholders})
                    ORDER BY instance_id
                    """,
                    tuple(prepared["affected_functions"]),
                ).fetchall()
                for instance in instance_rows:
                    affected_instances.append(instance["instance_id"])
                    if first_instance is None:
                        first_instance = instance["instance_id"]
                    if instance["state"] == STATE_INITIALIZING:
                        connection.execute(
                            """
                            UPDATE instances
                            SET pending_invalidation = 1, invalidation_source = ?
                            WHERE instance_id = ?
                            """,
                            (f"patch:{prepared['patch_id']}", instance["instance_id"]),
                        )
                        self._event(
                            connection,
                            instance["instance_id"],
                            None,
                            target_clock,
                            "INVALIDATION_PENDING",
                            STATE_INITIALIZING,
                            STATE_INITIALIZING,
                            "layer patch arrived during initialization; active work may finish",
                            {
                                "patch_id": prepared["patch_id"],
                                "trigger_sources": prepared["trigger_sources"],
                            },
                        )
                    elif instance["state"] != STATE_INVALIDATED:
                        connection.execute(
                            """
                            UPDATE instances
                            SET state = ?, invalidated_clock_ms = ?,
                                invalidation_source = ?, pending_invalidation = 0
                            WHERE instance_id = ?
                            """,
                            (
                                STATE_INVALIDATED,
                                target_clock,
                                f"patch:{prepared['patch_id']}",
                                instance["instance_id"],
                            ),
                        )
                        self._event(
                            connection,
                            instance["instance_id"],
                            None,
                            target_clock,
                            "INVALIDATED",
                            instance["state"],
                            STATE_INVALIDATED,
                            "layer dependency changed",
                            {
                                "patch_id": prepared["patch_id"],
                                "trigger_sources": prepared["trigger_sources"],
                            },
                        )
                cache_rows = connection.execute(
                    f"""
                    SELECT cache_key FROM init_cache
                    WHERE function_name IN ({placeholders}) AND cache_state = 'VALID'
                    ORDER BY cache_key
                    """,
                    tuple(prepared["affected_functions"]),
                ).fetchall()
                for row in cache_rows:
                    affected_cache.append(row["cache_key"])
                    connection.execute(
                        """
                        UPDATE init_cache
                        SET cache_state = 'INVALID', invalidated_clock_ms = ?,
                            invalidation_source = ?
                        WHERE cache_key = ?
                        """,
                        (
                            target_clock,
                            f"patch:{prepared['patch_id']}",
                            row["cache_key"],
                        ),
                    )

            Store.insert_config(
                connection,
                version=config_version,
                function_manifest=prepared["function_manifest"],
                layer_manifest=prepared["layer_manifest"],
                payload=prepared["config_payload"],
                source="patch:" + prepared["patch_id"],
                committed_clock_ms=target_clock,
                revision=int(pointer["revision"]) + 1,
            )
            result = {
                "ok": True,
                "command": "apply",
                "patch_id": prepared["patch_id"],
                "patch_hash": prepared["patch_hash"],
                "base_config_version": prepared["base_config_version"],
                "config_version": config_version,
                "snapshot_version": int(pointer["snapshot_version"]),
                "logical_clock_ms": target_clock,
                "changed_layers": prepared["changed_layers"],
                "dependency_closure_layers": prepared["closure_layers"],
                "trigger_sources": prepared["trigger_sources"],
                "affected_functions": prepared["affected_functions"],
                "affected_instances": affected_instances,
                "affected_init_cache": affected_cache,
                "first_affected_instance": first_instance,
                "unaffected_instances_reused_without_rewrite": True,
            }
            connection.execute(
                """
                INSERT INTO invalidations(
                    patch_id, trigger_source_json, affected_functions_json,
                    affected_instances_json, affected_cache_json,
                    first_instance_id, config_version, clock_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    prepared["patch_id"],
                    trigger_json,
                    canonical_json(prepared["affected_functions"]),
                    canonical_json(affected_instances),
                    canonical_json(affected_cache),
                    first_instance,
                    config_version,
                    target_clock,
                ),
            )
            connection.execute(
                """
                INSERT INTO patches(
                    patch_id, patch_hash, base_config_version,
                    resulting_config_version, applied_clock_ms, result_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    prepared["patch_id"],
                    prepared["patch_hash"],
                    prepared["base_config_version"],
                    config_version,
                    target_clock,
                    canonical_json(result),
                ),
            )
            Store.touch_pointer(
                connection,
                config_version=config_version,
                patch_version=patch_version,
                logical_clock_ms=target_clock,
            )
            self._mark_job_done(connection, job_id, target_clock, result)
            if failpoint == "mid-apply":
                os._exit(137)
            return result

    @staticmethod
    def _mark_job_done(
        connection: sqlite3.Connection,
        job_id: str,
        clock_ms: int,
        result: dict[str, Any],
    ) -> None:
        connection.execute(
            """
            UPDATE recovery_jobs
            SET state = 'DONE', updated_clock_ms = ?, result_json = ?
            WHERE job_id = ?
            """,
            (clock_ms, canonical_json(result), job_id),
        )

    # ------------------------------------------------------------------
    # resume / inspect
    # ------------------------------------------------------------------
    def resume(self) -> dict[str, Any]:
        if not self.store.exists():
            raise bad("E_NOT_LOADED", "engine state does not exist", str(self.state_dir))
        with state_file_lock(self.state_dir):
            self.store.initialize()
            with self.store.read_snapshot() as connection:
                jobs = connection.execute(
                    """
                    SELECT * FROM recovery_jobs
                    WHERE state IN ('PENDING', 'RUNNING')
                    ORDER BY job_id
                    """
                ).fetchall()
            recovered_jobs: list[dict[str, Any]] = []
            drained_calls: list[dict[str, Any]] = []
            for job in jobs:
                if job["kind"] == "apply_patch":
                    payload = json.loads(job["payload_json"])
                    with self.store.read_snapshot() as connection:
                        pointer = Store.pointer(connection)
                        function_manifest, layer_manifest, config_payload = Store.load_config(
                            connection, pointer["config_version"]
                        )
                    prepared = self._prepare_patch(
                        payload,
                        int(pointer["config_version"]),
                        function_manifest,
                        layer_manifest,
                        config_payload,
                    )
                    result = self._apply_prepared(
                        prepared, job_id=job["job_id"], failpoint=None
                    )
                    recovered_jobs.append(
                        {
                            "job_id": job["job_id"],
                            "kind": job["kind"],
                            "action": "reapplied_patch",
                            "config_version": result["config_version"],
                        }
                    )
                elif job["kind"] == "invoke":
                    outcomes = self._drain_all_calls()
                    drained_calls.extend(outcomes)
                    with self.store.transaction() as connection:
                        self._mark_job_done(
                            connection,
                            job["job_id"],
                            Store.pointer(connection)["logical_clock_ms"],
                            {"outcomes": outcomes},
                        )
                    recovered_jobs.append(
                        {
                            "job_id": job["job_id"],
                            "kind": job["kind"],
                            "action": "drained_calls",
                            "calls": len(outcomes),
                        }
                    )
                else:
                    with self.store.transaction() as connection:
                        self._mark_job_done(
                            connection,
                            job["job_id"],
                            Store.pointer(connection)["logical_clock_ms"],
                            {"warning": f"unknown job kind {job['kind']}"},
                        )
                    recovered_jobs.append(
                        {
                            "job_id": job["job_id"],
                            "kind": job["kind"],
                            "action": "ignored_unknown_job",
                        }
                    )

            outcomes = self._drain_all_calls()
            drained_calls.extend(outcomes)
            cleaned = self._clean_half_writes()
            with self.store.read_snapshot() as connection:
                pointer = Store.pointer(connection)
                unresolved = connection.execute(
                    "SELECT COUNT(*) AS n FROM calls WHERE phase != 'DONE'"
                ).fetchone()["n"]
                return {
                    "ok": True,
                    "command": "resume",
                    "recovered_jobs": recovered_jobs,
                    "drained_calls": drained_calls,
                    "cleaned": cleaned,
                    "unresolved_calls": unresolved,
                    "config_version": pointer["config_version"],
                    "snapshot_version": pointer["snapshot_version"],
                    "logical_clock_ms": pointer["logical_clock_ms"],
                }

    def _drain_all_calls(self) -> list[dict[str, Any]]:
        with self.store.read_snapshot() as connection:
            target_keys = [
                row["call_key"]
                for row in connection.execute(
                    "SELECT call_key FROM calls WHERE phase != 'DONE' ORDER BY call_seq, call_key"
                )
            ]
        if not target_keys:
            return []
        for _ in range(10000):
            with self.store.transaction() as connection:
                unresolved = connection.execute(
                    "SELECT call_key FROM calls WHERE phase != 'DONE' ORDER BY call_seq, call_key"
                ).fetchall()
                if not unresolved:
                    return self._results_for_keys(connection, target_keys)
                current = int(Store.pointer(connection)["logical_clock_ms"])
                self._dispatch_pending(connection, current)
                unresolved = connection.execute(
                    "SELECT call_key FROM calls WHERE phase != 'DONE' ORDER BY call_seq, call_key"
                ).fetchall()
                if not unresolved:
                    return self._results_for_keys(connection, target_keys)
                due = self._due_times(connection)
                if not due:
                    raise bad(
                        "E_DEADLOCK",
                        "unresolved calls have no reachable deterministic event",
                    )
                next_clock = max(current, min(due))
                self._tick(connection, next_clock)
        raise bad("E_EVENT_LIMIT", "recovery event loop exceeded its safety limit")

    def _clean_half_writes(self) -> dict[str, int]:
        removed_temp_files = 0
        if self.store.artifacts_dir.exists():
            for path in self.store.artifacts_dir.iterdir():
                if path.is_file() and path.name.endswith(".tmp"):
                    try:
                        path.unlink()
                        removed_temp_files += 1
                    except OSError:
                        pass
        with self.store.transaction() as connection:
            orphan_functions = connection.execute(
                """
                DELETE FROM functions
                WHERE config_version NOT IN (SELECT version FROM config_commits)
                """
            ).rowcount
            orphan_layers = connection.execute(
                """
                DELETE FROM layers
                WHERE config_version NOT IN (SELECT version FROM config_commits)
                """
            ).rowcount
        return {
            "temp_files_removed": removed_temp_files,
            "orphan_function_rows_removed": orphan_functions,
            "orphan_layer_rows_removed": orphan_layers,
        }

    def status(self) -> dict[str, Any]:
        if not self.store.exists():
            raise bad("E_NOT_LOADED", "engine state does not exist", str(self.state_dir))
        with self.store.read_snapshot() as connection:
            pointer = Store.pointer(connection)
            function_manifest, layer_manifest, _ = Store.load_config(
                connection, pointer["config_version"]
            )
            instances = connection.execute(
                "SELECT * FROM instances ORDER BY instance_id"
            ).fetchall()
            active_global = connection.execute(
                "SELECT COALESCE(SUM(occupancy), 0) AS n FROM concurrency_leases WHERE released = 0"
            ).fetchone()["n"]
            instance_output = []
            for instance in instances:
                basis_json = instance["decision_basis_json"]
                instance_output.append(
                    {
                        "instance_id": instance["instance_id"],
                        "function": instance["function_name"],
                        "stage": instance["state"],
                        "bound_layer_versions": json.loads(instance["bound_versions_json"]),
                        "warm_generation": instance["warm_generation"],
                        "active_occupancy": instance["active_occupancy"],
                        "capacity": instance["capacity"],
                        "pending_invalidation": bool(instance["pending_invalidation"]),
                        "config_version": instance["config_version"],
                        "snapshot_version": instance["snapshot_version"],
                        "ready_clock_ms": instance["ready_clock_ms"],
                        "expires_clock_ms": instance["expires_clock_ms"],
                        "invalidated_clock_ms": instance["invalidated_clock_ms"],
                        "decision_basis_sha256": "sha256:"
                        + hashlib.sha256(basis_json.encode("utf-8")).hexdigest(),
                    }
                )
            call_stats = {
                row["outcome"] or row["phase"]: row["n"]
                for row in connection.execute(
                    """
                    SELECT COALESCE(outcome, phase) AS outcome, COUNT(*) AS n
                    FROM calls GROUP BY COALESCE(outcome, phase)
                    ORDER BY 1
                    """
                )
            }
            function_output = []
            for name in sorted(function_manifest.functions):
                function = function_manifest.functions[name]
                function_output.append(
                    {
                        "name": name,
                        "max_concurrency": function.max_concurrency,
                        "warm_pool_target": function.warm_pool_target,
                        "effective_layer_versions": {
                            layer_name: layer_manifest.layers[layer_name].version
                            for layer_name in effective_layers(
                                function, layer_manifest.layers
                            )
                        },
                    }
                )
            return {
                "ok": True,
                "command": "status",
                "config_version": pointer["config_version"],
                "snapshot_version": pointer["snapshot_version"],
                "logical_clock_ms": pointer["logical_clock_ms"],
                "call_watermark": pointer["call_watermark"],
                "global_concurrency": {
                    "budget": function_manifest.global_concurrency_budget,
                    "active": int(active_global),
                },
                "functions": function_output,
                "instances": instance_output,
                "call_stats": call_stats,
            }

    def verify(self) -> dict[str, Any]:
        if not self.store.exists():
            raise bad("E_NOT_LOADED", "engine state does not exist", str(self.state_dir))
        checks: list[dict[str, Any]] = []

        def add(name: str, ok: bool, details: Any) -> None:
            checks.append({"name": name, "ok": bool(ok), "details": details})

        with self.store.read_snapshot() as connection:
            pointer = Store.pointer(connection)
            config_row = connection.execute(
                "SELECT * FROM config_commits WHERE version = ?",
                (pointer["config_version"],),
            ).fetchone()
            snapshot_row = connection.execute(
                "SELECT * FROM snapshot_commits WHERE version = ?",
                (pointer["snapshot_version"],),
            ).fetchone()
            add("config_pointer_exists", config_row is not None, pointer["config_version"])
            add(
                "snapshot_pointer_exists",
                snapshot_row is not None,
                pointer["snapshot_version"],
            )
            function_manifest, layer_manifest, _ = Store.load_config(
                connection, pointer["config_version"]
            )
            add(
                "config_closed_and_acyclic",
                True,
                {
                    "functions": len(function_manifest.functions),
                    "layers": len(layer_manifest.layers),
                },
            )
            invalid_states = [
                row["instance_id"]
                for row in connection.execute("SELECT instance_id, state FROM instances")
                if row["state"] not in ALL_STATES
            ]
            add("instance_states_known", not invalid_states, invalid_states)
            over_capacity = [
                row["instance_id"]
                for row in connection.execute(
                    "SELECT instance_id, active_occupancy, capacity FROM instances"
                )
                if row["active_occupancy"] < 0
                or row["active_occupancy"] > row["capacity"]
            ]
            add("instance_capacity_respected", not over_capacity, over_capacity)
            global_used = connection.execute(
                "SELECT COALESCE(SUM(occupancy), 0) AS n FROM concurrency_leases WHERE released = 0"
            ).fetchone()["n"]
            add(
                "global_budget_not_oversold",
                int(global_used) <= function_manifest.global_concurrency_budget,
                {
                    "active": int(global_used),
                    "budget": function_manifest.global_concurrency_budget,
                },
            )
            per_function_violations = []
            for row in connection.execute(
                """
                SELECT l.function_name, SUM(l.occupancy) AS used,
                       f.max_concurrency
                FROM concurrency_leases l
                JOIN functions f
                  ON f.config_version = ? AND f.name = l.function_name
                WHERE l.released = 0
                GROUP BY l.function_name
                """,
                (pointer["config_version"],),
            ):
                if int(row["used"]) > int(row["max_concurrency"]):
                    per_function_violations.append(
                        {
                            "function": row["function_name"],
                            "used": int(row["used"]),
                            "limit": int(row["max_concurrency"]),
                        }
                    )
            add(
                "function_concurrency_not_oversold",
                not per_function_violations,
                per_function_violations,
            )
            instance_active = connection.execute(
                "SELECT COALESCE(SUM(active_occupancy), 0) AS n FROM instances"
            ).fetchone()["n"]
            add(
                "lease_and_instance_occupancy_match",
                int(global_used) == int(instance_active),
                {"leases": int(global_used), "instances": int(instance_active)},
            )
            invalid_bound = []
            for row in connection.execute("SELECT * FROM instances"):
                if row["state"] == STATE_WARMED or (
                    row["state"] == STATE_INITIALIZING
                    and not row["pending_invalidation"]
                ):
                    function = function_manifest.functions[row["function_name"]]
                    expected = {
                        name: layer_manifest.layers[name].version
                        for name in effective_layers(function, layer_manifest.layers)
                    }
                    if json.loads(row["bound_versions_json"]) != expected:
                        invalid_bound.append(row["instance_id"])
            add("active_instances_bind_current_versions", not invalid_bound, invalid_bound)
            unresolved = connection.execute(
                "SELECT COUNT(*) AS n FROM calls WHERE phase != 'DONE'"
            ).fetchone()["n"]
            add("no_unresolved_calls", unresolved == 0, int(unresolved))
            wrong_watermark = connection.execute(
                "SELECT COALESCE(MAX(call_seq), 0) AS n FROM calls"
            ).fetchone()["n"]
            add(
                "call_watermark_matches_ledger",
                int(pointer["call_watermark"]) == int(wrong_watermark),
                {
                    "pointer": int(pointer["call_watermark"]),
                    "ledger_max": int(wrong_watermark),
                },
            )
            migration_issues = []
            for row in connection.execute(
                "SELECT detail_json FROM snapshot_migrations WHERE snapshot_version = ?",
                (pointer["snapshot_version"],),
            ):
                detail = json.loads(row["detail_json"])
                if not detail.get("logical_warmed_preserved") or detail.get(
                    "warm_hit_before"
                ) != detail.get("warm_hit_after"):
                    migration_issues.append(detail)
            add("snapshot_migration_preserves_warm_state", not migration_issues, migration_issues)
            no_chain = connection.execute(
                "SELECT COUNT(*) AS n FROM precision_notes WHERE source_chain_json = ''"
            ).fetchone()["n"]
            add("precision_notes_have_source_chains", int(no_chain) == 0, int(no_chain))
            orphan_versions = connection.execute(
                """
                SELECT COUNT(*) AS n FROM functions
                WHERE config_version NOT IN (SELECT version FROM config_commits)
                """
            ).fetchone()["n"]
            add("no_half_written_config_rows", int(orphan_versions) == 0, int(orphan_versions))
            invalidation_issues = []
            for row in connection.execute(
                "SELECT * FROM invalidations ORDER BY id"
            ):
                functions = json.loads(row["affected_functions_json"])
                instances = json.loads(row["affected_instances_json"])
                if functions != sorted(functions) or instances != sorted(instances):
                    invalidation_issues.append(row["id"])
                if row["first_instance_id"] is not None and (
                    not instances or instances[0] != row["first_instance_id"]
                ):
                    invalidation_issues.append(row["id"])
            add(
                "invalidation_closure_is_stably_sorted",
                not invalidation_issues,
                invalidation_issues,
            )
        return {
            "ok": all(check["ok"] for check in checks),
            "command": "verify",
            "checks": checks,
        }
