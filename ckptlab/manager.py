from __future__ import annotations

import copy
import json
import os
import re
from collections import Counter
from pathlib import Path
from typing import Any

import numpy as np

from .derivation import (
    CheckpointArtifacts,
    ShardArtifact,
    build_checkpoint_artifacts,
    build_manifest,
    canonical_manifest_bytes,
)
from .errors import BusinessError, fail
from .ledger import (
    Ledger,
    evaluate_preflight,
    read_snapshot_status,
    read_state,
)
from .plans import (
    CrashPlan,
    SavePlan,
    ShardPolicy,
    TrainingPlan,
    content_digest,
    validate_crash_plan,
    validate_manifest_shape,
    validate_shard_policy,
    validate_training_plan,
)
from .recovery import reconstruct_manifest
from .scheduler import ShardOutcome, schedule_shards
from .store import ObjectStore
from .util import canonical_json_bytes, pretty_json, sha256_hex


def _outcome_dict(outcome: ShardOutcome) -> dict[str, Any]:
    return {
        "shard_id": outcome.shard_id,
        "outcome": outcome.outcome,
        "reason": outcome.reason,
        "started": outcome.started,
        "logical_clock_ms": outcome.logical_clock_ms,
        "logical_cost_ms": outcome.logical_cost_ms,
        "detail": outcome.detail,
    }


def _outcome_summary(outcomes: list[dict[str, Any]]) -> dict[str, Any]:
    counts = Counter(item["outcome"] for item in outcomes)
    budget_unstarted = sum(
        1
        for item in outcomes
        if item["outcome"] == "not_started"
        and item.get("reason") == "logical_budget_exhausted"
    )
    failed_after_start = sum(
        1
        for item in outcomes
        if item["outcome"] == "failed" and bool(item.get("started"))
    )
    actively_abandoned = sum(
        1 for item in outcomes if item["outcome"] == "abandoned"
    )
    return {
        "total": len(outcomes),
        "written": counts.get("written", 0),
        "hit": counts.get("hit", 0),
        "not_started": counts.get("not_started", 0),
        "failed": counts.get("failed", 0),
        "abandoned": counts.get("abandoned", 0),
        "budget_unstarted": budget_unstarted,
        "failed_after_start": failed_after_start,
        "actively_abandoned": actively_abandoned,
    }


