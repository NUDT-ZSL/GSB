"""Thread-safe atomic budget ledger with per-job audit entries."""

from __future__ import annotations

import copy
import threading
from typing import Any, Iterable

from .errors import BusinessError, E_VALIDATION_FAILED
from .models import PipelineConfig


class BudgetLedger:
    """All mutations happen under one lock and are exposed as immutable copies."""

    def __init__(self, config: PipelineConfig) -> None:
        self._config = config
        self._lock = threading.RLock()
        self._version = 0
        self._state = "new"
        self._entries: dict[str, dict[str, Any]] = {}
        self._groups: list[dict[str, Any]] = []

    @property
    def version(self) -> int:
        with self._lock:
            return self._version

    def reserve_plan(self, plan: dict[str, Any]) -> None:
        with self._lock:
            if self._state != "new":
                raise BusinessError(
                    E_VALIDATION_FAILED,
                    "ledger has already reserved a plan",
                    stage="ledger_reserve",
                    details={"state": self._state, "version": self._version},
                )
            entries: dict[str, dict[str, Any]] = {}
            for decision in plan["decisions"]:
                if decision["decision"] != "admitted":
                    continue
                original = decision["original_estimate"]
                effective = decision["effective_estimate"]
                entries[decision["job_id"]] = {
                    "job_id": decision["job_id"],
                    "job_kind": decision["job_kind"],
                    "model_tag": decision["model_tag"],
                    "status": "reserved",
                    "degraded": decision["degraded"],
                    "degraded_fields": copy.deepcopy(decision["degraded_fields"]),
                    "original_compute_units": original["compute_units"],
                    "original_memory_bytes": original["memory_bytes"],
                    "effective_compute_units": effective["compute_units"],
                    "effective_memory_bytes": effective["memory_bytes"],
                    "degrade_returned_compute_units": decision[
                        "degrade_returned"
                    ]["compute_units"],
                    "degrade_returned_memory_bytes": decision["degrade_returned"][
                        "memory_bytes"
                    ],
                    "released_compute_units": 0,
                    "released_memory_bytes": 0,
                    "consumed_compute_units": 0,
                    "consumed_memory_bytes": 0,
                    "weight_charge_bytes": effective["weight_charged_bytes"],
                    "active": True,
                }
            self._entries = entries
            self._groups = copy.deepcopy(plan["parallel_groups"])
            self._state = "reserved"
            self._version += 1

    def settle_results(self, results: Iterable[dict[str, Any]]) -> None:
        with self._lock:
            if self._state != "reserved":
                raise BusinessError(
                    E_VALIDATION_FAILED,
                    "ledger is not reserved",
                    stage="ledger_settle",
                    details={"state": self._state, "version": self._version},
                )
            result_by_id: dict[str, dict[str, Any]] = {}
            for result in results:
                job_id = result["job_id"]
                if job_id in result_by_id:
                    raise BusinessError(
                        E_VALIDATION_FAILED,
                        "duplicate result while settling ledger",
                        stage="ledger_settle",
                        details={"job_id": job_id},
                    )
                result_by_id[job_id] = result
            if set(result_by_id) != set(self._entries):
                raise BusinessError(
                    E_VALIDATION_FAILED,
                    "ledger entries and results are not one-to-one",
                    stage="ledger_settle",
                    details={
                        "entries": sorted(self._entries),
                        "results": sorted(result_by_id),
                    },
                )

            for job_id, result in result_by_id.items():
                entry = self._entries[job_id]
                if result["status"] == "ok":
                    entry["status"] = "consumed"
                    entry["consumed_compute_units"] = entry[
                        "effective_compute_units"
                    ]
                    entry["consumed_memory_bytes"] = entry[
                        "effective_memory_bytes"
                    ]
                else:
                    entry["status"] = "released"
                    entry["released_compute_units"] = entry[
                        "effective_compute_units"
                    ]
                    entry["released_memory_bytes"] = entry[
                        "effective_memory_bytes"
                    ]
                entry["active"] = False

            # If a failed inference job was the weight owner, transfer the
            # resident weight charge to another surviving sharer atomically.
            for entry in self._entries.values():
                if (
                    entry["status"] == "released"
                    and entry["job_kind"] == "infer"
                    and entry["weight_charge_bytes"] > 0
                ):
                    survivors = [
                        candidate
                        for candidate in self._entries.values()
                        if candidate["job_id"] != entry["job_id"]
                        and candidate["status"] == "consumed"
                        and candidate["job_kind"] == "infer"
                        and candidate["model_tag"] == entry["model_tag"]
                    ]
                    if survivors:
                        survivor = sorted(
                            survivors, key=lambda candidate: candidate["job_id"]
                        )[0]
                        survivor["weight_charge_bytes"] += entry[
                            "weight_charge_bytes"
                        ]
                        entry["weight_charge_bytes"] = 0

            self._state = "settled"
            self._version += 1

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            entries = [
                copy.deepcopy(self._entries[job_id])
                for job_id in sorted(self._entries)
            ]
            totals = {
                "admitted": len(entries),
                "original_compute_units": sum(
                    entry["original_compute_units"] for entry in entries
                ),
                "effective_compute_units": sum(
                    entry["effective_compute_units"] for entry in entries
                ),
                "degrade_returned_compute_units": sum(
                    entry["degrade_returned_compute_units"] for entry in entries
                ),
                "released_compute_units": sum(
                    entry["released_compute_units"] for entry in entries
                ),
                "consumed_compute_units": sum(
                    entry["consumed_compute_units"] for entry in entries
                ),
                "original_memory_bytes": sum(
                    entry["original_memory_bytes"] for entry in entries
                ),
                "effective_memory_bytes": sum(
                    entry["effective_memory_bytes"] for entry in entries
                ),
                "degrade_returned_memory_bytes": sum(
                    entry["degrade_returned_memory_bytes"] for entry in entries
                ),
                "released_memory_bytes": sum(
                    entry["released_memory_bytes"] for entry in entries
                ),
                "consumed_memory_bytes": sum(
                    entry["consumed_memory_bytes"] for entry in entries
                ),
                "slot_limit": self._config.max_slots,
                "slot_peak": max(
                    (group["slot_count"] for group in self._groups),
                    default=0,
                ),
                "parallel_group_count": len(self._groups),
            }
            totals["net_committed_compute_units"] = (
                totals["original_compute_units"]
                - totals["degrade_returned_compute_units"]
                - totals["released_compute_units"]
            )
            totals["net_committed_memory_bytes"] = (
                totals["original_memory_bytes"]
                - totals["degrade_returned_memory_bytes"]
                - totals["released_memory_bytes"]
            )
            return {
                "schema_version": 1,
                "state": self._state,
                "ledger_revision": self._version,
                "limits": {
                    "compute_units": self._config.compute_budget,
                    "memory_bytes": self._config.memory_budget_bytes,
                    "slots": self._config.max_slots,
                },
                "entries": entries,
                "parallel_groups": copy.deepcopy(self._groups),
                "totals": totals,
            }

    def assert_invariants(self) -> None:
        with self._lock:
            net_compute = 0
            net_memory = 0
            consumed_compute = 0
            consumed_memory = 0
            released_compute = 0
            released_memory = 0
            for entry in self._entries.values():
                if (
                    entry["effective_compute_units"]
                    != entry["original_compute_units"]
                    - entry["degrade_returned_compute_units"]
                ):
                    raise BusinessError(
                        E_VALIDATION_FAILED,
                        "ledger compute degradation return is inconsistent",
                        stage="ledger_invariant",
                        details={"job_id": entry["job_id"]},
                    )
                if (
                    entry["effective_memory_bytes"]
                    != entry["original_memory_bytes"]
                    - entry["degrade_returned_memory_bytes"]
                ):
                    raise BusinessError(
                        E_VALIDATION_FAILED,
                        "ledger memory degradation return is inconsistent",
                        stage="ledger_invariant",
                        details={"job_id": entry["job_id"]},
                    )
                expected_consumed = (
                    entry["effective_compute_units"]
                    if entry["status"] == "consumed"
                    else 0
                )
                expected_consumed_memory = (
                    entry["effective_memory_bytes"]
                    if entry["status"] == "consumed"
                    else 0
                )
                expected_released = (
                    entry["effective_compute_units"]
                    if entry["status"] == "released"
                    else 0
                )
                expected_released_memory = (
                    entry["effective_memory_bytes"]
                    if entry["status"] == "released"
                    else 0
                )
                if entry["consumed_compute_units"] != expected_consumed:
                    raise BusinessError(
                        E_VALIDATION_FAILED,
                        "ledger compute fields are inconsistent",
                        stage="ledger_invariant",
                        details={"job_id": entry["job_id"]},
                    )
                if entry["consumed_memory_bytes"] != expected_consumed_memory:
                    raise BusinessError(
                        E_VALIDATION_FAILED,
                        "ledger consumed memory is inconsistent",
                        stage="ledger_invariant",
                        details={"job_id": entry["job_id"]},
                    )
                if entry["released_compute_units"] != expected_released:
                    raise BusinessError(
                        E_VALIDATION_FAILED,
                        "ledger released compute is inconsistent",
                        stage="ledger_invariant",
                        details={"job_id": entry["job_id"]},
                    )
                if entry["released_memory_bytes"] != expected_released_memory:
                    raise BusinessError(
                        E_VALIDATION_FAILED,
                        "ledger released memory is inconsistent",
                        stage="ledger_invariant",
                        details={"job_id": entry["job_id"]},
                    )
                if (
                    entry["consumed_compute_units"]
                    + entry["released_compute_units"]
                    > entry["effective_compute_units"]
                ):
                    raise BusinessError(
                        E_VALIDATION_FAILED,
                        "ledger compute settlement exceeds allocation",
                        stage="ledger_invariant",
                        details={"job_id": entry["job_id"]},
                    )
                if entry["effective_compute_units"] > self._config.compute_budget:
                    raise BusinessError(
                        E_VALIDATION_FAILED,
                        "single allocation exceeds compute budget",
                        stage="ledger_invariant",
                        details={"job_id": entry["job_id"]},
                    )
                if entry["effective_memory_bytes"] > self._config.memory_budget_bytes:
                    raise BusinessError(
                        E_VALIDATION_FAILED,
                        "single allocation exceeds memory budget",
                        stage="ledger_invariant",
                        details={"job_id": entry["job_id"]},
                    )
                net_compute += entry["effective_compute_units"]
                net_memory += entry["effective_memory_bytes"]
                consumed_compute += entry["consumed_compute_units"]
                consumed_memory += entry["consumed_memory_bytes"]
                released_compute += entry["released_compute_units"]
                released_memory += entry["released_memory_bytes"]
            if self._state == "settled":
                if consumed_compute + released_compute != net_compute:
                    raise BusinessError(
                        E_VALIDATION_FAILED,
                        "ledger total compute settlement is inconsistent",
                        stage="ledger_invariant",
                    )
                if consumed_memory + released_memory != net_memory:
                    raise BusinessError(
                        E_VALIDATION_FAILED,
                        "ledger total memory settlement is inconsistent",
                        stage="ledger_invariant",
                    )
                if net_compute > self._config.compute_budget:
                    raise BusinessError(
                        E_VALIDATION_FAILED,
                        "ledger exceeds total compute budget",
                        stage="ledger_invariant",
                    )
                if net_memory > self._config.memory_budget_bytes:
                    raise BusinessError(
                        E_VALIDATION_FAILED,
                        "ledger exceeds total memory budget",
                        stage="ledger_invariant",
                    )
