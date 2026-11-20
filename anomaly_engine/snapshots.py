"""Versioned whole-snapshot replacement and atomic budget ledger."""

from __future__ import annotations

import copy
import threading
from contextlib import contextmanager
from typing import Any, Iterator

from .canonical import canonical_sha256
from .errors import EngineError

SNAPSHOT_STAGES = ("plan", "budget", "result")
SNAPSHOT_SCHEMA = "whole-snapshot-sha256-v1"


def snapshot_checksum(stage: str, version: int, content: Any) -> str:
    return canonical_sha256(
        {
            "schema": SNAPSHOT_SCHEMA,
            "stage": stage,
            "version": version,
            "content": content,
        }
    )


def make_snapshot(stage: str, version: int, content: Any) -> dict[str, Any]:
    copied_content = copy.deepcopy(content)
    return {
        "schema": SNAPSHOT_SCHEMA,
        "stage": stage,
        "version": version,
        "content": copied_content,
        "checksum": snapshot_checksum(stage, version, copied_content),
        "checksum_algorithm": "sha256-canonical-json-v1",
    }


def verify_snapshot(snapshot: Any) -> dict[str, Any]:
    if not isinstance(snapshot, dict):
        raise EngineError(
            "E_SNAPSHOT_CORRUPT",
            "snapshot_verify",
            "snapshot must be an object",
            details={"category": "not_an_object"},
        )
    stage = snapshot.get("stage")
    version = snapshot.get("version")
    checksum = snapshot.get("checksum")
    if stage not in SNAPSHOT_STAGES:
        raise EngineError(
            "E_SNAPSHOT_CORRUPT",
            "snapshot_verify",
            "snapshot stage is invalid",
            details={"category": "invalid_stage", "stage": stage},
        )
    if isinstance(version, bool) or not isinstance(version, int) or version <= 0:
        raise EngineError(
            "E_SNAPSHOT_CORRUPT",
            "snapshot_verify",
            "snapshot version must be a positive integer",
            details={"category": "invalid_version", "stage": stage},
        )
    if not isinstance(checksum, str):
        raise EngineError(
            "E_SNAPSHOT_CORRUPT",
            "snapshot_verify",
            "snapshot checksum is missing",
            details={"category": "missing_checksum", "stage": stage},
        )
    if "content" not in snapshot:
        raise EngineError(
            "E_SNAPSHOT_CORRUPT",
            "snapshot_verify",
            "snapshot content is missing",
            details={"category": "missing_content", "stage": stage},
        )
    expected = snapshot_checksum(stage, version, snapshot["content"])
    if expected != checksum or snapshot.get("schema") != SNAPSHOT_SCHEMA:
        raise EngineError(
            "E_SNAPSHOT_CORRUPT",
            "snapshot_verify",
            "snapshot checksum or schema does not match its content",
            details={
                "category": "checksum_mismatch",
                "stage": stage,
                "declared": checksum,
                "computed": expected,
            },
        )
    return copy.deepcopy(snapshot)


def snapshot_reference(snapshot: dict[str, Any]) -> dict[str, Any]:
    return {
        "stage": snapshot["stage"],
        "version": snapshot["version"],
        "checksum": snapshot["checksum"],
        "checksum_algorithm": snapshot["checksum_algorithm"],
    }


class SnapshotStore:
    """A non-blocking, whole-value replacement store for three stages."""

    def __init__(self) -> None:
        self._snapshots: dict[str, dict[str, Any]] = {}
        self._history: dict[str, list[dict[str, Any]]] = {
            stage: [] for stage in SNAPSHOT_STAGES
        }
        self._locks = {stage: threading.Lock() for stage in SNAPSHOT_STAGES}

    @contextmanager
    def stage_guard(self, stage: str) -> Iterator[None]:
        self._validate_stage(stage)
        lock = self._locks[stage]
        if not lock.acquire(blocking=False):
            raise EngineError(
                "E_STAGE_BUSY",
                "snapshot_commit",
                f"stage {stage!r} already has an in-flight commit",
                field=stage,
            )
        try:
            yield
        finally:
            lock.release()

    def _validate_stage(self, stage: str) -> None:
        if stage not in SNAPSHOT_STAGES:
            raise EngineError(
                "E_INVALID_STAGE",
                "snapshot_commit",
                f"unknown snapshot stage {stage!r}",
                field="stage",
            )

    def commit(
        self,
        stage: str,
        content: Any,
        *,
        version: int | None = None,
    ) -> dict[str, Any]:
        self._validate_stage(stage)
        lock = self._locks[stage]
        if not lock.acquire(blocking=False):
            raise EngineError(
                "E_STAGE_BUSY",
                "snapshot_commit",
                f"stage {stage!r} already has an in-flight commit",
                field=stage,
            )
        try:
            current = self._snapshots.get(stage)
            current_version = int(current["version"]) if current else 0
            if version is None:
                next_version = current_version + 1
            else:
                if isinstance(version, bool) or not isinstance(version, int):
                    raise EngineError(
                        "E_INVALID_VERSION",
                        "snapshot_commit",
                        "snapshot version must be an integer",
                        field="version",
                        details={"stage": stage},
                    )
                if version <= current_version:
                    raise EngineError(
                        "E_SNAPSHOT_ROLLBACK",
                        "snapshot_commit",
                        "snapshot version must increase monotonically",
                        field="version",
                        details={
                            "stage": stage,
                            "current_version": current_version,
                            "attempted_version": version,
                        },
                    )
                next_version = version

            candidate = make_snapshot(stage, next_version, content)
            verify_snapshot(candidate)
            if current is not None:
                self._history[stage].append(copy.deepcopy(current))
            self._snapshots[stage] = candidate
            return snapshot_reference(candidate)
        finally:
            lock.release()

    def get(self, stage: str, *, verify: bool = True) -> dict[str, Any]:
        self._validate_stage(stage)
        snapshot = self._snapshots.get(stage)
        if snapshot is None:
            raise EngineError(
                "E_SNAPSHOT_MISSING",
                "snapshot_read",
                f"stage {stage!r} has no snapshot",
                field=stage,
            )
        if verify:
            return verify_snapshot(snapshot)
        return copy.deepcopy(snapshot)

    def history(self, stage: str) -> list[dict[str, Any]]:
        self._validate_stage(stage)
        return [verify_snapshot(snapshot) for snapshot in self._history[stage]]

    def references(self) -> dict[str, dict[str, Any]]:
        return {
            stage: snapshot_reference(self._snapshots[stage])
            for stage in SNAPSHOT_STAGES
            if stage in self._snapshots
        }