def _clean_build_row(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    cleaned = dict(row)
    cleaned.pop("plan_json", None)
    cleaned.pop("policy_json", None)
    cleaned.pop("manifest_json", None)
    for key in ("abandon_json", "failure_json"):
        if key in cleaned:
            cleaned[key] = json.loads(cleaned[key])
    return cleaned


class CheckpointManager:
    def __init__(
        self,
        db_path: str | os.PathLike[str],
        store_path: str | os.PathLike[str],
    ):
        self.db_path = Path(db_path)
        self.store_path = Path(store_path)
        self.store = ObjectStore(self.store_path)

    def save(
        self,
        training_plan_data: dict[str, Any],
        shard_policy_data: dict[str, Any],
        crash_plan_data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        plan = validate_training_plan(training_plan_data)
        policy = validate_shard_policy(shard_policy_data, plan)
        crash = validate_crash_plan(crash_plan_data)
        artifacts = build_checkpoint_artifacts(plan, policy)
        content_hash = content_digest(plan, policy)

        state = read_state(self.db_path)
        preflight = evaluate_preflight(
            state,
            version=plan.save.version,
            content_digest=content_hash,
            logical_clock_ms=plan.save.logical_clock_ms,
            tolerance_ms=plan.save.logical_clock_tolerance_ms,
        )
        if preflight["action"] == "idempotent":
            return self._idempotent_result("save", plan.save.version)

        with self.store.checkpoint_lock():
            with Ledger(self.db_path) as ledger:
                locked_preflight = ledger.preflight(
                    version=plan.save.version,
                    content_digest=content_hash,
                    logical_clock_ms=plan.save.logical_clock_ms,
                    tolerance_ms=plan.save.logical_clock_tolerance_ms,
                )
                if locked_preflight["action"] == "idempotent":
                    return self._idempotent_result("save", plan.save.version)
                return self._execute_build(
                    ledger=ledger,
                    plan=plan,
                    policy=policy,
                    artifacts=artifacts,
                    crash=crash,
                    preflight=locked_preflight,
                    command="save",
                )

    def resume(
        self,
        training_plan_data: dict[str, Any] | None = None,
        shard_policy_data: dict[str, Any] | None = None,
        crash_plan_data: dict[str, Any] | None = None,
        schedule_overrides: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if (training_plan_data is None) != (shard_policy_data is None):
            fail(
                "TRAINING_PLAN_INVALID",
                "resume 的 plan 与 shards 必须同时提供或同时省略",
            )
        if training_plan_data is None:
            state = read_state(self.db_path)
            incomplete = [
                row for row in state.get("builds", {}).values()
                if row["status"] != "committed"
            ]
            if not incomplete:
                fail("CHECKPOINT_NOT_FOUND", "没有需要恢复的未完成落盘轮次")
            build = sorted(incomplete, key=lambda row: row["version"])[-1]
            training_plan_data = json.loads(build["plan_json"])
            shard_policy_data = json.loads(build["policy_json"])
        else:
            training_plan_data = copy.deepcopy(training_plan_data)

        if schedule_overrides and training_plan_data is not None:
            save_section = training_plan_data.setdefault("save", {})
            if not isinstance(save_section, dict):
                fail("SAVE_PLAN_INVALID", "save 必须是对象")
            aliases = {
                "logical_clock_ms": "logical_clock_ms",
                "max_concurrency": "max_concurrency",
                "logical_time_budget_ms": "logical_time_budget_ms",
                "incremental": "incremental",
                "logical_clock_tolerance_ms": "logical_clock_tolerance_ms",
            }
            for source, target in aliases.items():
                value = schedule_overrides.get(source)
                if value is not None:
                    save_section[target] = value

        plan = validate_training_plan(training_plan_data)
        policy = validate_shard_policy(shard_policy_data, plan)
        crash = validate_crash_plan(crash_plan_data)
        artifacts = build_checkpoint_artifacts(plan, policy)
        content_hash = content_digest(plan, policy)

        state = read_state(self.db_path)
        preflight = evaluate_preflight(
            state,
            version=plan.save.version,
            content_digest=content_hash,
            logical_clock_ms=plan.save.logical_clock_ms,
            tolerance_ms=plan.save.logical_clock_tolerance_ms,
        )
        if preflight["action"] == "idempotent":
            return self._idempotent_result("resume", plan.save.version)
        if preflight["action"] == "create":
            fail(
                "CHECKPOINT_NOT_FOUND",
                "指定版本没有未完成落盘轮次可恢复",
                version=plan.save.version,
            )

        with self.store.checkpoint_lock():
            with Ledger(self.db_path) as ledger:
                locked_preflight = ledger.preflight(
                    version=plan.save.version,
                    content_digest=content_hash,
                    logical_clock_ms=plan.save.logical_clock_ms,
                    tolerance_ms=plan.save.logical_clock_tolerance_ms,
                )
                if locked_preflight["action"] == "idempotent":
                    return self._idempotent_result("resume", plan.save.version)
                if locked_preflight["action"] == "create":
                    fail(
                        "CHECKPOINT_NOT_FOUND",
                        "指定版本没有未完成落盘轮次可恢复",
                        version=plan.save.version,
                    )
                return self._execute_build(
                    ledger=ledger,
                    plan=plan,
                    policy=policy,
                    artifacts=artifacts,
                    crash=crash,
                    preflight=locked_preflight,
                    command="resume",
                )

    def _execute_build(
        self,
        *,
        ledger: Ledger,
        plan: TrainingPlan,
        policy: ShardPolicy,
        artifacts: CheckpointArtifacts,
        crash: CrashPlan,
        preflight: dict[str, Any],
        command: str,
    ) -> dict[str, Any]:
        version = plan.save.version
        if preflight["action"] == "create":
            ledger.begin_build(
                version=version,
                content_digest=artifacts.content_digest,
                initial_clock_ms=plan.save.logical_clock_ms,
                plan_json=canonical_json_bytes(plan.as_dict()).decode("utf-8"),
                policy_json=canonical_json_bytes(policy.as_dict()).decode("utf-8"),
            )
            build = ledger.get_build(version)
        else:
            build = preflight["build"]
        if build is None:
            fail("CHECKPOINT_NOT_FOUND", "落盘轮次不存在", version=version)

        start_clock = int(
            preflight.get(
                "effective_start_clock_ms",
                max(plan.save.logical_clock_ms, int(build["update_clock_ms"])),
            )
        )
        round_no = int(build["last_round"]) + 1
        removed_temps = self.store.cleanup_version_temp_files(version)
        ledger.audit(
            event="temp_cleanup",
            version=version,
            logical_clock_ms=start_clock,
            detail={"removed": removed_temps},
        )
        ledger.start_round(
            version=version, round_no=round_no, start_clock_ms=start_clock
        )

        previous_manifest = self._previous_manifest(ledger, plan, artifacts)
        previous_shards = (
            {item["shard_id"]: item for item in previous_manifest["shards"]}
            if previous_manifest is not None
            else {}
        )
        existing_commits = ledger.get_shard_commits(version)
        hit_rows: list[dict[str, Any]] = []
        hit_outcomes: list[ShardOutcome] = []
        pending: list[ShardArtifact] = []

        for shard in artifacts.shards:
            existing = existing_commits.get(shard.shard_id)
            if existing is not None and existing["digest"] != shard.digest:
                fail(
                    "VERSION_CONFLICT",
                    "已提交分片摘要与确定性重建不一致",
                    shard_id=shard.shard_id,
                    existing_digest=existing["digest"],
                    expected_digest=shard.digest,
                )
            if existing is not None and self.store.has_valid_object(shard.digest):
                reason = "resume_committed_shard"
                source_kind = "resume_shard_commit"
                source_version = version
            elif self.store.has_valid_object(shard.digest):
                previous = previous_shards.get(shard.shard_id)
                if (
                    plan.save.incremental
                    and previous is not None
                    and previous.get("digest") == shard.digest
                ):
                    reason = "unchanged_shard_digest"
                    source_kind = "incremental_manifest"
                    source_version = int(previous_manifest["version"])
                else:
                    reason = "content_address_object_present"
                    source_kind = "content_address"
                    source_version = (
                        int(previous_manifest["version"])
                        if previous_manifest is not None
                        else version
                    )
            else:
                pending.append(shard)
                continue

            ledger.record_hit(
                version=version,
                shard_id=shard.shard_id,
                digest=shard.digest,
                size=shard.size,
                relpath=shard.relpath,
                source_version=source_version,
                source_kind=source_kind,
                reason=reason,
                logical_clock_ms=start_clock,
                round_no=round_no,
            )
            hit = {
                "shard_id": shard.shard_id,
                "digest": shard.digest,
                "source_version": source_version,
                "source_kind": source_kind,
                "reason": reason,
            }
            hit_rows.append(hit)
            hit_outcomes.append(
                ShardOutcome(
                    shard_id=shard.shard_id,
                    outcome="hit",
                    reason=reason,
                    started=False,
                    logical_clock_ms=start_clock,
                    logical_cost_ms=0,
                    detail={
                        "source_version": source_version,
                        "source_kind": source_kind,
                        "digest": shard.digest,
                    },
                )
            )

        written_count = 0

        def materialize(shard: ShardArtifact) -> bool:
            if crash.enabled and shard.shard_id in set(crash.fail_shards):
                raise OSError(f"injected write failure for {shard.shard_id}")
            return self.store.write_object(
                shard.data,
                shard.digest,
                version=version,
            )

        def on_outcome(outcome: ShardOutcome) -> None:
            nonlocal written_count
            if outcome.outcome == "written":
                ledger.record_shard_commit(
                    version=version,
                    shard_id=outcome.shard_id,
                    digest=next(
                        shard.digest
                        for shard in artifacts.shards
                        if shard.shard_id == outcome.shard_id
                    ),
                    size=next(
                        shard.size
                        for shard in artifacts.shards
                        if shard.shard_id == outcome.shard_id
                    ),
                    relpath=next(
                        shard.relpath
                        for shard in artifacts.shards
                        if shard.shard_id == outcome.shard_id
                    ),
                    logical_cost_ms=outcome.logical_cost_ms,
                    logical_clock_ms=outcome.logical_clock_ms,
                    round_no=round_no,
                )
                written_count += 1
                if (
                    crash.enabled
                    and crash.point == "after_shard_commit"
                    and written_count >= max(1, crash.after_shards)
                ):
                    os._exit(crash.exit_code)
            else:
                ledger.record_outcome(
                    version=version,
                    shard_id=outcome.shard_id,
                    outcome=outcome.outcome,
                    reason=outcome.reason or "unknown",
                    started=outcome.started,
                    detail=outcome.detail,
                    logical_clock_ms=outcome.logical_clock_ms,
                    logical_cost_ms=outcome.logical_cost_ms,
                    round_no=round_no,
                )

        schedule = schedule_shards(
            pending,
            start_clock_ms=start_clock,
            max_concurrency=plan.save.max_concurrency,
            logical_budget_ms=plan.save.logical_time_budget_ms,
            materialize=materialize,
            on_outcome=on_outcome,
            prior_hits=len(hit_outcomes),
            fail_shards=set(crash.fail_shards),
            cancel_after_started=crash.cancel_after_started if crash.enabled else None,
        )
        all_outcomes = sorted(
            hit_outcomes + list(schedule.outcomes),
            key=lambda item: item.shard_id,
        )
        final_clock = max(start_clock, schedule.logical_clock_ms)

        if not schedule.complete:
            abandon = [
                {
                    "shard_id": item.shard_id,
                    "reason": item.reason,
                    "disposition": item.detail.get("disposition"),
                }
                for item in all_outcomes
                if item.outcome in {"not_started", "abandoned"}
            ]
            failures = [
                {
                    "shard_id": item.shard_id,
                    "reason": item.reason,
                    "detail": item.detail,
                }
                for item in all_outcomes
                if item.outcome == "failed"
            ]
            status = "failed" if failures else "paused"
            ledger.finish_round(
                version=version,
                status=status,
                logical_clock_ms=final_clock,
                abandon=abandon,
                failures=failures,
                round_no=round_no,
            )
            snapshot = read_snapshot_status(self.db_path, version)
            result = self._result_from_snapshot(
                snapshot,
                command=command,
                version=version,
                committed=False,
                idempotent=False,
            )
            result["recovery"] = {
                "resume_required": True,
                "removed_temp_files": removed_temps,
                "round_no": round_no,
            }
            return result

        if crash.enabled and crash.point == "after_all_shards":
            os._exit(crash.exit_code)
        if crash.enabled and crash.point == "before_manifest_commit":
            os._exit(crash.exit_code)

        manifest = build_manifest(plan, policy, artifacts)
        manifest_bytes = canonical_manifest_bytes(manifest)
        manifest_digest = sha256_hex(manifest_bytes)
        manifest_relpath = f"manifests/{manifest_digest}.json"

        def before_manifest_rename(tmp_path: Path) -> None:
            if crash.enabled and crash.point == "during_manifest_write":
                with tmp_path.open("r+b") as handle:
                    handle.truncate(max(1, len(manifest_bytes) // 2))
                    handle.flush()
                    os.fsync(handle.fileno())
                os._exit(crash.exit_code)

        self.store.write_manifest(
            manifest_bytes,
            manifest_digest,
            version=version,
            before_rename=before_manifest_rename,
        )
        if crash.enabled and crash.point == "after_manifest_file":
            os._exit(crash.exit_code)

        expected_shards = {
            shard.shard_id: {"digest": shard.digest} for shard in artifacts.shards
        }
        ledger.commit_manifest(
            version=version,
            manifest_json=canonical_json_bytes(manifest).decode("utf-8"),
            manifest_digest=manifest_digest,
            manifest_relpath=manifest_relpath,
            logical_clock_ms=final_clock,
            expected_shards=expected_shards,
            layers=[layer.manifest_entry() for layer in artifacts.layers],
            hits=hit_rows,
            round_no=round_no,
        )
        ledger.audit(
            event="resume_replay_summary",
            version=version,
            logical_clock_ms=final_clock,
            detail={
                "round_no": round_no,
                "hit_shards": [item["shard_id"] for item in hit_rows],
                "written_shards": [
                    item.shard_id
                    for item in all_outcomes
                    if item.outcome == "written"
                ],
                "removed_temp_files": removed_temps,
            },
        )
        snapshot = read_snapshot_status(self.db_path, version)
        result = self._result_from_snapshot(
            snapshot,
            command=command,
            version=version,
            committed=True,
            idempotent=False,
        )
        result["manifest_digest"] = manifest_digest
        result["manifest_relpath"] = manifest_relpath
        result["recovery"] = {
            "resume_required": False,
            "removed_temp_files": removed_temps,
            "round_no": round_no,
        }
        return result

    def _previous_manifest(
        self,
        ledger: Ledger,
        plan: TrainingPlan,
        artifacts: CheckpointArtifacts,
    ) -> dict[str, Any] | None:
        del artifacts
        if not plan.save.incremental:
            return None
        state = ledger.state()
        pointer = state.get("pointer")
        if pointer is None or pointer.get("version") is None:
            return None
        if int(pointer["version"]) >= plan.save.version:
            return None
        previous_build = ledger.get_build(int(pointer["version"]))
        if previous_build is None or previous_build["status"] != "committed":
            return None
        return self._read_manifest_by_digest(
            previous_build["manifest_digest"],
            expected_version=int(pointer["version"]),
        )

    def _idempotent_result(self, command: str, version: int) -> dict[str, Any]:
        snapshot = read_snapshot_status(self.db_path, version)
        result = self._result_from_snapshot(
            snapshot,
            command=command,
            version=version,
            committed=True,
            idempotent=True,
        )
        result["recovery"] = {"resume_required": False, "reason": "already_committed"}
        return result

    def _result_from_snapshot(
        self,
        snapshot: dict[str, Any],
        *,
        command: str,
        version: int,
        committed: bool,
        idempotent: bool,
    ) -> dict[str, Any]:
        build = snapshot.get("selected_build")
        outcomes = snapshot.get("outcomes", [])
        hits = snapshot.get("hits", [])
        abandoned = [
            {
                "shard_id": item["shard_id"],
                "outcome": item["outcome"],
                "reason": item["reason"],
                "disposition": item.get("detail", {}).get("disposition"),
            }
            for item in outcomes
            if item["outcome"] in {"not_started", "abandoned"}
        ]
        failures = [
            {
                "shard_id": item["shard_id"],
                "outcome": item["outcome"],
                "reason": item["reason"],
                "detail": item.get("detail", {}),
            }
            for item in outcomes
            if item["outcome"] == "failed"
        ]
        result = {
            "ok": True,
            "command": command,
            "committed": committed,
            "idempotent": idempotent,
            "version": version,
            "content_digest": build["content_digest"] if build else None,
            "manifest_digest": build["manifest_digest"] if build else None,
            "manifest_relpath": build["manifest_relpath"] if build else None,
            "pointer": snapshot.get("pointer"),
            "watermarks": snapshot.get("watermarks"),
            "logical_clock_ms": build["update_clock_ms"] if build else None,
            "round_no": build["last_round"] if build else None,
            "status": build["status"] if build else None,
            "summary": _outcome_summary(outcomes),
            "outcomes": outcomes,
            "hits": hits,
            "abandoned": abandoned,
            "failures": failures,
            "db": str(self.db_path),
            "store": str(self.store_path),
        }
        if not committed and build is not None:
            fail_code = "SAVE_INCOMPLETE"
            result["diagnostic_code"] = fail_code
        return result

    def status(self, version: int | None = None) -> dict[str, Any]:
        snapshot = read_snapshot_status(self.db_path, version)
        selected_version = snapshot.get("selected_version")
        selected_build = snapshot.get("selected_build")
        outcomes = snapshot.get("outcomes", [])
        commits = snapshot.get("shard_commits", [])
        hits = snapshot.get("hits", [])
        builds = {
            str(version_key): _clean_build_row(row)
            for version_key, row in snapshot.get("builds", {}).items()
        }

        expected_shards: list[str] = []
        plan_summary: dict[str, Any] | None = None
        if selected_build is not None:
            try:
                plan_data = json.loads(selected_build["plan_json"])
                policy_data = json.loads(selected_build["policy_json"])
                expected_shards = [
                    f"shard-{index:04d}"
                    for index in range(int(policy_data["num_shards"]))
                ]
                plan_summary = {
                    "seed": plan_data["seed"],
                    "layer_count": len(plan_data["layers"]),
                    "num_shards": policy_data["num_shards"],
                    "max_shard_bytes": policy_data["max_shard_bytes"],
                    "save": plan_data["save"],
                }
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                plan_summary = None

        committed_shards = {item["shard_id"]: item for item in commits}
        missing_shards = [
            shard_id for shard_id in expected_shards if shard_id not in committed_shards
        ]
        invalid_objects = [
            shard_id
            for shard_id, item in committed_shards.items()
            if not self.store.has_valid_object(item["digest"])
        ]
        outcome_summary = _outcome_summary(outcomes)
        manifest_check: dict[str, Any]
        if selected_build is None:
            manifest_check = {"ok": False, "reason": "no_selected_build"}
        elif selected_build["status"] != "committed":
            manifest_check = {
                "ok": False,
                "reason": "build_not_committed",
                "status": selected_build["status"],
            }
        else:
            try:
                manifest = self._read_manifest_by_digest(
                    selected_build["manifest_digest"],
                    expected_version=int(selected_build["version"]),
                )
                manifest_check = {
                    "ok": True,
                    "digest": selected_build["manifest_digest"],
                    "manifest_version": manifest["version"],
                }
            except BusinessError as exc:
                manifest_check = {
                    "ok": False,
                    "code": exc.code,
                    "details": exc.details,
                }

        recovery_basis = {
            "pointer_version": (
                snapshot.get("pointer", {}).get("version")
                if snapshot.get("pointer")
                else None
            ),
            "selected_status": (
                selected_build["status"] if selected_build is not None else None
            ),
            "expected_shards": expected_shards,
            "committed_shards": sorted(committed_shards),
            "missing_shards": missing_shards,
            "invalid_object_shards": sorted(invalid_objects),
            "hit_shards": [item["shard_id"] for item in hits],
            "decision": (
                "visible_committed"
                if selected_build is not None
                and selected_build["status"] == "committed"
                else "resume_required"
                if selected_build is not None
                else "no_checkpoint"
            ),
        }
        abandoned = [
            {
                "shard_id": item["shard_id"],
                "outcome": item["outcome"],
                "reason": item["reason"],
                "disposition": item.get("detail", {}).get("disposition"),
            }
            for item in outcomes
            if item["outcome"] in {"not_started", "abandoned"}
        ]
        failures = [
            {
                "shard_id": item["shard_id"],
                "reason": item["reason"],
                "detail": item.get("detail", {}),
            }
            for item in outcomes
            if item["outcome"] == "failed"
        ]
        return {
            "ok": True,
            "command": "status",
            "initialized": snapshot.get("initialized", False),
            "db": str(self.db_path),
            "store": str(self.store_path),
            "pointer": snapshot.get("pointer"),
            "watermarks": snapshot.get("watermarks"),
            "builds": builds,
            "selected_version": selected_version,
            "selected_build": _clean_build_row(selected_build),
            "plan_summary": plan_summary,
            "summary": outcome_summary,
            "outcomes": outcomes,
            "hits": hits,
            "abandoned": abandoned,
            "failures": failures,
            "recovery_decision": recovery_basis,
            "manifest_check": manifest_check,
            "audit_log": snapshot.get("audit_log", []),
        }

    def verify(self, version: int | None = None) -> dict[str, Any]:
        build, manifest, manifest_digest = self._load_committed_manifest(version)
        reconstruction, _ = reconstruct_manifest(manifest, self.store)
        self._verify_ledger_shards(int(build["version"]), manifest)
        return {
            "ok": True,
            "command": "verify",
            "version": int(build["version"]),
            "manifest_digest": manifest_digest,
            "content_digest": manifest["content_digest"],
            "logical_clock_ms": build["update_clock_ms"],
            "pointer": read_snapshot_status(self.db_path).get("pointer"),
            "verified": True,
            **reconstruction,
        }

    def restore(
        self,
        version: int | None = None,
        output_dir: str | os.PathLike[str] | None = None,
    ) -> dict[str, Any]:
        build, manifest, manifest_digest = self._load_committed_manifest(version)
        reconstruction, arrays = reconstruct_manifest(manifest, self.store)
        self._verify_ledger_shards(int(build["version"]), manifest)
        result = {
            "ok": True,
            "command": "restore",
            "version": int(build["version"]),
            "manifest_digest": manifest_digest,
            "content_digest": manifest["content_digest"],
            "logical_clock_ms": build["update_clock_ms"],
            "pointer": read_snapshot_status(self.db_path).get("pointer"),
            "restored": True,
            **reconstruction,
        }
        if output_dir is not None:
            result["output"] = self._write_restore_output(
                Path(output_dir), arrays, result
            )
        return result

    def _load_committed_manifest(
        self, version: int | None
    ) -> tuple[dict[str, Any], dict[str, Any], str]:
        snapshot = read_snapshot_status(self.db_path, version)
        build = snapshot.get("selected_build")
        if build is None:
            fail(
                "CHECKPOINT_NOT_FOUND",
                "没有可见的完整检查点",
                requested_version=version,
            )
        if build["status"] != "committed":
            fail(
                "CHECKPOINT_NOT_FOUND",
                "读取方只能访问完整已提交版本",
                version=build["version"],
                status=build["status"],
            )
        if version is None:
            pointer = snapshot.get("pointer")
            if pointer is None or pointer.get("version") != build["version"]:
                fail(
                    "CHECKPOINT_NOT_FOUND",
                    "检查点指针不可用",
                    pointer=pointer,
                )
        manifest_digest = build["manifest_digest"]
        if not manifest_digest:
            fail("MANIFEST_MISSING", "已提交轮次没有清单摘要", version=build["version"])
        expected_relpath = f"manifests/{manifest_digest}.json"
        if build["manifest_relpath"] != expected_relpath:
            fail(
                "MANIFEST_INVALID",
                "清单相对路径不符合内容寻址规则",
                actual=build["manifest_relpath"],
                expected=expected_relpath,
            )
        data = self.store.read_manifest(manifest_digest)
        actual_digest = sha256_hex(data)
        if actual_digest != manifest_digest:
            fail(
                "MANIFEST_DIGEST_MISMATCH",
                "清单摘要不匹配",
                expected_digest=manifest_digest,
                actual_digest=actual_digest,
            )
        try:
            manifest = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            fail("MANIFEST_INVALID", "清单不是合法 JSON", reason=str(exc))
        validate_manifest_shape(manifest)
        if manifest["version"] != build["version"]:
            fail(
                "MANIFEST_INVALID",
                "清单版本与账本版本不一致",
                manifest_version=manifest["version"],
                ledger_version=build["version"],
            )
        if manifest["content_digest"] != build["content_digest"]:
            fail(
                "MANIFEST_INVALID",
                "清单内容摘要与账本不一致",
                manifest_digest_value=manifest["content_digest"],
                ledger_digest=build["content_digest"],
            )
        return build, manifest, manifest_digest

    def _read_manifest_by_digest(
        self, digest: str, *, expected_version: int
    ) -> dict[str, Any]:
        data = self.store.read_manifest(digest)
        try:
            manifest = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            fail("MANIFEST_INVALID", "清单不是合法 JSON", reason=str(exc))
        validate_manifest_shape(manifest)
        if int(manifest["version"]) != expected_version:
            fail(
                "MANIFEST_INVALID",
                "清单版本与请求版本不一致",
                manifest_version=manifest["version"],
                expected_version=expected_version,
            )
        return manifest

    def _verify_ledger_shards(
        self, version: int, manifest: dict[str, Any]
    ) -> None:
        snapshot = read_snapshot_status(self.db_path, version)
        commits = {
            item["shard_id"]: item["digest"]
            for item in snapshot.get("shard_commits", [])
        }
        expected = {
            item["shard_id"]: item["digest"] for item in manifest["shards"]
        }
        if commits != expected:
            fail(
                "CONSISTENCY_MISMATCH",
                "SQLite 已提交分片集合与清单不一致",
                version=version,
                missing=sorted(set(expected) - set(commits)),
                extra=sorted(set(commits) - set(expected)),
                digest_mismatch=sorted(
                    shard_id
                    for shard_id in set(commits) & set(expected)
                    if commits[shard_id] != expected[shard_id]
                ),
            )

    def _write_restore_output(
        self,
        output_dir: Path,
        arrays: dict[str, dict[str, np.ndarray]],
        result: dict[str, Any],
    ) -> dict[str, Any]:
        try:
            output_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            fail("IO_ERROR", "无法创建恢复输出目录", path=str(output_dir), reason=str(exc))
        files: list[dict[str, str]] = []
        for index, name in enumerate(sorted(arrays)):
            safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", name)
            stem = f"{index:04d}-{safe_name}"
            for tensor_name, array in sorted(arrays[name].items()):
                filename = (
                    f"{stem}.npy"
                    if tensor_name == "parameter"
                    else f"{stem}.{tensor_name}.npy"
                )
                destination = output_dir / filename
                temp = output_dir / f".tmp-{os.getpid()}-{filename}"
                try:
                    with temp.open("wb") as handle:
                        np.save(handle, array, allow_pickle=False)
                        handle.flush()
                        os.fsync(handle.fileno())
                    os.replace(temp, destination)
                except OSError as exc:
                    temp.unlink(missing_ok=True)
                    fail(
                        "IO_ERROR",
                        "写入恢复数组失败",
                        path=str(destination),
                        reason=str(exc),
                    )
                files.append(
                    {
                        "layer": name,
                        "tensor": tensor_name,
                        "relative_path": filename,
                        "sha256": sha256_hex(destination.read_bytes()),
                    }
                )
        output_result = {
            "directory": str(output_dir),
            "files": files,
            "chain_digest": result["chain_digest"],
        }
        manifest_path = output_dir / "restore.json"
        temp = output_dir / f".tmp-{os.getpid()}-restore.json"
        try:
            with temp.open("w", encoding="utf-8", newline="\n") as handle:
                handle.write(pretty_json(output_result) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, manifest_path)
        except OSError as exc:
            temp.unlink(missing_ok=True)
            fail("IO_ERROR", "写入恢复清单失败", path=str(manifest_path), reason=str(exc))
        return output_result
