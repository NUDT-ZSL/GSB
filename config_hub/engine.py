"""Transactional configuration hot-update and deterministic compute engine."""

from __future__ import annotations

import copy
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .canonical import (
    CanonicalJSONError,
    atomic_write_text,
    canonical_json,
    stable_json_text,
    strict_json_loads,
)
from .errors import (
    ConfigHubError,
    bad_clock,
    bad_config,
    bad_input,
    bad_patch,
    clock_rollback,
    incomplete_work,
    not_found,
    patch_not_effective,
    result_conflict,
    version_conflict,
)
from .failpoints import failpoint
from .pipeline import (
    compute_pipeline,
    result_payload_digest,
    validate_pipeline_config,
)
from .storage import Store
from .validation import (
    input_digest,
    load_patch,
    load_snapshot,
    normalize_input,
    normalize_patch,
    normalize_snapshot,
    validate_client_id,
)


@dataclass(frozen=True)
class ComputeBinding:
    client_id: str
    round_no: int
    config_version: int
    config_digest: str
    input_json: str
    input_digest: str
    started_clock_ms: int
    snapshot: dict[str, Any]
    new_run: bool


def _require_clock(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise bad_clock("logical clock must be a non-negative integer millisecond value")
    return value


def _require_round(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise bad_input("round must be an integer >= 1")
    return value


def _clock_tick(
    now_ms: int,
    watermark_ms: int,
    tolerance_ms: int,
) -> int:
    if now_ms < watermark_ms - tolerance_ms:
        raise clock_rollback(
            "logical clock moved backwards beyond the configured tolerance",
            {
                "now_ms": now_ms,
                "watermark_ms": watermark_ms,
                "tolerance_ms": tolerance_ms,
            },
        )
    return max(now_ms, watermark_ms)


class ConfigHub:
    def __init__(self, db_path: str | Path) -> None:
        self.store = Store(db_path)

    # ------------------------------------------------------------------
    # Initialization and reads
    # ------------------------------------------------------------------
    def initialize(self, snapshot_path: str | Path) -> dict[str, Any]:
        snapshot = load_snapshot(snapshot_path)
        validate_pipeline_config(snapshot)
        snapshot_json = canonical_json(snapshot)
        with self.store.write_lock():
            with self.store.transaction(immediate=True) as connection:
                state = self.store.get_state(connection)
                if state is not None:
                    row = self.store.get_version_row(
                        connection, state["current_version"]
                    )
                    return {
                        "ok": True,
                        "created": False,
                        "current_version": state["current_version"],
                        "config_digest": row["config_digest"],
                        "clock_rollback_tolerance_ms": state[
                            "clock_rollback_tolerance_ms"
                        ],
                    }
                if connection.execute(
                    "SELECT COUNT(*) AS count FROM config_versions"
                ).fetchone()["count"]:
                    raise bad_config(
                        "configuration versions exist but current state is missing"
                    )
                connection.execute(
                    """
                    INSERT INTO config_versions(
                        version, parent_version, config_digest,
                        effective_clock_ms, applied_clock_ms, patch_id, snapshot_json
                    ) VALUES (?, ?, ?, ?, ?, NULL, ?)
                    """,
                    (
                        snapshot["version"],
                        None,
                        snapshot["content_digest"],
                        snapshot["logical_clock_ms"],
                        snapshot["logical_clock_ms"],
                        snapshot_json,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO state(
                        id, current_version, logical_watermark_ms,
                        clock_rollback_tolerance_ms
                    ) VALUES (1, ?, ?, ?)
                    """,
                    (
                        snapshot["version"],
                        snapshot["logical_clock_ms"],
                        snapshot["clock_rollback_tolerance_ms"],
                    ),
                )
        return {
            "ok": True,
            "created": True,
            "current_version": snapshot["version"],
            "config_digest": snapshot["content_digest"],
            "clock_rollback_tolerance_ms": snapshot[
                "clock_rollback_tolerance_ms"
            ],
        }

    def read_config(self, version: int | None = None) -> dict[str, Any]:
        with self.store.transaction() as connection:
            if version is None:
                _state, snapshot = self.store.current_snapshot(connection)
                return snapshot
            snapshot = self.store.version_snapshot(connection, version)
            if snapshot is None:
                raise not_found(f"configuration version {version} does not exist")
            return snapshot

    def status(self) -> dict[str, Any]:
        with self.store.transaction() as connection:
            state = self.store.get_state(connection)
            if state is None:
                raise not_found("configuration store is not initialized")
            current_row = self.store.get_version_row(
                connection, state["current_version"]
            )
            current = self.store.parse_snapshot_row(current_row)
            version_rows = connection.execute(
                "SELECT * FROM config_versions ORDER BY version"
            ).fetchall()
            clients = connection.execute(
                """
                SELECT client_id, last_round_no, bound_version, bound_digest,
                       result_digest, updated_clock_ms
                FROM client_state ORDER BY client_id
                """
            ).fetchall()
            patches = connection.execute(
                "SELECT status, COUNT(*) AS count FROM patches GROUP BY status"
            ).fetchall()
            runs = connection.execute(
                """
                SELECT status, COUNT(*) AS count FROM compute_runs
                GROUP BY status
                """
            ).fetchall()
        patch_counts = {"aborted": 0, "applied": 0, "prepared": 0}
        for row in patches:
            patch_counts[row["status"]] = row["count"]
        run_counts = {"completed": 0, "in_progress": 0}
        for row in runs:
            run_counts[row["status"]] = row["count"]
        return {
            "ok": True,
            "current_version": state["current_version"],
            "current_config": {
                "version": current["version"],
                "parent_version": current["parent_version"],
                "content_digest": current["content_digest"],
                "effective_clock_ms": current["logical_clock_ms"],
                "logical_clock_ms": current["logical_clock_ms"],
                "patch_id": current_row["patch_id"],
            },
            "logical_clock_watermark_ms": state["logical_watermark_ms"],
            "clock_rollback_tolerance_ms": state[
                "clock_rollback_tolerance_ms"
            ],
            "version_chain": [
                {
                    "version": row["version"],
                    "parent_version": row["parent_version"],
                    "content_digest": row["config_digest"],
                    "effective_clock_ms": row["effective_clock_ms"],
                    "applied_clock_ms": row["applied_clock_ms"],
                    "patch_id": row["patch_id"],
                }
                for row in version_rows
            ],
            "clients": [
                {
                    "client_id": row["client_id"],
                    "last_round": row["last_round_no"],
                    "bound_version": row["bound_version"],
                    "bound_digest": row["bound_digest"],
                    "result_digest": row["result_digest"],
                    "updated_clock_ms": row["updated_clock_ms"],
                }
                for row in clients
            ],
            "patches": patch_counts,
            "compute_runs": run_counts,
        }

    # ------------------------------------------------------------------
    # Patch preparation and atomic application
    # ------------------------------------------------------------------
    def _candidate_snapshot(
        self,
        current: dict[str, Any],
        patch: dict[str, Any],
        requested_clock_ms: int,
    ) -> dict[str, Any]:
        candidate = copy.deepcopy(current)
        candidate.pop("content_digest", None)
        namespace_entry: dict[str, Any] | None = None
        for entry in candidate["namespaces"]:
            if entry["namespace"] == patch["namespace"]:
                namespace_entry = entry
                break
        if namespace_entry is None:
            raise bad_patch(
                f"patch namespace {patch['namespace']!r} does not exist",
                {"namespace": patch["namespace"]},
            )
        configs = {item["key"]: item for item in namespace_entry["configs"]}
        for change in patch["changes"]:
            key = change["key"]
            if change["op"] == "set":
                existing = configs.get(key)
                if existing is not None and existing["type"] != change["type"]:
                    raise bad_patch(
                        f"configuration key {key!r} cannot change declared type",
                        {
                            "key": key,
                            "existing_type": existing["type"],
                            "patch_type": change["type"],
                        },
                    )
                configs[key] = {
                    "key": key,
                    "type": change["type"],
                    "value": change["value"],
                }
            else:
                if key not in configs:
                    raise bad_patch(
                        f"cannot remove unknown configuration key {key!r}",
                        {"namespace": patch["namespace"], "key": key},
                    )
                del configs[key]
        namespace_entry["configs"] = sorted(
            configs.values(), key=lambda item: item["key"]
        )
        candidate["version"] = patch["target_version"]
        candidate["parent_version"] = current["version"]
        candidate["logical_clock_ms"] = requested_clock_ms
        normalized = normalize_snapshot(candidate)
        validate_pipeline_config(normalized)
        return normalized

    @staticmethod
    def _patch_response(row: sqlite3.Row, *, idempotent: bool) -> dict[str, Any]:
        return {
            "ok": True,
            "patch_id": row["patch_id"],
            "version": row["target_version"],
            "previous_version": row["base_version"],
            "config_digest": row["applied_config_digest"],
            "patch_digest": row["content_digest"],
            "effective_clock_ms": row["requested_clock_ms"],
            "effective_from_ms": row["effective_from_ms"],
            "effective_until_ms": row["effective_until_ms"],
            "idempotent": idempotent,
        }

    def apply_patch(self, patch_value: Any, now_ms: int) -> dict[str, Any]:
        now_ms = _require_clock(now_ms)
        patch = normalize_patch(patch_value)
        with self.store.write_lock():
            with self.store.transaction(immediate=True) as connection:
                state = self.store.get_state(connection)
                if state is None:
                    raise not_found("configuration store is not initialized")
                watermark = _clock_tick(
                    now_ms,
                    state["logical_watermark_ms"],
                    state["clock_rollback_tolerance_ms"],
                )
                if watermark != state["logical_watermark_ms"]:
                    connection.execute(
                        "UPDATE state SET logical_watermark_ms = ? WHERE id = 1",
                        (watermark,),
                    )
                existing = connection.execute(
                    "SELECT * FROM patches WHERE patch_id = ?", (patch["patch_id"],)
                ).fetchone()
                if existing is not None:
                    if (
                        existing["content_digest"] != patch["content_digest"]
                        or existing["target_version"] != patch["target_version"]
                        or existing["base_version"] != patch["base_version"]
                    ):
                        raise version_conflict(
                            "patch_id was already used for different patch content",
                            {"patch_id": patch["patch_id"]},
                        )
                    if existing["status"] == "applied":
                        return self._patch_response(existing, idempotent=True)
                    if existing["status"] == "aborted":
                        raise version_conflict(
                            "patch attempt was previously aborted",
                            {
                                "patch_id": patch["patch_id"],
                                "error_code": existing["error_code"],
                            },
                        )
                    failpoint("after_patch_prepare")
                else:
                    active_target = connection.execute(
                        """
                        SELECT * FROM patches
                        WHERE target_version = ?
                          AND status IN ('prepared', 'applied')
                        """,
                        (patch["target_version"],),
                    ).fetchone()
                    if (
                        active_target is not None
                        and active_target["patch_id"] != patch["patch_id"]
                    ):
                        raise version_conflict(
                            "target version is already claimed by another patch",
                            {
                                "target_version": patch["target_version"],
                                "existing_patch_id": active_target["patch_id"],
                            },
                        )
                    target_row = self.store.get_version_row(
                        connection, patch["target_version"]
                    )
                    if target_row is not None:
                        raise version_conflict(
                            "target configuration version already exists",
                            {"target_version": patch["target_version"]},
                        )
                    _state_row, current = self.store.current_snapshot(connection)
                    if (
                        patch["base_version"] != current["version"]
                        or patch["target_version"] != current["version"] + 1
                    ):
                        raise version_conflict(
                            "patch is not based on the current configuration version",
                            {
                                "current_version": current["version"],
                                "patch_base_version": patch["base_version"],
                                "patch_target_version": patch["target_version"],
                            },
                        )
                    if not (
                        patch["effective_from_ms"]
                        <= now_ms
                        <= patch["effective_until_ms"]
                    ):
                        raise patch_not_effective(
                            "logical clock is outside the patch effective window",
                            {
                                "now_ms": now_ms,
                                "effective_from_ms": patch["effective_from_ms"],
                                "effective_until_ms": patch["effective_until_ms"],
                            },
                        )
                    self._candidate_snapshot(current, patch, now_ms)
                    connection.execute(
                        """
                        INSERT INTO patches(
                            patch_id, target_version, base_version, content_digest,
                            effective_from_ms, effective_until_ms,
                            requested_clock_ms, status, patch_json
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, 'prepared', ?)
                        """,
                        (
                            patch["patch_id"],
                            patch["target_version"],
                            patch["base_version"],
                            patch["content_digest"],
                            patch["effective_from_ms"],
                            patch["effective_until_ms"],
                            now_ms,
                            canonical_json(patch),
                        ),
                    )
            failpoint("after_patch_prepare")
            return self._finalize_patch(patch["patch_id"], idempotent=False)

    def _finalize_patch(
        self, patch_id: str, *, idempotent: bool
    ) -> dict[str, Any]:
        abort_error: ConfigHubError | None = None
        response: dict[str, Any] | None = None
        with self.store.transaction(immediate=True) as connection:
            state = self.store.get_state(connection)
            if state is None:
                raise not_found("configuration store is not initialized")
            row = connection.execute(
                "SELECT * FROM patches WHERE patch_id = ?", (patch_id,)
            ).fetchone()
            if row is None:
                raise not_found(f"patch {patch_id!r} does not exist")
            if row["status"] == "applied":
                response = self._patch_response(row, idempotent=True)
            elif row["status"] == "aborted":
                raise version_conflict(
                    "patch attempt was previously aborted",
                    {"patch_id": patch_id, "error_code": row["error_code"]},
                )
            else:
                _state_row, current = self.store.current_snapshot(connection)
                if (
                    row["base_version"] != current["version"]
                    or row["target_version"] != current["version"] + 1
                ):
                    abort_error = version_conflict(
                        "patch became stale before its atomic commit",
                        {
                            "patch_id": patch_id,
                            "current_version": current["version"],
                            "patch_base_version": row["base_version"],
                        },
                    )
                else:
                    patch = strict_json_loads(row["patch_json"])
                    try:
                        candidate = self._candidate_snapshot(
                            current, patch, row["requested_clock_ms"]
                        )
                    except ConfigHubError as exc:
                        abort_error = exc
                    else:
                        connection.execute(
                            """
                            INSERT INTO config_versions(
                                version, parent_version, config_digest,
                                effective_clock_ms, applied_clock_ms,
                                patch_id, snapshot_json
                            ) VALUES (?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                candidate["version"],
                                candidate["parent_version"],
                                candidate["content_digest"],
                                candidate["logical_clock_ms"],
                                row["requested_clock_ms"],
                                patch_id,
                                canonical_json(candidate),
                            ),
                        )
                        connection.execute(
                            """
                            UPDATE patches
                            SET status = 'applied', applied_version = ?,
                                applied_config_digest = ?, error_code = NULL
                            WHERE patch_id = ?
                            """,
                            (
                                candidate["version"],
                                candidate["content_digest"],
                                patch_id,
                            ),
                        )
                        connection.execute(
                            """
                            UPDATE state
                            SET current_version = ?,
                                logical_watermark_ms = ?,
                                clock_rollback_tolerance_ms = ?
                            WHERE id = 1
                            """,
                            (
                                candidate["version"],
                                max(
                                    state["logical_watermark_ms"],
                                    row["requested_clock_ms"],
                                ),
                                candidate["clock_rollback_tolerance_ms"],
                            ),
                        )
                        failpoint("before_patch_commit")
                        updated_row = connection.execute(
                            "SELECT * FROM patches WHERE patch_id = ?", (patch_id,)
                        ).fetchone()
                        response = self._patch_response(
                            updated_row, idempotent=idempotent
                        )
                if abort_error is not None:
                    connection.execute(
                        """
                        UPDATE patches
                        SET status = 'aborted', error_code = ?
                        WHERE patch_id = ?
                        """,
                        (abort_error.code, patch_id),
                    )
        if abort_error is not None:
            raise abort_error
        if response is None:
            raise RuntimeError("patch finalization produced no response")
        return response

    # ------------------------------------------------------------------
    # Deterministic compute lifecycle
    # ------------------------------------------------------------------
    def begin_compute(
        self,
        client_id: str,
        round_no: int,
        now_ms: int,
        normalized_input: dict[str, Any],
    ) -> ComputeBinding:
        client_id = validate_client_id(client_id)
        round_no = _require_round(round_no)
        now_ms = _require_clock(now_ms)
        input_json = canonical_json(normalized_input)
        digest = input_digest(normalized_input)
        with self.store.write_lock():
            with self.store.transaction(immediate=True) as connection:
                state = self.store.get_state(connection)
                if state is None:
                    raise not_found("configuration store is not initialized")
                watermark = _clock_tick(
                    now_ms,
                    state["logical_watermark_ms"],
                    state["clock_rollback_tolerance_ms"],
                )
                connection.execute(
                    "UPDATE state SET logical_watermark_ms = ? WHERE id = 1",
                    (watermark,),
                )
                existing = connection.execute(
                    """
                    SELECT * FROM compute_runs
                    WHERE client_id = ? AND round_no = ?
                    """,
                    (client_id, round_no),
                ).fetchone()
                if existing is not None:
                    if existing["input_digest"] != digest:
                        raise result_conflict(
                            "client round is already bound to different input",
                            {
                                "client_id": client_id,
                                "round": round_no,
                                "existing_input_digest": existing["input_digest"],
                                "new_input_digest": digest,
                            },
                        )
                    snapshot = self.store.version_snapshot(
                        connection, existing["config_version"]
                    )
                    if snapshot is None:
                        raise not_found(
                            "bound configuration version is missing",
                            {"version": existing["config_version"]},
                        )
                    return ComputeBinding(
                        client_id=client_id,
                        round_no=round_no,
                        config_version=existing["config_version"],
                        config_digest=existing["config_digest"],
                        input_json=existing["input_json"],
                        input_digest=existing["input_digest"],
                        started_clock_ms=existing["started_clock_ms"],
                        snapshot=snapshot,
                        new_run=False,
                    )
                _state_row, snapshot = self.store.current_snapshot(connection)
                connection.execute(
                    """
                    INSERT INTO compute_runs(
                        client_id, round_no, status, config_version,
                        config_digest, input_json, input_digest, started_clock_ms
                    ) VALUES (?, ?, 'in_progress', ?, ?, ?, ?, ?)
                    """,
                    (
                        client_id,
                        round_no,
                        snapshot["version"],
                        snapshot["content_digest"],
                        input_json,
                        digest,
                        now_ms,
                    ),
                )
                return ComputeBinding(
                    client_id=client_id,
                    round_no=round_no,
                    config_version=snapshot["version"],
                    config_digest=snapshot["content_digest"],
                    input_json=input_json,
                    input_digest=digest,
                    started_clock_ms=now_ms,
                    snapshot=snapshot,
                    new_run=True,
                )

    def _finish_compute_unlocked(
        self, binding: ComputeBinding, result: dict[str, Any]
    ) -> tuple[dict[str, Any], bool]:
        computed_digest = result_payload_digest(result)
        if result.get("result_digest") != computed_digest:
            raise result_conflict("computed result digest is not self-consistent")
        if result.get("client_id") != binding.client_id:
            raise result_conflict("computed result has the wrong client_id")
        if result.get("round") != binding.round_no:
            raise result_conflict("computed result has the wrong round")
        if (
            result.get("config_version") != binding.config_version
            or result.get("config_digest") != binding.config_digest
        ):
            raise result_conflict("computed result is bound to the wrong config version")
        if result.get("input_digest") != binding.input_digest:
            raise result_conflict("computed result is bound to the wrong input")
        result_json = canonical_json(result)
        with self.store.transaction(immediate=True) as connection:
                run = connection.execute(
                    """
                    SELECT * FROM compute_runs
                    WHERE client_id = ? AND round_no = ?
                    """,
                    (binding.client_id, binding.round_no),
                ).fetchone()
                if run is None:
                    raise not_found("compute run does not exist")
                if (
                    run["config_version"] != binding.config_version
                    or run["config_digest"] != binding.config_digest
                    or run["input_digest"] != binding.input_digest
                ):
                    raise result_conflict("compute run binding changed")
                existing_result = connection.execute(
                    """
                    SELECT * FROM results
                    WHERE client_id = ? AND round_no = ?
                    """,
                    (binding.client_id, binding.round_no),
                ).fetchone()
                if existing_result is not None:
                    if existing_result["result_digest"] != computed_digest:
                        raise result_conflict(
                            "result already committed with different content",
                            {
                                "client_id": binding.client_id,
                                "round": binding.round_no,
                            },
                        )
                    return strict_json_loads(existing_result["result_json"]), True
                connection.execute(
                    """
                    INSERT INTO results(
                        client_id, round_no, config_version, config_digest,
                        input_digest, result_json, result_digest, committed_clock_ms
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        binding.client_id,
                        binding.round_no,
                        binding.config_version,
                        binding.config_digest,
                        binding.input_digest,
                        result_json,
                        computed_digest,
                        binding.started_clock_ms,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO result_dependencies(
                        client_id, round_no, config_version,
                        config_digest, result_digest
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        binding.client_id,
                        binding.round_no,
                        binding.config_version,
                        binding.config_digest,
                        computed_digest,
                    ),
                )
                connection.execute(
                    """
                    UPDATE compute_runs
                    SET status = 'completed', result_digest = ?
                    WHERE client_id = ? AND round_no = ?
                    """,
                    (computed_digest, binding.client_id, binding.round_no),
                )
                client_state = connection.execute(
                    "SELECT * FROM client_state WHERE client_id = ?",
                    (binding.client_id,),
                ).fetchone()
                if (
                    client_state is None
                    or binding.round_no > client_state["last_round_no"]
                ):
                    connection.execute(
                        """
                        INSERT INTO client_state(
                            client_id, last_round_no, bound_version,
                            bound_digest, result_digest, updated_clock_ms
                        ) VALUES (?, ?, ?, ?, ?, ?)
                        ON CONFLICT(client_id) DO UPDATE SET
                            last_round_no = excluded.last_round_no,
                            bound_version = excluded.bound_version,
                            bound_digest = excluded.bound_digest,
                            result_digest = excluded.result_digest,
                            updated_clock_ms = excluded.updated_clock_ms
                        """,
                        (
                            binding.client_id,
                            binding.round_no,
                            binding.config_version,
                            binding.config_digest,
                            computed_digest,
                            binding.started_clock_ms,
                        ),
                    )
                failpoint("before_result_commit")
        return result, False

    def finish_compute(
        self, binding: ComputeBinding, result: dict[str, Any]
    ) -> tuple[dict[str, Any], bool]:
        with self.store.write_lock():
            return self._finish_compute_unlocked(binding, result)

    def compute(
        self,
        client_id: str,
        round_no: int,
        now_ms: int,
        normalized_input: dict[str, Any],
    ) -> dict[str, Any]:
        binding = self.begin_compute(client_id, round_no, now_ms, normalized_input)
        failpoint("after_compute_start")
        result = compute_pipeline(
            binding.snapshot,
            strict_json_loads(binding.input_json),
            client_id=binding.client_id,
            round_no=binding.round_no,
        )
        failpoint("after_compute_calculated")
        stored_result, idempotent = self.finish_compute(binding, result)
        return {"ok": True, "idempotent": idempotent, "result": stored_result}

    # ------------------------------------------------------------------
    # Crash recovery
    # ------------------------------------------------------------------
    def resume(self) -> dict[str, Any]:
        patch_outcomes: list[dict[str, Any]] = []
        computation_outcomes: list[dict[str, Any]] = []
        with self.store.write_lock():
            with self.store.transaction() as connection:
                prepared = connection.execute(
                    """
                    SELECT patch_id, target_version FROM patches
                    WHERE status = 'prepared'
                    ORDER BY target_version, patch_id
                    """
                ).fetchall()
                in_progress = connection.execute(
                    """
                    SELECT client_id, round_no FROM compute_runs
                    WHERE status = 'in_progress'
                    ORDER BY client_id, round_no
                    """
                ).fetchall()
            for row in prepared:
                try:
                    response = self._finalize_patch(
                        row["patch_id"], idempotent=False
                    )
                    patch_outcomes.append(
                        {
                            "patch_id": row["patch_id"],
                            "status": "applied",
                            "version": response["version"],
                        }
                    )
                except ConfigHubError as exc:
                    patch_outcomes.append(
                        {
                            "patch_id": row["patch_id"],
                            "status": "aborted",
                            "error_code": exc.code,
                        }
                    )
            for row in in_progress:
                with self.store.transaction(immediate=True) as connection:
                    run = connection.execute(
                        """
                        SELECT * FROM compute_runs
                        WHERE client_id = ? AND round_no = ?
                        """,
                        (row["client_id"], row["round_no"]),
                    ).fetchone()
                    if run is None or run["status"] != "in_progress":
                        continue
                    snapshot = self.store.version_snapshot(
                        connection, run["config_version"]
                    )
                    if snapshot is None:
                        raise not_found(
                            "cannot resume run with missing config version",
                            {"version": run["config_version"]},
                        )
                    normalized_input = normalize_input(
                        strict_json_loads(run["input_json"])
                    )
                    binding = ComputeBinding(
                        client_id=run["client_id"],
                        round_no=run["round_no"],
                        config_version=run["config_version"],
                        config_digest=run["config_digest"],
                        input_json=run["input_json"],
                        input_digest=run["input_digest"],
                        started_clock_ms=run["started_clock_ms"],
                        snapshot=snapshot,
                        new_run=False,
                    )
                result = compute_pipeline(
                    binding.snapshot,
                    normalized_input,
                    client_id=binding.client_id,
                    round_no=binding.round_no,
                )
                _stored, idempotent = self._finish_compute_unlocked(binding, result)
                computation_outcomes.append(
                    {
                        "client_id": binding.client_id,
                        "round": binding.round_no,
                        "config_version": binding.config_version,
                        "status": "completed",
                        "idempotent": idempotent,
                    }
                )
            status = self.status()
        return {
            "ok": True,
            "replayed_patches": patch_outcomes,
            "replayed_computations": computation_outcomes,
            "status": status,
        }

    # ------------------------------------------------------------------
    # Verification and replay
    # ------------------------------------------------------------------
    @staticmethod
    def _add_issue(
        issues: list[dict[str, Any]],
        code: str,
        message: str,
        details: Any | None = None,
    ) -> None:
        issue: dict[str, Any] = {"code": code, "message": message}
        if details is not None:
            issue["details"] = details
        issues.append(issue)

    @staticmethod
    def _deep_diff(
        expected: Any,
        actual: Any,
        *,
        abs_tolerance: float,
        rel_tolerance: float,
        path: str = "$",
        output: list[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        if output is None:
            output = []
        if len(output) >= 100:
            return output
        if (
            isinstance(expected, (int, float))
            and not isinstance(expected, bool)
            and isinstance(actual, (int, float))
            and not isinstance(actual, bool)
        ):
            if float(expected) == float(actual):
                return output
            difference = abs(float(expected) - float(actual))
            allowed = max(
                abs_tolerance,
                rel_tolerance * max(abs(float(expected)), abs(float(actual))),
            )
            output.append(
                {
                    "path": path,
                    "expected": expected,
                    "actual": actual,
                    "difference": difference,
                    "within_tolerance": difference <= allowed,
                }
            )
            return output
        if type(expected) is not type(actual):
            output.append(
                {
                    "path": path,
                    "expected": expected,
                    "actual": actual,
                    "within_tolerance": False,
                }
            )
            return output
        if isinstance(expected, dict):
            for key in sorted(set(expected) | set(actual)):
                if key not in expected or key not in actual:
                    output.append(
                        {
                            "path": f"{path}.{key}",
                            "expected": expected.get(key, "<missing>"),
                            "actual": actual.get(key, "<missing>"),
                            "within_tolerance": False,
                        }
                    )
                else:
                    ConfigHub._deep_diff(
                        expected[key],
                        actual[key],
                        abs_tolerance=abs_tolerance,
                        rel_tolerance=rel_tolerance,
                        path=f"{path}.{key}",
                        output=output,
                    )
            return output
        if isinstance(expected, list):
            if len(expected) != len(actual):
                output.append(
                    {
                        "path": f"{path}.length",
                        "expected": len(expected),
                        "actual": len(actual),
                        "within_tolerance": False,
                    }
                )
            for index in range(min(len(expected), len(actual))):
                ConfigHub._deep_diff(
                    expected[index],
                    actual[index],
                    abs_tolerance=abs_tolerance,
                    rel_tolerance=rel_tolerance,
                    path=f"{path}[{index}]",
                    output=output,
                )
            return output
        if expected != actual:
            output.append(
                {
                    "path": path,
                    "expected": expected,
                    "actual": actual,
                    "within_tolerance": False,
                }
            )
        return output

    def verify(
        self,
        *,
        replay_version: int | None = None,
        replay_input: dict[str, Any] | None = None,
        replay_client: str | None = None,
        replay_round: int | None = None,
        abs_tolerance: float = 0.0,
        rel_tolerance: float = 0.0,
    ) -> dict[str, Any]:
        issues: list[dict[str, Any]] = []
        replay_entries: list[dict[str, Any]] = []
        replay_map: dict[int, list[dict[str, Any]]] = {}
        if replay_client is not None:
            replay_client = validate_client_id(replay_client)
        if replay_round is not None:
            replay_round = _require_round(replay_round)
        if replay_input is not None:
            replay_input = normalize_input(replay_input)

        with self.store.transaction() as connection:
            integrity = [row[0] for row in connection.execute("PRAGMA integrity_check")]
            if integrity != ["ok"]:
                self._add_issue(
                    issues, "E_SQLITE_INTEGRITY", "SQLite integrity check failed", integrity
                )
            foreign_keys = [
                tuple(row)
                for row in connection.execute("PRAGMA foreign_key_check").fetchall()
            ]
            if foreign_keys:
                self._add_issue(
                    issues,
                    "E_SQLITE_FOREIGN_KEY",
                    "SQLite foreign key check failed",
                    foreign_keys,
                )
            state = self.store.get_state(connection)
            if state is None:
                self._add_issue(
                    issues, "E_NO_STATE", "configuration state row is missing"
                )
                return {
                    "ok": False,
                    "integrity_check": "ok" if integrity == ["ok"] else "failed",
                    "issues": issues,
                    "replay": None,
                }

            version_rows = connection.execute(
                "SELECT * FROM config_versions ORDER BY version"
            ).fetchall()
            if not version_rows:
                self._add_issue(
                    issues, "E_NO_VERSIONS", "configuration version chain is empty"
                )
            previous_row: sqlite3.Row | None = None
            snapshots_by_version: dict[int, dict[str, Any]] = {}
            for row in version_rows:
                try:
                    snapshot = normalize_snapshot(
                        strict_json_loads(row["snapshot_json"])
                    )
                except (CanonicalJSONError, ConfigHubError) as exc:
                    self._add_issue(
                        issues,
                        "E_BAD_STORED_CONFIG",
                        "stored configuration snapshot is invalid",
                        {"version": row["version"], "error": str(exc)},
                    )
                    continue
                snapshots_by_version[row["version"]] = snapshot
                if canonical_json(snapshot) != row["snapshot_json"]:
                    self._add_issue(
                        issues,
                        "E_NONCANONICAL_CONFIG",
                        "stored configuration snapshot is not canonical",
                        {"version": row["version"]},
                    )
                if snapshot["version"] != row["version"]:
                    self._add_issue(
                        issues,
                        "E_VERSION_MISMATCH",
                        "snapshot version differs from database version",
                        {"version": row["version"]},
                    )
                if snapshot["content_digest"] != row["config_digest"]:
                    self._add_issue(
                        issues,
                        "E_CONFIG_DIGEST_MISMATCH",
                        "snapshot digest differs from database digest",
                        {"version": row["version"]},
                    )
                if snapshot["logical_clock_ms"] != row["effective_clock_ms"]:
                    self._add_issue(
                        issues,
                        "E_CONFIG_CLOCK_MISMATCH",
                        "snapshot logical clock differs from version row",
                        {"version": row["version"]},
                    )
                if previous_row is None:
                    if row["parent_version"] is not None:
                        self._add_issue(
                            issues,
                            "E_BAD_GENESIS",
                            "genesis version must have a null parent",
                            {"version": row["version"]},
                        )
                else:
                    if row["version"] != previous_row["version"] + 1:
                        self._add_issue(
                            issues,
                            "E_CHAIN_GAP",
                            "configuration version chain is not consecutive",
                            {
                                "previous_version": previous_row["version"],
                                "version": row["version"],
                            },
                        )
                    if row["parent_version"] != previous_row["version"]:
                        self._add_issue(
                            issues,
                            "E_CHAIN_PARENT",
                            "configuration parent pointer is inconsistent",
                            {"version": row["version"]},
                        )
                previous_row = row

            if version_rows:
                latest_version = version_rows[-1]["version"]
                if state["current_version"] != latest_version:
                    self._add_issue(
                        issues,
                        "E_CURRENT_POINTER",
                        "current pointer does not reference the chain head",
                        {
                            "current_version": state["current_version"],
                            "chain_head": latest_version,
                        },
                    )
            current_snapshot = snapshots_by_version.get(state["current_version"])
            if current_snapshot is not None:
                if state["logical_watermark_ms"] < current_snapshot["logical_clock_ms"]:
                    self._add_issue(
                        issues,
                        "E_CLOCK_WATERMARK",
                        "logical clock watermark is below the current config clock",
                    )
                if (
                    state["clock_rollback_tolerance_ms"]
                    != current_snapshot["clock_rollback_tolerance_ms"]
                ):
                    self._add_issue(
                        issues,
                        "E_TOLERANCE_MISMATCH",
                        "state tolerance differs from the current config tolerance",
                    )

            patch_rows = connection.execute(
                "SELECT * FROM patches ORDER BY patch_id"
            ).fetchall()
            patch_counts = {"aborted": 0, "applied": 0, "prepared": 0}
            applied_patch_ids: set[str] = set()
            for row in patch_rows:
                patch_counts[row["status"]] += 1
                if row["status"] == "prepared":
                    self._add_issue(
                        issues,
                        "E_INCOMPLETE_PATCH",
                        "prepared patch has not been committed or aborted",
                        {"patch_id": row["patch_id"]},
                    )
                if row["status"] == "applied":
                    applied_patch_ids.add(row["patch_id"])
                    if row["applied_version"] != row["target_version"]:
                        self._add_issue(
                            issues,
                            "E_PATCH_VERSION",
                            "applied patch version differs from target version",
                            {"patch_id": row["patch_id"]},
                        )
                    config_row = self.store.get_version_row(
                        connection, row["target_version"]
                    )
                    if config_row is None:
                        self._add_issue(
                            issues,
                            "E_PATCH_MISSING_VERSION",
                            "applied patch references a missing config version",
                            {"patch_id": row["patch_id"]},
                        )
                    else:
                        if config_row["patch_id"] != row["patch_id"]:
                            self._add_issue(
                                issues,
                                "E_PATCH_LINK",
                                "config version is linked to a different patch",
                                {"patch_id": row["patch_id"]},
                            )
                        if (
                            config_row["config_digest"]
                            != row["applied_config_digest"]
                        ):
                            self._add_issue(
                                issues,
                                "E_PATCH_DIGEST",
                                "patch result digest differs from config digest",
                                {"patch_id": row["patch_id"]},
                            )
                    try:
                        stored_patch = normalize_patch(
                            strict_json_loads(row["patch_json"])
                        )
                        if stored_patch["content_digest"] != row["content_digest"]:
                            self._add_issue(
                                issues,
                                "E_PATCH_CONTENT_DIGEST",
                                "stored patch digest is inconsistent",
                                {"patch_id": row["patch_id"]},
                            )
                    except (CanonicalJSONError, ConfigHubError) as exc:
                        self._add_issue(
                            issues,
                            "E_BAD_STORED_PATCH",
                            "stored patch is invalid",
                            {"patch_id": row["patch_id"], "error": str(exc)},
                        )
            for row in version_rows:
                if row["patch_id"] is not None and row["patch_id"] not in applied_patch_ids:
                    self._add_issue(
                        issues,
                        "E_VERSION_PATCH_LINK",
                        "config version references a non-applied patch",
                        {
                            "version": row["version"],
                            "patch_id": row["patch_id"],
                        },
                    )

            run_rows = connection.execute(
                """
                SELECT * FROM compute_runs
                ORDER BY client_id, round_no
                """
            ).fetchall()
            run_counts = {"completed": 0, "in_progress": 0}
            for row in run_rows:
                run_counts[row["status"]] += 1
                if row["status"] != "completed":
                    self._add_issue(
                        issues,
                        "E_INCOMPLETE_RUN",
                        "compute run has not committed a result",
                        {
                            "client_id": row["client_id"],
                            "round": row["round_no"],
                        },
                    )
                snapshot = snapshots_by_version.get(row["config_version"])
                if snapshot is None:
                    self._add_issue(
                        issues,
                        "E_RUN_CONFIG_MISSING",
                        "compute run references a missing config version",
                        {
                            "client_id": row["client_id"],
                            "round": row["round_no"],
                        },
                    )
                elif row["config_digest"] != snapshot["content_digest"]:
                    self._add_issue(
                        issues,
                        "E_RUN_CONFIG_DIGEST",
                        "compute run config digest is inconsistent",
                        {
                            "client_id": row["client_id"],
                            "round": row["round_no"],
                        },
                    )
                try:
                    normalized_input = normalize_input(
                        strict_json_loads(row["input_json"])
                    )
                except (CanonicalJSONError, ConfigHubError) as exc:
                    self._add_issue(
                        issues,
                        "E_BAD_STORED_INPUT",
                        "stored compute input is invalid",
                        {
                            "client_id": row["client_id"],
                            "round": row["round_no"],
                            "error": str(exc),
                        },
                    )
                else:
                    if input_digest(normalized_input) != row["input_digest"]:
                        self._add_issue(
                            issues,
                            "E_INPUT_DIGEST",
                            "compute input digest is inconsistent",
                            {
                                "client_id": row["client_id"],
                                "round": row["round_no"],
                            },
                        )

            result_rows = connection.execute(
                """
                SELECT * FROM results
                ORDER BY client_id, round_no
                """
            ).fetchall()
            results_by_client: dict[str, list[sqlite3.Row]] = {}
            for row in result_rows:
                results_by_client.setdefault(row["client_id"], []).append(row)
                run_row = connection.execute(
                    """
                    SELECT * FROM compute_runs
                    WHERE client_id = ? AND round_no = ?
                    """,
                    (row["client_id"], row["round_no"]),
                ).fetchone()
                if run_row is None:
                    self._add_issue(
                        issues,
                        "E_RESULT_WITHOUT_RUN",
                        "result has no matching compute run",
                        {"client_id": row["client_id"], "round": row["round_no"]},
                    )
                    continue
                if run_row["status"] != "completed":
                    self._add_issue(
                        issues,
                        "E_RESULT_RUN_STATUS",
                        "result exists while run is not completed",
                        {"client_id": row["client_id"], "round": row["round_no"]},
                    )
                if (
                    run_row["config_version"] != row["config_version"]
                    or run_row["config_digest"] != row["config_digest"]
                    or run_row["input_digest"] != row["input_digest"]
                ):
                    self._add_issue(
                        issues,
                        "E_RESULT_BINDING",
                        "result binding differs from its compute run",
                        {"client_id": row["client_id"], "round": row["round_no"]},
                    )
                dependency = connection.execute(
                    """
                    SELECT * FROM result_dependencies
                    WHERE client_id = ? AND round_no = ?
                    """,
                    (row["client_id"], row["round_no"]),
                ).fetchone()
                if dependency is None:
                    self._add_issue(
                        issues,
                        "E_MISSING_DEPENDENCY",
                        "result dependency record is missing",
                        {"client_id": row["client_id"], "round": row["round_no"]},
                    )
                elif (
                    dependency["config_version"] != row["config_version"]
                    or dependency["config_digest"] != row["config_digest"]
                    or dependency["result_digest"] != row["result_digest"]
                ):
                    self._add_issue(
                        issues,
                        "E_DEPENDENCY_MISMATCH",
                        "result dependency record is inconsistent",
                        {"client_id": row["client_id"], "round": row["round_no"]},
                    )
                try:
                    stored_result = strict_json_loads(row["result_json"])
                except CanonicalJSONError as exc:
                    self._add_issue(
                        issues,
                        "E_BAD_RESULT_JSON",
                        "stored result JSON is invalid",
                        {
                            "client_id": row["client_id"],
                            "round": row["round_no"],
                            "error": str(exc),
                        },
                    )
                    continue
                if (
                    result_payload_digest(stored_result) != row["result_digest"]
                    or stored_result.get("result_digest") != row["result_digest"]
                ):
                    self._add_issue(
                        issues,
                        "E_RESULT_DIGEST",
                        "stored result digest is inconsistent",
                        {"client_id": row["client_id"], "round": row["round_no"]},
                    )
                snapshot = snapshots_by_version.get(row["config_version"])
                if snapshot is None:
                    continue
                try:
                    normalized_input = normalize_input(
                        strict_json_loads(run_row["input_json"])
                    )
                    replayed = compute_pipeline(
                        snapshot,
                        normalized_input,
                        client_id=row["client_id"],
                        round_no=row["round_no"],
                    )
                except (CanonicalJSONError, ConfigHubError) as exc:
                    self._add_issue(
                        issues,
                        "E_REPLAY_ERROR",
                        "replay calculation failed",
                        {
                            "client_id": row["client_id"],
                            "round": row["round_no"],
                            "error": str(exc),
                        },
                    )
                    continue
                differences = self._deep_diff(
                    replayed,
                    stored_result,
                    abs_tolerance=abs_tolerance,
                    rel_tolerance=rel_tolerance,
                )
                exact_match = replayed["result_digest"] == row["result_digest"]
                within_tolerance = not any(
                    not item["within_tolerance"] for item in differences
                )
                if not exact_match and not within_tolerance:
                    self._add_issue(
                        issues,
                        "E_REPLAY_DIFF",
                        "replayed result differs from stored result",
                        {
                            "client_id": row["client_id"],
                            "round": row["round_no"],
                            "differences": differences,
                        },
                    )
                replay_map.setdefault(row["config_version"], []).append(
                    {
                        "client_id": row["client_id"],
                        "round": row["round_no"],
                        "config_version": row["config_version"],
                        "stored_digest": row["result_digest"],
                        "replayed_digest": replayed["result_digest"],
                        "exact_match": exact_match,
                        "within_tolerance": within_tolerance,
                        "differences": differences,
                    }
                )

            state_rows = connection.execute(
                "SELECT * FROM client_state ORDER BY client_id"
            ).fetchall()
            state_by_client = {row["client_id"]: row for row in state_rows}
            for client_id, client_results in results_by_client.items():
                latest = max(client_results, key=lambda item: item["round_no"])
                state_row = state_by_client.get(client_id)
                if state_row is None:
                    self._add_issue(
                        issues,
                        "E_CLIENT_STATE_MISSING",
                        "client result exists without client binding state",
                        {"client_id": client_id},
                    )
                elif (
                    state_row["last_round_no"] != latest["round_no"]
                    or state_row["bound_version"] != latest["config_version"]
                    or state_row["bound_digest"] != latest["config_digest"]
                    or state_row["result_digest"] != latest["result_digest"]
                ):
                    self._add_issue(
                        issues,
                        "E_CLIENT_STATE",
                        "client binding state differs from its latest result",
                        {"client_id": client_id},
                    )
            for client_id, state_row in state_by_client.items():
                if client_id not in results_by_client:
                    self._add_issue(
                        issues,
                        "E_CLIENT_STATE_ORPHAN",
                        "client binding state has no committed result",
                        {"client_id": client_id},
                    )

        replay_report: dict[str, Any] | None = None
        if replay_version is not None:
            with self.store.transaction() as connection:
                snapshot = self.store.version_snapshot(connection, replay_version)
            if snapshot is None:
                raise not_found(
                    f"configuration version {replay_version} does not exist"
                )
            entries = replay_map.get(replay_version, [])
            if replay_client is not None:
                entries = [
                    item for item in entries if item["client_id"] == replay_client
                ]
            if replay_round is not None:
                entries = [
                    item for item in entries if item["round"] == replay_round
                ]
            entries = sorted(
                entries, key=lambda item: (item["client_id"], item["round"])
            )
            if not entries and replay_input is not None:
                synthetic = compute_pipeline(
                    snapshot,
                    replay_input,
                    client_id=replay_client or "verification",
                    round_no=replay_round or 1,
                )
                entries.append(
                    {
                        "client_id": synthetic["client_id"],
                        "round": synthetic["round"],
                        "config_version": replay_version,
                        "stored_digest": None,
                        "replayed_digest": synthetic["result_digest"],
                        "exact_match": None,
                        "within_tolerance": True,
                        "differences": [],
                        "result": synthetic,
                    }
                )
            replay_ok = bool(entries) and all(
                item["exact_match"] is not False
                or item["within_tolerance"]
                for item in entries
            )
            if not entries:
                self._add_issue(
                    issues,
                    "E_REPLAY_NO_INPUT",
                    "no stored result matched the replay selection and no input was provided",
                    {"version": replay_version},
                )
            replay_report = {
                "version": replay_version,
                "entries": entries,
                "ok": replay_ok and bool(entries),
            }

        return {
            "ok": not issues,
            "integrity_check": "ok" if integrity == ["ok"] else "failed",
            "current_version": state["current_version"],
            "logical_clock_watermark_ms": state["logical_watermark_ms"],
            "version_chain": [
                {
                    "version": row["version"],
                    "parent_version": row["parent_version"],
                    "content_digest": row["config_digest"],
                    "patch_id": row["patch_id"],
                }
                for row in version_rows
            ],
            "patches": patch_counts,
            "compute_runs": run_counts,
            "results_checked": len(result_rows),
            "replay": replay_report,
            "issues": issues,
        }

    # ------------------------------------------------------------------
    # File outputs
    # ------------------------------------------------------------------
    @staticmethod
    def write_json_file(path: str | Path, value: Any) -> None:
        atomic_write_text(path, stable_json_text(value))
