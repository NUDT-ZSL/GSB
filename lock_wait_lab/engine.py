"""Deterministic logical-clock transaction and lock-wait engine."""

from __future__ import annotations

import copy
import os
import signal
import time
from pathlib import Path
from typing import Any, Iterable

from .canonical import (
    budget_trigger_ns,
    convert_duration,
    sha256_value,
)
from .errors import ErrorCode, LabError
from .samples import PolicyView, SampleBundle
from .store import Store


TERMINAL_STATUSES = {
    "COMMITTED",
    "DEADLOCK_VICTIM_ROLLBACK",
    "ABANDONED_WAIT_TIMEOUT",
    "MANUAL_ROLLBACK",
    "NOT_STARTED_WITHIN_BUDGET",
    "ABANDONED_AT_HORIZON",
    "ACTIVE_ROLLBACK_AT_HORIZON",
}


class CrashInjected(RuntimeError):
    def __init__(self, step: int) -> None:
        super().__init__(f"simulated durable crash after step {step}")
        self.step = step


def _tx_sort_key(tx: dict[str, Any]) -> tuple[int, int, str]:
    return (tx["start_at_ns"], tx["script_index"], tx["tx_id"])


def _stable_tx_order(tx: dict[str, Any]) -> tuple[int, str]:
    return (tx["script_index"], tx["tx_id"])


def resource_name(table: str, key: dict[str, Any], table_def: dict[str, Any]) -> str:
    values = ",".join(
        f"{column}={key[column]}" for column in table_def["primary_key"]
    )
    return f"{table}#{values}"


def create_initial_state(
    bundle: SampleBundle,
    run_id: str,
    *,
    parallelism: int | None = None,
    round_budget_ms: int | None = None,
    max_logical_ms: int | None = None,
) -> dict[str, Any]:
    v1 = bundle.policy_views[min(bundle.policy_views)]
    rows: dict[str, dict[str, Any]] = {}
    for table in bundle.database["tables"]:
        for row in table["rows"]:
            rows[resource_name(table["name"], row, table)] = dict(row)
    txs: dict[str, dict[str, Any]] = {}
    for script_index, transaction in enumerate(bundle.transactions):
        txs[transaction["tx_id"]] = {
            "tx_id": transaction["tx_id"],
            "script_index": script_index,
            "start_at_ns": transaction["start_at_ms"] * 1_000_000,
            "isolation": transaction["isolation"],
            "retry_limit": transaction["retry_limit"],
            "retry_used": 0,
            "operations": copy.deepcopy(transaction["operations"]),
            "pc": 0,
            "state": "PENDING",
            "final_status": None,
            "reason": None,
            "bound_generation": None,
            "bound_policy_summary": None,
            "bound_scale": None,
            "bound_tolerance_ns": None,
            "workspace": {},
            "read_cache": {},
            "reads": [],
            "waiting_resource": None,
            "waiting_entry_id": None,
            "ready_at_ns": None,
            "started_ns": None,
            "finished_ns": None,
            "wait_attempts": 0,
        }
    chosen_parallelism = int(
        parallelism if parallelism is not None else bundle.clock["parallelism"]
    )
    chosen_round_budget_ms = int(
        round_budget_ms
        if round_budget_ms is not None
        else bundle.clock["round_budget_ms"]
    )
    chosen_max_logical_ms = int(
        max_logical_ms
        if max_logical_ms is not None
        else bundle.clock["max_logical_ms"]
    )
    if chosen_parallelism < 1:
        raise LabError(
            ErrorCode.SCHEMA_INVALID,
            "parallelism must be positive",
            field="parallelism",
        )
    if chosen_round_budget_ms < 1:
        raise LabError(
            ErrorCode.SCHEMA_INVALID,
            "round budget must be positive",
            field="round_budget_ms",
        )
    if chosen_max_logical_ms < 1:
        raise LabError(
            ErrorCode.SCHEMA_INVALID,
            "logical budget must be positive",
            field="max_logical_ms",
        )
    switch_at_ns = max(
        bundle.clock["policy_switch_at_ms"] * 1_000_000,
        max(view.effective_ms for view in bundle.policy_views.values()) * 1_000_000,
    )
    state: dict[str, Any] = {
        "state_schema": 1,
        "run_id": run_id,
        "bundle_fingerprint": bundle.fingerprint,
        "samples_root": str(bundle.root.resolve()),
        "run_status": "READY",
        "run_reason": None,
        "logical_ns": 0,
        "step": 0,
        "max_logical_ns": chosen_max_logical_ms * 1_000_000,
        "parallelism": chosen_parallelism,
        "round_budget_ns": chosen_round_budget_ms * 1_000_000,
        "policy_switch_at_ns": switch_at_ns,
        "policy_switch_done": False,
        "txs": txs,
        "rows": rows,
        "locks": {},
        "queues": {},
        "ledger_entries": [],
        "conversions": [],
        "sensitive_records": [],
        "decisions": [],
        "retry_events": [],
        "round_summaries": [],
        "policy": {
            "generation": v1.generation,
            "summary": v1.summary,
            "scale": v1.scale,
            "tolerance_ns": v1.tolerance_ns,
            "ledger_version": 1,
            "last_commit_seq": 0,
            "ledger_created_step": 0,
            "ledger_created_ns": 0,
            "switches": [],
        },
        "policy_activations": [
            {
                "generation": v1.generation,
                "summary": v1.summary,
                "logical_ns": 0,
                "reason": "initial_generation",
            }
        ],
        "next_entry_sequence": 1,
        "next_conversion_sequence": 1,
        "next_sensitive_sequence": 1,
        "next_decision_sequence": 1,
        "resume_count": 0,
        "last_event": "initialized",
    }
    return state


