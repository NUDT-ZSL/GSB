from __future__ import annotations

from pathlib import Path
from typing import Any

from .errors import SessionFixError
from .models import (
    ATTEMPT_RESULTS,
    Event,
    Manifest,
    NormalizedSample,
    Rejection,
    Session,
    SessionCycle,
    normalize_key,
)
from .policies import load_policy_v1, load_policy_v2, merge_policies
from .util import canonical_json, digest_json, read_json, safe_relative_path


def _record_rejection(
    category: str,
    index: int,
    code: str,
    message: str,
    source_ref: str,
    raw: Any,
) -> Rejection:
    return Rejection(
        source_category=category,
        record_index=index,
        code=code,
        message=message,
        source_ref=source_ref,
        record_digest=digest_json(raw),
    )


def _require_int(value: Any, name: str, *, nonnegative: bool = False) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer")
    if nonnegative and value < 0:
        raise ValueError(f"{name} must be non-negative")
    return value


def _require_bool(value: Any, name: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{name} must be a boolean")
    return value


def _require_text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value.strip()


def _optional_text(value: Any, name: str) -> str | None:
    if value is None:
        return None
    return _require_text(value, name)


def _first(raw: dict[str, Any], names: tuple[str, ...]) -> Any:
    for name in names:
        if name in raw:
            return raw[name]
    return None


def _records(document: Any, wrapper: str) -> list[Any]:
    if isinstance(document, list):
        return document
    if isinstance(document, dict) and isinstance(document.get(wrapper), list):
        return list(document[wrapper])
    raise ValueError(f"expected a list or an object containing '{wrapper}'")


def _stable_raw_records(raw_records: list[Any], category: str) -> list[Any]:
    def key(raw: Any) -> tuple[Any, ...]:
        if isinstance(raw, dict) and category == "events":
            event_seq = raw.get("event_seq", raw.get("sequence", raw.get("seq")))
            event_time = raw.get(
                "event_time_ms",
                raw.get("logical_time_ms", raw.get("clock_ms")),
            )
            sequence = (
                event_seq
                if isinstance(event_seq, int) and not isinstance(event_seq, bool)
                else 2**63
            )
            timestamp = (
                event_time
                if isinstance(event_time, int) and not isinstance(event_time, bool)
                else 2**63
            )
            return (0, sequence, timestamp, canonical_json(raw))
        if isinstance(raw, dict) and category == "sessions":
            session_id = raw.get("session_id")
            generation = raw.get("generation")
            stable_id = session_id if isinstance(session_id, str) else ""
            stable_generation = (
                generation
                if isinstance(generation, int) and not isinstance(generation, bool)
                else -1
            )
            return (0, stable_id, stable_generation, canonical_json(raw))
        return (0, canonical_json(raw))

    return sorted(raw_records, key=key)


def _load_json_records(path: Path, wrapper: str, category: str) -> list[Any]:
    if not path.exists():
        raise SessionFixError(
            f"E_{category.upper()}_MISSING",
            f"{category} file not found: {path}",
            str(path),
        )
    try:
        document = read_json(path)
        return _records(document, wrapper)
    except (OSError, ValueError, TypeError) as exc:
        raise SessionFixError(
            f"E_{category.upper()}_INVALID",
            f"cannot parse {category} file: {exc}",
            str(path),
        ) from exc


def _parse_manifest(root: Path) -> tuple[Manifest, dict[str, Path]]:
    path = root / "manifest.json"
    if not path.exists():
        raise SessionFixError(
            "E_MANIFEST_MISSING",
            f"manifest not found: {path}",
            str(path),
        )
    try:
        raw = read_json(path)
    except (OSError, ValueError, TypeError) as exc:
        raise SessionFixError(
            "E_MANIFEST_INVALID",
            f"cannot parse manifest: {exc}",
            str(path),
        ) from exc
    if not isinstance(raw, dict):
        raise SessionFixError(
            "E_MANIFEST_INVALID",
            "manifest must be an object",
            str(path),
        )
    try:
        sample_id = _require_text(raw.get("sample_id"), "sample_id")
        seed = _require_int(raw.get("seed"), "seed")
        observed_clock_ms = _require_int(
            raw.get("observed_clock_ms"),
            "observed_clock_ms",
            nonnegative=True,
        )
        files_raw = raw.get("files", {})
        if not isinstance(files_raw, dict):
            raise ValueError("files must be an object")
    except (TypeError, ValueError) as exc:
        raise SessionFixError(
            "E_MANIFEST_INVALID",
            str(exc),
            str(path),
        ) from exc
    defaults = {
        "sessions": "sessions.json",
        "events": "events.json",
        "policy_v1": "policies/v1.json",
        "policy_v2": "policies/v2",
    }
    files = dict(defaults)
    for key, value in files_raw.items():
        if key not in defaults:
            continue
        if not isinstance(value, str) or not value.strip():
            raise SessionFixError(
                "E_MANIFEST_INVALID",
                f"manifest file path {key} must be a non-empty string",
                str(path),
            )
        files[key] = value.strip()
    paths: dict[str, Path] = {}
    try:
        for key, relative in files.items():
            paths[key] = safe_relative_path(root, relative)
    except ValueError as exc:
        raise SessionFixError("E_MANIFEST_INVALID", str(exc), str(path)) from exc
    manifest = Manifest(
        sample_id=sample_id,
        seed=seed,
        observed_clock_ms=observed_clock_ms,
        files=files,
    )
    return manifest, paths


def _parse_sessions(
    path: Path,
) -> tuple[list[Session], list[Rejection]]:
    raw_records = _stable_raw_records(
        _load_json_records(path, "sessions", "sessions"),
        "sessions",
    )
    sessions: list[Session] = []
    rejections: list[Rejection] = []
    seen_ids: set[str] = set()
    for index, raw in enumerate(raw_records):
        source_ref = f"{path.name}#{index}"
        try:
            if not isinstance(raw, dict):
                raise ValueError("record must be an object")
            session_id = _require_text(raw.get("session_id"), "session_id")
            if session_id in seen_ids:
                raise ValueError(f"duplicate session_id: {session_id}")
            user_id = _require_text(raw.get("user_id"), "user_id")
            issued_at_ms = _require_int(
                _first(raw, ("issued_at_ms", "issued_at")),
                "issued_at_ms",
                nonnegative=True,
            )
            expires_at_ms = _require_int(
                _first(raw, ("expires_at_ms", "expires_at")),
                "expires_at_ms",
                nonnegative=True,
            )
            if issued_at_ms >= expires_at_ms:
                raise ValueError("issued_at_ms must be less than expires_at_ms")
            device_id = _require_text(raw.get("device_id"), "device_id")
            generation = _require_int(
                raw.get("generation"),
                "generation",
                nonnegative=True,
            )
            parent_value = _first(raw, ("parent_session_id", "parent"))
            parent_session_id = _optional_text(parent_value, "parent_session_id")
            window_start_ms = _require_int(
                _first(raw, ("window_start_ms", "window_start")),
                "window_start_ms",
                nonnegative=True,
            )
            window_end_ms = _require_int(
                _first(raw, ("window_end_ms", "window_end")),
                "window_end_ms",
                nonnegative=True,
            )
            if window_start_ms > window_end_ms:
                raise ValueError("window_start_ms must not exceed window_end_ms")
            policy_key = normalize_key(
                _require_text(
                    raw.get("policy_key", "default"),
                    "policy_key",
                )
            )
            sessions.append(
                Session(
                    session_id=session_id,
                    user_id=user_id,
                    issued_at_ms=issued_at_ms,
                    expires_at_ms=expires_at_ms,
                    device_id=device_id,
                    generation=generation,
                    parent_session_id=parent_session_id,
                    window_start_ms=window_start_ms,
                    window_end_ms=window_end_ms,
                    policy_key=policy_key,
                    source_index=index,
                )
            )
            seen_ids.add(session_id)
        except (TypeError, ValueError) as exc:
            rejections.append(
                _record_rejection(
                    "sessions",
                    index,
                    "E_SESSION_RECORD",
                    str(exc),
                    source_ref,
                    raw,
                )
            )
    return sessions, rejections


def _parse_events(
    path: Path,
    observed_clock_ms: int,
) -> tuple[list[Event], list[Rejection]]:
    raw_records = _stable_raw_records(
        _load_json_records(path, "events", "events"),
        "events",
    )
    events: list[Event] = []
    rejections: list[Rejection] = []
    seen_sequences: set[int] = set()
    for index, raw in enumerate(raw_records):
        source_ref = f"{path.name}#{index}"
        try:
            if not isinstance(raw, dict):
                raise ValueError("record must be an object")
            event_seq = _require_int(
                _first(raw, ("event_seq", "sequence", "seq")),
                "event_seq",
                nonnegative=True,
            )
            if event_seq in seen_sequences:
                raise ValueError(f"duplicate event_seq: {event_seq}")
            session_id = _require_text(raw.get("session_id"), "session_id")
            event_time_value = _first(
                raw,
                ("event_time_ms", "logical_time_ms", "clock_ms"),
            )
            if event_time_value is None:
                event_time_ms = observed_clock_ms + event_seq
            else:
                event_time_ms = _require_int(
                    event_time_value,
                    "event_time_ms",
                    nonnegative=True,
                )
            attempt_result = _require_text(
                _first(raw, ("attempt_result", "result")),
                "attempt_result",
            )
            if attempt_result not in ATTEMPT_RESULTS:
                raise ValueError(
                    f"attempt_result must be one of {sorted(ATTEMPT_RESULTS)}"
                )
            privilege_escalated = _require_bool(
                _first(raw, ("privilege_escalated", "privilege_escalation")),
                "privilege_escalated",
            )
            event_type = _require_text(
                raw.get("event_type", "login"),
                "event_type",
            )
            user_id = _optional_text(raw.get("user_id"), "user_id")
            device_id = _optional_text(raw.get("device_id"), "device_id")
            policy_value = raw.get("policy_key")
            policy_key = (
                normalize_key(_require_text(policy_value, "policy_key"))
                if policy_value is not None
                else None
            )
            events.append(
                Event(
                    event_seq=event_seq,
                    session_id=session_id,
                    event_time_ms=event_time_ms,
                    attempt_result=attempt_result,
                    privilege_escalated=privilege_escalated,
                    event_type=event_type,
                    user_id=user_id,
                    device_id=device_id,
                    policy_key=policy_key,
                    source_index=index,
                )
            )
            seen_sequences.add(event_seq)
        except (TypeError, ValueError) as exc:
            rejections.append(
                _record_rejection(
                    "events",
                    index,
                    "E_EVENT_RECORD",
                    str(exc),
                    source_ref,
                    raw,
                )
            )
    return events, rejections


def _find_cycles(sessions: list[Session]) -> tuple[SessionCycle, ...]:
    parent_of = {
        session.session_id: session.parent_session_id
        for session in sessions
        if session.parent_session_id is not None
    }
    nodes = set(parent_of)
    index = 0
    stack: list[str] = []
    on_stack: set[str] = set()
    indices: dict[str, int] = {}
    lowlinks: dict[str, int] = {}
    components: list[list[str]] = []

    def strongconnect(node: str) -> None:
        nonlocal index
        indices[node] = index
        lowlinks[node] = index
        index += 1
        stack.append(node)
        on_stack.add(node)
        successor = parent_of.get(node)
        if successor in parent_of:
            if successor not in indices:
                strongconnect(successor)
                lowlinks[node] = min(lowlinks[node], lowlinks[successor])
            elif successor in on_stack:
                lowlinks[node] = min(lowlinks[node], indices[successor])
        if lowlinks[node] == indices[node]:
            component: list[str] = []
            while True:
                member = stack.pop()
                on_stack.remove(member)
                component.append(member)
                if member == node:
                    break
            components.append(component)

    for node in sorted(nodes):
        if node not in indices:
            strongconnect(node)

    cycles: list[SessionCycle] = []
    for component in components:
        is_cycle = len(component) > 1 or (
            len(component) == 1 and parent_of.get(component[0]) == component[0]
        )
        if is_cycle:
            cycles.append(
                SessionCycle(
                    session_ids=tuple(sorted(component)),
                    reason="parent_session_id cycle",
                )
            )
    return tuple(sorted(cycles, key=lambda item: item.session_ids))


def _validate_chain(
    sessions: list[Session],
    initial_rejections: list[Rejection],
    path: Path,
) -> tuple[list[Session], list[Rejection]]:
    by_id = {session.session_id: session for session in sessions}
    valid: dict[str, Session] = {}
    rejections = list(initial_rejections)

    def reject(session: Session, code: str, message: str) -> None:
        rejections.append(
            _record_rejection(
                "sessions",
                session.source_index,
                code,
                message,
                f"{path.name}#{session.source_index}",
                session.to_dict(),
            )
        )

    for session in sessions:
        if session.generation == 0:
            if session.parent_session_id is not None:
                reject(
                    session,
                    "E_SESSION_CHAIN",
                    "generation 0 must not declare a parent_session_id",
                )
            else:
                valid[session.session_id] = session
            continue
        parent_id = session.parent_session_id
        if parent_id is None:
            reject(
                session,
                "E_SESSION_CHAIN",
                "a non-zero generation must declare parent_session_id",
            )
            continue
        parent = by_id.get(parent_id)
        if parent is None:
            reject(
                session,
                "E_SESSION_CHAIN",
                f"parent_session_id not found: {parent_id}",
            )
            continue
        if parent.user_id != session.user_id:
            reject(
                session,
                "E_SESSION_CHAIN",
                "parent and child must belong to the same user_id",
            )
            continue
        if parent.generation != session.generation - 1:
            reject(
                session,
                "E_SESSION_CHAIN",
                "parent generation must be exactly generation - 1",
            )
            continue
        if session.issued_at_ms < parent.issued_at_ms:
            reject(
                session,
                "E_SESSION_CHAIN",
                "child issued_at_ms must not precede parent issued_at_ms",
            )
            continue
        valid[session.session_id] = session

    changed = True
    while changed:
        changed = False
        for session_id, session in list(valid.items()):
            if session.generation == 0:
                continue
            if session.parent_session_id not in valid:
                reject(
                    session,
                    "E_SESSION_CHAIN",
                    "parent session is not valid",
                )
                del valid[session_id]
                changed = True
    return sorted(valid.values(), key=lambda item: item.stable_key), rejections


def _sample_digest(
    manifest: Manifest,
    sessions: list[Session],
    events: list[Event],
    rejections: list[Rejection],
    cycles: tuple[SessionCycle, ...],
    policy_views: dict[str, Any],
    conflicts: tuple[Any, ...],
) -> str:
    payload = {
        "sample_id": manifest.sample_id,
        "seed": manifest.seed,
        "observed_clock_ms": manifest.observed_clock_ms,
        "sessions": [
            {
                key: value
                for key, value in item.to_dict().items()
                if key != "source_index"
            }
            for item in sessions
        ],
        "events": [
            {
                key: value
                for key, value in item.to_dict().items()
                if key != "source_index"
            }
            for item in events
        ],
        "rejections": [
            {
                "source_category": item.source_category,
                "code": item.code,
                "record_digest": item.record_digest,
                "message": item.message,
            }
            for item in rejections
        ],
        "cycles": [item.to_dict() for item in cycles],
        "policy_views": [
            policy_views[item].to_dict() for item in sorted(policy_views)
        ],
        "policy_conflicts": [item.to_dict() for item in conflicts],
    }
    return digest_json(payload)


def parse_sample(root: Path) -> NormalizedSample:
    root = root.resolve()
    if not root.is_dir():
        raise SessionFixError("E_SAMPLE_PATH", f"sample directory not found: {root}")
    manifest, paths = _parse_manifest(root)

    sessions, session_rejections = _parse_sessions(paths["sessions"])
    cycles = _find_cycles(sessions)
    sessions, chain_rejections = _validate_chain(
        sessions,
        session_rejections,
        paths["sessions"],
    )
    events, event_rejections = _parse_events(
        paths["events"],
        manifest.observed_clock_ms,
    )

    valid_sessions = {session.session_id: session for session in sessions}
    accepted_events: list[Event] = []
    unknown_rejections = list(event_rejections)
    for event in events:
        source_ref = f"{paths['events'].name}#{event.source_index}"
        session = valid_sessions.get(event.session_id)
        if session is None:
            unknown_rejections.append(
                _record_rejection(
                    "events",
                    event.source_index,
                    "E_EVENT_UNKNOWN_SESSION",
                    f"session_id not found in valid sessions: {event.session_id}",
                    source_ref,
                    event.to_dict(),
                )
            )
            continue
        if event.user_id is not None and event.user_id != session.user_id:
            unknown_rejections.append(
                _record_rejection(
                    "events",
                    event.source_index,
                    "E_EVENT_USER_MISMATCH",
                    "event user_id does not match the session owner",
                    source_ref,
                    event.to_dict(),
                )
            )
            continue
        accepted_events.append(event)
    accepted_events.sort(key=lambda item: item.canonical_key)

    v1_records, v1_rejections = load_policy_v1(
        paths["policy_v1"],
        manifest.observed_clock_ms,
    )
    v2_records, v2_rejections = load_policy_v2(paths["policy_v2"])
    policy_views, policy_conflicts, merge_rejections = merge_policies(
        v1_records + v2_records
    )
    rejections = tuple(
        sorted(
            chain_rejections
            + unknown_rejections
            + v1_rejections
            + v2_rejections
            + merge_rejections,
            key=lambda item: item.stable_key,
        )
    )
    events_tuple = tuple(accepted_events)
    digest = _sample_digest(
        manifest,
        sessions,
        accepted_events,
        list(rejections),
        cycles,
        policy_views,
        policy_conflicts,
    )
    return NormalizedSample(
        manifest=manifest,
        sessions=tuple(sessions),
        events=events_tuple,
        rejections=rejections,
        cycles=cycles,
        policy_views=policy_views,
        policy_conflicts=policy_conflicts,
        sample_digest=digest,
    )


def discover_samples(path: Path) -> list[Path]:
    path = path.resolve()
    if (path / "manifest.json").is_file():
        return [path]
    if not path.is_dir():
        return []
    return sorted(
        child
        for child in path.iterdir()
        if child.is_dir() and (child / "manifest.json").is_file()
    )
