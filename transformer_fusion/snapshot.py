"""Versioned, checksummed full-file execution plans and result snapshots."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import numpy as np

from .errors import Diagnostic, E_SNAPSHOT_CORRUPT
from .model import CasePlan
from .util import (
    LockedPath,
    atomic_write_bytes,
    atomic_write_json,
    canonical_json,
    ensure_dir,
    npy_bytes,
    read_json,
    sha256_bytes,
    sha256_json,
    sha256_text,
)


SNAPSHOT_SCHEMA = "tf-result-snapshot-v1"
PLAN_SCHEMA = "tf-execution-plan-v1"
SNAPSHOT_INDEX_SCHEMA = "tf-snapshot-index-v1"


class SnapshotCorruption(RuntimeError):
    def __init__(self, case_id: str, message: str, details: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.case_id = case_id
        self.details = dict(details or {})


def _index_body(index: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schema": index.get("schema", SNAPSHOT_INDEX_SCHEMA),
        "version": int(index.get("version", 0)),
        "cases": index.get("cases", {}),
    }


def _case_stem(case_id: str) -> str:
    safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in case_id)[:40]
    return f"{safe}-{sha256_text(case_id)[:12]}"


def _payload_checksum(value: Mapping[str, Any]) -> str:
    return sha256_json({k: v for k, v in value.items() if k != "checksum"})


@dataclass
class SnapshotInfo:
    case_id: str
    version: int
    checksum: str
    plan_checksum: str
    output_sha256: str
    replacement: str = "full"

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "version": self.version,
            "checksum": self.checksum,
            "plan_checksum": self.plan_checksum,
            "output_sha256": self.output_sha256,
            "replacement": self.replacement,
        }


class SnapshotStore:
    """Each commit atomically replaces complete plan/snapshot/output files."""

    def __init__(self, root: str | Path) -> None:
        self.root = ensure_dir(root)
        self.cases_dir = ensure_dir(self.root / "cases")
        self.index_path = self.root / "index.json"

    def _empty_index(self) -> dict[str, Any]:
        body = {"schema": SNAPSHOT_INDEX_SCHEMA, "version": 0, "cases": {}}
        body["checksum"] = sha256_json(body)
        return body

    def _load_index_unlocked(self) -> dict[str, Any]:
        if not self.index_path.exists():
            return self._empty_index()
        try:
            value = read_json(self.index_path)
        except (OSError, json.JSONDecodeError) as exc:
            raise SnapshotCorruption("*", f"snapshot index is unreadable: {exc}") from exc
        if not isinstance(value, Mapping) or value.get("schema") != SNAPSHOT_INDEX_SCHEMA:
            raise SnapshotCorruption("*", "snapshot index schema mismatch")
        body = _index_body(value)
        if value.get("checksum") != sha256_json(body):
            raise SnapshotCorruption("*", "snapshot index checksum mismatch")
        return dict(value)

    def _save_index_unlocked(self, index: Mapping[str, Any]) -> None:
        body = _index_body(index)
        output = dict(body)
        output["checksum"] = sha256_json(body)
        atomic_write_json(self.index_path, output)

    def _load_snapshot_unlocked(self, case_id: str, entry: Mapping[str, Any]) -> dict[str, Any]:
        snapshot_path = self.root / str(entry.get("snapshot", ""))
        if not snapshot_path.exists():
            raise SnapshotCorruption(case_id, "snapshot file is missing")
        try:
            snapshot = read_json(snapshot_path)
        except (OSError, json.JSONDecodeError) as exc:
            raise SnapshotCorruption(case_id, f"snapshot file is unreadable: {exc}") from exc
        if snapshot.get("schema") != SNAPSHOT_SCHEMA or snapshot.get("case_id") != case_id:
            raise SnapshotCorruption(case_id, "snapshot identity/schema mismatch")
        if snapshot.get("checksum") != _payload_checksum(snapshot):
            raise SnapshotCorruption(case_id, "snapshot checksum mismatch")
        if int(snapshot.get("version", -1)) != int(entry.get("version", -2)):
            raise SnapshotCorruption(
                case_id,
                "snapshot version changed without a full replacement",
                {"snapshot_version": snapshot.get("version"), "index_version": entry.get("version")},
            )
        if snapshot.get("checksum") != entry.get("snapshot_checksum"):
            raise SnapshotCorruption(case_id, "snapshot checksum disagrees with index")

        plan_path = self.root / str(entry.get("plan", ""))
        if not plan_path.exists():
            raise SnapshotCorruption(case_id, "execution plan is missing")
        try:
            plan = read_json(plan_path)
        except (OSError, json.JSONDecodeError) as exc:
            raise SnapshotCorruption(case_id, f"execution plan is unreadable: {exc}") from exc
        if plan.get("schema") != PLAN_SCHEMA or plan.get("case_id") != case_id:
            raise SnapshotCorruption(case_id, "execution plan identity/schema mismatch")
        if plan.get("checksum") != _payload_checksum(plan):
            raise SnapshotCorruption(case_id, "execution plan checksum mismatch")
        if int(plan.get("version", -1)) != int(entry.get("version", -2)):
            raise SnapshotCorruption(case_id, "execution plan version mismatch")
        if plan.get("checksum") != entry.get("plan_checksum"):
            raise SnapshotCorruption(case_id, "execution plan checksum disagrees with index")
        if snapshot.get("payload", {}).get("plan_checksum") != plan.get("checksum"):
            raise SnapshotCorruption(case_id, "snapshot references a different plan checksum")

        output_path = self.root / str(entry.get("output", ""))
        if not output_path.exists():
            raise SnapshotCorruption(case_id, "snapshot output artifact is missing")
        actual_output_sha = sha256_bytes(output_path.read_bytes())
        expected_output_sha = snapshot.get("payload", {}).get("output_sha256")
        if actual_output_sha != expected_output_sha:
            raise SnapshotCorruption(
                case_id,
                "snapshot output checksum mismatch",
                {"expected": expected_output_sha, "actual": actual_output_sha},
            )
        return snapshot

    def validate_case(self, case_id: str) -> tuple[SnapshotInfo | None, Diagnostic | None]:
        try:
            with LockedPath(self.root):
                index = self._load_index_unlocked()
                entry = index.get("cases", {}).get(case_id)
                if entry is None:
                    return None, None
                snapshot = self._load_snapshot_unlocked(case_id, entry)
                return (
                    SnapshotInfo(
                        case_id=case_id,
                        version=int(snapshot["version"]),
                        checksum=str(snapshot["checksum"]),
                        plan_checksum=str(snapshot["payload"]["plan_checksum"]),
                        output_sha256=str(snapshot["payload"]["output_sha256"]),
                    ),
                    None,
                )
        except (SnapshotCorruption, OSError, ValueError) as exc:
            return None, Diagnostic(
                code=E_SNAPSHOT_CORRUPT,
                case_id=getattr(exc, "case_id", case_id),
                stage="snapshot",
                message=str(exc),
                details=getattr(exc, "details", {}),
            )

    def commit(
        self,
        case_id: str,
        *,
        plan: CasePlan,
        output: np.ndarray,
        payload: Mapping[str, Any],
        repair_corrupt_existing: bool = False,
    ) -> SnapshotInfo:
        output_bytes = npy_bytes(output)
        output_sha = sha256_bytes(output_bytes)
        with LockedPath(self.root):
            index = self._load_index_unlocked()
            existing = index.get("cases", {}).get(case_id)
            if existing is not None:
                # Detect stale/corrupt state before replacing anything.
                try:
                    self._load_snapshot_unlocked(case_id, existing)
                except SnapshotCorruption:
                    if not repair_corrupt_existing:
                        raise
            new_version = int(index.get("version", 0)) + 1
            stem = _case_stem(case_id)
            plan_name = f"cases/{stem}.plan.json"
            snapshot_name = f"cases/{stem}.snapshot.json"
            output_name = f"cases/{stem}.output.npy"

            plan_body = {
                "schema": PLAN_SCHEMA,
                "case_id": case_id,
                "version": new_version,
                "plan": plan.to_dict(),
            }
            plan_checksum = sha256_json(plan_body)
            plan_document = dict(plan_body)
            plan_document["checksum"] = plan_checksum

            snapshot_payload = dict(payload)
            snapshot_payload.update(
                {
                    "output_sha256": output_sha,
                    "output_shape": list(output.shape),
                    "output_dtype": str(output.dtype),
                    "plan_checksum": plan_checksum,
                    "cache_key": payload.get("cache_key"),
                }
            )
            snapshot_body = {
                "schema": SNAPSHOT_SCHEMA,
                "case_id": case_id,
                "version": new_version,
                "payload": snapshot_payload,
            }
            snapshot_checksum = sha256_json(snapshot_body)
            snapshot_document = dict(snapshot_body)
            snapshot_document["checksum"] = snapshot_checksum

            # Full-file replacement, never an incremental patch.
            atomic_write_bytes(self.root / output_name, output_bytes)
            atomic_write_json(self.root / plan_name, plan_document)
            atomic_write_json(self.root / snapshot_name, snapshot_document)

            case_entry = {
                "version": new_version,
                "snapshot": snapshot_name,
                "plan": plan_name,
                "output": output_name,
                "snapshot_checksum": snapshot_checksum,
                "plan_checksum": plan_checksum,
                "output_sha256": output_sha,
            }
            cases = dict(index.get("cases", {}))
            cases[case_id] = case_entry
            new_index = {
                "schema": SNAPSHOT_INDEX_SCHEMA,
                "version": new_version,
                "cases": cases,
            }
            self._save_index_unlocked(new_index)
            return SnapshotInfo(
                case_id=case_id,
                version=new_version,
                checksum=snapshot_checksum,
                plan_checksum=plan_checksum,
                output_sha256=output_sha,
                replacement="full",
            )

    def summary(self) -> dict[str, Any]:
        try:
            with LockedPath(self.root):
                index = self._load_index_unlocked()
                return {
                    "root": str(self.root),
                    "version": int(index.get("version", 0)),
                    "checksum": index.get("checksum"),
                    "case_count": len(index.get("cases", {})),
                    "case_ids": sorted(index.get("cases", {})),
                }
        except (SnapshotCorruption, OSError, ValueError) as exc:
            return {
                "root": str(self.root),
                "version": None,
                "checksum": None,
                "case_count": 0,
                "diagnostic": Diagnostic(
                    code=E_SNAPSHOT_CORRUPT,
                    case_id=getattr(exc, "case_id", "*"),
                    stage="snapshot",
                    message=str(exc),
                ).to_dict(),
            }

    def validate_all(self) -> list[Diagnostic]:
        diagnostics: list[Diagnostic] = []
        try:
            with LockedPath(self.root):
                index = self._load_index_unlocked()
        except (SnapshotCorruption, OSError, ValueError) as exc:
            return [
                Diagnostic(
                    code=E_SNAPSHOT_CORRUPT,
                    case_id=getattr(exc, "case_id", "*"),
                    stage="snapshot",
                    message=str(exc),
                )
            ]
        for case_id in index.get("cases", {}):
            _, diagnostic = self.validate_case(case_id)
            if diagnostic is not None:
                diagnostics.append(diagnostic)
        return diagnostics
