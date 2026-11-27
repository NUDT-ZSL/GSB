"""Deterministic multi-dimensional resource scheduler and audit journal."""

from __future__ import annotations

import copy
from collections import defaultdict
from typing import Any, Iterable, Mapping

from .model import (
    CATEGORIES,
    DIMENSIONS,
    demand_ratio,
    validate_workload,
    vector_add,
    vector_leq,
    vector_nonnegative,
    vector_positive,
    vector_sub,
    vector_sum,
    workload_digest,
    zero_vector,
)

URGENCY_HORIZON = 10_000
ACTIVE_STATE_RANK = 1
EXPIRED_STATE_RANK = 0
EXPIRED_BASE_PENALTY = 1_000_000

DECISION_TYPES = ("ADMIT", "PREEMPT", "DOWNGRADE", "REJECT")

REASON_TEXT = {
    "CAPACITY_AVAILABLE": "节点三维余量均可容纳作业",
    "CAPACITY_AVAILABLE_AFTER_RELEASE": "让步动作释放资源后余量恰好或足够容纳作业",
    "DEADLINE_EXPIRED": "作业在决策时刻已到期，紧迫度进入过期降级态",
    "NO_ELIGIBLE_NODE": "没有声明可接纳该业务类别的节点",
    "INSUFFICIENT_RESOURCES": "没有低紧迫度可让步作业能释放足够资源",
    "LOWER_URGENCY_PREEMPTED": "高紧迫度作业抢占低紧迫度作业",
    "LOWER_URGENCY_MIGRATED": "低紧迫度作业迁移到其他节点以腾出资源",
    "LOWER_URGENCY_DEMOTED": "低紧迫度作业切换降级形态并释放资源",
}


def compute_urgency(job: Mapping[str, Any], logical_time: int) -> dict[str, Any]:
    """Build a deterministic, auditable urgency row.

    Active jobs use ``priority * 10001 + deadline_pressure``.  Deadline
    pressure grows as remaining time shrinks.  Jobs with remaining time <= 0
    are never compared as ordinary active jobs: they enter the distinct
    ``EXPIRED`` state with effective priority 0 and negative urgency.
    """

    remaining = int(job["deadline"]) - int(logical_time)
    priority = int(job["priority"])
    submit_time = int(job["submit_time"])
    if remaining > 0:
        deadline_pressure = max(0, URGENCY_HORIZON - remaining)
        urgency_score = priority * (URGENCY_HORIZON + 1) + deadline_pressure
        state = "ACTIVE"
        state_rank = ACTIVE_STATE_RANK
        effective_priority = priority
    else:
        overdue = -remaining
        deadline_pressure = 0
        urgency_score = -EXPIRED_BASE_PENALTY - min(overdue, 999_999)
        state = "EXPIRED"
        state_rank = EXPIRED_STATE_RANK
        effective_priority = 0

    sort_key = [
        -state_rank,
        -urgency_score,
        -effective_priority,
        remaining,
        submit_time,
        str(job["job_id"]),
    ]
    urgency_rank = [
        state_rank,
        urgency_score,
        effective_priority,
        -remaining if state == "ACTIVE" else -overdue,
    ]
    return {
        "at": int(logical_time),
        "job_id": str(job["job_id"]),
        "priority": priority,
        "effective_priority": effective_priority,
        "deadline": int(job["deadline"]),
        "remaining": remaining,
        "state": state,
        "deadline_pressure": deadline_pressure,
        "urgency_score": urgency_score,
        "sort_key": sort_key,
        "urgency_rank": urgency_rank,
    }


def _urgency_sort_key(job: Mapping[str, Any], logical_time: int) -> list[Any]:
    return compute_urgency(job, logical_time)["sort_key"]


