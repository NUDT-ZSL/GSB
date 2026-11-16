"""Atomic session snapshots and whole-state replacement restoration."""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .alerts import ALERT_STATES
from .config import EvaluationConfig
from .errors import ConfigError, InputError, SnapshotError
from .models import (
    AlertRecord,
    AlertTransition,
    EvaluationSession,
    EvaluationStep,
    ResourceReport,
    WindowResult,
)


SNAPSHOT_FORMAT = "sre-burnrate-session-snapshot"
SNAPSHOT_VERSION = 1
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _reject_float(value: str) -> Any:
    raise SnapshotError(f"snapshot must not contain floating-point number {value!r}")


@dataclass(frozen=True, slots=True)
class Snapshot:
    session: EvaluationSession
    format_version: int = SNAPSHOT_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "snapshot_format": SNAPSHOT_FORMAT,
            "snapshot_version": self.format_version,
            "config": self.session.config.to_dict(),
            "session": self.session.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class RestoreReport:
    action: str
    merged: bool
    snapshot_data_version: str
    snapshot_data_sha256: str
    current_data_version: str | None
    current_data_sha256: str | None
    version_differences: tuple[dict[str, str], ...]
    window_count: int
    step_count: int
    final_alert_state: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": "OK",
            "action": self.action,
            "merged": self.merged,
            "snapshot_data_version": self.snapshot_data_version,
            "snapshot_data_sha256": self.snapshot_data_sha256,
            "current_data_version": self.current_data_version,
            "current_data_sha256": self.current_data_sha256,
            "version_differences": list(self.version_differences),
            "window_count": self.window_count,
            "step_count": self.step_count,
            "final_alert_state": self.final_alert_state,
        }


@dataclass(frozen=True, slots=True)
class RestoreResult:
    session: EvaluationSession
    report: RestoreReport


