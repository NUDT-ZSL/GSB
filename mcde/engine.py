from __future__ import annotations

from pathlib import Path
from typing import Any

from .errors import (
    E_APPROVAL_ALREADY_CONSUMED,
    E_APPROVAL_EXPIRED,
    E_NO_ACTIVE_VERSION,
    E_WINDOW_CLOSED,
    EngineError,
    StateError,
)
from .loading import load_bundle
from .merge import build_plan, flatten_config, plan_digest
from .model import AdmissionRules, Bundle, ClockScript
from .storage import Repository
from .util import sha256_json


def _request_digest(bundle: Bundle, abandoned: list[str]) -> str:
    return sha256_json(
        {
            "snapshot_digest": bundle.digest(),
            "abandoned": sorted(set(abandoned)),
        }
    )


def _window_state(
    rules: AdmissionRules, now_ms: int
) -> tuple[bool, int | None, str]:
    if rules.window_start_ms <= now_ms < rules.window_end_ms:
        return True, now_ms, "INSIDE_WINDOW"
    if now_ms < rules.window_start_ms:
        return False, rules.window_start_ms, "BEFORE_WINDOW"
    if rules.window_period_ms:
        elapsed = now_ms - rules.window_start_ms
        periods = elapsed // rules.window_period_ms + 1
        earliest = rules.window_start_ms + periods * rules.window_period_ms
        return False, earliest, "AFTER_WINDOW"
    return False, None, "WINDOW_CLOSED"


def _approval_preflight_error(
    decision: dict[str, Any],
    *,
    now_ms: int,
    consumed: frozenset[str],
) -> dict[str, Any] | None:
    for key_record in decision.get("keys", []):
        approval = key_record["policy"].get("approval")
        if not approval or approval.get("status") != "approved":
            continue
        approval_id = approval["approval_id"]
        if approval_id in consumed:
            return {
                "code": E_APPROVAL_ALREADY_CONSUMED,
                "message": "approval was consumed before dispatch start",
                "key": key_record["key"],
                "approval_id": approval_id,
            }
        if now_ms >= int(approval["expires_ms"]):
            return {
                "code": E_APPROVAL_EXPIRED,
                "message": "approval expired before dispatch start",
                "key": key_record["key"],
                "approval_id": approval_id,
                "expires_ms": approval["expires_ms"],
                "logical_ms": now_ms,
            }
    return None


