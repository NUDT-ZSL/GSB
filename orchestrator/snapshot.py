"""Session snapshot persistence, version matching and replacement recovery."""
from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from typing import Dict, List, Optional

from .errors import SnapshotError
from .models import Attempt, StageResult, TraceEvent

SNAPSHOT_FORMAT_VERSION = 1


def normalize_stderr(raw: str) -> str:
    """Normalize CRLF/CR to LF so summaries are byte-stable across OSes."""
    return raw.replace("\r\n", "\n").replace("\r", "\n")


@dataclass
class Snapshot:
    format_version: int
    session_id: str
    dag_id: str
    dag_version: str
    dag_fingerprint: str
    global_resource_budget: int
    default_max_concurrency: int
    default_timeout_ms: Optional[int]
    dag_definition: dict
    states: Dict[str, StageResult]
    trace: List[TraceEvent]
    completed_stages: List[str]
    recovery_count: int
    created_iso: str
    updated_iso: str
    finished: bool
    unknown_stages: List[str]

    def to_dict(self) -> dict:
        return {
            "format_version": self.format_version,
            "session_id": self.session_id,
            "dag_id": self.dag_id,
            "dag_version": self.dag_version,
            "dag_fingerprint": self.dag_fingerprint,
            "global_resource_budget": self.global_resource_budget,
            "default_max_concurrency": self.default_max_concurrency,
            "default_timeout_ms": self.default_timeout_ms,
            "dag_definition": self.dag_definition,
            "states": {
                sid: result.to_dict() for sid, result in sorted(self.states.items())
            },
            "trace": [event.to_dict() for event in self.trace],
            "completed_stages": sorted(self.completed_stages),
            "recovery_count": self.recovery_count,
            "created_iso": self.created_iso,
            "updated_iso": self.updated_iso,
            "finished": self.finished,
            "unknown_stages": sorted(self.unknown_stages),
        }


def atomic_write_json(path: str, payload: dict) -> None:
    directory = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=".snap-", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(tmp_path, path)
    except BaseException:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise


def save_snapshot(path: str, snapshot: Snapshot) -> None:
    atomic_write_json(path, snapshot.to_dict())


def load_snapshot(path: str) -> Snapshot:
    if not os.path.isfile(path):
        raise SnapshotError("snapshot not found", [{"path": path}])
    try:
        with open(path, "r", encoding="utf-8") as handle:
            raw = json.load(handle)
    except json.JSONDecodeError as exc:
        raise SnapshotError(
            "snapshot is not valid JSON",
            [{"line": exc.lineno, "column": exc.colno}],
        ) from exc
    return _hydrate(raw)


def _hydrate(raw: dict) -> Snapshot:
    required = (
        "format_version",
        "session_id",
        "dag_id",
        "dag_version",
        "dag_fingerprint",
        "global_resource_budget",
        "dag_definition",
        "states",
        "trace",
    )
    missing = [key for key in required if key not in raw]
    if missing:
        raise SnapshotError(
            "snapshot missing required fields", [{"missing": sorted(missing)}]
        )
    if raw["format_version"] != SNAPSHOT_FORMAT_VERSION:
        raise SnapshotError(
            "unsupported snapshot format version",
            [
                {
                    "expected": SNAPSHOT_FORMAT_VERSION,
                    "actual": raw["format_version"],
                }
            ],
        )
    states: Dict[str, StageResult] = {}
    for sid, item in raw.get("states", {}).items():
        attempts = [Attempt(**attempt) for attempt in item.get("attempts", [])]
        item_copy = dict(item)
        item_copy["attempts"] = attempts
        states[sid] = StageResult(**item_copy)
    trace = [TraceEvent(**event) for event in raw.get("trace", [])]
    return Snapshot(
        format_version=raw["format_version"],
        session_id=raw["session_id"],
        dag_id=raw["dag_id"],
        dag_version=raw["dag_version"],
        dag_fingerprint=raw["dag_fingerprint"],
        global_resource_budget=raw["global_resource_budget"],
        default_max_concurrency=raw.get("default_max_concurrency", 1),
        default_timeout_ms=raw.get("default_timeout_ms"),
        dag_definition=raw["dag_definition"],
        states=states,
        trace=trace,
        completed_stages=list(raw.get("completed_stages", [])),
        recovery_count=raw.get("recovery_count", 0),
        created_iso=raw.get("created_iso", ""),
        updated_iso=raw.get("updated_iso", ""),
        finished=raw.get("finished", False),
        unknown_stages=list(raw.get("unknown_stages", [])),
    )


class VersionDecision:
    """Result of comparing an on-disk snapshot against the current DAG."""

    MATCH = "match"
    REPLACE = "replace"

    def __init__(self, action: str, reasons: List[str]):
        self.action = action
        self.reasons = reasons


def compare_version(
    snapshot: Snapshot, current_version: str, current_fingerprint: str
) -> VersionDecision:
    reasons: List[str] = []
    if snapshot.dag_version != current_version:
        reasons.append(
            f"dag_version differs: snapshot={snapshot.dag_version} "
            f"current={current_version}"
        )
    if snapshot.dag_fingerprint != current_fingerprint:
        reasons.append(
            "dag_fingerprint differs (definition or script content changed)"
        )
    if reasons:
        return VersionDecision(VersionDecision.REPLACE, reasons)
    return VersionDecision(VersionDecision.MATCH, [])


def fresh_snapshot(
    session_id: str,
    dag_definition: dict,
    dag_version: str,
    dag_fingerprint: str,
    budget: int,
    default_max_concurrency: int,
    default_timeout_ms: Optional[int],
    created_iso: str,
) -> Snapshot:
    return Snapshot(
        format_version=SNAPSHOT_FORMAT_VERSION,
        session_id=session_id,
        dag_id=dag_definition["dag_id"],
        dag_version=dag_version,
        dag_fingerprint=dag_fingerprint,
        global_resource_budget=budget,
        default_max_concurrency=default_max_concurrency,
        default_timeout_ms=default_timeout_ms,
        dag_definition=dag_definition,
        states={},
        trace=[],
        completed_stages=[],
        recovery_count=0,
        created_iso=created_iso,
        updated_iso=created_iso,
        finished=False,
        unknown_stages=[],
    )


def recover_completed(snapshot: Snapshot) -> Dict[str, StageResult]:
    """Only stages explicitly recorded as completed survive a hard crash.

    Everything that was running or merely queued is reported as unknown and
    must be rerun; no exit report is synthesized for those stages.
    """
    recovered: Dict[str, StageResult] = {}
    for sid in sorted(snapshot.completed_stages):
        result = snapshot.states.get(sid)
        if result is not None and result.state == "succeeded":
            recovered[sid] = result
    return recovered