def _mapping(value: Any, context: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise SnapshotError(f"{context} must be an object")
    return dict(value)


def _string(value: Any, context: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SnapshotError(f"{context} must be a non-empty string")
    return value


def _int(value: Any, context: str, *, minimum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise SnapshotError(f"{context} must be an integer")
    if minimum is not None and value < minimum:
        raise SnapshotError(f"{context} must be >= {minimum}")
    return value


def _int_or_none(value: Any, context: str) -> int | None:
    if value is None:
        return None
    return _int(value, context)


def _list(value: Any, context: str) -> list[Any]:
    if not isinstance(value, list):
        raise SnapshotError(f"{context} must be a list")
    return value


def _validate_session(payload: Mapping[str, Any], config: EvaluationConfig) -> EvaluationSession:
    data = _mapping(payload, "snapshot.session")
    data_version = _string(data.get("data_version"), "session.data_version")
    data_sha256 = _string(data.get("data_sha256"), "session.data_sha256")
    if not _SHA256_RE.fullmatch(data_sha256):
        raise SnapshotError("session.data_sha256 must be 64 lowercase hexadecimal characters")
    source_path = _string(data.get("source_path"), "session.source_path")
    row_count = _int(data.get("row_count"), "session.row_count", minimum=1)
    first_minute = _int_or_none(data.get("first_minute"), "session.first_minute")
    last_minute = _int_or_none(data.get("last_minute"), "session.last_minute")

    windows = _mapping(data.get("windows"), "session.windows")
    long_windows = tuple(
        WindowResult.from_dict(item, config) for item in _list(windows.get("long"), "session.windows.long")
    )
    short_windows = tuple(
        WindowResult.from_dict(item, config)
        for item in _list(windows.get("short"), "session.windows.short")
    )
    if len(long_windows) != len(short_windows):
        raise SnapshotError("snapshot long and short window counts differ")
    if long_windows and row_count < 1:
        raise SnapshotError("snapshot with windows must have row_count >= 1")

    steps = tuple(
        EvaluationStep.from_dict(item) for item in _list(data.get("steps"), "session.steps")
    )
    if len(steps) != len(long_windows):
        raise SnapshotError("snapshot step count does not match window count")
    for index, step in enumerate(steps):
        if step.step_index != index:
            raise SnapshotError("snapshot steps must be ordered by contiguous step_index")
        if step.long_window_id != long_windows[index].window_id:
            raise SnapshotError(f"step {index} references a different long window")
        if step.short_window_id != short_windows[index].window_id:
            raise SnapshotError(f"step {index} references a different short window")
        if step.state not in ALERT_STATES:
            raise SnapshotError(f"step {index} has invalid alert state")

    alerts = _mapping(data.get("alerts"), "session.alerts")
    history = tuple(AlertRecord.from_dict(item) for item in _list(alerts.get("history"), "session.alerts.history"))
    transitions = tuple(
        AlertTransition.from_dict(item)
        for item in _list(alerts.get("transitions"), "session.alerts.transitions")
    )
    final_state = _string(alerts.get("final_state"), "session.alerts.final_state")
    if final_state not in ALERT_STATES:
        raise SnapshotError("snapshot final alert state is invalid")
    if len(history) != len(steps):
        raise SnapshotError("snapshot alert history count does not match step count")
    for index, record in enumerate(history):
        if record.step_index != index:
            raise SnapshotError("snapshot alert history must be ordered by contiguous step_index")
        if record.state != steps[index].state:
            raise SnapshotError(f"alert history state differs from step {index}")
    if transitions:
        if transitions[-1].to_state != final_state:
            raise SnapshotError("snapshot final transition does not match final alert state")
        prior = "normal"
        for expected_index, transition in enumerate(transitions):
            if transition.from_state != prior:
                raise SnapshotError("snapshot alert transitions do not form a continuous chain")
            if transition.to_state == prior or transition.to_state not in ALERT_STATES:
                raise SnapshotError("snapshot contains an invalid alert state transition")
            if not 0 <= transition.step_index < len(steps):
                raise SnapshotError("snapshot transition step_index is out of range")
            if expected_index and transition.step_index <= transitions[expected_index - 1].step_index:
                raise SnapshotError("snapshot alert transitions are not ordered")
            prior = transition.to_state
    elif final_state != "normal":
        raise SnapshotError("non-normal final state requires at least one transition")

    resource = ResourceReport.from_dict(_mapping(data.get("resource"), "session.resource"))
    if resource.row_count != row_count:
        raise SnapshotError("resource.row_count differs from session.row_count")
    if first_minute is None or last_minute is None or last_minute < first_minute:
        raise SnapshotError("snapshot minute range is invalid")

    return EvaluationSession(
        config=config,
        data_version=data_version,
        data_sha256=data_sha256,
        source_path=source_path,
        row_count=row_count,
        first_minute=first_minute,
        last_minute=last_minute,
        long_windows=long_windows,
        short_windows=short_windows,
        steps=steps,
        alert_records=history,
        alert_transitions=transitions,
        final_alert_state=final_state,
        resource=resource,
    )


def load_snapshot(path: str | Path) -> Snapshot:
    snapshot_path = Path(path)
    try:
        text = snapshot_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise InputError(f"cannot read snapshot: {snapshot_path}", details={"path": str(snapshot_path)}) from exc
    try:
        payload = json.loads(text, parse_float=_reject_float, parse_constant=_reject_float)
    except SnapshotError:
        raise
    except json.JSONDecodeError as exc:
        raise SnapshotError(
            f"invalid snapshot JSON: {exc.msg}",
            details={"line": exc.lineno, "column": exc.colno, "path": str(snapshot_path)},
        ) from exc
    root = _mapping(payload, "snapshot")
    if root.get("snapshot_format") != SNAPSHOT_FORMAT:
        raise SnapshotError("unsupported snapshot_format")
    version = _int(root.get("snapshot_version"), "snapshot_version", minimum=1)
    if version != SNAPSHOT_VERSION:
        raise SnapshotError(f"unsupported snapshot_version: {version}")
    try:
        config = EvaluationConfig.from_dict(_mapping(root.get("config"), "snapshot.config"))
    except ConfigError as exc:
        raise SnapshotError(f"invalid snapshot config: {exc.message}") from exc
    session = _validate_session(_mapping(root.get("session"), "snapshot.session"), config)
    return Snapshot(session=session, format_version=version)


def save_snapshot(session: EvaluationSession, path: str | Path) -> Path:
    snapshot_path = Path(path)
    snapshot_path.parent.mkdir(parents=True, exist_ok=True)
    payload = Snapshot(session=session).to_dict()
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=snapshot_path.parent,
            prefix=f".{snapshot_path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary_name = temporary.name
            temporary.write(text)
            temporary.write("\n")
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_name, snapshot_path)
    except OSError as exc:
        if temporary_name is not None:
            try:
                Path(temporary_name).unlink(missing_ok=True)
            except OSError:
                pass
        raise InputError(
            f"cannot write snapshot: {snapshot_path}", details={"path": str(snapshot_path)}
        ) from exc
    return snapshot_path


def restore_snapshot(
    path: str | Path,
    *,
    current_data_version: str | None = None,
    current_data_sha256: str | None = None,
    current_session: EvaluationSession | None = None,
) -> RestoreResult:
    """Restore the complete snapshot state without merging with a live session."""

    snapshot = load_snapshot(path)
    session = snapshot.session
    if current_data_version is None and current_session is not None:
        current_data_version = current_session.data_version
    if current_data_sha256 is None and current_session is not None:
        current_data_sha256 = current_session.data_sha256
    differences: list[dict[str, str]] = []
    if current_data_version is not None and current_data_version != session.data_version:
        differences.append(
            {
                "field": "data_version",
                "snapshot": session.data_version,
                "current": current_data_version,
            }
        )
    if current_data_sha256 is not None and current_data_sha256 != session.data_sha256:
        differences.append(
            {
                "field": "data_sha256",
                "snapshot": session.data_sha256,
                "current": current_data_sha256,
            }
        )
    report = RestoreReport(
        action="full_replace",
        merged=False,
        snapshot_data_version=session.data_version,
        snapshot_data_sha256=session.data_sha256,
        current_data_version=current_data_version,
        current_data_sha256=current_data_sha256,
        version_differences=tuple(differences),
        window_count=session.window_count,
        step_count=len(session.steps),
        final_alert_state=session.final_alert_state,
    )
    return RestoreResult(session=session, report=report)