class Engine:
    def __init__(
        self,
        *,
        samples_dir: str | Path = "samples",
        db_path: str | Path = "state/mcde.sqlite3",
        lock_dir: str | Path | None = None,
    ) -> None:
        self.samples_dir = Path(samples_dir)
        self.db_path = Path(db_path)
        self.repo = Repository(self.db_path, lock_dir)

    def _bundle_for_read(self) -> tuple[Bundle, str]:
        if self.repo.exists and self.repo.get_active_snapshot_digest():
            return self.repo.get_active_bundle(), "loaded_snapshot"
        return load_bundle(self.samples_dir), "sample_files"

    def _now_for_read(self, bundle: Bundle) -> int:
        if self.repo.exists and self.repo.get_active_snapshot_digest():
            return self.repo.get_clock()[0]
        return bundle.clock.start_ms

    def load(self) -> dict[str, Any]:
        # Full validation happens before any state path is created. Invalid
        # input therefore leaves either no database or the old database intact.
        bundle = load_bundle(self.samples_dir)
        result = self.repo.load_snapshot(bundle)
        return {"ok": True, "command": "load", **result}

    def merge(self) -> dict[str, Any]:
        bundle, source = self._bundle_for_read()
        now_ms = self._now_for_read(bundle)
        plan = build_plan(bundle, now_ms=now_ms)
        return {
            "ok": True,
            "command": "merge",
            "state_source": source,
            "snapshot_digest": bundle.digest(),
            "logical_clock_ms": now_ms,
            **plan,
        }

    def plan(self, *, abandoned: list[str] | None = None) -> dict[str, Any]:
        abandoned_list = sorted(set(abandoned or []))
        if self.repo.exists and self.repo.get_active_snapshot_digest():
            bundle = self.repo.get_active_bundle()
            now_ms = self.repo.get_clock()[0]
            source = "loaded_snapshot"
        else:
            bundle = load_bundle(self.samples_dir)
            now_ms = bundle.clock.start_ms
            source = "sample_files"
        request = _request_digest(bundle, abandoned_list)
        active = self.repo.get_active_version() if self.repo.exists else None
        if active is not None and active["request_digest"] == request:
            version = int(active["version"])
            preview = {
                "already_committed": True,
                "version": version,
                "rounds": self.repo.get_rounds(version),
                "dispatches": list(
                    self.repo.get_dispatch_states(version).values()
                ),
            }
            plan = active["plan"]
            return {
                "ok": True,
                "command": "plan",
                "state_source": source,
                "snapshot_digest": bundle.digest(),
                "logical_clock_ms": now_ms,
                "request_digest": request,
                "schedule": preview,
                **plan,
            }

        consumed = (
            self.repo.get_consumed_approvals() if self.repo.exists else frozenset()
        )
        plan = build_plan(
            bundle, now_ms=now_ms, consumed_approvals=consumed
        )
        schedule = self._preview_schedule(
            plan=plan,
            rules=bundle.rules,
            script=bundle.clock,
            now_ms=now_ms,
            cursor=self.repo.get_clock()[1] if self.repo.exists else 0,
            abandoned=abandoned_list,
        )
        return {
            "ok": True,
            "command": "plan",
            "state_source": source,
            "snapshot_digest": bundle.digest(),
            "logical_clock_ms": now_ms,
            "request_digest": request,
            "schedule": schedule,
            **plan,
        }

    def apply(
        self,
        *,
        abandoned: list[str] | None = None,
        requested_version: int | None = None,
    ) -> dict[str, Any]:
        abandoned_list = sorted(set(abandoned or []))
        bundle = self.repo.get_active_bundle()
        now_ms = self.repo.get_clock()[0]
        request = _request_digest(bundle, abandoned_list)

        with self.repo.request_lock(request):
            existing = self.repo.find_version_by_request(request)
            if existing is not None:
                existing_version = int(existing["version"])
                with self.repo.version_lock(existing_version):
                    return self._execution_result(
                        version=existing_version,
                        bundle=bundle,
                        idempotent=True,
                    )

            consumed = self.repo.get_consumed_approvals()
            plan = build_plan(
                bundle, now_ms=now_ms, consumed_approvals=consumed
            )
            digest = plan_digest(plan)
            registered = self.repo.register_version(
                bundle=bundle,
                plan=plan,
                request_digest=request,
                plan_digest=digest,
                requested_version=requested_version,
                abandoned=abandoned_list,
            )
            version = int(registered["version"])
            if registered["existing"]:
                with self.repo.version_lock(version):
                    return self._execution_result(
                        version=version, bundle=bundle, idempotent=True
                    )
            payload = registered["plan"]
            bundle_for_version = self.repo.get_bundle_for_snapshot(
                payload["snapshot_digest"]
            )
            with self.repo.version_lock(version):
                self._execute_version(
                    version=version,
                    payload=payload,
                    bundle=bundle_for_version,
                    resume_mode=False,
                )
                return self._execution_result(
                    version=version,
                    bundle=bundle_for_version,
                    idempotent=False,
                )

    def resume(self) -> dict[str, Any]:
        payload = self.repo.get_active_version()
        if payload is None:
            raise StateError(
                E_NO_ACTIVE_VERSION,
                "no configuration version is active; run apply first",
            )
        version = int(payload["version"])
        bundle = self.repo.get_bundle_for_snapshot(payload["snapshot_digest"])
        with self.repo.version_lock(version):
            self._execute_version(
                version=version,
                payload=payload,
                bundle=bundle,
                resume_mode=True,
            )
            return self._execution_result(
                version=version, bundle=bundle, idempotent=False
            )

    def status(self) -> dict[str, Any]:
        result = self.repo.status_snapshot()
        return {"ok": True, "command": "status", **result}

    def verify(self) -> dict[str, Any]:
        payload = self.repo.get_active_version()
        if payload is None:
            raise StateError(
                E_NO_ACTIVE_VERSION,
                "no configuration version is active; run apply first",
            )
        version = int(payload["version"])
        plan = payload["plan"]
        dispatches = self.repo.get_dispatch_states(version)
        residual: list[dict[str, Any]] = []
        explained = 0
        unexplained = 0
        applied_clean = 0
        for cluster_id in sorted(plan["clusters"]):
            decision = plan["clusters"][cluster_id]
            dispatch = dispatches.get(cluster_id)
            dispatch_status = dispatch["status"] if dispatch else "unknown"
            expected_config = (
                decision["candidate_config"]
                if decision["status"] == "rejected"
                else decision["effective_config"]
            )
            expected = flatten_config(expected_config)
            readback_record = self.repo.cluster_readback(cluster_id)
            actual = (
                flatten_config(readback_record["config"])
                if readback_record is not None
                else {}
            )
            key_records = {
                item["key"]: item for item in decision.get("keys", [])
            }
            keys = sorted(set(expected) | set(actual))
            cluster_residuals = 0
            for key in keys:
                expected_value = expected.get(key)
                actual_value = actual.get(key)
                if expected_value == actual_value and key in expected:
                    continue
                key_record = key_records.get(key)
                is_unexplained = (
                    dispatch_status == "applied"
                    and (key_record is None or key_record.get("applied", True))
                )
                basis: list[dict[str, Any]] = [
                    {
                        "step": "snapshot",
                        "snapshot_digest": payload["snapshot_digest"],
                        "version": version,
                    }
                ]
                if key_record is not None:
                    basis.append(
                        {
                            "step": "merge",
                            "key": key,
                            "value": key_record["value"],
                            "proposed_value": key_record["proposed_value"],
                            "source_layer": key_record["source_layer"],
                            "overridden_layers": key_record[
                                "overridden_layers"
                            ],
                            "merge_path": key_record["merge_path"],
                        }
                    )
                    basis.append(
                        {
                            "step": "policy",
                            "applied": key_record["applied"],
                            "cluster_diff": key_record["cluster_diff"],
                            "policy": key_record["policy"],
                            "violation": key_record.get("violation"),
                        }
                    )
                    approval = key_record["policy"].get("approval")
                    if approval:
                        basis.append(
                            {"step": "approval", "record": approval}
                        )
                else:
                    basis.append(
                        {
                            "step": "merge",
                            "key": key,
                            "result": "key absent from expected configuration",
                        }
                    )
                if any(
                    violation.get("key") == key
                    for violation in decision.get("violations", [])
                ):
                    basis.append(
                        {
                            "step": "quota_or_policy_rejection",
                            "violations": [
                                violation
                                for violation in decision.get("violations", [])
                                if violation.get("key") == key
                            ],
                        }
                    )
                basis.append(
                    {
                        "step": "dispatch",
                        "version": version,
                        "status": dispatch_status,
                        "reason_code": (
                            dispatch["reason_code"] if dispatch else None
                        ),
                        "reason": dispatch["reason"] if dispatch else None,
                    }
                )
                basis.append(
                    {
                        "step": "commit",
                        "expected_version": version,
                        "readback_version": (
                            readback_record["version"]
                            if readback_record is not None
                            else None
                        ),
                        "cluster_decision": decision["status"],
                        "atomic_cluster_rejection": (
                            decision["status"] == "rejected"
                            and dispatch_status != "applied"
                        ),
                    }
                )
                if is_unexplained:
                    unexplained += 1
                    verdict = "unexplained_drift"
                else:
                    explained += 1
                    verdict = "explained_residual"
                cluster_residuals += 1
                residual.append(
                    {
                        "cluster": cluster_id,
                        "key": key,
                        "expected": expected_value,
                        "readback": actual_value,
                        "verdict": verdict,
                        "basis": basis,
                    }
                )
            if dispatch_status == "applied" and cluster_residuals == 0:
                applied_clean += 1
        return {
            "ok": unexplained == 0,
            "command": "verify",
            "version": version,
            "snapshot_digest": payload["snapshot_digest"],
            "residual_count": len(residual),
            "explained_residual_count": explained,
            "unexplained_drift_count": unexplained,
            "applied_clean_clusters": applied_clean,
            "residuals": residual,
            "summary": {
                "verdict": (
                    "all_residuals_explained"
                    if unexplained == 0
                    else "unexplained_drift_detected"
                )
            },
        }

    def _preview_schedule(
        self,
        *,
        plan: dict[str, Any],
        rules: AdmissionRules,
        script: ClockScript,
        now_ms: int,
        cursor: int,
        abandoned: list[str],
    ) -> dict[str, Any]:
        inside, earliest, window_reason = _window_state(rules, now_ms)
        accepted = [
            cluster_id
            for cluster_id, decision in sorted(plan["clusters"].items())
            if decision["status"] == "accepted"
            and cluster_id not in set(abandoned)
        ]
        outcomes: dict[str, dict[str, Any]] = {}
        for cluster_id, decision in sorted(plan["clusters"].items()):
            if decision["status"] == "rejected":
                outcomes[cluster_id] = {
                    "status": "rejected",
                    "reason_code": decision["rejection_code"],
                }
            elif cluster_id in set(abandoned):
                outcomes[cluster_id] = {
                    "status": "abandoned",
                    "reason_code": "OPERATOR_ABANDONED",
                }
            else:
                outcomes[cluster_id] = {"status": "queued"}

        if not inside:
            for cluster_id in accepted:
                outcomes[cluster_id] = {
                    "status": "queued_window",
                    "reason_code": E_WINDOW_CLOSED,
                    "earliest_ms": earliest,
                    "window_reason": window_reason,
                }
            return {
                "window": {
                    "inside": False,
                    "reason": window_reason,
                    "earliest_executable_ms": earliest,
                },
                "rounds": [],
                "outcomes": outcomes,
            }

        rounds: list[dict[str, Any]] = []
        pending = list(accepted)
        round_no = 1
        sim_now = now_ms
        sim_cursor = cursor
        while pending:
            batch = pending[: rules.max_parallel]
            round_info: dict[str, Any] = {
                "round_no": round_no,
                "max_parallel": rules.max_parallel,
                "budget_ms": rules.round_budget_ms,
                "elapsed_ms": 0,
                "clusters": [],
            }
            elapsed = 0
            for slot, cluster_id in enumerate(batch):
                cost, start_ms, finish_ms, next_cursor = Repository.peek_cost_from(
                    sim_now, sim_cursor, script
                )
                if elapsed + cost > rules.round_budget_ms:
                    remaining = pending[pending.index(cluster_id):]
                    for item in remaining:
                        outcomes[item] = {
                            "status": "not_started_budget",
                            "reason_code": "ROUND_BUDGET_EXHAUSTED",
                            "round_no": round_no,
                            "budget_ms": rules.round_budget_ms,
                            "elapsed_ms": elapsed,
                            "required_cost_ms": cost,
                        }
                    round_info["status"] = "budget_exhausted"
                    rounds.append(round_info)
                    pending = []
                    break
                elapsed += cost
                sim_now = finish_ms
                sim_cursor = next_cursor
                decision = plan["clusters"][cluster_id]
                outcome_status = (
                    "planned"
                    if decision["healthy"]
                    else "failed_after_start"
                )
                outcomes[cluster_id] = {
                    "status": outcome_status,
                    "round_no": round_no,
                    "slot": slot,
                    "started_logical_ms": start_ms,
                    "finished_logical_ms": finish_ms,
                    "reason_code": (
                        None
                        if decision["healthy"]
                        else "CLUSTER_UNHEALTHY"
                    ),
                }
                round_info["clusters"].append(cluster_id)
            if round_info.get("status") != "budget_exhausted":
                round_info["status"] = "planned"
                round_info["elapsed_ms"] = elapsed
                rounds.append(round_info)
                pending = pending[len(batch):]
            round_no += 1
        return {
            "window": {
                "inside": True,
                "reason": window_reason,
                "earliest_executable_ms": now_ms,
            },
            "rounds": rounds,
            "outcomes": outcomes,
        }

    def _execute_version(
        self,
        *,
        version: int,
        payload: dict[str, Any],
        bundle: Bundle,
        resume_mode: bool,
    ) -> None:
        plan = payload["plan"]
        decisions = plan["clusters"]
        rules = bundle.rules
        script = bundle.clock

        # A committed "started" marker means the dispatcher was killed after
        # admission but before finish. Resume only the finish/commit half.
        states = self.repo.get_dispatch_states(version)
        for cluster_id in sorted(states):
            dispatch = states[cluster_id]
            if dispatch["status"] != "started":
                continue
            decision = decisions[cluster_id]
            if not decision["healthy"]:
                self.repo.mark_failed_after_start(
                    version=version,
                    cluster_id=cluster_id,
                    reason_code="CLUSTER_UNHEALTHY",
                    reason="cluster adapter failed after dispatch started",
                    detail={"healthy": False},
                    script=script,
                )
                continue
            try:
                self.repo.commit_cluster(
                    version=version,
                    cluster_id=cluster_id,
                    decision=decision,
                    script=script,
                )
            except EngineError as exc:
                self.repo.mark_failed_after_start(
                    version=version,
                    cluster_id=cluster_id,
                    reason_code=exc.code,
                    reason=exc.message,
                    detail=exc.details,
                    script=script,
                )

        states = self.repo.get_dispatch_states(version)
        pending = [
            cluster_id
            for cluster_id, dispatch in sorted(states.items())
            if decisions[cluster_id]["status"] == "accepted"
            and dispatch["status"] in {"queued", "not_started_budget"}
        ]
        if not pending:
            return

        now_ms = self.repo.get_clock()[0]
        inside, earliest, window_reason = _window_state(rules, now_ms)
        if not inside and resume_mode and earliest is not None:
            self.repo.set_clock_watermark(
                now_ms=earliest,
                reason="resume_fast_forward_window",
                detail={
                    "earliest_ms": earliest,
                    "window_reason": window_reason,
                },
            )
            now_ms = earliest
            inside, earliest, window_reason = _window_state(rules, now_ms)

        if not inside:
            rounds = self.repo.get_rounds(version)
            if not any(item["status"] == "queued" for item in rounds):
                round_no = max(
                    [item["round_no"] for item in rounds], default=0
                ) + 1
                self.repo.create_round(
                    version=version,
                    round_no=round_no,
                    status="queued",
                    max_parallel=rules.max_parallel,
                    budget_ms=rules.round_budget_ms,
                    earliest_ms=earliest,
                    now_ms=now_ms,
                    reason_code=E_WINDOW_CLOSED,
                    detail={"window_reason": window_reason},
                )
            self.repo.queue_outside_window(
                version=version,
                earliest_ms=earliest,
                reason_code=E_WINDOW_CLOSED,
                now_ms=now_ms,
            )
            return

        if resume_mode:
            self.repo.reopen_for_resume(version, now_ms=now_ms)
        rounds = self.repo.get_rounds(version)
        for round_info in rounds:
            if round_info["status"] == "queued":
                self.repo.finish_round(
                    round_id=round_info["round_id"],
                    status="window_reopened",
                    reason_code="WINDOW_REOPENED",
                    detail={"resume_logical_ms": now_ms},
                )
        rounds = self.repo.get_rounds(version)
        next_round_no = max(
            [item["round_no"] for item in rounds], default=0
        ) + 1

        while True:
            states = self.repo.get_dispatch_states(version)
            pending = [
                cluster_id
                for cluster_id, dispatch in sorted(states.items())
                if decisions[cluster_id]["status"] == "accepted"
                and dispatch["status"] in {"queued", "not_started_budget"}
            ]
            if not pending:
                break
            batch = pending[: rules.max_parallel]
            round_id = self.repo.create_round(
                version=version,
                round_no=next_round_no,
                status="running",
                max_parallel=rules.max_parallel,
                budget_ms=rules.round_budget_ms,
                earliest_ms=now_ms,
                now_ms=now_ms,
                detail={"clusters": batch},
            )
            elapsed = 0
            budget_exhausted = False
            for slot, cluster_id in enumerate(batch):
                state = self.repo.get_dispatch_states(version)[cluster_id]
                if state["status"] not in {"queued", "not_started_budget"}:
                    continue
                decision = decisions[cluster_id]
                now_ms, cursor = self.repo.get_clock()
                consumed = self.repo.get_consumed_approvals()
                preflight = _approval_preflight_error(
                    decision, now_ms=now_ms, consumed=consumed
                )
                if preflight is not None:
                    self.repo.mark_rejected_at_dispatch(
                        version=version,
                        cluster_id=cluster_id,
                        reason_code=preflight["code"],
                        reason=preflight["message"],
                        detail=preflight,
                    )
                    continue
                cost, _, _, _ = Repository.peek_cost_from(
                    now_ms, cursor, script
                )
                if elapsed + cost > rules.round_budget_ms:
                    remaining_states = self.repo.get_dispatch_states(version)
                    remaining = [
                        item
                        for item, dispatch in sorted(remaining_states.items())
                        if decisions[item]["status"] == "accepted"
                        and dispatch["status"]
                        in {"queued", "not_started_budget"}
                    ]
                    self.repo.mark_not_started_budget(
                        version=version,
                        cluster_ids=remaining,
                        round_id=round_id,
                        reason_code="ROUND_BUDGET_EXHAUSTED",
                        reason=(
                            "logical time budget exhausted before dispatch start"
                        ),
                        detail={
                            "budget_ms": rules.round_budget_ms,
                            "elapsed_ms": elapsed,
                            "next_dispatch_cost_ms": cost,
                            "slot": slot,
                        },
                    )
                    self.repo.finish_round(
                        round_id=round_id,
                        status="budget_exhausted",
                        reason_code="ROUND_BUDGET_EXHAUSTED",
                        detail={
                            "budget_ms": rules.round_budget_ms,
                            "elapsed_ms": elapsed,
                            "not_started": remaining,
                        },
                    )
                    budget_exhausted = True
                    break
                started = self.repo.mark_started(
                    version=version,
                    cluster_id=cluster_id,
                    script=script,
                    round_id=round_id,
                    slot=slot,
                )
                if started is None:
                    continue
                elapsed += cost
                if not decision["healthy"]:
                    self.repo.mark_failed_after_start(
                        version=version,
                        cluster_id=cluster_id,
                        reason_code="CLUSTER_UNHEALTHY",
                        reason=(
                            "cluster adapter failed after dispatch started"
                        ),
                        detail={"healthy": False},
                        script=script,
                    )
                    continue
                try:
                    self.repo.commit_cluster(
                        version=version,
                        cluster_id=cluster_id,
                        decision=decision,
                        script=script,
                    )
                except EngineError as exc:
                    self.repo.mark_failed_after_start(
                        version=version,
                        cluster_id=cluster_id,
                        reason_code=exc.code,
                        reason=exc.message,
                        detail=exc.details,
                        script=script,
                    )
            if budget_exhausted:
                break
            self.repo.finish_round(
                round_id=round_id,
                status="completed",
                detail={"clusters": batch, "elapsed_ms": elapsed},
            )
            now_ms = self.repo.get_clock()[0]
            next_round_no += 1

    def _execution_result(
        self, *, version: int, bundle: Bundle, idempotent: bool
    ) -> dict[str, Any]:
        payload = self.repo.get_plan_for_version(version)
        plan = payload["plan"]
        dispatches = self.repo.get_dispatch_states(version)
        rounds = self.repo.get_rounds(version)
        status = self.repo.status_snapshot()
        ledger = {
            item["cluster"]: {
                "replicas": item["replicas"],
                "resources": item["resources"],
            }
            for item in status["clusters"]
        }
        cluster_rows = []
        for cluster_id in sorted(plan["clusters"]):
            decision = plan["clusters"][cluster_id]
            dispatch = dispatches[cluster_id]
            cluster_rows.append(
                {
                    "cluster": cluster_id,
                    "region": decision["region"],
                    "decision_status": decision["status"],
                    "rejection_code": decision["rejection_code"],
                    "first_violation": decision["first_violation"],
                    "conflict_count": decision["conflict_count"],
                    "dispatch_status": dispatch["status"],
                    "dispatch_reason_code": dispatch["reason_code"],
                    "dispatch_reason": dispatch["reason"],
                    "round_id": dispatch["round_id"],
                    "started_logical_ms": dispatch["started_logical_ms"],
                    "finished_logical_ms": dispatch["finished_logical_ms"],
                    "ledger": ledger.get(cluster_id),
                }
            )
        counts: dict[str, int] = {}
        for row in cluster_rows:
            key = row["dispatch_status"]
            counts[key] = counts.get(key, 0) + 1
        return {
            "ok": True,
            "command": "apply" if not idempotent else "apply-idempotent",
            "version": version,
            "idempotent": idempotent,
            "request_digest": payload["request_digest"],
            "snapshot_digest": payload["snapshot_digest"],
            "logical_clock_ms": status["logical_clock_ms"],
            "window": {
                "start_ms": bundle.rules.window_start_ms,
                "end_ms": bundle.rules.window_end_ms,
                "period_ms": bundle.rules.window_period_ms,
            },
            "rounds": rounds,
            "clusters": cluster_rows,
            "summary": {
                "dispatch_status_counts": counts,
                "conflicts": sum(
                    row["conflict_count"] for row in cluster_rows
                ),
            },
        }

