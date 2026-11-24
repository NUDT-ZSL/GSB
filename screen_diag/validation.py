from __future__ import annotations

import json
import math
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Dict, List, Tuple

from .errors import SchemaError
from .model import FrameEvent, Manifest, Screen, Session, SnapshotRecord


V1_IMPLICIT_GENERATION = 1


def _reject_constant(value: str) -> None:
    raise SchemaError(f"non-finite JSON constant is not allowed: {value}")


def _object_pairs_hook(pairs: List[Tuple[str, Any]]) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise SchemaError(f"duplicate JSON object key: {key}")
        result[key] = value
    return result


def load_json_file(path: Path | str) -> Any:
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SchemaError(f"cannot read JSON input: {path}", {"path": str(path), "reason": str(exc)}) from exc
    try:
        return json.loads(
            text,
            object_pairs_hook=_object_pairs_hook,
            parse_constant=_reject_constant,
        )
    except SchemaError:
        raise
    except (json.JSONDecodeError, ValueError) as exc:
        raise SchemaError(f"invalid JSON: {path}", {"path": str(path), "reason": str(exc)}) from exc


def require_dict(value: Any, label: str) -> Dict[str, Any]:
    if not isinstance(value, dict):
        raise SchemaError(f"{label} must be an object", {"label": label})
    return value


