from __future__ import annotations

import fcntl
import json
import os
import re
import threading
from pathlib import Path
from typing import Any, Iterable

from .canonical import atomic_write_bytes, canonical_json_bytes, checksum, read_json
from .errors import (
    E_CONFIG_INVALID,
    E_LEDGER_INSUFFICIENT,
    E_SNAPSHOT_CORRUPT,
    E_STAGE_BUSY,
    PipelineError,
)


SNAPSHOT_OBJECTS = ("schedule_plan", "budget_ledger", "training_summary")
SNAPSHOT_SCHEMA_VERSION = 1
_SAFE_NAME = re.compile(r"^[A-Za-z0-9_.-]+$")


class SnapshotStore:
    def __init__(self, directory: str | Path) -> None:
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)

    def _path(self, object_name: str) -> Path:
        if not _SAFE_NAME.match(object_name):
            raise PipelineError(
                E_CONFIG_INVALID,
                "snapshot",
                "invalid snapshot object name",
                {"field": "object_name", "object_name": object_name},
            )
        return self.directory / f"{object_name}.json"

    @staticmethod
    def _content_checksum(
        object_name: str,
        version: int,
        payload: Any,
        schema_version: int = SNAPSHOT_SCHEMA_VERSION,
    ) -> str:
        return checksum(
            {
                "object_name": object_name,
                "payload": payload,
                "schema_version": schema_version,
                "version": version,
            }
        )

    def load(self, object_name: str) -> dict[str, Any]:
        path = self._path(object_name)
        try:
            envelope = read_json(path)
        except (OSError, json.JSONDecodeError) as exc:
            raise PipelineError(
                E_SNAPSHOT_CORRUPT,
                "snapshot_load",
                f"cannot load snapshot: {exc}",
                {
                    "field": "snapshot",
                    "object_name": object_name,
                    "version": "unknown",
                },
            ) from exc
        if not isinstance(envelope, dict):
            raise PipelineError(
                E_SNAPSHOT_CORRUPT,
                "snapshot_load",
                "snapshot envelope is not an object",
                {
                    "field": "snapshot",
                    "object_name": object_name,
                    "version": "unknown",
                },
            )
        version = envelope.get("version", "unknown")
        required = {"checksum", "object_name", "payload", "schema_version", "version"}
        if not required.issubset(envelope):
            missing = sorted(required - set(envelope))
            raise PipelineError(
                E_SNAPSHOT_CORRUPT,
                "snapshot_load",
                f"snapshot envelope is missing {missing[0]}",
                {
                    "field": missing[0],
                    "object_name": object_name,
                    "version": version,
                },
            )
        if (
            isinstance(envelope["version"], bool)
            or not isinstance(envelope["version"], int)
            or envelope["version"] <= 0
            or isinstance(envelope["schema_version"], bool)
            or not isinstance(envelope["schema_version"], int)
            or not isinstance(envelope["checksum"], str)
        ):
            raise PipelineError(
                E_SNAPSHOT_CORRUPT,
                "snapshot_load",
                "snapshot envelope metadata has invalid types",
                {
                    "field": "envelope_metadata",
                    "object_name": object_name,
                    "version": version,
                },
            )
        if envelope["object_name"] != object_name:
            raise PipelineError(
                E_SNAPSHOT_CORRUPT,
                "snapshot_load",
                "snapshot object name does not match its file",
                {
                    "actual_object_name": envelope["object_name"],
                    "field": "object_name",
                    "object_name": object_name,
                    "version": version,
                },
            )
        if envelope["schema_version"] != SNAPSHOT_SCHEMA_VERSION:
            raise PipelineError(
                E_SNAPSHOT_CORRUPT,
                "snapshot_load",
                "unsupported snapshot schema version",
                {
                    "actual_schema_version": envelope["schema_version"],
                    "field": "schema_version",
                    "object_name": object_name,
                    "version": version,
                },
            )
        expected = self._content_checksum(
            object_name,
            envelope["version"],
            envelope["payload"],
            envelope["schema_version"],
        )
        if expected != envelope["checksum"]:
            raise PipelineError(
                E_SNAPSHOT_CORRUPT,
                "snapshot_load",
                "snapshot checksum mismatch",
                {
                    "expected_checksum": expected,
                    "actual_checksum": envelope["checksum"],
                    "field": "checksum",
                    "object_name": object_name,
                    "version": version,
                },
            )
        return envelope

    def publish(
        self,
        object_name: str,
        payload: Any,
        *,
        version: int,
    ) -> dict[str, Any]:
        if isinstance(version, bool) or not isinstance(version, int) or version <= 0:
            raise PipelineError(
                E_CONFIG_INVALID,
                "snapshot",
                "snapshot version must be a positive integer",
                {"field": "version", "object_name": object_name, "version": version},
            )
        content_checksum = self._content_checksum(object_name, version, payload)
        path = self._path(object_name)
        if path.exists():
            existing = self.load(object_name)
            if version < int(existing["version"]):
                raise PipelineError(
                    E_CONFIG_INVALID,
                    "snapshot",
                    "snapshot version must be monotonic",
                    {
                        "field": "version",
                        "new_version": version,
                        "object_name": object_name,
                        "version": existing["version"],
                    },
                )
            if version == int(existing["version"]):
                if content_checksum == existing["checksum"]:
                    return existing
                raise PipelineError(
                    E_CONFIG_INVALID,
                    "snapshot",
                    "snapshot content changed without a version increment",
                    {
                        "field": "version",
                        "object_name": object_name,
                        "version": version,
                    },
                )
        envelope = {
            "checksum": content_checksum,
            "object_name": object_name,
            "payload": payload,
            "schema_version": SNAPSHOT_SCHEMA_VERSION,
            "version": version,
        }
        atomic_write_bytes(path, canonical_json_bytes(envelope) + b"\n")
        return envelope

    def load_all(self) -> dict[str, dict[str, Any]]:
        loaded: dict[str, dict[str, Any]] = {}
        for path in sorted(self.directory.glob("*.json")):
            object_name = path.stem
            try:
                loaded[object_name] = self.load(object_name)
            except PipelineError:
                raise
        return loaded