class Engine:
    def __init__(
        self,
        bundle: SampleBundle,
        store: Store,
        run_id: str = "main",
    ) -> None:
        self.bundle = bundle
        self.store = store
        self.run_id = run_id
        self.state = self.store.load_state(run_id)

    @classmethod
    def initialize(
        cls,
        bundle: SampleBundle,
        store: Store,
        run_id: str = "main",
        *,
        reset: bool = False,
        parallelism: int | None = None,
        round_budget_ms: int | None = None,
        max_logical_ms: int | None = None,
    ) -> "Engine":
        existing = store.get_run(run_id)
        if existing is not None and reset:
            store.reset_run(run_id)
            existing = None
        if existing is not None:
            state = store.load_state(run_id)
            if state["bundle_fingerprint"] != bundle.fingerprint:
                raise LabError(
                    ErrorCode.STATE_INVALID,
                    "existing run was created from a different sample bundle; use --reset",
                    field="bundle_fingerprint",
                )
            adjusted = False
            if parallelism is not None and state["parallelism"] != parallelism:
                state["parallelism"] = parallelism
                adjusted = True
            if (
                round_budget_ms is not None
                and state["round_budget_ns"] != round_budget_ms * 1_000_000
            ):
                state["round_budget_ns"] = round_budget_ms * 1_000_000
                adjusted = True
            if (
                max_logical_ms is not None
                and state["max_logical_ns"] != max_logical_ms * 1_000_000
            ):
                state["max_logical_ns"] = max_logical_ms * 1_000_000
                adjusted = True
            if adjusted and state["step"] > 0:
                raise LabError(
                    ErrorCode.STATE_INVALID,
                    "execution limits cannot be changed after the first committed step",
                    field="run_id",
                )
            if adjusted:
                store.save_checkpoint(
                    run_id,
                    state,
                    {"kind": "initialized", "step": 0, "logical_ns": 0},
                )
            return cls(bundle, store, run_id)
        store.ensure_policies(bundle)
        state = create_initial_state(
            bundle,
            run_id,
            parallelism=parallelism,
            round_budget_ms=round_budget_ms,
            max_logical_ms=max_logical_ms,
        )
        store.save_checkpoint(
            run_id,
            state,
            {"kind": "initialized", "step": 0, "logical_ns": 0},
        )
        return cls(bundle, store, run_id)

    def policy_view(self, generation: int | None = None) -> PolicyView:
        chosen = (
            generation
            if generation is not None
            else int(self.state["policy"]["generation"])
        )
        return self.bundle.policy(chosen)

    def _entry_by_id(self, entry_id: str | None) -> dict[str, Any] | None:
        if entry_id is None:
            return None
        for entry in self.state["ledger_entries"]:
            if entry["entry_id"] == entry_id:
                return entry
        return None

    def _conversion_by_id(self, conversion_id: str | None) -> dict[str, Any] | None:
        if conversion_id is None:
            return None
        for conversion in self.state["conversions"]:
            if conversion["conversion_id"] == conversion_id:
                return conversion
        return None

    def _active_count(self) -> int:
        return sum(
            1
            for tx in self.state["txs"].values()
            if tx["state"] in {"READY", "RUNNING", "WAITING"}
        )

    def _terminal(self, tx: dict[str, Any]) -> bool:
        return tx["final_status"] in TERMINAL_STATUSES

    def _table(self, table_name: str) -> dict[str, Any]:
        return self.bundle.table(table_name)

    def _op_resource(self, operation: dict[str, Any]) -> str:
        table = self._table(operation["table"])
        return resource_name(operation["table"], operation["key"], table)

    def _record_conversion(
        self,
        *,
        conversion_id: str,
        source_kind: str,
        source_id: str,
        tx_id: str,
        resource: str | None,
        raw_ns: int,
        scale: int,
    ) -> dict[str, Any]:
        existing = self._conversion_by_id(conversion_id)
        if existing is not None:
            return existing
        fixed = convert_duration(raw_ns, scale)
        conversion = {
            "conversion_id": conversion_id,
            "sequence": self.state["next_conversion_sequence"],
            "source_kind": source_kind,
            "source_id": source_id,
            "tx_id": tx_id,
            "resource": resource,
            "raw_ns": fixed.raw_ns,
            "scale": fixed.scale,
            "rounded_ms": fixed.rounded_ms,
            "rounded_ns": fixed.rounded_ns,
            "rounding": fixed.rounding,
            "rounding_direction": fixed.direction,
            "discarded_remainder_ns": fixed.discarded_remainder_ns,
            "residual_ns": fixed.residual_ns,
            "error_bound_ns": fixed.error_bound_ns,
        }
        self.state["next_conversion_sequence"] += 1
        self.state["conversions"].append(conversion)
        return conversion

    def _conflict_blockers(
        self, tx_id: str, resource: str, mode: str
    ) -> list[str]:
        lock = self.state["locks"].get(resource)
        if lock is None:
            return []
        blockers: set[str] = set()
        for owner in lock["owners"]:
            if owner["tx_id"] == tx_id:
                continue
            if mode == "EXCLUSIVE" or owner["mode"] == "EXCLUSIVE":
                blockers.add(owner["tx_id"])
        return sorted(blockers)

    def _holder_dependency_item(
        self, tx_id: str, resource: str
    ) -> dict[str, Any] | None:
        lock = self.state["locks"].get(resource)
        if lock is None:
            return None
        for owner in lock["owners"]:
            if owner["tx_id"] != tx_id:
                continue
            conversion = self._conversion_by_id(owner.get("conversion_id"))
            return {
                "tx_id": tx_id,
                "resource": resource,
                "op_id": owner["op_id"],
                "holding_duration_ns": owner["hold_duration_ns"],
                "rounded_ms": conversion["rounded_ms"] if conversion else None,
                "error_bound_ns": (
                    conversion["error_bound_ns"] if conversion else 0
                ),
            }
        return None

    def _dependency_chain(
        self, blocker_tx_ids: Iterable[str], resource: str
    ) -> tuple[list[dict[str, Any]], int]:
        chain: list[dict[str, Any]] = []
        total = 0
        visited: set[str] = set()

        def visit(blocker: str) -> None:
            nonlocal total
            if blocker in visited:
                return
            visited.add(blocker)
            item = self._holder_dependency_item(blocker, resource)
            if item is not None:
                chain.append(item)
                total += int(item["error_bound_ns"])
            blocker_tx = self.state["txs"].get(blocker)
            if blocker_tx is None:
                return
            nested = self._entry_by_id(blocker_tx.get("waiting_entry_id"))
            if nested is not None:
                for nested_item in nested.get("dependency_chain", []):
                    key = (
                        nested_item.get("tx_id"),
                        nested_item.get("resource"),
                        nested_item.get("op_id"),
                    )
                    if any(
                        (
                            existing.get("tx_id"),
                            existing.get("resource"),
                            existing.get("op_id"),
                        )
                        == key
                        for existing in chain
                    ):
                        continue
                    chain.append(nested_item)
                    total += int(nested_item.get("error_bound_ns", 0))

        for blocker in sorted(set(blocker_tx_ids)):
            visit(blocker)
        chain.sort(
            key=lambda item: (
                str(item.get("tx_id")),
                str(item.get("resource")),
                str(item.get("op_id")),
            )
        )
        return chain, total

    def _grant_lock(
        self, tx: dict[str, Any], operation: dict[str, Any], resource: str, now: int
    ) -> None:
        lock = self.state["locks"].get(resource)
        if lock is None:
            lock = {"mode": operation["lock"], "owners": []}
            self.state["locks"][resource] = lock
        if operation["lock"] == "EXCLUSIVE":
            lock["mode"] = "EXCLUSIVE"
            lock["owners"] = [
                owner for owner in lock["owners"] if owner["tx_id"] != tx["tx_id"]
            ]
        else:
            existing = [
                owner for owner in lock["owners"] if owner["tx_id"] == tx["tx_id"]
            ]
            if existing:
                return
            if lock["mode"] != "EXCLUSIVE":
                lock["mode"] = "SHARED"
        conversion_id = f"HOLD:{tx['tx_id']}:{operation['op_id']}"
        conversion = self._record_conversion(
            conversion_id=conversion_id,
            source_kind="HOLD",
            source_id=operation["op_id"],
            tx_id=tx["tx_id"],
            resource=resource,
            raw_ns=operation["duration_ns"],
            scale=int(tx["bound_scale"]),
        )
        lock["owners"].append(
            {
                "tx_id": tx["tx_id"],
                "op_id": operation["op_id"],
                "mode": operation["lock"],
                "acquired_ns": now,
                "hold_duration_ns": operation["duration_ns"],
                "conversion_id": conversion["conversion_id"],
            }
        )
        lock["owners"].sort(key=lambda item: item["tx_id"])

    def _release_locks(self, tx: dict[str, Any]) -> None:
        for resource in sorted(self.state["locks"]):
            lock = self.state["locks"][resource]
            lock["owners"] = [
                owner for owner in lock["owners"] if owner["tx_id"] != tx["tx_id"]
            ]
            if not lock["owners"]:
                del self.state["locks"][resource]
            elif all(owner["mode"] == "SHARED" for owner in lock["owners"]):
                lock["mode"] = "SHARED"

    def _remove_queue_entry(self, tx: dict[str, Any]) -> None:
        entry_id = tx.get("waiting_entry_id")
        if entry_id is None:
            return
        for resource in sorted(self.state["queues"]):
            queue = self.state["queues"][resource]
            self.state["queues"][resource] = [
                item for item in queue if item != entry_id
            ]
            if not self.state["queues"][resource]:
                del self.state["queues"][resource]
        tx["waiting_entry_id"] = None
        tx["waiting_resource"] = None

    def _add_decision(
        self,
        tx: dict[str, Any],
        status: str,
        reason: str,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        decision = {
            "decision_seq": self.state["next_decision_sequence"],
            "tx_id": tx["tx_id"],
            "status": status,
            "reason": reason,
            "logical_ns": self.state["logical_ns"],
            "policy_generation": (
                tx["bound_generation"]
                if tx["bound_generation"] is not None
                else int(self.state["policy"]["generation"])
            ),
        }
        if extra:
            decision.update(extra)
        self.state["next_decision_sequence"] += 1
        self.state["decisions"].append(decision)
        return decision

    def _finalize_tx(
        self,
        tx: dict[str, Any],
        status: str,
        reason: str,
        *,
        extra: dict[str, Any] | None = None,
        commit_writes: bool = False,
    ) -> dict[str, Any]:
        if self._terminal(tx):
            return tx
        if commit_writes:
            for resource, values in sorted(tx["workspace"].items()):
                if resource in self.state["rows"]:
                    self.state["rows"][resource].update(values)
        self._remove_queue_entry(tx)
        self._release_locks(tx)
        tx["state"] = "DONE"
        tx["final_status"] = status
        tx["reason"] = reason
        tx["finished_ns"] = self.state["logical_ns"]
        tx["ready_at_ns"] = None
        return self._add_decision(tx, status, reason, extra)

    def _add_sensitive(self, record: dict[str, Any]) -> dict[str, Any]:
        for existing in self.state["sensitive_records"]:
            if existing["record_id"] == record["record_id"]:
                return existing
        record["sequence"] = self.state["next_sensitive_sequence"]
        self.state["next_sensitive_sequence"] += 1
        self.state["sensitive_records"].append(record)
        return record

    def _finalize_wait_entry(
        self, entry: dict[str, Any], dequeue_ns: int, status: str
    ) -> None:
        if entry.get("dequeue_ns") is not None:
            return
        duration_ns = max(0, dequeue_ns - entry["enqueue_ns"])
        fixed = convert_duration(duration_ns, entry["scale"])
        entry.update(
            {
                "dequeue_ns": dequeue_ns,
                "duration_ns": duration_ns,
                "rounded_ms": fixed.rounded_ms,
                "rounding": fixed.rounding,
                "rounding_direction": fixed.direction,
                "discarded_remainder_ns": fixed.discarded_remainder_ns,
                "residual_ns": fixed.residual_ns,
                "own_error_bound_ns": fixed.error_bound_ns,
                "cumulative_error_bound_ns": (
                    fixed.error_bound_ns + entry["dependency_error_bound_ns"]
                ),
                "status": status,
            }
        )
        self._record_conversion(
            conversion_id=f"WAIT:{entry['entry_id']}",
            source_kind="WAIT",
            source_id=entry["entry_id"],
            tx_id=entry["tx_id"],
            resource=entry["resource"],
            raw_ns=duration_ns,
            scale=entry["scale"],
        )
        tolerance_ns = int(entry["tolerance_ns"])
        budget_ns = int(entry["budget_ns"])
        if abs(duration_ns - budget_ns) < tolerance_ns:
            next_scale = min(6, entry["scale"] + 1)
            next_fixed = convert_duration(duration_ns, next_scale)
            verdict_at_scale = fixed.rounded_ns >= budget_ns
            verdict_at_next = next_fixed.rounded_ns >= budget_ns
            record = {
                "record_id": f"P:WAIT:{entry['entry_id']}",
                "kind": "WAIT_BUDGET_BOUNDARY",
                "tx_ids": [entry["tx_id"]],
                "resources": [entry["resource"]],
                "error_bound_ns": entry["cumulative_error_bound_ns"],
                "scale": entry["scale"],
                "next_scale": next_scale,
                "flip": verdict_at_scale != verdict_at_next,
                "duration_ns": duration_ns,
                "budget_ns": budget_ns,
                "difference_ns": abs(duration_ns - budget_ns),
                "tolerance_ns": tolerance_ns,
                "duration_ms": fixed.rounded_ms,
                "duration_ms_next_scale": next_fixed.rounded_ms,
                "verdict_at_scale": (
                    "WAIT_TIMEOUT" if verdict_at_scale else "WAIT_CONTINUES"
                ),
                "verdict_at_next_scale": (
                    "WAIT_TIMEOUT" if verdict_at_next else "WAIT_CONTINUES"
                ),
                "ledger_status": status,
                "dependency_chain": entry["dependency_chain"],
            }
            self._add_sensitive(record)

    def _start_tx(self, tx: dict[str, Any], now: int) -> dict[str, Any]:
        if tx["bound_generation"] is None:
            view = self.policy_view()
            tx["bound_generation"] = view.generation
            tx["bound_policy_summary"] = view.summary
            tx["bound_scale"] = view.scale
            tx["bound_tolerance_ns"] = view.tolerance_ns
        tx["state"] = "READY"
        tx["started_ns"] = now
        return {
            "kind": "start_transaction",
            "tx_id": tx["tx_id"],
            "policy_generation": tx["bound_generation"],
        }

    def _enqueue_wait(
        self,
        tx: dict[str, Any],
        operation: dict[str, Any],
        resource: str,
        now: int,
        blockers: list[str],
    ) -> dict[str, Any]:
        tx["wait_attempts"] += 1
        entry_id = (
            f"W:{tx['tx_id']}:{operation['op_id']}:"
            f"{tx['wait_attempts']:02d}"
        )
        view = self.policy_view(int(tx["bound_generation"]))
        base_budget_ms, retry_multiplier = view.resolve(
            tx["tx_id"], operation["table"], resource
        )
        budget_ms = base_budget_ms * (retry_multiplier**tx["retry_used"])
        budget_ns = budget_ms * 1_000_000
        trigger_ns = now + budget_trigger_ns(budget_ns, view.scale)
        chain, dependency_error = self._dependency_chain(blockers, resource)
        entry = {
            "entry_id": entry_id,
            "sequence": self.state["next_entry_sequence"],
            "tx_id": tx["tx_id"],
            "op_id": operation["op_id"],
            "resource": resource,
            "mode": operation["lock"],
            "enqueue_ns": now,
            "dequeue_ns": None,
            "duration_ns": None,
            "scale": view.scale,
            "rounded_ms": None,
            "rounding": "HALF_UP",
            "rounding_direction": None,
            "discarded_remainder_ns": None,
            "residual_ns": None,
            "own_error_bound_ns": 0,
            "dependency_error_bound_ns": dependency_error,
            "cumulative_error_bound_ns": dependency_error,
            "dependency_chain": chain,
            "policy_generation": view.generation,
            "policy_summary": view.summary,
            "tolerance_ns": view.tolerance_ns,
            "budget_ms": budget_ms,
            "base_budget_ms": base_budget_ms,
            "retry_used": tx["retry_used"],
            "budget_ns": budget_ns,
            "trigger_ns": trigger_ns,
            "blockers": sorted(blockers),
            "status": "WAITING",
        }
        self.state["next_entry_sequence"] += 1
        self.state["ledger_entries"].append(entry)
        self.state["queues"].setdefault(resource, []).append(entry_id)
        tx["state"] = "WAITING"
        tx["waiting_entry_id"] = entry_id
        tx["waiting_resource"] = resource
        return {
            "kind": "enqueue_wait",
            "tx_id": tx["tx_id"],
            "entry_id": entry_id,
            "resource": resource,
            "mode": operation["lock"],
            "budget_ms": budget_ms,
            "trigger_ns": trigger_ns,
            "blockers": sorted(blockers),
        }

    def _begin_operation(
        self, tx: dict[str, Any], now: int
    ) -> dict[str, Any]:
        operation = tx["operations"][tx["pc"]]
        if operation["kind"] == "ROLLBACK":
            self._finalize_tx(
                tx,
                "MANUAL_ROLLBACK",
                "EXPLICIT_ROLLBACK",
                extra={"operation_id": operation["op_id"]},
            )
            return {
                "kind": "manual_rollback",
                "tx_id": tx["tx_id"],
                "operation_id": operation["op_id"],
            }
        resource = self._op_resource(operation)
        blockers = self._conflict_blockers(tx["tx_id"], resource, operation["lock"])
        queue = self.state["queues"].get(resource, [])
        if blockers or queue:
            return self._enqueue_wait(tx, operation, resource, now, blockers)
        self._grant_lock(tx, operation, resource, now)
        tx["state"] = "RUNNING"
        tx["ready_at_ns"] = now + operation["duration_ns"]
        return {
            "kind": "acquire_lock",
            "tx_id": tx["tx_id"],
            "operation_id": operation["op_id"],
            "resource": resource,
            "mode": operation["lock"],
            "ready_at_ns": tx["ready_at_ns"],
        }

    def _grant_from_queue(
        self, resource: str, entry: dict[str, Any], now: int
    ) -> dict[str, Any]:
        tx = self.state["txs"][entry["tx_id"]]
        operation = tx["operations"][tx["pc"]]
        queue = self.state["queues"].get(resource, [])
        if not queue or queue[0] != entry["entry_id"]:
            raise LabError(
                ErrorCode.STATE_INVALID,
                "attempted FIFO grant out of order",
                field="wait_queue",
            )
        self._finalize_wait_entry(entry, now, "WAIT_GRANTED")
        queue.pop(0)
        if not queue:
            del self.state["queues"][resource]
        tx["waiting_entry_id"] = None
        tx["waiting_resource"] = None
        self._grant_lock(tx, operation, resource, now)
        tx["state"] = "RUNNING"
        tx["ready_at_ns"] = now + operation["duration_ns"]
        return {
            "kind": "wake_waiter",
            "tx_id": tx["tx_id"],
            "entry_id": entry["entry_id"],
            "resource": resource,
            "dequeue_ns": now,
            "ready_at_ns": tx["ready_at_ns"],
        }

    def _complete_operation(self, tx: dict[str, Any]) -> dict[str, Any]:
        operation = tx["operations"][tx["pc"]]
        resource = self._op_resource(operation)
        if operation["kind"] == "WRITE":
            values = operation.get("values", {})
            workspace = tx["workspace"].setdefault(resource, {})
            workspace.update(copy.deepcopy(values))
        else:
            row = copy.deepcopy(self.state["rows"][resource])
            if tx["isolation"] in {"REPEATABLE_READ", "SERIALIZABLE"}:
                row = copy.deepcopy(tx["read_cache"].setdefault(resource, row))
            else:
                tx["read_cache"][resource] = copy.deepcopy(row)
            tx["reads"].append(
                {
                    "resource": resource,
                    "value": row,
                    "logical_ns": self.state["logical_ns"],
                }
            )
        tx["pc"] += 1
        tx["ready_at_ns"] = None
        if tx["pc"] >= len(tx["operations"]):
            self._finalize_tx(
                tx,
                "COMMITTED",
                "ALL_OPERATIONS_COMPLETED",
                extra={
                    "operation_id": operation["op_id"],
                    "writes_applied": len(tx["workspace"]),
                },
                commit_writes=True,
            )
            return {
                "kind": "commit_transaction",
                "tx_id": tx["tx_id"],
                "operation_id": operation["op_id"],
            }
        tx["state"] = "READY"
        return {
            "kind": "complete_operation",
            "tx_id": tx["tx_id"],
            "operation_id": operation["op_id"],
            "next_operation_index": tx["pc"],
        }

    def _timeout_wait_entry(self, entry: dict[str, Any], now: int) -> dict[str, Any]:
        tx = self.state["txs"][entry["tx_id"]]
        self._remove_queue_entry(tx)
        self._finalize_wait_entry(entry, now, "WAIT_TIMEOUT")
        if tx["retry_used"] < tx["retry_limit"]:
            self._release_locks(tx)
            tx["retry_used"] += 1
            tx["pc"] = 0
            tx["workspace"] = {}
            tx["read_cache"] = {}
            tx["reads"] = []
            tx["state"] = "PENDING"
            tx["start_at_ns"] = now
            tx["ready_at_ns"] = None
            tx["waiting_entry_id"] = None
            tx["waiting_resource"] = None
            event = {
                "kind": "retry_after_wait_timeout",
                "tx_id": tx["tx_id"],
                "entry_id": entry["entry_id"],
                "retry_used": tx["retry_used"],
                "retry_limit": tx["retry_limit"],
            }
            self.state["retry_events"].append(dict(event))
            return event
        decision = self._finalize_tx(
            tx,
            "ABANDONED_WAIT_TIMEOUT",
            "WAIT_BUDGET_EXHAUSTED",
            extra={
                "entry_id": entry["entry_id"],
                "resource": entry["resource"],
                "budget_ns": entry["budget_ns"],
                "duration_ns": entry["duration_ns"],
                "rounded_ms": entry["rounded_ms"],
                "error_bound_ns": entry["cumulative_error_bound_ns"],
            },
        )
        return {
            "kind": "abandon_wait_timeout",
            "tx_id": tx["tx_id"],
            "entry_id": entry["entry_id"],
            "decision_seq": decision["decision_seq"],
        }

    def _wait_graph(self) -> dict[str, set[str]]:
        graph: dict[str, set[str]] = {
            tx_id: set() for tx_id in sorted(self.state["txs"])
        }
        for resource in sorted(self.state["queues"]):
            queue = self.state["queues"][resource]
            predecessors: list[str] = []
            for entry_id in queue:
                entry = self._entry_by_id(entry_id)
                if entry is None:
                    continue
                tx_id = entry["tx_id"]
                for blocker in self._conflict_blockers(
                    tx_id, resource, entry["mode"]
                ):
                    graph[tx_id].add(blocker)
                for predecessor in predecessors:
                    if predecessor != tx_id:
                        graph[tx_id].add(predecessor)
                predecessors.append(tx_id)
        return graph

    def _cyclic_components(self) -> list[list[str]]:
        graph = self._wait_graph()
        index = 0
        indices: dict[str, int] = {}
        lowlinks: dict[str, int] = {}
        stack: list[str] = []
        on_stack: set[str] = set()
        components: list[list[str]] = []

        def strongconnect(node: str) -> None:
            nonlocal index
            indices[node] = index
            lowlinks[node] = index
            index += 1
            stack.append(node)
            on_stack.add(node)
            for neighbor in sorted(graph.get(node, set())):
                if neighbor not in indices:
                    strongconnect(neighbor)
                    lowlinks[node] = min(lowlinks[node], lowlinks[neighbor])
                elif neighbor in on_stack:
                    lowlinks[node] = min(lowlinks[node], indices[neighbor])
            if lowlinks[node] == indices[node]:
                component: list[str] = []
                while True:
                    member = stack.pop()
                    on_stack.remove(member)
                    component.append(member)
                    if member == node:
                        break
                component.sort()
                has_cycle = len(component) > 1 or (
                    len(component) == 1 and component[0] in graph.get(component[0], set())
                )
                if has_cycle:
                    components.append(component)

        for node in sorted(graph):
            if node not in indices:
                strongconnect(node)
        components.sort(key=lambda component: (component[0], len(component)))
        return components

    def _deadlock_precision_record(
        self,
        cycle: list[str],
        candidates: list[dict[str, Any]],
        selected: str,
        selected_next: str,
    ) -> dict[str, Any] | None:
        overlap = False
        comparisons: list[dict[str, Any]] = []
        for candidate in candidates:
            interval_low = candidate["rounded_ns"] - candidate["error_bound_ns"]
            interval_high = candidate["rounded_ns"] + candidate["error_bound_ns"]
            comparisons.append(
                {
                    "tx_id": candidate["tx_id"],
                    "resource": candidate["resource"],
                    "wait_duration_ns": candidate["raw_duration_ns"],
                    "wait_duration_ms": candidate["rounded_ms"],
                    "rounded_ns": candidate["rounded_ns"],
                    "error_bound_ns": candidate["error_bound_ns"],
                    "interval_ns": [interval_low, interval_high],
                    "next_scale": candidate["next_scale"],
                    "wait_duration_ms_next_scale": candidate["next_rounded_ms"],
                }
            )
        for left_index in range(len(candidates)):
            for right_index in range(left_index + 1, len(candidates)):
                left = candidates[left_index]
                right = candidates[right_index]
                if (
                    abs(left["raw_duration_ns"] - right["raw_duration_ns"])
                    <= left["error_bound_ns"] + right["error_bound_ns"]
                ):
                    overlap = True
        if not overlap:
            return None
        scale = max(candidate["scale"] for candidate in candidates)
        next_scale = min(6, max(candidate["next_scale"] for candidate in candidates))
        record = {
            "record_id": (
                "P:DEADLOCK:"
                + ",".join(cycle)
                + f":{self.state['next_sensitive_sequence']:02d}"
            ),
            "kind": "DEADLOCK_VICTIM_SELECTION",
            "tx_ids": sorted(cycle),
            "resources": sorted({candidate["resource"] for candidate in candidates}),
            "error_bound_ns": max(
                candidate["error_bound_ns"] for candidate in candidates
            ),
            "scale": scale,
            "next_scale": next_scale,
            "flip": selected != selected_next,
            "cycle": cycle,
            "comparisons": comparisons,
            "selected_victim_at_scale": selected,
            "selected_victim_at_next_scale": selected_next,
        }
        return self._add_sensitive(record)

    def _resolve_deadlock(self, cycle: list[str]) -> dict[str, Any]:
        candidates: list[dict[str, Any]] = []
        for tx_id in cycle:
            tx = self.state["txs"][tx_id]
            entry = self._entry_by_id(tx.get("waiting_entry_id"))
            if entry is None:
                continue
            raw_duration = self.state["logical_ns"] - entry["enqueue_ns"]
            current_fixed = convert_duration(raw_duration, entry["scale"])
            next_scale = min(6, entry["scale"] + 1)
            next_fixed = convert_duration(raw_duration, next_scale)
            candidates.append(
                {
                    "tx_id": tx_id,
                    "resource": entry["resource"],
                    "entry": entry,
                    "raw_duration_ns": raw_duration,
                    "rounded_ns": current_fixed.rounded_ns,
                    "rounded_ms": current_fixed.rounded_ms,
                    "error_bound_ns": (
                        entry["dependency_error_bound_ns"]
                        + current_fixed.error_bound_ns
                    ),
                    "scale": entry["scale"],
                    "next_scale": next_scale,
                    "next_rounded_ns": next_fixed.rounded_ns,
                    "next_rounded_ms": next_fixed.rounded_ms,
                }
            )
        if not candidates:
            raise LabError(
                ErrorCode.STATE_INVALID,
                "deadlock cycle has no waiting candidate",
                field="wait_graph",
            )
        candidates.sort(key=lambda item: item["tx_id"])
        selected = max(
            candidates, key=lambda item: (item["rounded_ns"], item["tx_id"])
        )["tx_id"]
        selected_next = max(
            candidates, key=lambda item: (item["next_rounded_ns"], item["tx_id"])
        )["tx_id"]
        sensitive = self._deadlock_precision_record(
            cycle, candidates, selected, selected_next
        )
        victim = self.state["txs"][selected]
        entry = self._entry_by_id(victim["waiting_entry_id"])
        assert entry is not None
        self._remove_queue_entry(victim)
        self._finalize_wait_entry(entry, self.state["logical_ns"], "DEADLOCK_VICTIM")
        decision = self._finalize_tx(
            victim,
            "DEADLOCK_VICTIM_ROLLBACK",
            "DEADLOCK_CYCLE",
            extra={
                "cycle": cycle,
                "entry_id": entry["entry_id"],
                "resource": entry["resource"],
                "selected_by": "LONGEST_QUANTIZED_WAIT_THEN_MAX_TX_ID",
                "precision_record_id": (
                    sensitive["record_id"] if sensitive is not None else None
                ),
            },
        )
        return {
            "kind": "deadlock_victim_rollback",
            "cycle": cycle,
            "victim": selected,
            "decision_seq": decision["decision_seq"],
            "precision_record_id": (
                sensitive["record_id"] if sensitive is not None else None
            ),
        }

    def _earliest_timeout(self) -> dict[str, Any] | None:
        candidates: list[dict[str, Any]] = []
        now = int(self.state["logical_ns"])
        for resource in sorted(self.state["queues"]):
            for entry_id in self.state["queues"][resource]:
                entry = self._entry_by_id(entry_id)
                if entry is None or entry["trigger_ns"] > now:
                    continue
                candidates.append(entry)
        if not candidates:
            return None
        candidates.sort(
            key=lambda item: (item["trigger_ns"], item["enqueue_ns"], item["entry_id"])
        )
        return candidates[0]

    def _first_grantable_waiter(self) -> tuple[str, dict[str, Any]] | None:
        for resource in sorted(self.state["queues"]):
            queue = self.state["queues"][resource]
            if not queue:
                continue
            entry = self._entry_by_id(queue[0])
            if entry is None:
                continue
            if not self._conflict_blockers(entry["tx_id"], resource, entry["mode"]):
                return resource, entry
        return None

    def _switch_policy(self, now: int) -> dict[str, Any]:
        current_generation = int(self.state["policy"]["generation"])
        candidates = [
            view
            for view in self.bundle.policy_views.values()
            if view.generation > current_generation
            and view.effective_ms * 1_000_000 <= now
        ]
        if not candidates:
            self.state["policy_switch_done"] = True
            return {"kind": "policy_switch_complete", "logical_ns": now}
        target = sorted(candidates, key=lambda view: view.generation)[0]
        current = self.policy_view(current_generation)
        if target.generation <= current.generation:
            raise LabError(
                ErrorCode.POLICY_VERSION_ROLLBACK,
                "a policy switch cannot reduce the generation",
                field="generation",
            )
        if target.effective_ms < current.effective_ms:
            raise LabError(
                ErrorCode.POLICY_MONOTONIC,
                "a policy switch cannot reduce absolute effective time",
                field="effective_ms",
            )
        commit_seq = int(self.state["policy"]["last_commit_seq"]) + 1
        ledger_version = int(self.state["policy"]["ledger_version"]) + 1
        commit = {
            "commit_seq": commit_seq,
            "from_generation": current.generation,
            "to_generation": target.generation,
            "effective_ms": target.effective_ms,
            "from_summary": current.summary,
            "to_summary": target.summary,
            "ledger_version": ledger_version,
            "logical_ns": now,
            "replacement_mode": "WHOLE_LEDGER_POINTER",
        }
        self.state["policy"]["switches"].append(commit)
        self.state["policy"].update(
            {
                "generation": target.generation,
                "summary": target.summary,
                "scale": target.scale,
                "tolerance_ns": target.tolerance_ns,
                "ledger_version": ledger_version,
                "last_commit_seq": commit_seq,
                "ledger_created_step": self.state["step"] + 1,
                "ledger_created_ns": now,
            }
        )
        self.state["policy_activations"].append(
            {
                "generation": target.generation,
                "summary": target.summary,
                "logical_ns": now,
                "reason": "version_increment",
            }
        )
        return {
            "kind": "replace_policy_ledger",
            "from_generation": current.generation,
            "to_generation": target.generation,
            "effective_ms": target.effective_ms,
            "ledger_version": ledger_version,
            "commit_seq": commit_seq,
        }

    def activate_snapshot(self, generation: int) -> dict[str, Any]:
        current = int(self.state["policy"]["generation"])
        if generation < current:
            raise LabError(
                ErrorCode.POLICY_VERSION_ROLLBACK,
                f"cannot roll policy generation back from {current} to {generation}",
                field="generation",
            )
        if generation == current:
            return {
                "activated": False,
                "reason": "already_active",
                "generation": current,
            }
        target = self.bundle.policy(generation)
        if target.effective_ms * 1_000_000 > self.state["logical_ns"]:
            raise LabError(
                ErrorCode.POLICY_NOT_EFFECTIVE,
                f"generation {generation} is not effective at the current logical time",
                field="effective_ms",
                details={
                    "effective_ms": target.effective_ms,
                    "logical_ns": self.state["logical_ns"],
                },
            )
        event = self._switch_policy(self.state["logical_ns"])
        self.state["step"] += 1
        event["step"] = self.state["step"]
        self._refresh_rounds()
        self.store.save_checkpoint(self.run_id, self.state, event)
        return {"activated": True, "event": event}

    def _finalize_at_horizon(self, now: int) -> dict[str, Any]:
        self.state["logical_ns"] = now
        decisions: list[int] = []
        for tx in sorted(self.state["txs"].values(), key=_stable_tx_order):
            if self._terminal(tx):
                continue
            tx = self.state["txs"][tx["tx_id"]]
            if tx["state"] == "PENDING":
                decision = self._finalize_tx(
                    tx,
                    "NOT_STARTED_WITHIN_BUDGET",
                    "RUN_BUDGET_NOT_STARTED",
                    extra={"start_at_ns": tx["start_at_ns"]},
                )
            elif tx["state"] == "WAITING":
                entry = self._entry_by_id(tx.get("waiting_entry_id"))
                if entry is not None:
                    self._finalize_wait_entry(entry, now, "ABANDONED_AT_HORIZON")
                decision = self._finalize_tx(
                    tx,
                    "ABANDONED_AT_HORIZON",
                    "RUN_BUDGET_EXHAUSTED_WHILE_WAITING",
                    extra={
                        "entry_id": (
                            entry["entry_id"] if entry is not None else None
                        ),
                        "resource": (
                            entry["resource"] if entry is not None else None
                        ),
                    },
                )
            else:
                decision = self._finalize_tx(
                    tx,
                    "ACTIVE_ROLLBACK_AT_HORIZON",
                    "RUN_BUDGET_EXHAUSTED_WHILE_ACTIVE",
                )
            decisions.append(decision["decision_seq"])
        self.state["run_status"] = "COMPLETED"
        self.state["run_reason"] = "LOGICAL_BUDGET_REACHED"
        return {
            "kind": "finalize_logical_budget",
            "logical_ns": now,
            "decision_sequences": decisions,
        }

    def _finalize_all_done(self) -> dict[str, Any]:
        self.state["run_status"] = "COMPLETED"
        self.state["run_reason"] = "ALL_TRANSACTIONS_FINALIZED"
        return {
            "kind": "finalize_all_done",
            "logical_ns": self.state["logical_ns"],
        }

    def _next_event_time(self) -> int | None:
        now = int(self.state["logical_ns"])
        candidates: list[int] = []
        if not self.state["policy_switch_done"]:
            if self.state["policy_switch_at_ns"] > now:
                candidates.append(int(self.state["policy_switch_at_ns"]))
        for tx in self.state["txs"].values():
            if tx["state"] == "PENDING" and tx["start_at_ns"] > now:
                candidates.append(int(tx["start_at_ns"]))
            if tx["state"] == "RUNNING" and tx["ready_at_ns"] is not None:
                if tx["ready_at_ns"] > now:
                    candidates.append(int(tx["ready_at_ns"]))
            if tx["state"] == "WAITING" and tx["waiting_entry_id"] is not None:
                entry = self._entry_by_id(tx["waiting_entry_id"])
                if entry is not None and entry["trigger_ns"] > now:
                    candidates.append(int(entry["trigger_ns"]))
        return min(candidates) if candidates else None

    def _step(self) -> dict[str, Any]:
        now = int(self.state["logical_ns"])
        if self.state["run_status"] == "READY":
            self.state["run_status"] = "RUNNING"
        if now >= self.state["max_logical_ns"]:
            return self._finalize_at_horizon(int(self.state["max_logical_ns"]))
        if all(self._terminal(tx) for tx in self.state["txs"].values()):
            return self._finalize_all_done()
        if (
            not self.state["policy_switch_done"]
            and now >= self.state["policy_switch_at_ns"]
        ):
            return self._switch_policy(now)
        cycles = self._cyclic_components()
        if cycles:
            return self._resolve_deadlock(cycles[0])
        timeout_entry = self._earliest_timeout()
        if timeout_entry is not None:
            return self._timeout_wait_entry(timeout_entry, now)
        running = [
            tx
            for tx in self.state["txs"].values()
            if tx["state"] == "RUNNING"
            and tx["ready_at_ns"] is not None
            and tx["ready_at_ns"] <= now
        ]
        if running:
            running.sort(
                key=lambda tx: (tx["ready_at_ns"], tx["script_index"], tx["tx_id"])
            )
            return self._complete_operation(running[0])
        grantable = self._first_grantable_waiter()
        if grantable is not None:
            resource, entry = grantable
            return self._grant_from_queue(resource, entry, now)
        if self._active_count() < self.state["parallelism"]:
            pending = [
                tx
                for tx in self.state["txs"].values()
                if tx["state"] == "PENDING" and tx["start_at_ns"] <= now
            ]
            if pending:
                pending.sort(key=_tx_sort_key)
                return self._start_tx(pending[0], now)
        ready = [
            tx
            for tx in self.state["txs"].values()
            if tx["state"] == "READY" and tx["ready_at_ns"] is None
        ]
        if ready:
            ready.sort(key=_stable_tx_order)
            return self._begin_operation(ready[0], now)
        next_time = self._next_event_time()
        if next_time is None:
            return self._finalize_all_done()
        if next_time > self.state["max_logical_ns"]:
            return self._finalize_at_horizon(int(self.state["max_logical_ns"]))
        self.state["logical_ns"] = next_time
        return {
            "kind": "advance_logical_clock",
            "from_ns": now,
            "to_ns": next_time,
        }

    def _refresh_rounds(self) -> None:
        budget = int(self.state["round_budget_ns"])
        horizon = int(self.state["max_logical_ns"])
        current = max(0, int(self.state["logical_ns"]))
        round_count = max(1, (current + budget - 1) // budget)
        summaries: list[dict[str, Any]] = []
        for round_no in range(round_count):
            start_ns = round_no * budget
            end_ns = start_ns + budget
            pending_at_end: list[str] = []
            started: list[str] = []
            finalized: list[str] = []
            for tx in sorted(self.state["txs"].values(), key=_stable_tx_order):
                if tx["started_ns"] is not None and start_ns <= tx["started_ns"] < end_ns:
                    started.append(tx["tx_id"])
                if tx["finished_ns"] is not None and start_ns <= tx["finished_ns"] < end_ns:
                    finalized.append(tx["tx_id"])
                finished = tx["finished_ns"]
                if finished is None or finished > end_ns:
                    if end_ns <= horizon or round_no < round_count - 1:
                        pending_at_end.append(tx["tx_id"])
            summaries.append(
                {
                    "round_no": round_no,
                    "start_ns": start_ns,
                    "end_ns": end_ns,
                    "pending": pending_at_end,
                    "started": started,
                    "finalized": finalized,
                }
            )
        self.state["round_summaries"] = summaries

    def run(
        self,
        *,
        max_steps: int = 100000,
        crash_after_step: int | None = None,
        pause_after_step: int | None = None,
        pause_seconds: float = 30.0,
        simulate_crash: bool = False,
    ) -> dict[str, Any]:
        if self.state["run_status"] == "COMPLETED":
            return self.summary()
        starting_step = int(self.state["step"])
        actions = 0
        while self.state["run_status"] != "COMPLETED":
            if actions >= max_steps:
                raise LabError(
                    ErrorCode.RUN_INCOMPLETE,
                    "execution did not converge within max_steps",
                    field="max_steps",
                    details={"step": self.state["step"]},
                )
            event = self._step()
            self.state["step"] += 1
            event["step"] = self.state["step"]
            self.state["last_event"] = event["kind"]
            self._refresh_rounds()
            self.store.save_checkpoint(self.run_id, self.state, event)
            actions += 1
            target = int(self.state["step"])
            if (
                crash_after_step is not None
                and starting_step < crash_after_step <= target
            ):
                if simulate_crash:
                    raise CrashInjected(target)
                os.kill(os.getpid(), signal.SIGKILL)
            if (
                pause_after_step is not None
                and starting_step < pause_after_step <= target
                and pause_seconds > 0
            ):
                time.sleep(pause_seconds)
        return self.summary()

    def mark_resumed(self) -> None:
        self.state["resume_count"] = int(self.state.get("resume_count", 0)) + 1
        self.store.save_resume_marker(self.run_id, self.state)

    def _duration_summary(self) -> dict[str, Any]:
        current_version = int(self.state["policy"]["ledger_version"])
        entries = [
            entry
            for entry in self.state["ledger_entries"]
        ]
        total_wait_ns = sum(int(entry.get("duration_ns") or 0) for entry in entries)
        rounded_units = 0
        for entry in entries:
            if entry.get("duration_ns") is None:
                continue
            fixed = convert_duration(entry["duration_ns"], entry["scale"])
            rounded_units += fixed.rounded_units
        denominator = 10 ** max(
            (entry["scale"] for entry in entries if entry.get("scale") is not None),
            default=0,
        )
        total_scaled = 0
        for entry in entries:
            if entry.get("duration_ns") is None:
                continue
            fixed = convert_duration(entry["duration_ns"], entry["scale"])
            total_scaled += fixed.rounded_units * (
                denominator // (10 ** entry["scale"])
            )
        max_error = max(
            (int(entry.get("cumulative_error_bound_ns") or 0) for entry in entries),
            default=0,
        )
        return {
            "active_ledger_version": current_version,
            "entry_count": len(entries),
            "closed_entry_count": sum(
                1 for entry in entries if entry.get("dequeue_ns") is not None
            ),
            "open_entry_count": sum(
                1 for entry in entries if entry.get("dequeue_ns") is None
            ),
            "total_wait_ns": total_wait_ns,
            "total_wait_ms": (
                f"{total_scaled // denominator}."
                f"{total_scaled % denominator:0{len(str(denominator)) - 1}d}"
                if denominator > 1
                else str(total_scaled)
            ),
            "max_cumulative_error_bound_ns": max_error,
            "ledger_versions": list(range(1, current_version + 1)),
            "archived_ledger_versions": list(range(1, current_version)),
        }

    def summary(self) -> dict[str, Any]:
        txs = sorted(self.state["txs"].values(), key=_stable_tx_order)
        pending = [
            tx
            for tx in txs
            if tx["final_status"] is None
        ]
        actual_policy_versions = sorted(
            {
                int(tx["bound_generation"])
                for tx in txs
                if tx["bound_generation"] is not None
            }
            | {
                int(activation["generation"])
                for activation in self.state["policy_activations"]
            }
        )
        active_generation = int(self.state["policy"]["generation"])
        active_view = self.bundle.policy(active_generation)
        entries = sorted(
            self.state["ledger_entries"], key=lambda entry: entry["sequence"]
        )
        ledger_digest = sha256_value(
            [
                {
                    key: entry.get(key)
                    for key in (
                        "entry_id",
                        "tx_id",
                        "op_id",
                        "resource",
                        "mode",
                        "enqueue_ns",
                        "dequeue_ns",
                        "duration_ns",
                        "scale",
                        "rounded_ms",
                        "rounding_direction",
                        "discarded_remainder_ns",
                        "residual_ns",
                        "own_error_bound_ns",
                        "dependency_error_bound_ns",
                        "cumulative_error_bound_ns",
                        "status",
                        "policy_generation",
                    )
                }
                for entry in entries
            ]
        )
        summary = {
            "run_id": self.run_id,
            "status": self.state["run_status"],
            "reason": self.state["run_reason"],
            "logical_ns": self.state["logical_ns"],
            "logical_ms": (
                f"{self.state['logical_ns'] // 1_000_000}."
                f"{self.state['logical_ns'] % 1_000_000:06d}"
            ),
            "committed_step": self.state["step"],
            "parallelism": self.state["parallelism"],
            "round_budget_ns": self.state["round_budget_ns"],
            "max_logical_ns": self.state["max_logical_ns"],
            "resume_count": self.state["resume_count"],
            "active_policy": {
                "generation": active_generation,
                "summary": self.state["policy"]["summary"],
                "effective_ms": active_view.effective_ms,
                "ledger_version": self.state["policy"]["ledger_version"],
                "last_commit_seq": self.state["policy"]["last_commit_seq"],
            },
            "actual_policy_versions": actual_policy_versions,
            "policy_activations": sorted(
                self.state["policy_activations"],
                key=lambda item: (item["logical_ns"], item["generation"]),
            ),
            "policy_switches": sorted(
                self.state["policy"]["switches"],
                key=lambda item: item["commit_seq"],
            ),
            "pending_transactions": [
                {
                    "tx_id": tx["tx_id"],
                    "state": tx["state"],
                    "bound_generation": tx["bound_generation"],
                    "start_at_ns": tx["start_at_ns"],
                    "waiting_resource": tx["waiting_resource"],
                }
                for tx in pending
            ],
            "transactions": [
                {
                    "tx_id": tx["tx_id"],
                    "status": tx["final_status"] or tx["state"],
                    "reason": tx["reason"],
                    "policy_generation": tx["bound_generation"],
                    "retry_used": tx["retry_used"],
                    "started_ns": tx["started_ns"],
                    "finished_ns": tx["finished_ns"],
                }
                for tx in txs
            ],
            "ledger_summary": self._duration_summary(),
            "ledger_digest": ledger_digest,
            "error_bound": {
                "max_cumulative_ns": max(
                    (
                        int(entry.get("cumulative_error_bound_ns") or 0)
                        for entry in entries
                    ),
                    default=0,
                ),
                "by_entry": [
                    {
                        "entry_id": entry["entry_id"],
                        "tx_id": entry["tx_id"],
                        "resource": entry["resource"],
                        "cumulative_error_bound_ns": int(
                            entry.get("cumulative_error_bound_ns") or 0
                        ),
                    }
                    for entry in entries
                ],
            },
            "ledger_entries": entries,
            "duration_conversions": sorted(
                self.state["conversions"],
                key=lambda item: item["sequence"],
            ),
            "conversion_summary": {
                "count": len(self.state["conversions"]),
                "hold_count": sum(
                    1
                    for item in self.state["conversions"]
                    if item["source_kind"] == "HOLD"
                ),
                "wait_count": sum(
                    1
                    for item in self.state["conversions"]
                    if item["source_kind"] == "WAIT"
                ),
                "exact_count": sum(
                    1
                    for item in self.state["conversions"]
                    if item["rounding_direction"] == "EXACT"
                ),
                "max_error_bound_ns": max(
                    (
                        int(item["error_bound_ns"])
                        for item in self.state["conversions"]
                    ),
                    default=0,
                ),
            },
            "precision_sensitive_records": sorted(
                self.state["sensitive_records"],
                key=lambda item: item["sequence"],
            ),
            "decisions": sorted(
                self.state["decisions"],
                key=lambda item: item["decision_seq"],
            ),
            "retry_events": sorted(
                self.state["retry_events"],
                key=lambda item: (item["tx_id"], item["retry_used"]),
            ),
            "rounds": self.state.get("round_summaries", []),
            "locks": sorted(
                [
                    {
                        "resource": resource,
                        **self.state["locks"][resource],
                    }
                    for resource in self.state["locks"]
                ],
                key=lambda item: item["resource"],
            ),
            "wait_queue": [
                {
                    "resource": resource,
                    "entry_ids": list(self.state["queues"][resource]),
                }
                for resource in sorted(self.state["queues"])
            ],
        }
        semantic_state = {
            "run_status": self.state["run_status"],
            "run_reason": self.state["run_reason"],
            "logical_ns": self.state["logical_ns"],
            "step": self.state["step"],
            "transactions": summary["transactions"],
            "ledger_entries": entries,
            "conversions": self.state["conversions"],
            "precision_sensitive_records": self.state["sensitive_records"],
            "decisions": self.state["decisions"],
            "policy": self.state["policy"],
            "policy_activations": self.state["policy_activations"],
            "retry_events": self.state["retry_events"],
        }
        summary["state_digest"] = sha256_value(semantic_state)
        return summary

    def status(self) -> dict[str, Any]:
        return self.summary()


def load_engine(
    samples: Path | str,
    db_path: Path | str,
    run_id: str = "main",
) -> tuple[Engine, Store]:
    from .samples import load_bundle

    bundle = load_bundle(Path(samples))
    store = Store(Path(db_path))
    try:
        engine = Engine(bundle, store, run_id)
    except LabError:
        store.close()
        raise
    return engine, store
