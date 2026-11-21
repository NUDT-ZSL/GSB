from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import fail
from .plan import AggregationPlan
from .state import GroupState, StateVector


SPILL_SCHEMA_VERSION = 1


def maybe_crash(point: str) -> None:
    if os.environ.get("FLOWAGG_FAULT_POINT") != point:
        return
    if point == "mid_spill":
        os._exit(98)
    os.kill(os.getpid(), 9)


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=".flowagg-", suffix=".tmp", dir=path.parent)
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        if path.exists():
            raise fail(
                "E_SPILL_CONFLICT",
                f"spill target was already materialized: {path.name}",
                path=path.name,
            )
        os.replace(temp_path, path)
        if os.name != "nt":
            dir_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
    finally:
        if temp_path.exists():
            try:
                temp_path.unlink()
            except OSError:
                pass


@dataclass(frozen=True, slots=True)
class SpillRecord:
    spill_seq: int
    group_id: str
    group_key_json: str
    values: tuple[Any, ...]
    state_version: int
    path: Path
    sha256: str
    size_bytes: int
    previous_sha256: str | None
    delta: StateVector
    batch_id: str

    def manifest_row(self, plan: AggregationPlan) -> tuple[Any, ...]:
        return (
            self.spill_seq,
            self.group_id,
            self.state_version,
            self.batch_id,
            self.path.name,
            self.sha256,
            self.size_bytes,
            self.previous_sha256,
            canonical_json(list(self.values)),
            self.group_key_json,
        )


class SpillWriter:
    def __init__(
        self,
        plan: AggregationPlan,
        batch_id: str,
        first_sequence: int,
    ) -> None:
        self.plan = plan
        self.batch_id = batch_id
        self.next_sequence = first_sequence
        self.records: list[SpillRecord] = []
        self.materialized_paths: list[Path] = []

    def write(self, group: GroupState) -> SpillRecord | None:
        delta = group.pending_delta()
        if delta.is_zero():
            return None
        seq = self.next_sequence
        self.next_sequence += 1
        payload_obj = {
            "batch_id": self.batch_id,
            "delta": delta.to_dict(),
            "dimensions": list(self.plan.dimensions),
            "group_id": group.group_id,
            "group_key": list(group.values),
            "group_key_json": group.group_key_json,
            "plan_id": self.plan.plan_id,
            "previous_sha256": group.last_spill_sha256,
            "schema_version": SPILL_SCHEMA_VERSION,
            "spill_seq": seq,
            "state_version": max(group.state_version, 1),
        }
        payload = (canonical_json(payload_obj) + "\n").encode("utf-8")
        sha256 = hashlib.sha256(payload).hexdigest()
        file_name = f"spill-{seq:08d}-{group.group_id[:12]}.json"
        path = self.plan.spill_dir / file_name
        atomic_write(path, payload)
        self.materialized_paths.append(path)
        maybe_crash("after_spill")
        record = SpillRecord(
            spill_seq=seq,
            group_id=group.group_id,
            group_key_json=group.group_key_json,
            values=group.values,
            state_version=max(group.state_version, 1),
            path=path,
            sha256=sha256,
            size_bytes=len(payload),
            previous_sha256=group.last_spill_sha256,
            delta=delta,
            batch_id=self.batch_id,
        )
        group.mark_spilled(delta, seq, sha256)
        self.records.append(record)
        return record

    def cleanup_uncommitted(self) -> None:
        for path in self.materialized_paths:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass

    def discard_group(self, group_id: str) -> None:
        retained: list[SpillRecord] = []
        removed: list[Path] = []
        for record in self.records:
            if record.group_id == group_id:
                removed.append(record.path)
            else:
                retained.append(record)
        for path in removed:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
        self.records = retained
        self.materialized_paths = [
            path for path in self.materialized_paths if path not in removed
        ]


def read_spill_file(
    path: Path,
    expected: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], bytes]:
    try:
        payload = path.read_bytes()
    except FileNotFoundError as exc:
        raise fail(
            "E_SPILL_TAMPERED",
            f"spill file is missing: {path.name}",
            path=path.name,
        ) from exc
    except OSError as exc:
        raise fail(
            "E_SPILL_TAMPERED",
            f"spill file cannot be read: {path.name}: {exc}",
            path=path.name,
        ) from exc
    actual_sha = hashlib.sha256(payload).hexdigest()
    if expected is not None and actual_sha != expected["sha256"]:
        raise fail(
            "E_SPILL_TAMPERED",
            f"spill digest mismatch for {path.name}",
            path=path.name,
            expected_sha256=expected["sha256"],
            actual_sha256=actual_sha,
        )
    try:
        obj = json.loads(payload)
    except Exception as exc:
        raise fail(
            "E_SPILL_TAMPERED",
            f"spill JSON is invalid: {path.name}",
            path=path.name,
        ) from exc
    if not isinstance(obj, dict) or obj.get("schema_version") != SPILL_SCHEMA_VERSION:
        raise fail("E_SPILL_TAMPERED", f"spill schema mismatch: {path.name}", path=path.name)
    if expected is not None:
        for field in ("spill_seq", "group_id", "group_key_json", "state_version"):
            if obj.get(field) != expected[field]:
                raise fail(
                    "E_SPILL_TAMPERED",
                    f"spill metadata mismatch for {field}: {path.name}",
                    path=path.name,
                    field=field,
                    expected=expected[field],
                    actual=obj.get(field),
                )
    return obj, payload