class SnapshotCache:
    def __init__(self) -> None:
        self.objects: dict[str, dict[str, Any]] = {}
        self.replacements: list[dict[str, Any]] = []

    def reconcile(self, snapshot: dict[str, Any]) -> bool:
        name = str(snapshot["object_name"])
        previous = self.objects.get(name)
        changed = (
            previous is None
            or previous["version"] != snapshot["version"]
            or previous["checksum"] != snapshot["checksum"]
        )
        if changed:
            self.objects[name] = snapshot
            self.replacements.append(
                {
                    "checksum": snapshot["checksum"],
                    "object_name": name,
                    "previous_checksum": (
                        previous["checksum"] if previous is not None else None
                    ),
                    "previous_version": (
                        previous["version"] if previous is not None else None
                    ),
                    "replaced_whole_object": True,
                    "version": snapshot["version"],
                }
            )
        return changed

    def reconcile_all(self, snapshots: Iterable[dict[str, Any]]) -> list[str]:
        changed: list[str] = []
        for snapshot in snapshots:
            if self.reconcile(snapshot):
                changed.append(str(snapshot["object_name"]))
        return changed


def startup_snapshot_cache(store: SnapshotStore) -> SnapshotCache:
    cache = SnapshotCache()
    cache.reconcile_all(store.load_all().values())
    return cache


class SubmissionGate:
    def __init__(self, lock_path: str | Path) -> None:
        self.lock_path = Path(lock_path)
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        self._fd: int | None = None

    def acquire(self) -> None:
        if self._fd is not None:
            return
        fd = os.open(self.lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            os.close(fd)
            raise PipelineError(
                E_STAGE_BUSY,
                "schedule_submit",
                "another schedule plan submission is active",
                {"field": "submission", "lock_path": str(self.lock_path)},
            ) from exc
        self._fd = fd

    def release(self) -> None:
        if self._fd is None:
            return
        fd = self._fd
        self._fd = None
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def __enter__(self) -> "SubmissionGate":
        self.acquire()
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.release()


def submit_schedule_plan(
    gate: SubmissionGate,
    store: SnapshotStore,
    plan: dict[str, Any],
    *,
    version: int,
) -> dict[str, Any]:
    with gate:
        return store.publish("schedule_plan", plan, version=version)


class BudgetLedger:
    def __init__(self, initial_balance: int) -> None:
        if isinstance(initial_balance, bool) or not isinstance(initial_balance, int) or initial_balance < 0:
            raise PipelineError(
                E_CONFIG_INVALID,
                "budget_ledger",
                "initial_balance must be a non-negative integer",
                {"field": "initial_balance", "actual": initial_balance},
            )
        self.initial_balance = initial_balance
        self.balance = initial_balance
        self.entries: dict[str, dict[str, Any]] = {}
        self.sequence = 0
        self._lock = threading.Lock()

    def debit(self, transaction_id: str, amount: int) -> dict[str, Any]:
        if not isinstance(transaction_id, str) or not transaction_id:
            raise PipelineError(
                E_CONFIG_INVALID,
                "budget_ledger",
                "transaction_id must be a non-empty string",
                {"field": "transaction_id", "actual": transaction_id},
            )
        if isinstance(amount, bool) or not isinstance(amount, int) or amount <= 0:
            raise PipelineError(
                E_CONFIG_INVALID,
                "budget_ledger",
                "amount must be a positive integer",
                {"field": "amount", "actual": amount},
            )
        with self._lock:
            existing = self.entries.get(transaction_id)
            if existing is not None:
                return {**existing, "applied": False}
            if amount > self.balance:
                raise PipelineError(
                    E_LEDGER_INSUFFICIENT,
                    "budget_ledger",
                    "insufficient ledger balance",
                    {
                        "amount": amount,
                        "available": self.balance,
                        "field": "amount",
                        "transaction_id": transaction_id,
                    },
                )
            self.sequence += 1
            entry = {
                "amount": amount,
                "applied": True,
                "balance_after": self.balance - amount,
                "operation": "debit",
                "sequence": self.sequence,
                "transaction_id": transaction_id,
            }
            self.balance -= amount
            self.entries[transaction_id] = entry
            return dict(entry)

    def release(self, transaction_id: str) -> dict[str, Any]:
        with self._lock:
            existing = self.entries.get(transaction_id)
            if existing is None:
                return {
                    "amount": 0,
                    "applied": False,
                    "balance_after": self.balance,
                    "operation": "release",
                    "sequence": self.sequence,
                    "transaction_id": transaction_id,
                }
            amount = int(existing["amount"])
            self.balance += amount
            del self.entries[transaction_id]
            self.sequence += 1
            return {
                "amount": amount,
                "applied": True,
                "balance_after": self.balance,
                "operation": "release",
                "sequence": self.sequence,
                "transaction_id": transaction_id,
            }

    def snapshot_payload(self) -> dict[str, Any]:
        with self._lock:
            return {
                "balance": self.balance,
                "entries": [
                    dict(self.entries[key]) for key in sorted(self.entries)
                ],
                "initial_balance": self.initial_balance,
                "sequence": self.sequence,
            }