class SchedulerEngine:
    """Advance an event stream while maintaining a replayable capacity ledger."""

    def __init__(self, workload: Mapping[str, Any]) -> None:
        self.workload = validate_workload(workload)
        self.workload_hash = workload_digest(self.workload)
        self.jobs = {
            job["job_id"]: copy.deepcopy(job) for job in self.workload["jobs"]
        }
        self.nodes = {
            node["node_id"]: copy.deepcopy(node) for node in self.workload["nodes"]
        }
        self.node_ids = sorted(self.nodes)
        self.events = copy.deepcopy(self.workload["events"])
        self.current_time = -1
        self.event_index = 0
        self.decision_seq = 0
        self.change_seq = 0
        self.snapshot_seq = 0
        self.finished = False

        self.node_used = {node_id: zero_vector() for node_id in self.node_ids}
        self.active: dict[str, dict[str, Any]] = {}
        self.decisions: list[dict[str, Any]] = []
        self.capacity_changes: list[dict[str, Any]] = []
        self.urgency_rows: list[dict[str, Any]] = []
        self.snapshots: list[dict[str, Any]] = []
        self.job_state: dict[str, dict[str, Any]] = {}
        for job_id, job in self.jobs.items():
            self.job_state[job_id] = {
                "job_id": job_id,
                "status": "pending",
                "submit_time": int(job["submit_time"]),
                "deadline": int(job["deadline"]),
                "base_demand": copy.deepcopy(job["demand"]),
                "current_alloc": zero_vector(),
                "delivered_alloc": zero_vector(),
                "current_profile": None,
                "current_node_id": None,
                "admitted_at": None,
                "completed_at": None,
                "canceled_at": None,
                "preempted_at": None,
                "rejected_at": None,
                "deadline_met": None,
                "miss_reason": None,
                "downgraded": False,
                "migration_count": 0,
                "preemption_count": 0,
                "decision_ids": [],
            }

        self._assert_invariants()

    # ------------------------------------------------------------------
    # Event progression
    # ------------------------------------------------------------------
    def _next_completion_time(self) -> int | None:
        candidates = [
            int(assignment["finish_time"])
            for assignment in self.active.values()
            if int(assignment["finish_time"]) > self.current_time
        ]
        return min(candidates) if candidates else None

    def _peek_next_time(self) -> int | None:
        event_time = (
            int(self.events[self.event_index]["at"])
            if self.event_index < len(self.events)
            else None
        )
        completion_time = self._next_completion_time()
        candidates = [
            value for value in (event_time, completion_time) if value is not None
        ]
        return min(candidates) if candidates else None

    def run_next_time(self) -> bool:
        """Advance to the next logical timestamp and process all work there."""

        next_time = self._peek_next_time()
        if next_time is None:
            self.finished = True
            return False
        if next_time < self.current_time:
            raise RuntimeError("事件流时间发生倒退")

        self.current_time = next_time
        self._process_due_completions(next_time)
        self._process_external_events_at(next_time)
        self._record_snapshot()
        self._assert_invariants()
        self.finished = self._peek_next_time() is None
        return True

    def run_to_end(self) -> "SchedulerEngine":
        while self.run_next_time():
            pass
        self._assert_invariants()
        return self

    def run_until(self, logical_time: int) -> "SchedulerEngine":
        """Process all events and completions at or before ``logical_time``."""

        if isinstance(logical_time, bool) or not isinstance(logical_time, int):
            raise ValueError("logical_time 必须是整数")
        if logical_time < self.current_time:
            raise ValueError("不能回到检查点之前的逻辑时刻")
        while True:
            next_time = self._peek_next_time()
            if next_time is None or next_time > logical_time:
                break
            self.run_next_time()
        if self.current_time < logical_time:
            self.current_time = logical_time
        self.finished = self._peek_next_time() is None
        self._assert_invariants()
        return self

    def _process_due_completions(self, now: int) -> None:
        due = sorted(
            (
                (int(assignment["finish_time"]), job_id)
                for job_id, assignment in self.active.items()
                if int(assignment["finish_time"]) <= now
            )
        )
        for finish_time, job_id in due:
            if job_id not in self.active:
                continue
            assignment = self.active[job_id]
            job = self.jobs[job_id]
            state = self.job_state[job_id]
            allocated = copy.deepcopy(assignment["allocation"])
            self._set_current_allocation(job_id, zero_vector(), None, None)
            state["delivered_alloc"] = allocated
            self._release_assignment(
                assignment,
                operation="COMPLETE",
                reason="作业运行时长结束",
                event_id=f"completion:{job_id}:{finish_time}",
            )
            state["status"] = "completed"
            state["completed_at"] = finish_time
            state["deadline_met"] = finish_time <= int(job["deadline"])
            if not state["deadline_met"]:
                state["miss_reason"] = "COMPLETED_LATE"
            self.active.pop(job_id)

    def _process_external_events_at(self, now: int) -> None:
        events_here: list[dict[str, Any]] = []
        while (
            self.event_index < len(self.events)
            and int(self.events[self.event_index]["at"]) == now
        ):
            events_here.append(self.events[self.event_index])
            self.event_index += 1

        for event in events_here:
            if event["type"] == "cancel":
                self._process_cancel(event)

        submissions = [event for event in events_here if event["type"] == "submit"]
        pending_jobs = [
            self.jobs[event["job_id"]]
            for event in submissions
            if self.job_state[event["job_id"]]["status"] == "pending"
        ]
        for job in pending_jobs:
            self.job_state[job["job_id"]]["status"] = "submitted"
        pending_jobs.sort(key=lambda job: _urgency_sort_key(job, now))

        for job in pending_jobs:
            urgency = compute_urgency(job, now)
            row = copy.deepcopy(urgency)
            row["urgency_index"] = len(self.urgency_rows)
            self.urgency_rows.append(row)
            decision = self._decide_submission(job, urgency)
            row["decision_id"] = decision["decision_id"]
            row["decision_type"] = decision["type"]

    def _process_cancel(self, event: Mapping[str, Any]) -> None:
        job_id = str(event["job_id"])
        state = self.job_state[job_id]
        if state["status"] == "active":
            assignment = self.active[job_id]
            delivered = copy.deepcopy(assignment["allocation"])
            state["delivered_alloc"] = delivered
            self._set_current_allocation(job_id, zero_vector(), None, None)
            self._release_assignment(
                assignment,
                operation="CANCEL",
                reason="收到业务取消事件",
                event_id=str(event["event_id"]),
            )
            self.active.pop(job_id)
            state["status"] = "canceled"
            state["canceled_at"] = int(event["at"])
            state["deadline_met"] = False
            state["miss_reason"] = "CANCELED"
        elif state["status"] in ("pending", "submitted"):
            state["status"] = "canceled"
            state["canceled_at"] = int(event["at"])
            state["deadline_met"] = False
            state["miss_reason"] = "CANCELED"

    # ------------------------------------------------------------------
    # Admission and contention decisions
    # ------------------------------------------------------------------
    def _decide_submission(
        self, job: Mapping[str, Any], urgency: Mapping[str, Any]
    ) -> dict[str, Any]:
        if urgency["state"] == "EXPIRED":
            return self._reject(job, urgency, "DEADLINE_EXPIRED", None)

        candidates = [
            node_id
            for node_id in self.node_ids
            if job["category"] in self.nodes[node_id]["categories"]
        ]
        if not candidates:
            return self._reject(job, urgency, "NO_ELIGIBLE_NODE", None)

        for node_id in candidates:
            if self._can_place(job, node_id):
                return self._admit(
                    job,
                    node_id,
                    urgency,
                    reason="CAPACITY_AVAILABLE",
                    supporting_decisions=[],
                    released={dimension: 0 for dimension in DIMENSIONS},
                )

        for node_id in candidates:
            plan = self._build_mitigation_plan(job, node_id, urgency)
            if plan is None:
                continue
            supporting_ids: list[str] = []
            supporting_jobs: list[str] = []
            released_total = zero_vector()
            for action in plan["actions"]:
                decision, released = self._execute_action(
                    action, incoming_job=job, incoming_urgency=urgency
                )
                supporting_ids.append(decision["decision_id"])
                supporting_jobs.append(action["job_id"])
                released_total = vector_add(released_total, released)
            return self._admit(
                job,
                node_id,
                urgency,
                reason="CAPACITY_AVAILABLE_AFTER_RELEASE",
                supporting_decisions=supporting_ids,
                supporting_jobs=supporting_jobs,
                released=released_total,
            )

        target_nodes = ", ".join(candidates)
        return self._reject(
            job,
            urgency,
            "INSUFFICIENT_RESOURCES",
            None,
            detail={
                "candidate_nodes": candidates,
                "message": f"候选节点 {target_nodes} 均无法在低紧迫度让步后容纳作业",
            },
        )

    def _can_place(self, job: Mapping[str, Any], node_id: str) -> bool:
        free = vector_sub(
            self.nodes[node_id]["capacity"], self.node_used[node_id]
        )
        if not vector_leq(job["demand"], free):
            return False
        if job["mutex_group"] is None:
            return True
        return all(
            assignment["mutex_group"] != job["mutex_group"]
            for assignment in self.active.values()
            if assignment["node_id"] == node_id
        )

    def _admit(
        self,
        job: Mapping[str, Any],
        node_id: str,
        urgency: Mapping[str, Any],
        *,
        reason: str,
        supporting_decisions: list[str],
        supporting_jobs: list[str] | None = None,
        released: Mapping[str, int] | None = None,
    ) -> dict[str, Any]:
        if not self._can_place(job, node_id):
            raise RuntimeError(
                f"降级/迁移后重新核算仍无法接纳 {job['job_id']} 到 {node_id}"
            )
        decision = self._record_decision(
            decision_type="ADMIT",
            job_id=job["job_id"],
            node_id=node_id,
            reason=reason,
            urgency=urgency,
            related_job_ids=list(supporting_jobs or []),
            extra={
                "supporting_decision_ids": list(supporting_decisions),
                "released_total": copy.deepcopy(
                    released or {dimension: 0 for dimension in DIMENSIONS}
                ),
            },
        )
        state = self.job_state[job["job_id"]]
        state["status"] = "active"
        state["admitted_at"] = self.current_time
        state["current_node_id"] = node_id
        state["current_profile"] = "base"
        state["current_alloc"] = copy.deepcopy(job["demand"])
        assignment = {
            "job_id": job["job_id"],
            "node_id": node_id,
            "category": job["category"],
            "priority": int(job["priority"]),
            "deadline": int(job["deadline"]),
            "mutex_group": job["mutex_group"],
            "profile": "base",
            "allocation": copy.deepcopy(job["demand"]),
            "admitted_at": self.current_time,
            "finish_time": self.current_time + int(job["duration"]),
        }
        self.active[job["job_id"]] = assignment
        self._append_capacity_change(
            node_id=node_id,
            job_id=job["job_id"],
            delta=copy.deepcopy(job["demand"]),
            operation="ALLOCATE",
            reason=reason,
            decision_id=decision["decision_id"],
        )
        return decision

    def _reject(
        self,
        job: Mapping[str, Any],
        urgency: Mapping[str, Any],
        reason: str,
        node_id: str | None,
        *,
        detail: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        decision = self._record_decision(
            decision_type="REJECT",
            job_id=job["job_id"],
            node_id=node_id,
            reason=reason,
            urgency=urgency,
            extra={"detail": copy.deepcopy(dict(detail or {}))},
        )
        state = self.job_state[job["job_id"]]
        state["status"] = "rejected"
        state["rejected_at"] = self.current_time
        state["current_alloc"] = zero_vector()
        state["delivered_alloc"] = zero_vector()
        state["current_profile"] = None
        state["current_node_id"] = None
        state["deadline_met"] = False
        state["miss_reason"] = reason
        return decision

    # ------------------------------------------------------------------
    # Mitigation planning in an isolated virtual ledger
    # ------------------------------------------------------------------
    def _build_mitigation_plan(
        self,
        incoming_job: Mapping[str, Any],
        target_node_id: str,
        incoming_urgency: Mapping[str, Any],
    ) -> dict[str, Any] | None:
        virtual = {
            "used": {
                node_id: copy.deepcopy(self.node_used[node_id])
                for node_id in self.node_ids
            },
            "assignments": {
                job_id: copy.deepcopy(assignment)
                for job_id, assignment in self.active.items()
            },
        }
        target_assignments = [
            assignment
            for assignment in virtual["assignments"].values()
            if assignment["node_id"] == target_node_id
        ]
        conflict = [
            assignment
            for assignment in target_assignments
            if incoming_job["mutex_group"] is not None
            and assignment["mutex_group"] == incoming_job["mutex_group"]
        ]
        for assignment in conflict:
            if not self._is_lower_urgency(
                assignment["job_id"], incoming_urgency
            ) or not self.jobs[assignment["job_id"]]["preemptible"]:
                return None

        victims = [
            assignment
            for assignment in target_assignments
            if self.jobs[assignment["job_id"]]["preemptible"]
            and self._is_lower_urgency(assignment["job_id"], incoming_urgency)
        ]
        victims.sort(
            key=lambda assignment: (
                compute_urgency(
                    self.jobs[assignment["job_id"]], self.current_time
                )["urgency_rank"],
                assignment["job_id"],
            )
        )

        actions: list[dict[str, Any]] = []
        for victim in victims:
            job_id = victim["job_id"]
            current_victim = virtual["assignments"].get(job_id)
            if current_victim is None:
                continue
            conflict_now = (
                incoming_job["mutex_group"] is not None
                and current_victim["mutex_group"] == incoming_job["mutex_group"]
            )
            action: dict[str, Any] | None = None

            if not conflict_now:
                action = self._plan_demotion(
                    incoming_job,
                    target_node_id,
                    current_victim,
                    virtual,
                )
            if action is None and not conflict_now:
                action = self._plan_migration(current_victim, virtual)
            if action is None:
                action = {
                    "kind": "preempt",
                    "job_id": job_id,
                    "node_id": current_victim["node_id"],
                    "allocation": copy.deepcopy(
                        current_victim["allocation"]
                    ),
                }

            self._apply_virtual_action(virtual, action)
            actions.append(action)
            if self._virtual_can_place(virtual, incoming_job, target_node_id):
                return {
                    "target_node_id": target_node_id,
                    "actions": actions,
                }

        return None

    def _plan_demotion(
        self,
        incoming_job: Mapping[str, Any],
        target_node_id: str,
        victim: Mapping[str, Any],
        virtual: Mapping[str, Any],
    ) -> dict[str, Any] | None:
        job = self.jobs[victim["job_id"]]
        plan = job.get("downgrade_plan")
        if plan is None or victim["profile"] != "base":
            return None
        target_free = vector_sub(
            self.nodes[target_node_id]["capacity"],
            virtual["used"][target_node_id],
        )
        useful = any(
            plan["release"][dimension] > 0
            and target_free[dimension] < incoming_job["demand"][dimension]
            for dimension in DIMENSIONS
        )
        if not useful:
            return None
        return {
            "kind": "demote",
            "job_id": victim["job_id"],
            "node_id": victim["node_id"],
            "previous_allocation": copy.deepcopy(victim["allocation"]),
            "new_allocation": copy.deepcopy(plan["demand"]),
            "release": copy.deepcopy(plan["release"]),
            "profile": plan["profile"],
        }

    def _plan_migration(
        self,
        victim: Mapping[str, Any],
        virtual: Mapping[str, Any],
    ) -> dict[str, Any] | None:
        source_node_id = victim["node_id"]
        for node_id in self.node_ids:
            if node_id == source_node_id:
                continue
            if victim["category"] not in self.nodes[node_id]["categories"]:
                continue
            free = vector_sub(
                self.nodes[node_id]["capacity"], virtual["used"][node_id]
            )
            if not vector_leq(victim["allocation"], free):
                continue
            if victim["mutex_group"] is not None and any(
                other["node_id"] == node_id
                and other["mutex_group"] == victim["mutex_group"]
                for other in virtual["assignments"].values()
                if other["job_id"] != victim["job_id"]
            ):
                continue
            return {
                "kind": "migrate",
                "job_id": victim["job_id"],
                "from_node_id": source_node_id,
                "to_node_id": node_id,
                "allocation": copy.deepcopy(victim["allocation"]),
            }
        return None

    def _apply_virtual_action(
        self, virtual: dict[str, Any], action: Mapping[str, Any]
    ) -> None:
        kind = action["kind"]
        job_id = action["job_id"]
        assignment = virtual["assignments"][job_id]
        if kind == "demote":
            release = action["release"]
            virtual["used"][assignment["node_id"]] = vector_sub(
                virtual["used"][assignment["node_id"]], release
            )
            assignment["allocation"] = copy.deepcopy(action["new_allocation"])
            assignment["profile"] = action["profile"]
        elif kind == "migrate":
            virtual["used"][action["from_node_id"]] = vector_sub(
                virtual["used"][action["from_node_id"]], action["allocation"]
            )
            virtual["used"][action["to_node_id"]] = vector_add(
                virtual["used"][action["to_node_id"]], action["allocation"]
            )
            assignment["node_id"] = action["to_node_id"]
        elif kind == "preempt":
            virtual["used"][assignment["node_id"]] = vector_sub(
                virtual["used"][assignment["node_id"]], action["allocation"]
            )
            del virtual["assignments"][job_id]
        else:
            raise RuntimeError(f"未知虚拟动作: {kind}")

    def _virtual_can_place(
        self,
        virtual: Mapping[str, Any],
        job: Mapping[str, Any],
        node_id: str,
    ) -> bool:
        free = vector_sub(
            self.nodes[node_id]["capacity"], virtual["used"][node_id]
        )
        if not vector_leq(job["demand"], free):
            return False
        if job["mutex_group"] is None:
            return True
        return all(
            assignment["mutex_group"] != job["mutex_group"]
            for assignment in virtual["assignments"].values()
            if assignment["node_id"] == node_id
        )

    def _is_lower_urgency(
        self, other_job_id: str, incoming_urgency: Mapping[str, Any]
    ) -> bool:
        other = compute_urgency(self.jobs[other_job_id], self.current_time)
        return tuple(other["urgency_rank"]) < tuple(incoming_urgency["urgency_rank"])

    # ------------------------------------------------------------------
    # Applying committed actions
    # ------------------------------------------------------------------
    def _execute_action(
        self,
        action: Mapping[str, Any],
        *,
        incoming_job: Mapping[str, Any],
        incoming_urgency: Mapping[str, Any],
    ) -> tuple[dict[str, Any], dict[str, int]]:
        kind = action["kind"]
        victim_job = self.jobs[action["job_id"]]
        victim_urgency = compute_urgency(victim_job, self.current_time)
        if kind == "demote":
            decision = self._record_decision(
                decision_type="DOWNGRADE",
                job_id=action["job_id"],
                node_id=action["node_id"],
                reason="LOWER_URGENCY_DEMOTED",
                urgency=victim_urgency,
                related_job_ids=[incoming_job["job_id"]],
                extra={
                    "action": "DEMOTE",
                    "requesting_job_id": incoming_job["job_id"],
                    "profile": action["profile"],
                    "released": copy.deepcopy(action["release"]),
                },
            )
            assignment = self.active[action["job_id"]]
            old_alloc = copy.deepcopy(assignment["allocation"])
            new_alloc = copy.deepcopy(action["new_allocation"])
            delta = vector_sub(new_alloc, old_alloc)
            assignment["allocation"] = new_alloc
            assignment["profile"] = action["profile"]
            state = self.job_state[action["job_id"]]
            state["current_alloc"] = copy.deepcopy(new_alloc)
            state["current_profile"] = action["profile"]
            state["downgraded"] = True
            self._append_capacity_change(
                node_id=action["node_id"],
                job_id=action["job_id"],
                delta=delta,
                operation="DEMOTE_RELEASE",
                reason="降级预案回写容量账本",
                decision_id=decision["decision_id"],
            )
            return decision, copy.deepcopy(action["release"])

        if kind == "migrate":
            decision = self._record_decision(
                decision_type="PREEMPT",
                job_id=action["job_id"],
                node_id=action["from_node_id"],
                reason="LOWER_URGENCY_MIGRATED",
                urgency=victim_urgency,
                related_job_ids=[incoming_job["job_id"]],
                extra={
                    "action": "MIGRATE",
                    "requesting_job_id": incoming_job["job_id"],
                    "target_node_id": action["to_node_id"],
                },
            )
            assignment = self.active[action["job_id"]]
            allocation = copy.deepcopy(assignment["allocation"])
            self._append_capacity_change(
                node_id=action["from_node_id"],
                job_id=action["job_id"],
                delta={dimension: -allocation[dimension] for dimension in DIMENSIONS},
                operation="MIGRATE_RELEASE",
                reason="迁移释放源节点容量",
                decision_id=decision["decision_id"],
            )
            self._append_capacity_change(
                node_id=action["to_node_id"],
                job_id=action["job_id"],
                delta=allocation,
                operation="MIGRATE_ALLOCATE",
                reason="迁移占用目标节点容量",
                decision_id=decision["decision_id"],
            )
            assignment["node_id"] = action["to_node_id"]
            state = self.job_state[action["job_id"]]
            state["current_node_id"] = action["to_node_id"]
            state["migration_count"] += 1
            return decision, zero_vector()

        if kind == "preempt":
            decision = self._record_decision(
                decision_type="PREEMPT",
                job_id=action["job_id"],
                node_id=action["node_id"],
                reason="LOWER_URGENCY_PREEMPTED",
                urgency=victim_urgency,
                related_job_ids=[incoming_job["job_id"]],
                extra={
                    "action": "EVICT",
                    "requesting_job_id": incoming_job["job_id"],
                },
            )
            assignment = self.active[action["job_id"]]
            allocation = copy.deepcopy(assignment["allocation"])
            self._release_assignment(
                assignment,
                operation="PREEMPT_RELEASE",
                reason="高紧迫度准入触发抢占",
                decision_id=decision["decision_id"],
            )
            self.active.pop(action["job_id"])
            state = self.job_state[action["job_id"]]
            state["status"] = "preempted"
            state["preempted_at"] = self.current_time
            state["preemption_count"] += 1
            state["current_alloc"] = zero_vector()
            state["delivered_alloc"] = zero_vector()
            state["current_profile"] = None
            state["current_node_id"] = None
            state["deadline_met"] = False
            state["miss_reason"] = "PREEMPTED"
            return decision, allocation

        raise RuntimeError(f"未知动作: {kind}")

    # ------------------------------------------------------------------
    # Journal helpers
    # ------------------------------------------------------------------
    def _record_decision(
        self,
        *,
        decision_type: str,
        job_id: str,
        node_id: str | None,
        reason: str,
        urgency: Mapping[str, Any],
        related_job_ids: Iterable[str] | None = None,
        extra: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        if decision_type not in DECISION_TYPES:
            raise RuntimeError(f"非法决策类型: {decision_type}")
        self.decision_seq += 1
        job = self.jobs[job_id]
        decision = {
            "decision_id": f"D{self.decision_seq:06d}",
            "at": self.current_time,
            "sequence": self.decision_seq,
            "type": decision_type,
            "job_id": job_id,
            "category": job["category"],
            "priority": int(job["priority"]),
            "mutex_group": job["mutex_group"],
            "node_id": node_id,
            "reason": reason,
            "reason_text": REASON_TEXT.get(reason, reason),
            "urgency": copy.deepcopy(dict(urgency)),
            "related_job_ids": sorted(set(related_job_ids or [])),
        }
        if extra:
            decision.update(copy.deepcopy(dict(extra)))
        self.decisions.append(decision)
        self.job_state[job_id]["decision_ids"].append(decision["decision_id"])
        return decision

    def _append_capacity_change(
        self,
        *,
        node_id: str,
        job_id: str,
        delta: Mapping[str, int],
        operation: str,
        reason: str,
        decision_id: str | None = None,
        event_id: str | None = None,
    ) -> dict[str, Any]:
        if decision_id is None and event_id is None:
            raise RuntimeError("容量变更必须关联决策或事件")
        if set(delta) != set(DIMENSIONS):
            raise RuntimeError("容量变更必须包含三个维度")
        before_used = copy.deepcopy(self.node_used[node_id])
        capacity = self.nodes[node_id]["capacity"]
        before_free = vector_sub(capacity, before_used)
        after_used = vector_add(before_used, delta)
        after_free = vector_sub(capacity, after_used)
        if not vector_nonnegative(after_used) or not vector_nonnegative(after_free):
            raise RuntimeError(
                f"容量变更越界: node={node_id} job={job_id} delta={delta}"
            )
        self.node_used[node_id] = after_used
        self.change_seq += 1
        cause = (
            {"kind": "decision", "id": decision_id}
            if decision_id is not None
            else {"kind": "event", "id": event_id}
        )
        change = {
            "change_id": f"C{self.change_seq:06d}",
            "at": self.current_time,
            "sequence": self.change_seq,
            "node_id": node_id,
            "job_id": job_id,
            "operation": operation,
            "delta": copy.deepcopy(dict(delta)),
            "before": {
                "capacity": copy.deepcopy(capacity),
                "used": before_used,
                "free": before_free,
            },
            "after": {
                "capacity": copy.deepcopy(capacity),
                "used": copy.deepcopy(after_used),
                "free": copy.deepcopy(after_free),
            },
            "decision_id": decision_id,
            "event_id": event_id,
            "cause": cause,
            "reason": reason,
        }
        self.capacity_changes.append(change)
        return change

    def _release_assignment(
        self,
        assignment: Mapping[str, Any],
        *,
        operation: str,
        reason: str,
        decision_id: str | None = None,
        event_id: str | None = None,
    ) -> None:
        allocation = assignment["allocation"]
        self._append_capacity_change(
            node_id=assignment["node_id"],
            job_id=assignment["job_id"],
            delta={dimension: -allocation[dimension] for dimension in DIMENSIONS},
            operation=operation,
            reason=reason,
            decision_id=decision_id,
            event_id=event_id,
        )

    def _set_current_allocation(
        self,
        job_id: str,
        allocation: Mapping[str, int],
        profile: str | None,
        node_id: str | None,
    ) -> None:
        state = self.job_state[job_id]
        state["current_alloc"] = copy.deepcopy(dict(allocation))
        state["current_profile"] = profile
        state["current_node_id"] = node_id

    # ------------------------------------------------------------------
    # Snapshots, validation, and reporting
    # ------------------------------------------------------------------
    def _record_snapshot(self) -> None:
        self.snapshot_seq += 1
        nodes_snapshot: list[dict[str, Any]] = []
        total_capacity = zero_vector()
        total_used = zero_vector()
        total_free = zero_vector()
        for node_id in self.node_ids:
            capacity = copy.deepcopy(self.nodes[node_id]["capacity"])
            used = copy.deepcopy(self.node_used[node_id])
            free = vector_sub(capacity, used)
            assignments = [
                {
                    "job_id": assignment["job_id"],
                    "category": assignment["category"],
                    "profile": assignment["profile"],
                    "allocation": copy.deepcopy(assignment["allocation"]),
                    "mutex_group": assignment["mutex_group"],
                    "priority": assignment["priority"],
                    "deadline": assignment["deadline"],
                    "finish_time": assignment["finish_time"],
                }
                for assignment in sorted(
                    self.active.values(), key=lambda item: item["job_id"]
                )
                if assignment["node_id"] == node_id
            ]
            nodes_snapshot.append(
                {
                    "node_id": node_id,
                    "categories": copy.deepcopy(self.nodes[node_id]["categories"]),
                    "capacity": capacity,
                    "used": used,
                    "free": free,
                    "active_jobs": [item["job_id"] for item in assignments],
                    "active_assignments": assignments,
                }
            )
            total_capacity = vector_add(total_capacity, capacity)
            total_used = vector_add(total_used, used)
            total_free = vector_add(total_free, free)
        self.snapshots.append(
            {
                "snapshot_id": f"S{self.snapshot_seq:06d}",
                "at": self.current_time,
                "nodes": nodes_snapshot,
                "totals": {
                    "capacity": total_capacity,
                    "used": total_used,
                    "free": total_free,
                },
                "active_job_count": len(self.active),
                "decision_count": len(self.decisions),
                "capacity_change_count": len(self.capacity_changes),
            }
        )

    def check_invariants(self) -> list[str]:
        errors: list[str] = []
        for node_id in self.node_ids:
            capacity = self.nodes[node_id]["capacity"]
            used = self.node_used[node_id]
            free = vector_sub(capacity, used)
            if not vector_nonnegative(used):
                errors.append(f"{node_id} used 为负数: {used}")
            if not vector_nonnegative(free):
                errors.append(f"{node_id} free 为负数: {free}")
            if vector_add(used, free) != capacity:
                errors.append(f"{node_id} 账本不守恒: used+free != capacity")
            expected = zero_vector()
            mutex_groups: set[str] = set()
            for job_id, assignment in self.active.items():
                if assignment["node_id"] != node_id:
                    continue
                expected = vector_add(expected, assignment["allocation"])
                group = assignment["mutex_group"]
                if group is not None:
                    if group in mutex_groups:
                        errors.append(
                            f"{node_id} 互斥组 {group} 同时接纳多个作业"
                        )
                    mutex_groups.add(group)
                job = self.jobs[job_id]
                if job["category"] not in self.nodes[node_id]["categories"]:
                    errors.append(
                        f"{job_id} 被放到不接纳 {job['category']} 的 {node_id}"
                    )
                if self.job_state[job_id]["status"] != "active":
                    errors.append(f"{job_id} 活跃账本与作业状态不一致")
            if expected != used:
                errors.append(
                    f"{node_id} used 与活跃分配之和不一致: {used} != {expected}"
                )
            if not vector_leq(used, capacity):
                errors.append(f"{node_id} 占用量超过容量")

        decision_ids = [item["decision_id"] for item in self.decisions]
        if len(decision_ids) != len(set(decision_ids)):
            errors.append("决策标识重复")
        for index, decision in enumerate(self.decisions):
            if decision["sequence"] != index + 1:
                errors.append("决策序号不连续")
            if index and decision["at"] < self.decisions[index - 1]["at"]:
                errors.append("决策未按逻辑时刻有序")
        change_ids = [item["change_id"] for item in self.capacity_changes]
        if len(change_ids) != len(set(change_ids)):
            errors.append("容量变更标识重复")
        decision_id_set = set(decision_ids)
        for index, change in enumerate(self.capacity_changes):
            if change["sequence"] != index + 1:
                errors.append("容量变更序号不连续")
            if index and change["at"] < self.capacity_changes[index - 1]["at"]:
                errors.append("容量变更未按逻辑时刻有序")
            decision_id = change.get("decision_id")
            if decision_id is not None and decision_id not in decision_id_set:
                errors.append(
                    f"{change['change_id']} 引用了不存在的决策 {decision_id}"
                )
        for index, snapshot in enumerate(self.snapshots):
            if index and snapshot["at"] < self.snapshots[index - 1]["at"]:
                errors.append("快照未按逻辑时刻有序")
        return errors

    def _assert_invariants(self) -> None:
        errors = self.check_invariants()
        if errors:
            raise RuntimeError("账本不变量失败: " + "; ".join(errors))

    def _metric_allocation(self, job_id: str) -> dict[str, int]:
        state = self.job_state[job_id]
        if state["status"] == "completed":
            return copy.deepcopy(state["delivered_alloc"])
        if state["status"] == "active":
            return copy.deepcopy(state["current_alloc"])
        if state["status"] == "canceled" and state["delivered_alloc"]:
            return copy.deepcopy(state["delivered_alloc"])
        return zero_vector()

    def summarize(self) -> dict[str, Any]:
        by_category: dict[str, dict[str, Any]] = {}
        for category in CATEGORIES:
            by_category[category] = {
                "category": category,
                "category_code": None,
                "jobs_submitted": 0,
                "jobs_admitted": 0,
                "jobs_completed": 0,
                "jobs_preempted": 0,
                "jobs_rejected": 0,
                "jobs_canceled": 0,
                "jobs_downgraded": 0,
                "demand_totals": zero_vector(),
                "allocated_totals": zero_vector(),
                "deadline_met": 0,
                "deadline_missed": 0,
                "deadline_met_ratio": 0.0,
                "dimension_satisfaction": {
                    dimension: 0.0 for dimension in DIMENSIONS
                },
                "resource_satisfaction_ratio": 0.0,
                "average_job_satisfaction_ratio": 0.0,
            }

        job_reports: list[dict[str, Any]] = []
        submitted_states = [
            state
            for state in self.job_state.values()
            if state["status"] != "pending"
        ]
        for state in sorted(submitted_states, key=lambda item: item["job_id"]):
            job = self.jobs[state["job_id"]]
            category = job["category"]
            bucket = by_category[category]
            bucket["category_code"] = job.get("category_code")
            allocation = self._metric_allocation(state["job_id"])
            ratio = demand_ratio(allocation, job["demand"])
            status = state["status"]
            deadline_met = status == "completed" and bool(state["deadline_met"])
            if status == "canceled" and state["deadline_met"] is True:
                deadline_met = True

            bucket["jobs_submitted"] += 1
            bucket["demand_totals"] = vector_add(
                bucket["demand_totals"], job["demand"]
            )
            bucket["allocated_totals"] = vector_add(
                bucket["allocated_totals"], allocation
            )
            if state["admitted_at"] is not None:
                bucket["jobs_admitted"] += 1
            if status == "completed":
                bucket["jobs_completed"] += 1
            elif status == "preempted":
                bucket["jobs_preempted"] += 1
            elif status == "rejected":
                bucket["jobs_rejected"] += 1
            elif status == "canceled":
                bucket["jobs_canceled"] += 1
            if state["downgraded"]:
                bucket["jobs_downgraded"] += 1
            if deadline_met:
                bucket["deadline_met"] += 1
            else:
                bucket["deadline_missed"] += 1

            job_reports.append(
                {
                    "job_id": state["job_id"],
                    "category": category,
                    "category_code": job.get("category_code"),
                    "priority": int(job["priority"]),
                    "preemptible": bool(job["preemptible"]),
                    "submit_time": int(job["submit_time"]),
                    "deadline": int(job["deadline"]),
                    "status": status,
                    "admitted_at": state["admitted_at"],
                    "completed_at": state["completed_at"],
                    "preempted_at": state["preempted_at"],
                    "canceled_at": state["canceled_at"],
                    "rejected_at": state["rejected_at"],
                    "deadline_met": deadline_met,
                    "miss_reason": None if deadline_met else state["miss_reason"],
                    "downgraded": bool(state["downgraded"]),
                    "migration_count": int(state["migration_count"]),
                    "preemption_count": int(state["preemption_count"]),
                    "node_id": state["current_node_id"],
                    "profile": state["current_profile"],
                    "demand": copy.deepcopy(job["demand"]),
                    "allocated": allocation,
                    "resource_satisfaction_ratio": ratio,
                    "decision_ids": list(state["decision_ids"]),
                }
            )

        for category, bucket in by_category.items():
            submitted = bucket["jobs_submitted"]
            demand_total = vector_sum(bucket["demand_totals"])
            allocated_total = vector_sum(bucket["allocated_totals"])
            bucket["dimension_satisfaction"] = {
                dimension: (
                    round(
                        bucket["allocated_totals"][dimension]
                        / bucket["demand_totals"][dimension],
                        6,
                    )
                    if bucket["demand_totals"][dimension] > 0
                    else 0.0
                )
                for dimension in DIMENSIONS
            }
            bucket["resource_satisfaction_ratio"] = (
                round(allocated_total / demand_total, 6)
                if demand_total > 0
                else 0.0
            )
            bucket["average_job_satisfaction_ratio"] = (
                round(
                    sum(
                        item["resource_satisfaction_ratio"]
                        for item in job_reports
                        if item["category"] == category
                    )
                    / submitted,
                    6,
                )
                if submitted
                else 0.0
            )
            bucket["deadline_met_ratio"] = (
                round(bucket["deadline_met"] / submitted, 6)
                if submitted
                else 0.0
            )

        deadline_misses = [
            self._build_deadline_trace(report)
            for report in job_reports
            if not report["deadline_met"]
        ]
        return {
            "by_category": by_category,
            "jobs": job_reports,
            "deadline_misses": deadline_misses,
            "totals": {
                "jobs_submitted": sum(
                    item["jobs_submitted"] for item in by_category.values()
                ),
                "jobs_completed": sum(
                    item["jobs_completed"] for item in by_category.values()
                ),
                "deadline_met": sum(
                    item["deadline_met"] for item in by_category.values()
                ),
                "deadline_missed": sum(
                    item["deadline_missed"] for item in by_category.values()
                ),
            },
        }

    def _build_deadline_trace(self, report: Mapping[str, Any]) -> dict[str, Any]:
        job_id = report["job_id"]
        start = int(report["submit_time"])
        end = int(report["deadline"])
        own_nodes = {
            decision["node_id"]
            for decision in self.decisions
            if decision.get("node_id") is not None
            and (
                decision.get("job_id") == job_id
                or decision.get("requesting_job_id") == job_id
                or job_id in decision.get("related_job_ids", [])
            )
        }
        chain: list[dict[str, Any]] = []
        for decision in self.decisions:
            if not (start <= int(decision["at"]) <= end):
                continue
            involved = (
                decision.get("job_id") == job_id
                or decision.get("requesting_job_id") == job_id
                or job_id in decision.get("related_job_ids", [])
            )
            competing_node = decision.get("node_id") in own_nodes
            competing_category = decision.get("category") == report["category"]
            competing_mutex = (
                report.get("category") is not None
                and decision.get("mutex_group") is not None
                and decision.get("mutex_group")
                == self.jobs[job_id].get("mutex_group")
            )
            if involved or competing_node or competing_category or competing_mutex:
                chain.append(copy.deepcopy(decision))

        competition_job_ids: set[str] = set()
        for decision in chain:
            for candidate in (
                decision.get("job_id"),
                decision.get("requesting_job_id"),
            ):
                if candidate and candidate != job_id:
                    competition_job_ids.add(candidate)
            for candidate in decision.get("related_job_ids", []):
                if candidate != job_id:
                    competition_job_ids.add(candidate)
        return {
            "job_id": job_id,
            "category": report["category"],
            "priority": report["priority"],
            "submit_time": start,
            "deadline": end,
            "status": report["status"],
            "miss_reason": report["miss_reason"],
            "chain": chain,
            "chain_decision_ids": [
                decision["decision_id"] for decision in chain
            ],
            "competition_job_ids": sorted(competition_job_ids),
        }

    def result(self) -> dict[str, Any]:
        return {
            "schema_version": "1.0",
            "workload_hash": self.workload_hash,
            "seed": self.workload.get("seed"),
            "workload": copy.deepcopy(self.workload),
            "times": {
                "current": self.current_time,
                "event_index": self.event_index,
                "total_events": len(self.events),
                "finished": self.finished,
            },
            "decisions": copy.deepcopy(self.decisions),
            "capacity_changes": copy.deepcopy(self.capacity_changes),
            "urgency_table": copy.deepcopy(self.urgency_rows),
            "samples": copy.deepcopy(self.snapshots),
            "summary": self.summarize(),
            "invariant_errors": self.check_invariants(),
        }

    # ------------------------------------------------------------------
    # Persistence state
    # ------------------------------------------------------------------
    def export_state(self) -> dict[str, Any]:
        return {
            "current_time": self.current_time,
            "event_index": self.event_index,
            "decision_seq": self.decision_seq,
            "change_seq": self.change_seq,
            "snapshot_seq": self.snapshot_seq,
            "finished": self.finished,
            "node_used": copy.deepcopy(self.node_used),
            "active": copy.deepcopy(self.active),
            "job_state": copy.deepcopy(self.job_state),
            "decisions": copy.deepcopy(self.decisions),
            "capacity_changes": copy.deepcopy(self.capacity_changes),
            "urgency_rows": copy.deepcopy(self.urgency_rows),
            "snapshots": copy.deepcopy(self.snapshots),
        }

    @classmethod
    def from_checkpoint_content(
        cls, workload: Mapping[str, Any], state: Mapping[str, Any]
    ) -> "SchedulerEngine":
        engine = cls(workload)
        expected_keys = {
            "current_time",
            "event_index",
            "decision_seq",
            "change_seq",
            "snapshot_seq",
            "finished",
            "node_used",
            "active",
            "job_state",
            "decisions",
            "capacity_changes",
            "urgency_rows",
            "snapshots",
        }
        missing = expected_keys - set(state)
        if missing:
            raise ValueError(f"检查点状态缺少字段: {sorted(missing)}")
        engine.current_time = int(state["current_time"])
        engine.event_index = int(state["event_index"])
        engine.decision_seq = int(state["decision_seq"])
        engine.change_seq = int(state["change_seq"])
        engine.snapshot_seq = int(state["snapshot_seq"])
        engine.finished = bool(state["finished"])
        engine.node_used = copy.deepcopy(state["node_used"])
        engine.active = copy.deepcopy(state["active"])
        engine.job_state = copy.deepcopy(state["job_state"])
        engine.decisions = copy.deepcopy(state["decisions"])
        engine.capacity_changes = copy.deepcopy(state["capacity_changes"])
        engine.urgency_rows = copy.deepcopy(state["urgency_rows"])
        engine.snapshots = copy.deepcopy(state["snapshots"])
        if set(engine.node_used) != set(engine.node_ids):
            raise ValueError("检查点节点账本与作业工程不一致")
        if any(job_id not in engine.jobs for job_id in engine.active):
            raise ValueError("检查点包含未知活跃作业")
        if set(engine.job_state) != set(engine.jobs):
            raise ValueError("检查点作业状态集合不完整")
        if not (0 <= engine.event_index <= len(engine.events)):
            raise ValueError("检查点 event_index 越界")
        engine._assert_invariants()
        return engine