class BudgetLedger:
    """Thread-safe comparison budget with atomically visible reserve/release."""

    def __init__(self, limit: int) -> None:
        if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
            raise EngineError(
                "E_INVALID_BUDGET",
                "budget_ledger",
                "comparison budget must be a positive integer",
                field="budgets.max_comparisons",
            )
        self.limit = limit
        self._reserved = 0
        self._transactions: dict[str, dict[str, Any]] = {}
        self._events: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        self._sequence = 0

    def reserve(self, transaction_id: str, amount: int, *, metadata: Any = None) -> dict[str, Any]:
        if not transaction_id:
            raise EngineError(
                "E_INVALID_TRANSACTION",
                "budget_reserve",
                "transaction_id must be non-empty",
                field="transaction_id",
            )
        if isinstance(amount, bool) or not isinstance(amount, int) or amount < 0:
            raise EngineError(
                "E_INVALID_TRANSACTION",
                "budget_reserve",
                "reservation amount must be a non-negative integer",
                field="amount",
            )
        with self._lock:
            if transaction_id in self._transactions:
                raise EngineError(
                    "E_LEDGER_DUPLICATE_TXN",
                    "budget_reserve",
                    f"transaction {transaction_id!r} already exists",
                    field="transaction_id",
                    details={"transaction_id": transaction_id},
                )
            if self._reserved + amount > self.limit:
                raise EngineError(
                    "E_COMPARE_BUDGET",
                    "budget_reserve",
                    "reservation would exceed the remaining comparison budget",
                    field="budgets.max_comparisons",
                    details={
                        "limit": self.limit,
                        "reserved": self._reserved,
                        "requested": amount,
                        "remaining": self.limit - self._reserved,
                    },
                )
            self._reserved += amount
            self._sequence += 1
            transaction = {
                "transaction_id": transaction_id,
                "amount": amount,
                "status": "reserved",
                "sequence": self._sequence,
                "metadata": copy.deepcopy(metadata),
            }
            self._transactions[transaction_id] = transaction
            self._events.append(
                {
                    "sequence": self._sequence,
                    "event": "reserve",
                    "transaction_id": transaction_id,
                    "amount": amount,
                    "reserved_after": self._reserved,
                }
            )
            return copy.deepcopy(transaction)

    def release(self, transaction_id: str, *, reason: str = "completed") -> dict[str, Any]:
        with self._lock:
            transaction = self._transactions.get(transaction_id)
            if transaction is None:
                raise EngineError(
                    "E_LEDGER_UNKNOWN_TXN",
                    "budget_release",
                    f"transaction {transaction_id!r} does not exist",
                    field="transaction_id",
                    details={"transaction_id": transaction_id},
                )
            if transaction["status"] == "released":
                raise EngineError(
                    "E_LEDGER_DOUBLE_RELEASE",
                    "budget_release",
                    f"transaction {transaction_id!r} was already released",
                    field="transaction_id",
                    details={"transaction_id": transaction_id},
                )
            transaction["status"] = "released"
            transaction["release_reason"] = reason
            self._reserved -= int(transaction["amount"])
            self._sequence += 1
            transaction["release_sequence"] = self._sequence
            self._events.append(
                {
                    "sequence": self._sequence,
                    "event": "release",
                    "transaction_id": transaction_id,
                    "amount": transaction["amount"],
                    "reserved_after": self._reserved,
                    "reason": reason,
                }
            )
            return copy.deepcopy(transaction)

    @property
    def reserved(self) -> int:
        with self._lock:
            return self._reserved

    @property
    def available(self) -> int:
        with self._lock:
            return self.limit - self._reserved

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            transactions = [
                copy.deepcopy(transaction)
                for _, transaction in sorted(
                    self._transactions.items(),
                    key=lambda item: (item[1]["sequence"], item[0]),
                )
            ]
            return {
                "limit": self.limit,
                "reserved": self._reserved,
                "available": self.limit - self._reserved,
                "transactions": transactions,
                "events": copy.deepcopy(self._events),
                "atomicity": "all reserve/release operations occur under one mutex",
            }
