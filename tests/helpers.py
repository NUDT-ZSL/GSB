from __future__ import annotations

import json
from pathlib import Path

from klp_audit.config import AuditConfig
from klp_audit.constants import ALGORITHM_TO_ID, STAGE_TO_ID
from klp_audit.engine import AuditEngine
from klp_audit.models import Event
from klp_audit.parser import build_file, build_payload, parse_file


def load_json(path: str | Path) -> dict[str, object]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def audit_path(
    path: str | Path,
    *,
    config: AuditConfig | None = None,
    checkpoint_path: str | Path | None = None,
    workspace: str | Path | None = None,
    on_checkpoint=None,
) -> dict[str, object]:
    engine = AuditEngine(
        config,
        checkpoint_path=checkpoint_path,
        workspace=workspace,
        on_checkpoint=on_checkpoint,
    )
    return engine.audit_file(path)


def reencode_reversed(path: str | Path) -> bytes:
    parsed, _data = parse_file(path)
    payloads: list[bytes] = []
    for record_index, event in enumerate(reversed(parsed.events)):
        payloads.append(
            build_payload(
                record_index=record_index,
                session_id=event.session_id,
                stage_id=STAGE_TO_ID[event.stage],
                algorithm_id=ALGORITHM_TO_ID[event.algorithm],
                seq=event.seq,
                iterations=event.iterations,
                salt=event.salt,
                output_length=event.output_length,
                level=event.level,
                sync_marker=event.sync_marker,
            )
        )
    return build_file(payloads, max_iterations=parsed.max_iterations)


def make_event(
    session_id: str,
    stage: str,
    seq: int,
    *,
    salt: bytes = b"",
    iterations: int = 100_000,
    output_length: int = 64,
    level: int = 1,
    algorithm: str = "HKDF-SHA256",
    sync_marker: int = 0,
) -> Event:
    return Event(
        session_id=session_id,
        stage=stage,
        algorithm=algorithm,
        seq=seq,
        iterations=iterations,
        salt=salt,
        output_length=output_length,
        level=level,
        sync_marker=sync_marker,
        record_index=-1,
        offset=-1,
    )


def encode_events(events: list[Event], *, max_iterations: int = 1_000_000) -> bytes:
    payloads: list[bytes] = []
    for record_index, event in enumerate(events):
        payloads.append(
            build_payload(
                record_index=record_index,
                session_id=event.session_id,
                stage_id=STAGE_TO_ID[event.stage],
                algorithm_id=ALGORITHM_TO_ID[event.algorithm],
                seq=event.seq,
                iterations=event.iterations,
                salt=event.salt,
                output_length=event.output_length,
                level=event.level,
                sync_marker=event.sync_marker,
            )
        )
    return build_file(payloads, max_iterations=max_iterations)


def session_by_id(report: dict[str, object], session_id: str) -> dict[str, object]:
    for session in report["sessions"]:  # type: ignore[index]
        if session["session_id"] == session_id:
            return session
    raise KeyError(session_id)


def semantic_session(session: dict[str, object]) -> dict[str, object]:
    events = [
        {
            key: value
            for key, value in event.items()
            if key not in {"record_index", "offset"}
        }
        for event in session["events"]  # type: ignore[index]
    ]
    issues = [
        {key: value for key, value in issue.items() if key != "offset"}
        for issue in session["issues"]  # type: ignore[index]
    ]
    return {
        "status": session["status"],
        "error_codes": session["error_codes"],
        "stages": session["stages"],
        "checks": session["checks"],
        "events": events,
        "issues": issues,
    }