def require_nonempty_str(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SchemaError(f"{label} must be a non-empty string", {"label": label})
    return value


def require_int(value: Any, label: str, *, minimum: int | None = None) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise SchemaError(f"{label} must be an integer", {"label": label, "value": value})
    if minimum is not None and value < minimum:
        raise SchemaError(f"{label} must be >= {minimum}", {"label": label, "value": value})
    return value


def require_finite_positive(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SchemaError(f"{label} must be a finite positive number", {"label": label, "value": value})
    numeric = float(value)
    if not math.isfinite(numeric) or numeric <= 0:
        raise SchemaError(f"{label} must be a finite positive number", {"label": label, "value": value})
    return numeric


def optional_int(value: Any, label: str, *, minimum: int = 0) -> int | None:
    if value is None:
        return None
    return require_int(value, label, minimum=minimum)


def relative_seconds_to_ms(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SchemaError(f"{label} must be a finite non-negative number of seconds", {"label": label})
    numeric = float(value)
    if not math.isfinite(numeric) or numeric < 0:
        raise SchemaError(f"{label} must be a finite non-negative number of seconds", {"label": label})
    try:
        decimal_value = Decimal(str(value)) * Decimal(1000)
    except InvalidOperation as exc:
        raise SchemaError(f"{label} cannot be converted to milliseconds", {"label": label}) from exc
    if decimal_value != decimal_value.to_integral_value():
        raise SchemaError(f"{label} must convert to an integer millisecond value", {"label": label})
    return int(decimal_value)


def parse_manifest(raw: Any) -> Manifest:
    root = require_dict(raw, "manifest")
    raw_sessions = root.get("sessions")
    if not isinstance(raw_sessions, list) or not raw_sessions:
        raise SchemaError("manifest.sessions must be a non-empty list")

    bounds = require_dict(root.get("logic_bounds", {}), "logic_bounds")
    min_ms = require_int(bounds.get("min_ms", 0), "logic_bounds.min_ms", minimum=0)
    max_ms = require_int(bounds.get("max_ms"), "logic_bounds.max_ms", minimum=0)
    if min_ms > max_ms:
        raise SchemaError("logic_bounds.min_ms must not exceed max_ms")

    adjudication = root.get("adjudication", {})
    if adjudication is None:
        adjudication = {}
    adjudication = require_dict(adjudication, "adjudication")
    tolerance = require_int(adjudication.get("late_tolerance_ms", 1500), "late_tolerance_ms", minimum=0)

    persistence = root.get("persistence", {})
    if persistence is None:
        persistence = {}
    persistence = require_dict(persistence, "persistence")
    chunk_interval = require_int(persistence.get("chunk_interval_ms", 2000), "chunk_interval_ms", minimum=1)

    sessions: List[Session] = []
    seen_session_ids: set[str] = set()
    seen_screen_ids: set[str] = set()
    seen_canonical_ids: set[str] = set()

    for session_index, raw_session in enumerate(raw_sessions):
        session = require_dict(raw_session, f"sessions[{session_index}]")
        session_id = require_nonempty_str(session.get("session_id"), "session_id")
        if session_id in seen_session_ids:
            raise SchemaError(f"duplicate session_id: {session_id}", {"session_id": session_id})
        seen_session_ids.add(session_id)

        protocol = require_nonempty_str(session.get("protocol"), f"{session_id}.protocol")
        if protocol not in {"S1", "S2"}:
            raise SchemaError(f"{session_id}.protocol must be S1 or S2", {"protocol": protocol})

        sender = session.get("sender", {})
        if sender is None:
            sender = {}
        if isinstance(sender, str):
            sender_id = require_nonempty_str(sender, f"{session_id}.sender")
        else:
            sender_obj = require_dict(sender, f"{session_id}.sender")
            sender_id = require_nonempty_str(sender_obj.get("id", sender_obj.get("sender_id")), "sender.id")

        start_ms = require_int(session.get("start_logic_ms"), f"{session_id}.start_logic_ms", minimum=0)
        if start_ms < min_ms or start_ms > max_ms:
            raise SchemaError(f"{session_id}.start_logic_ms is outside logic_bounds")

        raw_screens = session.get("screens")
        if not isinstance(raw_screens, list) or not raw_screens:
            raise SchemaError(f"{session_id}.screens must be a non-empty list")

        screens: List[Screen] = []
        for screen_index, raw_screen in enumerate(raw_screens):
            screen_obj = require_dict(raw_screen, f"{session_id}.screens[{screen_index}]")
            screen_id = require_nonempty_str(screen_obj.get("screen_id"), "screen_id")
            canonical_id = require_nonempty_str(
                screen_obj.get("canonical_id", screen_obj.get("canonical_screen_id")),
                "canonical_id",
            )
            role = require_nonempty_str(screen_obj.get("role"), f"{screen_id}.role")
            if screen_id in seen_screen_ids:
                raise SchemaError(f"duplicate screen_id: {screen_id}")
            if canonical_id in seen_canonical_ids:
                raise SchemaError(f"duplicate canonical_id: {canonical_id}")
            seen_screen_ids.add(screen_id)
            seen_canonical_ids.add(canonical_id)

            width = require_int(screen_obj.get("width_px", screen_obj.get("width")), f"{screen_id}.width_px", minimum=1)
            height = require_int(screen_obj.get("height_px", screen_obj.get("height")), f"{screen_id}.height_px", minimum=1)
            scale = require_finite_positive(screen_obj.get("scale", screen_obj.get("scale_factor", 1.0)), f"{screen_id}.scale")

            screens.append(Screen(screen_id, canonical_id, role, width, height, scale))

        sessions.append(
            Session(
                session_id=session_id,
                multicast_group=require_nonempty_str(session.get("multicast_group"), "multicast_group"),
                protocol=protocol,
                sender_id=sender_id,
                start_logic_ms=start_ms,
                screens=screens,
            )
        )

    seed = root.get("seed", 384)
    seed = require_int(seed, "seed", minimum=0)

    return Manifest(
        seed=seed,
        min_logic_ms=min_ms,
        max_logic_ms=max_ms,
        late_tolerance_ms=tolerance,
        chunk_interval_ms=chunk_interval,
        sessions=sessions,
        raw=root,
    )


def parse_events(raw: Any, manifest: Manifest) -> List[FrameEvent]:
    if isinstance(raw, list):
        raw_events = raw
    else:
        root = require_dict(raw, "frame_events")
        raw_events = root.get("events")
    if not isinstance(raw_events, list):
        raise SchemaError("frame events must be a list or an object containing an events list")

    screen_index = manifest.screen_index
    events: List[FrameEvent] = []
    seen_event_seqs: set[int] = set()
    last_event_seq: int | None = None
    last_frame_seq_by_screen: Dict[str, int] = {}

    for index, raw_event in enumerate(raw_events):
        event = require_dict(raw_event, f"events[{index}]")
        event_seq = require_int(event.get("event_seq"), f"events[{index}].event_seq", minimum=0)
        if event_seq in seen_event_seqs:
            raise SchemaError(f"duplicate event_seq: {event_seq}", {"event_seq": event_seq})
        if last_event_seq is not None and event_seq <= last_event_seq:
            raise SchemaError("event_seq must be strictly increasing in declaration order", {"event_seq": event_seq})
        seen_event_seqs.add(event_seq)
        last_event_seq = event_seq

        session_id = require_nonempty_str(event.get("session_id"), f"events[{index}].session_id")
        session = manifest.session_map.get(session_id)
        if session is None:
            raise SchemaError(f"event references unknown session: {session_id}", {"session_id": session_id})

        screen_id = require_nonempty_str(event.get("screen_id"), f"events[{index}].screen_id")
        pair = screen_index.get(screen_id)
        if pair is None or pair[0].session_id != session_id:
            raise SchemaError(
                f"event references screen {screen_id} not in session {session_id}",
                {"session_id": session_id, "screen_id": screen_id},
            )

        frame_seq = require_int(event.get("frame_seq"), f"events[{index}].frame_seq", minimum=0)
        last_frame_seq = last_frame_seq_by_screen.get(screen_id)
        if last_frame_seq is not None and frame_seq < last_frame_seq:
            raise SchemaError(
                "frame_seq must be monotonically non-decreasing per screen",
                {"screen_id": screen_id, "frame_seq": frame_seq, "last_frame_seq": last_frame_seq},
            )
        last_frame_seq_by_screen[screen_id] = frame_seq

        sampled_at_ms = require_int(event.get("sampled_at_ms"), f"events[{index}].sampled_at_ms", minimum=0)
        if sampled_at_ms < manifest.min_logic_ms or sampled_at_ms > manifest.max_logic_ms:
            raise SchemaError(
                "sampled_at_ms is outside declared logical clock bounds",
                {"sampled_at_ms": sampled_at_ms, "min_ms": manifest.min_logic_ms, "max_ms": manifest.max_logic_ms},
            )
        if sampled_at_ms < session.start_logic_ms:
            raise SchemaError(
                "sampled_at_ms precedes session start_logic_ms",
                {"session_id": session_id, "sampled_at_ms": sampled_at_ms, "start_logic_ms": session.start_logic_ms},
            )

        is_keyframe = event.get("is_keyframe")
        if not isinstance(is_keyframe, bool):
            raise SchemaError(f"events[{index}].is_keyframe must be boolean")

        payload_digest = require_nonempty_str(event.get("payload_digest"), f"events[{index}].payload_digest")
        generation = require_int(event.get("generation", V1_IMPLICIT_GENERATION), f"events[{index}].generation", minimum=1)

        events.append(
            FrameEvent(
                event_seq=event_seq,
                session_id=session_id,
                screen_id=screen_id,
                frame_seq=frame_seq,
                sampled_at_ms=sampled_at_ms,
                is_keyframe=is_keyframe,
                payload_digest=payload_digest,
                generation=generation,
            )
        )

    if not events:
        raise SchemaError("frame event stream must not be empty")
    return events


def parse_v1_snapshot(raw: Any, manifest: Manifest, source_path: str) -> List[SnapshotRecord]:
    root = require_dict(raw, "v1_snapshot")
    raw_records = root.get("records")
    if not isinstance(raw_records, list) or not raw_records:
        raise SchemaError("v1 snapshot must contain a non-empty records list")

    records: List[SnapshotRecord] = []
    for index, raw_record in enumerate(raw_records):
        record = require_dict(raw_record, f"v1.records[{index}]")
        screen_id = require_nonempty_str(record.get("screen_id"), f"v1.records[{index}].screen_id")
        pair = manifest.screen_index.get(screen_id)
        if pair is None:
            raise SchemaError(f"v1 record references unknown screen_id: {screen_id}")
        session, screen = pair

        second_value = None
        for key in ("relative_second", "relative_seconds", "offset_second", "offset_seconds", "at_second"):
            if key in record:
                second_value = record[key]
                break
        if second_value is None:
            raise SchemaError("v1 record requires relative_second", {"index": index})
        offset_ms = relative_seconds_to_ms(second_value, f"v1.records[{index}].relative_second")
        logic_ms = session.start_logic_ms + offset_ms
        if logic_ms > manifest.max_logic_ms:
            raise SchemaError("v1 relative second converts to logic_ms outside bounds", {"logic_ms": logic_ms})

        keyframe_seq = optional_int(record.get("keyframe_seq"), f"v1.records[{index}].keyframe_seq")
        decoded = optional_int(record.get("decoded_frame_seq"), f"v1.records[{index}].decoded_frame_seq")
        position = optional_int(record.get("play_position_ms"), f"v1.records[{index}].play_position_ms")
        if position is not None and position < 0:
            raise SchemaError("play_position_ms must be non-negative")

        locator = f"{source_path}#records[{index}]"
        records.append(
            SnapshotRecord(
                canonical_screen_id=screen.canonical_id,
                screen_id=screen_id,
                logic_ms=logic_ms,
                generation=V1_IMPLICIT_GENERATION,
                keyframe_seq=keyframe_seq,
                decoded_frame_seq=decoded,
                play_position_ms=position,
                tombstone=False,
                source_kind="v1",
                source_locator=locator,
                shard_id=None,
            )
        )
    return records
