"""Checkpoint save, incremental reuse, crash recovery and verification."""

import json
import os
import shutil
import tempfile
import threading
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, wait
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Union

from .crash import CrashInjector
from .derivation import (
    CheckpointContent,
    ShardContent,
    derive_checkpoint,
)
from .errors import (
    CheckpointConflictError,
    CheckpointIncompleteError,
    CheckpointNotFoundError,
    ClockRegressionError,
    InternalError,
    ManifestDigestMismatchError,
    ParameterDigestMismatchError,
    PlanDigestMismatchError,
    ReuseRecordInvalidError,
    RoundBudgetExhaustedError,
    ShardDigestMismatchError,
    ShardMissingError,
    ShardWriteFailedError,
    StitchOrderMismatchError,
    UsageError,
    VerificationFailedError,
    VersionRollbackError,
)
from .jsonio import (
    atomic_write_bytes,
    atomic_write_json,
    digest_bytes,
    digest_file,
    digest_json,
    fsync_dir,
)
from .ledger import Ledger
from .model import Plan, parse_plan
from .scheduler import Schedule, schedule_round
from .verification import (
    MANIFEST_SCHEMA,
    VerifiedRestore,
    verify_checkpoint_manifest,
)

PathLike = Union[str, os.PathLike]


def _safe_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class CheckpointManager:
    def __init__(self, root: PathLike) -> None:
        self.root = Path(root)
        self.ledger = Ledger(self.root)

    def _require_ledger(self) -> None:
        if not self.ledger.db_path.exists():
            raise CheckpointNotFoundError(
                "checkpoint ledger does not exist", root=str(self.root)
            )

    def _resolve_clock(self, requested: Optional[int], previous: Optional[int], tolerance: int) -> int:
        if requested is None:
            return 0 if previous is None else previous + 1
        if not isinstance(requested, int) or isinstance(requested, bool) or requested < 0:
            raise UsageError("--clock-ms must be a non-negative integer")
        if previous is not None and requested + tolerance < previous:
            raise ClockRegressionError(
                "logical clock moved backwards beyond the configured tolerance",
                requested_clock_ms=requested,
                watermark_ms=previous,
                tolerance_ms=tolerance,
            )
        return requested if previous is None else max(requested, previous)

    def _check_restore_clock(
        self, requested: Optional[int], tolerance: int
    ) -> None:
        if requested is None:
            return
        previous = self.ledger.get_clock() or 0
        if not isinstance(requested, int) or isinstance(requested, bool) or requested < 0:
            raise UsageError("--clock-ms must be a non-negative integer")
        if requested + tolerance < previous:
            raise ClockRegressionError(
                "logical clock moved backwards beyond the configured tolerance",
                requested_clock_ms=requested,
                watermark_ms=previous,
                tolerance_ms=tolerance,
            )

    def _validate_target_version(self, plan: Plan, version: Optional[int]) -> int:
        if version is None:
            version = plan.default_version()
        if not isinstance(version, int) or isinstance(version, bool) or version <= 0:
            raise UsageError("checkpoint version must be a positive integer")
        return version

    def save(
        self,
        plan: Plan,
        version: Optional[int] = None,
        mode: str = "auto",
        clock_ms: Optional[int] = None,
        base_version: Optional[int] = None,
        crash_injector: Optional[CrashInjector] = None,
    ) -> Dict[str, Any]:
        version = self._validate_target_version(plan, version)
        mode = mode.lower()
        if mode not in ("auto", "full", "incremental"):
            raise UsageError("save mode must be auto, full or incremental")
        content = derive_checkpoint(plan, version)
        plan_digest = plan.digest()
        injector = crash_injector or CrashInjector()
        with self.ledger.file_lock(exclusive=True):
            self.ledger.initialize()
            self._cleanup_orphans()
            with self.ledger.connection() as connection:
                existing = connection.execute(
                    "SELECT * FROM checkpoints WHERE checkpoint = ? AND version = ?",
                    (plan.checkpoint, version),
                ).fetchone()
                max_version = self.ledger.max_version(plan.checkpoint)
                round_row = connection.execute(
                    "SELECT * FROM rounds WHERE checkpoint = ? AND version = ?",
                    (plan.checkpoint, version),
                ).fetchone()
            if existing is not None:
                if existing["plan_digest"] != plan_digest:
                    raise CheckpointConflictError(
                        "checkpoint version already exists with a different plan",
                        checkpoint=plan.checkpoint,
                        version=version,
                    )
                self._export_manifest_if_needed(dict(existing))
                return self._idempotent_result(dict(existing), "already-committed")
            if max_version is not None and version < max_version:
                raise VersionRollbackError(
                    "new checkpoint version must be greater than all committed versions",
                    checkpoint=plan.checkpoint,
                    requested_version=version,
                    latest_version=max_version,
                )
            if round_row is not None:
                if round_row["plan_digest"] != plan_digest:
                    raise CheckpointConflictError(
                        "an incomplete round exists for this version with a different plan",
                        checkpoint=plan.checkpoint,
                        version=version,
                        round_id=round_row["round_id"],
                    )
                raise CheckpointIncompleteError(
                    "checkpoint has an incomplete round; run resume",
                    checkpoint=plan.checkpoint,
                    version=version,
                    round_id=round_row["round_id"],
                    status=round_row["status"],
                )

            previous_clock = self.ledger.get_clock()
            start_clock = self._resolve_clock(
                clock_ms, previous_clock, plan.strategy.clock_tolerance_ms
            )
            previous_checkpoint = self._select_base(plan, version, mode, base_version)
            reuse = self._prepare_reuse(plan, content, previous_checkpoint)
            write_shards = [
                shard for shard in content.shards if not reuse[shard.shard_id]["hit"]
            ]
            schedule = schedule_round(
                (
                    (shard.shard_id, shard.shard_index, shard.logical_cost_ms)
                    for shard in write_shards
                ),
                start_clock,
                plan.strategy.round_budget_ms,
                plan.strategy.max_concurrency,
            )
            round_id = self._create_round(
                plan,
                content,
                version,
                mode,
                previous_checkpoint,
                start_clock,
                schedule,
                reuse,
            )
            if injector.describes("write") and injector.threshold() == 0:
                injector.trigger("write", 0)
            return self._run_existing_round(
                plan,
                content,
                version,
                round_id,
                schedule,
                reuse,
                start_clock,
                injector,
                (
                    int(previous_checkpoint["version"])
                    if previous_checkpoint is not None
                    else None
                ),
            )

    def resume(
        self,
        plan: Optional[Plan] = None,
        version: Optional[int] = None,
        clock_ms: Optional[int] = None,
        crash_injector: Optional[CrashInjector] = None,
        checkpoint: Optional[str] = None,
    ) -> Dict[str, Any]:
        if plan is None:
            if checkpoint is None or version is None:
                raise UsageError(
                    "resume requires either --plan or both --checkpoint and --version"
                )
            self._require_ledger()
            stored_round = self.ledger.get_round(checkpoint, version)
            if stored_round is None:
                raise CheckpointNotFoundError(
                    "no resumable round exists",
                    checkpoint=checkpoint,
                    version=version,
                )
            if not stored_round.get("plan_json"):
                raise UsageError(
                    "resume plan snapshot is unavailable; provide --plan",
                    checkpoint=checkpoint,
                    version=version,
                )
            plan = parse_plan(json.loads(stored_round["plan_json"]))
        version = self._validate_target_version(plan, version)
        content = derive_checkpoint(plan, version)
        plan_digest = plan.digest()
        injector = crash_injector or CrashInjector()
        with self.ledger.file_lock(exclusive=True):
            self.ledger.initialize()
            self._cleanup_orphans()
            existing = None
            try:
                existing = self.ledger.get_checkpoint(plan.checkpoint, version)
            except CheckpointNotFoundError:
                pass
            if existing is not None:
                if existing["plan_digest"] != plan_digest:
                    raise CheckpointConflictError(
                        "committed checkpoint was produced by a different plan",
                        checkpoint=plan.checkpoint,
                        version=version,
                    )
                self._export_manifest_if_needed(existing)
                return self._idempotent_result(existing, "already-committed")
            round_row = self.ledger.get_round(plan.checkpoint, version)
            if round_row is None:
                raise CheckpointNotFoundError(
                    "no resumable round exists",
                    checkpoint=plan.checkpoint,
                    version=version,
                )
            if round_row["plan_digest"] != plan_digest:
                raise PlanDigestMismatchError(
                    "resume plan digest differs from the interrupted round",
                    expected=round_row["plan_digest"],
                    actual=plan_digest,
                    checkpoint=plan.checkpoint,
                    version=version,
                )
            max_version = self.ledger.max_version(plan.checkpoint)
            if max_version is not None and version < max_version:
                raise VersionRollbackError(
                    "cannot resume an older version after a newer checkpoint committed",
                    checkpoint=plan.checkpoint,
                    requested_version=version,
                    latest_version=max_version,
                )
            previous_clock = self.ledger.get_clock()
            start_clock = self._resolve_clock(
                clock_ms, previous_clock, plan.strategy.clock_tolerance_ms
            )
            reuse = self._recover_round_reuse(round_row, content)
            self._adopt_completed_objects(round_row["round_id"], content, start_clock)
            self._reset_uncommitted_round_shards(round_row["round_id"])
            rows = {
                row["shard_id"]: row
                for row in self.ledger.get_round_shards(round_row["round_id"])
            }
            write_shards = [
                shard
                for shard in content.shards
                if rows[shard.shard_id]["status"] not in ("committed", "reused")
            ]
            schedule = schedule_round(
                (
                    (shard.shard_id, shard.shard_index, shard.logical_cost_ms)
                    for shard in write_shards
                ),
                start_clock,
                plan.strategy.round_budget_ms,
                plan.strategy.max_concurrency,
            )
            with self.ledger.transaction() as connection:
                connection.execute(
                    "UPDATE rounds SET status = 'writing', start_clock_ms = ?, "
                    "end_clock_ms = NULL, deadline_ms = ?, budget_ms = ?, "
                    "concurrency = ?, diagnostics = ? WHERE round_id = ?",
                    (
                        start_clock,
                        schedule.deadline_ms,
                        plan.strategy.round_budget_ms,
                        plan.strategy.max_concurrency,
                        _safe_json(
                            {
                                "resume_clock_ms": start_clock,
                                "resume_schedule": schedule.to_dict(),
                            }
                        ),
                        round_row["round_id"],
                    ),
                )
                for item in schedule.budget_unstarted:
                    connection.execute(
                        "UPDATE round_shards SET status = 'budget_unstarted', "
                        "started = 0, finished = 0, reason = ? "
                        "WHERE round_id = ? AND shard_id = ?",
                        (item.reason, round_row["round_id"], item.shard_id),
                    )
                self.ledger.set_clock_in_transaction(connection, start_clock)
            if injector.describes("write") and injector.threshold() == 0:
                injector.trigger("write", 0)
            base_version = round_row["base_version"]
            return self._run_existing_round(
                plan,
                content,
                version,
                round_row["round_id"],
                schedule,
                reuse,
                start_clock,
                injector,
                base_version,
            )

    def _select_base(
        self,
        plan: Plan,
        version: int,
        mode: str,
        base_version: Optional[int],
    ) -> Optional[Dict[str, Any]]:
        if mode == "full":
            if base_version is not None:
                raise UsageError("base_version is not valid in full mode")
            return None
        if base_version is not None:
            if not isinstance(base_version, int) or isinstance(base_version, bool):
                raise UsageError("--base-version must be an integer")
            if base_version >= version:
                raise UsageError("--base-version must be lower than the target version")
            return self.ledger.get_checkpoint(plan.checkpoint, base_version)
        versions = [
            row
            for row in self.ledger.list_checkpoints(plan.checkpoint)
            if int(row["version"]) < version
        ]
        if not versions:
            return None
        return versions[-1]

    def _prepare_reuse(
        self,
        plan: Plan,
        content: CheckpointContent,
        previous_checkpoint: Optional[Mapping[str, Any]],
    ) -> Dict[str, Dict[str, Any]]:
        previous_by_index: Dict[int, Mapping[str, Any]] = {}
        previous_version: Optional[int] = None
        if previous_checkpoint is not None:
            previous_version = int(previous_checkpoint["version"])
            for row in self.ledger.get_checkpoint_shards(
                plan.checkpoint, previous_version
            ):
                previous_by_index[int(row["shard_index"])] = row
        reuse: Dict[str, Dict[str, Any]] = {}
        for shard in content.shards:
            prior = previous_by_index.get(shard.shard_index)
            object_path = self.ledger.object_path(shard.digest)
            object_valid = object_path.exists() and digest_file(object_path) == shard.digest
            if prior is not None and prior["digest"] == shard.digest and object_valid:
                reuse[shard.shard_id] = {
                    "hit": True,
                    "from_version": previous_version,
                    "basis": "geometric-shard-digest-match",
                }
            elif object_valid:
                reuse[shard.shard_id] = {
                    "hit": True,
                    "from_version": None,
                    "basis": "content-addressed-object-hit",
                }
            else:
                reuse[shard.shard_id] = {
                    "hit": False,
                    "from_version": None,
                    "basis": "changed-shard",
                }
        return reuse

    def _create_round(
        self,
        plan: Plan,
        content: CheckpointContent,
        version: int,
        mode: str,
        base_checkpoint: Optional[Mapping[str, Any]],
        start_clock: int,
        schedule: Schedule,
        reuse: Mapping[str, Mapping[str, Any]],
    ) -> int:
        base_version = int(base_checkpoint["version"]) if base_checkpoint else None
        started = {item.shard_id for item in schedule.started}
        budget_unstarted = {item.shard_id: item for item in schedule.budget_unstarted}
        with self.ledger.transaction() as connection:
            cursor = connection.execute(
                "INSERT INTO rounds(checkpoint, version, plan_digest, plan_json, mode, "
                "base_version, start_clock_ms, deadline_ms, budget_ms, concurrency, "
                "status, diagnostics) "
                "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'writing', ?)",
                (
                    plan.checkpoint,
                    version,
                    plan.digest(),
                    _safe_json(plan.to_canonical()),
                    mode,
                    base_version,
                    start_clock,
                    schedule.deadline_ms,
                    plan.strategy.round_budget_ms,
                    plan.strategy.max_concurrency,
                    _safe_json({"schedule": schedule.to_dict()}),
                ),
            )
            round_id = int(cursor.lastrowid)
            for shard in content.shards:
                item = reuse[shard.shard_id]
                if item["hit"]:
                    status = "reused"
                    started_flag = 0
                    finished_flag = 1
                    reason = None
                elif shard.shard_id in started:
                    status = "planned"
                    started_flag = 0
                    finished_flag = 0
                    reason = None
                else:
                    status = "budget_unstarted"
                    started_flag = 0
                    finished_flag = 0
                    reason = budget_unstarted[shard.shard_id].reason
                connection.execute(
                    "INSERT INTO round_shards(round_id, shard_id, shard_index, digest, "
                    "layer_names, size_bytes, logical_cost_ms, status, started, finished, "
                    "reason, reuse_hit, reuse_from_version, reuse_basis) "
                    "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        round_id,
                        shard.shard_id,
                        shard.shard_index,
                        shard.digest,
                        _safe_json(list(shard.layer_names)),
                        shard.size_bytes,
                        shard.logical_cost_ms,
                        status,
                        started_flag,
                        finished_flag,
                        reason,
                        1 if item["hit"] else 0,
                        item["from_version"],
                        item["basis"],
                    ),
                )
                if item["hit"]:
                    connection.execute(
                        "INSERT OR IGNORE INTO objects(digest, relpath, size_bytes, created_clock_ms) "
                        "VALUES(?, ?, ?, ?)",
                        (
                            shard.digest,
                            self.ledger.object_relpath(shard.digest),
                            shard.size_bytes,
                            start_clock,
                        ),
                    )
            self.ledger.set_clock_in_transaction(connection, start_clock)
            connection.execute(
                "INSERT INTO events(checkpoint, version, round_id, stage, clock_ms, details_json) "
                "VALUES(?, ?, ?, 'round-created', ?, ?)",
                (
                    plan.checkpoint,
                    version,
                    round_id,
                    start_clock,
                    _safe_json(
                        {
                            "mode": mode,
                            "base_version": base_version,
                            "schedule": schedule.to_dict(),
                        }
                    ),
                ),
            )
        return round_id

    def _recover_round_reuse(
        self, round_row: Mapping[str, Any], content: CheckpointContent
    ) -> Dict[str, Dict[str, Any]]:
        rows = {
            row["shard_id"]: row
            for row in self.ledger.get_round_shards(int(round_row["round_id"]))
        }
        reuse: Dict[str, Dict[str, Any]] = {}
        for shard in content.shards:
            row = rows.get(shard.shard_id)
            if row is None:
                raise InternalError(
                    "round shard inventory does not match the plan",
                    shard_id=shard.shard_id,
                    round_id=round_row["round_id"],
                )
            if row["digest"] != shard.digest or int(row["size_bytes"]) != shard.size_bytes:
                raise InternalError(
                    "round shard digest does not match deterministic replay",
                    shard_id=shard.shard_id,
                    expected=shard.digest,
                    actual=row["digest"],
                )
            reuse[shard.shard_id] = {
                "hit": bool(row["reuse_hit"]),
                "from_version": row["reuse_from_version"],
                "basis": row["reuse_basis"] or "changed-shard",
            }
        return reuse

    def _adopt_completed_objects(
        self, round_id: int, content: CheckpointContent, clock_ms: int
    ) -> None:
        rows = {
            row["shard_id"]: row
            for row in self.ledger.get_round_shards(round_id)
        }
        for shard in content.shards:
            row = rows[shard.shard_id]
            if row["status"] in ("committed", "reused"):
                object_path = self.ledger.object_path(shard.digest)
                if not object_path.exists():
                    raise ShardMissingError(
                        "committed shard object is missing during resume",
                        shard_id=shard.shard_id,
                        path=str(object_path),
                    )
                if digest_file(object_path) != shard.digest:
                    raise ShardDigestMismatchError(
                        "committed shard object is corrupt during resume",
                        shard_id=shard.shard_id,
                        path=str(object_path),
                    )
                continue
            object_path = self.ledger.object_path(shard.digest)
            if object_path.exists() and digest_file(object_path) == shard.digest:
                with self.ledger.transaction() as connection:
                    connection.execute(
                        "INSERT OR IGNORE INTO objects(digest, relpath, size_bytes, created_clock_ms) "
                        "VALUES(?, ?, ?, ?)",
                        (
                            shard.digest,
                            self.ledger.object_relpath(shard.digest),
                            shard.size_bytes,
                            clock_ms,
                        ),
                    )
                    connection.execute(
                        "UPDATE round_shards SET status = 'committed', started = 1, "
                        "finished = 1, reason = 'adopted-complete-object-after-crash', "
                        "reuse_hit = 1, reuse_from_version = NULL, "
                        "reuse_basis = 'adopted-after-crash' "
                        "WHERE round_id = ? AND shard_id = ?",
                        (round_id, shard.shard_id),
                    )
                    self.ledger.set_clock_in_transaction(connection, clock_ms)

    def _reset_uncommitted_round_shards(self, round_id: int) -> None:
        with self.ledger.transaction() as connection:
            connection.execute(
                "UPDATE round_shards SET status = 'planned', started = 0, finished = 0, "
                "reason = NULL WHERE round_id = ? "
                "AND status NOT IN ('committed', 'reused')",
                (round_id,),
            )

    def _run_existing_round(
        self,
        plan: Plan,
        content: CheckpointContent,
        version: int,
        round_id: int,
        schedule: Schedule,
        reuse: Mapping[str, Mapping[str, Any]],
        start_clock: int,
        injector: CrashInjector,
        base_version: Optional[int],
    ) -> Dict[str, Any]:
        rows = {
            row["shard_id"]: row
            for row in self.ledger.get_round_shards(round_id)
        }
        shard_map = content.shard_map()
        commit_lock = threading.Lock()
        committed_this_run = [0]
        failures: List[Dict[str, Any]] = []
        abandoned: List[str] = []

        selected = [
            item
            for item in schedule.started
            if rows[item.shard_id]["status"] not in ("committed", "reused")
        ]
        waves: "OrderedDict[int, List[Any]]" = OrderedDict()
        for item in selected:
            waves.setdefault(item.start_ms, []).append(item)

        stop_after_failure = False
        for _, wave in waves.items():
            if stop_after_failure:
                for item in wave:
                    abandoned.append(item.shard_id)
                    self._mark_abandoned(round_id, item.shard_id, "aborted-after-started-shard-failure")
                continue
            with self.ledger.transaction() as connection:
                for item in wave:
                    connection.execute(
                        "UPDATE round_shards SET status = 'running', started = 1, "
                        "finished = 0, attempts = attempts + 1, reason = NULL "
                        "WHERE round_id = ? AND shard_id = ?",
                        (round_id, item.shard_id),
                    )
                self.ledger.set_clock_in_transaction(connection, start_clock)
            futures = {}
            with ThreadPoolExecutor(
                max_workers=min(plan.strategy.max_concurrency, len(wave))
            ) as executor:
                for item in wave:
                    future = executor.submit(
                        self._write_one_shard,
                        round_id,
                        shard_map[item.shard_id],
                        item.end_ms,
                        commit_lock,
                        committed_this_run,
                        injector,
                    )
                    futures[future] = item
                wait(futures)
            for future, item in futures.items():
                try:
                    future.result()
                except BaseException as exc:
                    stop_after_failure = True
                    failure = {
                        "shard_id": item.shard_id,
                        "error": type(exc).__name__,
                        "message": str(exc),
                    }
                    failures.append(failure)
                    self._mark_failed(round_id, item.shard_id, failure["message"])

        budget_unstarted = [item.shard_id for item in schedule.budget_unstarted]
        end_clock = max(
            start_clock,
            schedule.virtual_end_ms,
            self.ledger.get_clock() or start_clock,
        )
        if failures:
            diagnostics = {
                "started_failed": failures,
                "budget_unstarted": budget_unstarted,
                "abandoned": abandoned,
            }
            with self.ledger.transaction() as connection:
                connection.execute(
                    "UPDATE rounds SET status = 'failed', end_clock_ms = ?, diagnostics = ? "
                    "WHERE round_id = ?",
                    (end_clock, _safe_json(diagnostics), round_id),
                )
                self.ledger.set_clock_in_transaction(connection, end_clock)
            raise ShardWriteFailedError(
                "one or more started shard writes failed",
                checkpoint=plan.checkpoint,
                version=version,
                round_id=round_id,
                **diagnostics
            )
        if budget_unstarted:
            diagnostics = {
                "budget_unstarted": budget_unstarted,
                "reason": "round logical-time budget exhausted before task start",
                "started_failed": [],
                "abandoned": abandoned,
            }
            with self.ledger.transaction() as connection:
                connection.execute(
                    "UPDATE rounds SET status = 'incomplete', end_clock_ms = ?, diagnostics = ? "
                    "WHERE round_id = ?",
                    (end_clock, _safe_json(diagnostics), round_id),
                )
                self.ledger.set_clock_in_transaction(connection, end_clock)
            raise RoundBudgetExhaustedError(
                "round logical-time budget exhausted; run resume",
                checkpoint=plan.checkpoint,
                version=version,
                round_id=round_id,
                **diagnostics
            )
        if abandoned:
            with self.ledger.transaction() as connection:
                connection.execute(
                    "UPDATE rounds SET status = 'failed', end_clock_ms = ? WHERE round_id = ?",
                    (end_clock, round_id),
                )
            raise ShardWriteFailedError(
                "remaining shard waves were actively abandoned",
                checkpoint=plan.checkpoint,
                version=version,
                round_id=round_id,
                abandoned=abandoned,
            )

        with self.ledger.transaction() as connection:
            connection.execute(
                "UPDATE rounds SET status = 'committing', end_clock_ms = ? WHERE round_id = ?",
                (end_clock, round_id),
            )
            self.ledger.set_clock_in_transaction(connection, end_clock)
        injector.trigger("commit")
        return self._finalize(
            plan,
            content,
            version,
            round_id,
            schedule,
            start_clock,
            end_clock,
            reuse,
            base_version,
        )

    def _write_one_shard(
        self,
        round_id: int,
        shard: ShardContent,
        logical_finish_ms: int,
        commit_lock: threading.Lock,
        committed_this_run: List[int],
        injector: CrashInjector,
    ) -> None:
        object_path = self.ledger.object_path(shard.digest)
        if not object_path.exists() or digest_file(object_path) != shard.digest:
            atomic_write_bytes(object_path, shard.file_bytes)
        if digest_file(object_path) != shard.digest:
            raise ShardDigestMismatchError(
                "newly written shard failed content verification",
                shard_id=shard.shard_id,
                path=str(object_path),
            )
        with commit_lock:
            with self.ledger.transaction() as connection:
                connection.execute(
                    "INSERT OR IGNORE INTO objects(digest, relpath, size_bytes, created_clock_ms) "
                    "VALUES(?, ?, ?, ?)",
                    (
                        shard.digest,
                        self.ledger.object_relpath(shard.digest),
                        shard.size_bytes,
                        logical_finish_ms,
                    ),
                )
                connection.execute(
                    "UPDATE round_shards SET status = 'committed', started = 1, "
                    "finished = 1, reason = NULL "
                    "WHERE round_id = ? AND shard_id = ?",
                    (round_id, shard.shard_id),
                )
                self.ledger.set_clock_in_transaction(connection, logical_finish_ms)
            committed_this_run[0] += 1
            injector.trigger("write", committed_this_run[0])

    def _mark_failed(self, round_id: int, shard_id: str, reason: str) -> None:
        with self.ledger.transaction() as connection:
            connection.execute(
                "UPDATE round_shards SET status = 'started_failed', started = 1, "
                "finished = 1, reason = ? WHERE round_id = ? AND shard_id = ?",
                (reason, round_id, shard_id),
            )

    def _mark_abandoned(self, round_id: int, shard_id: str, reason: str) -> None:
        with self.ledger.transaction() as connection:
            connection.execute(
                "UPDATE round_shards SET status = 'abandoned', started = 0, "
                "finished = 0, reason = ? WHERE round_id = ? AND shard_id = ?",
                (reason, round_id, shard_id),
            )

    def _build_manifest(
        self,
        plan: Plan,
        content: CheckpointContent,
        round_id: int,
        schedule: Schedule,
        start_clock: int,
        end_clock: int,
        reuse: Mapping[str, Mapping[str, Any]],
    ) -> Dict[str, Any]:
        rows = {
            row["shard_id"]: row
            for row in self.ledger.get_round_shards(round_id)
        }
        shard_for_layer: Dict[str, int] = {}
        offset_for_layer: Dict[str, int] = {}
        offsets = [0] * len(content.shards)
        for layer in content.layers:
            for shard in content.shards:
                if layer.name in shard.layer_names:
                    shard_for_layer[layer.name] = shard.shard_index
                    offset_for_layer[layer.name] = offsets[shard.shard_index]
                    offsets[shard.shard_index] += layer.nbytes
                    break
        layers = []
        for layer in content.layers:
            layers.append(
                {
                    "name": layer.name,
                    "shape": list(layer.shape),
                    "dtype": layer.dtype,
                    "revision": layer.revision,
                    "nbytes": layer.nbytes,
                    "param_digest": layer.param_digest,
                    "shard_id": content.shards[shard_for_layer[layer.name]].shard_id,
                    "shard_index": shard_for_layer[layer.name],
                    "offset_in_shard": offset_for_layer[layer.name],
                }
            )
        shards = []
        for shard in content.shards:
            row = rows[shard.shard_id]
            shards.append(
                {
                    "shard_id": shard.shard_id,
                    "shard_index": shard.shard_index,
                    "digest": shard.digest,
                    "size_bytes": shard.size_bytes,
                    "layers": list(shard.layer_names),
                    "reuse": {
                        "hit": bool(row["reuse_hit"]),
                        "from_version": row["reuse_from_version"],
                        "basis": row["reuse_basis"] or "written",
                    },
                }
            )
        return {
            "schema": MANIFEST_SCHEMA,
            "checkpoint": plan.checkpoint,
            "version": content.version,
            "global_seed": plan.global_seed,
            "plan_digest": plan.digest(),
            "plan": plan.to_canonical(),
            "strategy": plan.strategy.to_canonical(),
            "parameter_digest": content.parameter_digest,
            "layer_order": list(content.layer_order),
            "layers": layers,
            "shards": shards,
            "shard_order": [shard.shard_id for shard in content.shards],
            "round": {
                "round_id": round_id,
                "start_clock_ms": start_clock,
                "end_clock_ms": end_clock,
                "budget_ms": plan.strategy.round_budget_ms,
                "concurrency": plan.strategy.max_concurrency,
                "schedule": schedule.to_dict(),
            },
            "clock_ms": end_clock,
        }

    def _finalize(
        self,
        plan: Plan,
        content: CheckpointContent,
        version: int,
        round_id: int,
        schedule: Schedule,
        start_clock: int,
        end_clock: int,
        reuse: Mapping[str, Mapping[str, Any]],
        base_version: Optional[int],
    ) -> Dict[str, Any]:
        manifest = self._build_manifest(
            plan, content, round_id, schedule, start_clock, end_clock, reuse
        )
        manifest_digest = digest_json(manifest)
        manifest_json = _safe_json(manifest)
        rows = {
            row["shard_id"]: row
            for row in self.ledger.get_round_shards(round_id)
        }
        for shard in content.shards:
            status = rows[shard.shard_id]["status"]
            if status not in ("committed", "reused"):
                raise InternalError(
                    "cannot finalize a round with uncommitted shards",
                    shard_id=shard.shard_id,
                    status=status,
                )
        offsets = [0] * len(content.shards)
        shard_for_layer: Dict[str, int] = {}
        for layer in content.layers:
            for shard in content.shards:
                if layer.name in shard.layer_names:
                    shard_for_layer[layer.name] = shard.shard_index
                    break
        layer_offsets: Dict[str, int] = {}
        for layer in content.layers:
            shard_index = shard_for_layer[layer.name]
            layer_offsets[layer.name] = offsets[shard_index]
            offsets[shard_index] += layer.nbytes

        with self.ledger.transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM checkpoints WHERE checkpoint = ? AND version = ?",
                (plan.checkpoint, version),
            ).fetchone()
            if existing is not None:
                if (
                    existing["plan_digest"] != plan.digest()
                    or existing["manifest_digest"] != manifest_digest
                ):
                    raise CheckpointConflictError(
                        "checkpoint was concurrently committed with different content",
                        checkpoint=plan.checkpoint,
                        version=version,
                    )
                result = dict(existing)
                connection.execute(
                    "UPDATE rounds SET status = 'complete', end_clock_ms = ? "
                    "WHERE round_id = ?",
                    (end_clock, round_id),
                )
                self.ledger.set_clock_in_transaction(connection, end_clock)
            else:
                max_row = connection.execute(
                    "SELECT MAX(version) AS version FROM checkpoints WHERE checkpoint = ?",
                    (plan.checkpoint,),
                ).fetchone()
                max_version = (
                    int(max_row["version"])
                    if max_row is not None and max_row["version"] is not None
                    else None
                )
                if max_version is not None and version < max_version:
                    raise VersionRollbackError(
                        "checkpoint pointer cannot move backwards",
                        checkpoint=plan.checkpoint,
                        requested_version=version,
                        latest_version=max_version,
                    )
                connection.execute(
                    "INSERT INTO checkpoints(checkpoint, version, plan_digest, "
                    "parameter_digest, manifest_json, manifest_digest, base_version, "
                    "round_id, mode, clock_ms, committed_clock_ms) "
                    "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        plan.checkpoint,
                        version,
                        plan.digest(),
                        content.parameter_digest,
                        manifest_json,
                        manifest_digest,
                        base_version,
                        round_id,
                        "incremental" if base_version is not None else "full",
                        start_clock,
                        end_clock,
                    ),
                )
                for shard in content.shards:
                    row = rows[shard.shard_id]
                    connection.execute(
                        "INSERT INTO checkpoint_shards(checkpoint, version, shard_id, "
                        "shard_index, digest, size_bytes, object_relpath, layer_names, "
                        "reuse_hit, reuse_from_version, reuse_basis) "
                        "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            plan.checkpoint,
                            version,
                            shard.shard_id,
                            shard.shard_index,
                            shard.digest,
                            shard.size_bytes,
                            self.ledger.object_relpath(shard.digest),
                            _safe_json(list(shard.layer_names)),
                            1 if row["reuse_hit"] else 0,
                            row["reuse_from_version"],
                            row["reuse_basis"] or "written",
                        ),
                    )
                    connection.execute(
                        "INSERT OR IGNORE INTO objects(digest, relpath, size_bytes, created_clock_ms) "
                        "VALUES(?, ?, ?, ?)",
                        (
                            shard.digest,
                            self.ledger.object_relpath(shard.digest),
                            shard.size_bytes,
                            end_clock,
                        ),
                    )
                for layer in content.layers:
                    connection.execute(
                        "INSERT INTO parameter_digests(checkpoint, version, layer_name, "
                        "digest, revision, shape_json, dtype, nbytes, shard_id, shard_index, "
                        "offset_in_shard) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            plan.checkpoint,
                            version,
                            layer.name,
                            layer.param_digest,
                            layer.revision,
                            _safe_json(list(layer.shape)),
                            layer.dtype,
                            layer.nbytes,
                            content.shards[shard_for_layer[layer.name]].shard_id,
                            shard_for_layer[layer.name],
                            layer_offsets[layer.name],
                        ),
                    )
                for shard in content.shards:
                    row = rows[shard.shard_id]
                    connection.execute(
                        "INSERT INTO reuse_records(checkpoint, version, shard_id, "
                        "source_version, hit, basis, digest) VALUES(?, ?, ?, ?, ?, ?, ?)",
                        (
                            plan.checkpoint,
                            version,
                            shard.shard_id,
                            row["reuse_from_version"],
                            1 if row["reuse_hit"] else 0,
                            row["reuse_basis"] or "written",
                            shard.digest,
                        ),
                    )
                connection.execute(
                    "INSERT INTO pointers(checkpoint, version, clock_ms) VALUES(?, ?, ?) "
                    "ON CONFLICT(checkpoint) DO UPDATE SET version = excluded.version, "
                    "clock_ms = excluded.clock_ms",
                    (plan.checkpoint, version, end_clock),
                )
                connection.execute(
                    "UPDATE rounds SET status = 'complete', end_clock_ms = ?, "
                    "manifest_json = ?, manifest_digest = ? WHERE round_id = ?",
                    (end_clock, manifest_json, manifest_digest, round_id),
                )
                self.ledger.set_clock_in_transaction(connection, end_clock)
                connection.execute(
                    "INSERT INTO events(checkpoint, version, round_id, stage, clock_ms, details_json) "
                    "VALUES(?, ?, ?, 'checkpoint-committed', ?, ?)",
                    (
                        plan.checkpoint,
                        version,
                        round_id,
                        end_clock,
                        _safe_json(
                            {
                                "manifest_digest": manifest_digest,
                                "parameter_digest": content.parameter_digest,
                                "base_version": base_version,
                            }
                        ),
                    ),
                )
                result = {
                    "checkpoint": plan.checkpoint,
                    "version": version,
                    "plan_digest": plan.digest(),
                    "manifest_json": manifest_json,
                    "manifest_digest": manifest_digest,
                    "parameter_digest": content.parameter_digest,
                    "base_version": base_version,
                    "round_id": round_id,
                    "clock_ms": start_clock,
                    "committed_clock_ms": end_clock,
                }
        atomic_write_json(
            self.ledger.manifest_path(plan.checkpoint, version), manifest
        )
        with self.ledger.connection() as connection:
            pointer = connection.execute(
                "SELECT version FROM pointers WHERE checkpoint = ?", (plan.checkpoint,)
            ).fetchone()
            final_rows = [
                dict(row)
                for row in connection.execute(
                    "SELECT shard_id, status, reuse_hit, reuse_from_version, "
                    "reuse_basis FROM round_shards "
                    "WHERE round_id = ? ORDER BY shard_index",
                    (round_id,),
                ).fetchall()
            ]
        final_by_shard = {row["shard_id"]: row for row in final_rows}
        committed = [
            row["shard_id"]
            for row in final_rows
            if row["status"] == "committed" and not bool(row["reuse_hit"])
        ]
        reused = [
            row["shard_id"]
            for row in final_rows
            if row["status"] == "reused" or bool(row["reuse_hit"])
        ]
        return {
            "ok": True,
            "idempotent": False,
            "checkpoint": plan.checkpoint,
            "version": version,
            "mode": "incremental" if base_version is not None else "full",
            "plan_digest": plan.digest(),
            "manifest_digest": manifest_digest,
            "parameter_digest": content.parameter_digest,
            "round_id": round_id,
            "clock_ms": end_clock,
            "pointer_version": int(pointer["version"]),
            "shards": [
                {
                    "shard_id": shard.shard_id,
                    "digest": shard.digest,
                    "size_bytes": shard.size_bytes,
                    "reuse": {
                        "hit": bool(final_by_shard[shard.shard_id]["reuse_hit"]),
                        "from_version": final_by_shard[shard.shard_id][
                            "reuse_from_version"
                        ],
                        "basis": final_by_shard[shard.shard_id]["reuse_basis"]
                        or "written",
                    },
                }
                for shard in content.shards
            ],
            "outcomes": {
                "committed": committed,
                "reused": reused,
                "budget_unstarted": [],
                "started_failed": [],
                "abandoned": [],
            },
        }

    def _idempotent_result(
        self, checkpoint_row: Mapping[str, Any], reason: str
    ) -> Dict[str, Any]:
        return {
            "ok": True,
            "idempotent": True,
            "idempotent_reason": reason,
            "checkpoint": checkpoint_row["checkpoint"],
            "version": int(checkpoint_row["version"]),
            "plan_digest": checkpoint_row["plan_digest"],
            "manifest_digest": checkpoint_row["manifest_digest"],
            "parameter_digest": checkpoint_row["parameter_digest"],
            "round_id": int(checkpoint_row["round_id"]),
            "clock_ms": int(checkpoint_row["committed_clock_ms"]),
        }

    def _export_manifest_if_needed(self, checkpoint_row: Mapping[str, Any]) -> None:
        manifest = json.loads(checkpoint_row["manifest_json"])
        target = self.ledger.manifest_path(
            checkpoint_row["checkpoint"], int(checkpoint_row["version"])
        )
        if not target.exists() or digest_file(target) != digest_bytes(
            _safe_json(manifest).encode("utf-8") + b"\n"
        ):
            atomic_write_json(target, manifest)

    def restore(
        self,
        checkpoint: Optional[str] = None,
        version: Optional[int] = None,
        clock_ms: Optional[int] = None,
        materialize: Optional[PathLike] = None,
    ) -> Dict[str, Any]:
        if not self.root.exists():
            raise CheckpointNotFoundError(
                "checkpoint workspace does not exist", root=str(self.root)
            )
        with self.ledger.file_lock(exclusive=False):
            self._require_ledger()
            checkpoint_name = checkpoint
            if checkpoint_name is None:
                names = sorted(
                    {row["checkpoint"] for row in self.ledger.list_checkpoints()}
                )
                if len(names) != 1:
                    raise UsageError(
                        "--checkpoint is required when the ledger has zero or multiple checkpoints",
                        checkpoints=names,
                    )
                checkpoint_name = names[0]
            row = self.ledger.get_checkpoint(checkpoint_name, version)
            manifest = row["manifest"]
            manifest_file = self.ledger.manifest_path(
                checkpoint_name, int(row["version"])
            )
            if manifest_file.exists():
                try:
                    file_manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError) as exc:
                    raise ManifestDigestMismatchError(
                        "exported checkpoint manifest cannot be parsed",
                        path=str(manifest_file),
                    ) from exc
                file_digest = digest_json(file_manifest)
                if file_digest != row["manifest_digest"]:
                    raise ManifestDigestMismatchError(
                        "exported checkpoint manifest differs from the ledger",
                        path=str(manifest_file),
                        expected=row["manifest_digest"],
                        actual=file_digest,
                    )
                manifest = file_manifest
            manifest_digest = digest_json(manifest)
            if manifest_digest != row["manifest_digest"]:
                raise ManifestDigestMismatchError(
                    "stored checkpoint manifest digest does not match its content",
                    expected=row["manifest_digest"],
                    actual=manifest_digest,
                    checkpoint=checkpoint_name,
                    version=row["version"],
                )
            self._verify_stored_metadata(
                checkpoint_name, int(row["version"]), row, manifest
            )
            tolerance = int(
                manifest.get("strategy", {}).get("clock_tolerance_ms", 0)
            )
            self._check_restore_clock(clock_ms, tolerance)
            verified = verify_checkpoint_manifest(
                self.root,
                manifest,
                self.ledger.get_checkpoint_shards(checkpoint_name, int(row["version"])),
            )
            result = verified.summary()
            result["pointer"] = self._pointer_matches(
                checkpoint_name, int(row["version"])
            )
            if materialize is not None:
                target = Path(materialize)
                if target.exists():
                    raise UsageError(
                        "restore materialization target already exists",
                        path=str(target),
                    )
                self._materialize_verified(verified, target)
                result["materialized"] = str(target)
                result["materialized_layers"] = len(verified.layers)
            return result

    def _materialize_verified(self, verified: VerifiedRestore, target: Path) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        stage = Path(
            tempfile.mkdtemp(
                prefix="." + target.name + ".restore.tmp.",
                dir=str(target.parent),
            )
        )
        try:
            index_layers: List[Dict[str, Any]] = []
            for position, layer in enumerate(verified.layers):
                safe_name = (
                    layer.name.replace("/", "__")
                    .replace("\\", "__")
                    .replace("..", "_")
                )
                filename = "{0:04d}-{1}.bin".format(position, safe_name)
                atomic_write_bytes(stage / filename, layer.payload)
                index_layers.append(
                    {
                        "name": layer.name,
                        "file": filename,
                        "shape": list(layer.shape),
                        "dtype": layer.dtype,
                        "revision": layer.revision,
                        "nbytes": layer.nbytes,
                        "param_digest": layer.param_digest,
                    }
                )
            atomic_write_json(
                stage / "index.json",
                {
                    "schema": "ckpt-restore-index/v1",
                    "checkpoint": verified.checkpoint,
                    "version": verified.version,
                    "parameter_digest": verified.parameter_digest,
                    "stitched_digest": verified.stitched_digest,
                    "independent_digest": verified.independent_digest,
                    "layers": index_layers,
                },
            )
            os.replace(stage, target)
            fsync_dir(target.parent)
        except BaseException:
            shutil.rmtree(stage, ignore_errors=True)
            raise

    def status(self, checkpoint: Optional[str] = None) -> Dict[str, Any]:
        if not self.ledger.db_path.exists():
            return {
                "ok": True,
                "clock_watermark_ms": None,
                "pointers": [],
                "checkpoints": [],
                "rounds": [],
                "round_shards": [],
                "objects": {"count": 0, "bytes": 0},
                "orphan_objects": [],
            }
        with self.ledger.file_lock(exclusive=False):
            result = self.ledger.status()
            if checkpoint:
                result["pointers"] = [
                    row for row in result["pointers"] if row["checkpoint"] == checkpoint
                ]
                result["checkpoints"] = [
                    row
                    for row in result["checkpoints"]
                    if row["checkpoint"] == checkpoint
                ]
                result["rounds"] = [
                    row for row in result["rounds"] if row["checkpoint"] == checkpoint
                ]
                round_ids = {
                    int(row["round_id"]) for row in result["rounds"]
                }
                result["round_shards"] = [
                    row
                    for row in result["round_shards"]
                    if int(row["round_id"]) in round_ids
                ]
            result["orphan_objects"] = self._find_orphan_objects()
            return result

    def verify(self, checkpoint: Optional[str] = None) -> Dict[str, Any]:
        if not self.root.exists():
            raise CheckpointNotFoundError(
                "checkpoint workspace does not exist", root=str(self.root)
            )
        with self.ledger.file_lock(exclusive=False):
            self._require_ledger()
            checkpoint_rows = self.ledger.list_checkpoints(checkpoint)
            if not checkpoint_rows:
                raise CheckpointNotFoundError(
                    "no committed checkpoints are available to verify",
                    checkpoint=checkpoint,
                )
            results: List[Dict[str, Any]] = []
            failures: List[Dict[str, Any]] = []
            for row in checkpoint_rows:
                name = row["checkpoint"]
                version = int(row["version"])
                try:
                    manifest = json.loads(row["manifest_json"])
                    manifest_digest = digest_json(manifest)
                    if manifest_digest != row["manifest_digest"]:
                        raise ManifestDigestMismatchError(
                            "stored checkpoint manifest digest does not match its content",
                            expected=row["manifest_digest"],
                            actual=manifest_digest,
                        )
                    self._verify_stored_metadata(name, version, row, manifest)
                    manifest_file = self.ledger.manifest_path(name, version)
                    if manifest_file.exists():
                        file_manifest = json.loads(
                            manifest_file.read_text(encoding="utf-8")
                        )
                        if digest_json(file_manifest) != row["manifest_digest"]:
                            raise ManifestDigestMismatchError(
                                "exported checkpoint manifest differs from the ledger",
                                path=str(manifest_file),
                                expected=row["manifest_digest"],
                                actual=digest_json(file_manifest),
                            )
                    verified = verify_checkpoint_manifest(
                        self.root,
                        manifest,
                        self.ledger.get_checkpoint_shards(name, version),
                    )
                    self._verify_reuse_records(name, version)
                    item = verified.summary()
                    item["manifest_digest"] = manifest_digest
                    item["full_regeneration_match"] = True
                    results.append(item)
                except Exception as exc:
                    if hasattr(exc, "to_dict"):
                        error = exc.to_dict()["error"]
                    else:
                        error = {
                            "code": "E_INTERNAL",
                            "message": str(exc),
                            "details": {},
                        }
                    failure = {
                        "checkpoint": name,
                        "version": version,
                        "error": error,
                    }
                    failures.append(failure)
                    results.append(
                        {
                            "ok": False,
                            "checkpoint": name,
                            "version": version,
                            "error": error,
                        }
                    )
            orphan_objects = self._find_orphan_objects()
            if orphan_objects:
                failures.append(
                    {
                        "checkpoint": checkpoint,
                        "version": None,
                        "error": {
                            "code": "E_ORPHAN_OBJECT",
                            "message": "unreferenced shard objects exist",
                            "details": {"digests": orphan_objects},
                        },
                    }
                )
            if failures:
                raise VerificationFailedError(
                    "one or more checkpoints failed verification",
                    failures=failures,
                    orphan_objects=orphan_objects,
                )
            return {
                "ok": True,
                "verified_checkpoints": len(results),
                "checkpoints": results,
                "orphan_objects": [],
            }

    def _verify_reuse_records(self, checkpoint: str, version: int) -> None:
        rows = self.ledger.get_checkpoint_shards(checkpoint, version)
        for row in rows:
            if not row["reuse_hit"]:
                continue
            if not row["reuse_basis"]:
                raise ReuseRecordInvalidError(
                    "reuse hit is missing its basis",
                    shard_id=row["shard_id"],
                )
            source_version = row["reuse_from_version"]
            if source_version is None:
                continue
            sources = {
                item["shard_id"]: item
                for item in self.ledger.get_checkpoint_shards(
                    checkpoint, int(source_version)
                )
            }
            source = sources.get(row["shard_id"])
            if source is None or source["digest"] != row["digest"]:
                raise ReuseRecordInvalidError(
                    "reuse record does not match its source checkpoint shard",
                    checkpoint=checkpoint,
                    version=version,
                    shard_id=row["shard_id"],
                    source_version=source_version,
                )

    def _verify_stored_metadata(
        self,
        checkpoint: str,
        version: int,
        checkpoint_row: Mapping[str, Any],
        manifest: Mapping[str, Any],
    ) -> None:
        if checkpoint_row["plan_digest"] != manifest["plan_digest"]:
            raise PlanDigestMismatchError(
                "checkpoint row plan digest differs from the manifest",
                checkpoint=checkpoint,
                version=version,
                expected=checkpoint_row["plan_digest"],
                actual=manifest["plan_digest"],
            )
        if checkpoint_row["parameter_digest"] != manifest["parameter_digest"]:
            raise ParameterDigestMismatchError(
                "checkpoint row parameter digest differs from the manifest",
                checkpoint=checkpoint,
                version=version,
                expected=checkpoint_row["parameter_digest"],
                actual=manifest["parameter_digest"],
            )
        shard_rows = {
            row["shard_id"]: row
            for row in self.ledger.get_checkpoint_shards(checkpoint, version)
        }
        for shard in manifest["shards"]:
            shard_id = shard["shard_id"]
            row = shard_rows.get(shard_id)
            if row is None:
                raise StitchOrderMismatchError(
                    "manifest shard is absent from the committed shard table",
                    checkpoint=checkpoint,
                    version=version,
                    shard_id=shard_id,
                )
            reuse = shard.get("reuse", {})
            expected_reuse = {
                "hit": bool(row["reuse_hit"]),
                "from_version": row["reuse_from_version"],
                "basis": row["reuse_basis"] or "written",
            }
            actual_reuse = {
                "hit": bool(reuse.get("hit")),
                "from_version": reuse.get("from_version"),
                "basis": reuse.get("basis") or "written",
            }
            if expected_reuse != actual_reuse:
                raise ReuseRecordInvalidError(
                    "manifest reuse record differs from committed shard row",
                    checkpoint=checkpoint,
                    version=version,
                    shard_id=shard_id,
                    expected=expected_reuse,
                    actual=actual_reuse,
                )
        with self.ledger.connection() as connection:
            parameter_rows = {
                row["layer_name"]: dict(row)
                for row in connection.execute(
                    "SELECT * FROM parameter_digests WHERE checkpoint = ? AND version = ?",
                    (checkpoint, version),
                ).fetchall()
            }
        manifest_layers = {layer["name"]: layer for layer in manifest["layers"]}
        if set(parameter_rows) != set(manifest_layers):
            raise ParameterDigestMismatchError(
                "committed parameter digest rows do not cover the manifest layer set",
                checkpoint=checkpoint,
                version=version,
                expected=sorted(manifest_layers),
                actual=sorted(parameter_rows),
            )
        for name, layer in manifest_layers.items():
            row = parameter_rows[name]
            expected = {
                "digest": layer["param_digest"],
                "revision": layer["revision"],
                "shape": layer["shape"],
                "dtype": layer["dtype"],
                "nbytes": layer["nbytes"],
                "shard_id": layer["shard_id"],
                "shard_index": layer["shard_index"],
                "offset_in_shard": layer["offset_in_shard"],
            }
            actual = {
                "digest": row["digest"],
                "revision": row["revision"],
                "shape": json.loads(row["shape_json"]),
                "dtype": row["dtype"],
                "nbytes": row["nbytes"],
                "shard_id": row["shard_id"],
                "shard_index": row["shard_index"],
                "offset_in_shard": row["offset_in_shard"],
            }
            if expected != actual:
                raise ParameterDigestMismatchError(
                    "committed parameter digest row differs from the manifest",
                    checkpoint=checkpoint,
                    version=version,
                    layer=name,
                    expected=expected,
                    actual=actual,
                )

    def _pointer_matches(self, checkpoint: str, version: int) -> bool:
        pointer = self.ledger.get_pointer(checkpoint)
        return bool(pointer and int(pointer["version"]) == version)

    def _expected_digests(self) -> set:
        with self.ledger.connection() as connection:
            rows = connection.execute(
                "SELECT digest FROM checkpoint_shards UNION "
                "SELECT digest FROM round_shards WHERE round_id IN ("
                "SELECT round_id FROM rounds WHERE status != 'complete')"
            ).fetchall()
            return {str(row["digest"]) for row in rows}

    def _find_orphan_objects(self) -> List[str]:
        if not self.ledger.objects_dir.exists():
            return []
        expected = self._expected_digests()
        orphans: List[str] = []
        for path in self.ledger.objects_dir.rglob("*.shard"):
            digest = path.name[: -len(".shard")]
            if digest not in expected:
                orphans.append(digest)
        return sorted(orphans)

    def _cleanup_orphans(self) -> None:
        for entry in self.ledger.tmp_dir.iterdir() if self.ledger.tmp_dir.exists() else []:
            try:
                if entry.is_dir():
                    for child in entry.iterdir():
                        child.unlink()
                    entry.rmdir()
                else:
                    entry.unlink()
            except OSError:
                pass
        expected = self._expected_digests()
        with self.ledger.connection() as connection:
            object_rows = connection.execute("SELECT digest FROM objects").fetchall()
        for row in object_rows:
            digest = str(row["digest"])
            if digest not in expected:
                try:
                    self.ledger.object_path(digest).unlink()
                except OSError:
                    pass
                with self.ledger.transaction() as connection:
                    connection.execute("DELETE FROM objects WHERE digest = ?", (digest,))
        if self.ledger.objects_dir.exists():
            for path in self.ledger.objects_dir.rglob("*"):
                if not path.is_file():
                    continue
                if ".tmp." in path.name:
                    try:
                        path.unlink()
                    except OSError:
                        pass
                    continue
                if path.suffix != ".shard":
                    continue
                digest = path.name[: -len(".shard")]
                if digest not in expected:
                    try:
                        path.unlink()
                    except OSError:
                        pass
        self._cleanup_manifests()

    def _cleanup_manifests(self) -> None:
        if not self.ledger.manifests_dir.exists():
            return
        known = {
            (
                row["checkpoint"].replace("/", "_").replace("\\", "_"),
                int(row["version"]),
            )
            for row in self.ledger.list_checkpoints()
        }
        for path in self.ledger.manifests_dir.rglob("*.json"):
            try:
                version = int(path.stem)
                checkpoint = path.parent.name
            except ValueError:
                continue
            if (checkpoint, version) not in known:
                try:
                    path.unlink()
                except OSError:
                    pass
        for path in self.ledger.manifests_dir.rglob("*"):
            if path.is_file() and ".tmp." in path.name:
                try:
                    path.unlink()
                except OSError:
                    pass
