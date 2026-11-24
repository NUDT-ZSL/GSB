from __future__ import annotations

import json
import math
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable

from .errors import (
    E_CLOCK,
    E_DUP_RECEIVER,
    E_DUP_SESSION,
    E_FRAME_ORDER,
    E_FRAME_TYPE,
    E_GEOMETRY,
    E_REFERENCE,
    E_SCHEMA,
    E_TIMELINE,
    ScreenError,
)


def is_plain_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def require_nonnegative_int(value: Any, code: str, field: str, **details: Any) -> int:
    if not is_plain_int(value) or value < 0:
        raise ScreenError(code, f"{field} 必须是非负整数", {"field": field, "value": value, **details})
    return value


def require_positive_int(value: Any, code: str, field: str, **details: Any) -> int:
    if not is_plain_int(value) or value <= 0:
        raise ScreenError(code, f"{field} 必须是正整数", {"field": field, "value": value, **details})
    return value


def require_finite_positive(value: Any, field: str, code: str = E_GEOMETRY, **details: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ScreenError(code, f"{field} 必须是有限正数", {"field": field, "value": value, **details})
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise ScreenError(code, f"{field} 必须是有限正数", {"field": field, "value": value, **details})
    return number


def load_json(path: str | Path, *, code: str = E_SCHEMA, what: str = "JSON 文件") -> Any:
    path = Path(path)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ScreenError(code, f"{what}不存在", {"path": str(path)}) from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise ScreenError(code, f"{what}不可读取或解析", {"path": str(path), "cause": str(exc)}) from exc


def _first_key(data: dict[str, Any], keys: Iterable[str], default: Any = None) -> Any:
    for key in keys:
        if key in data and data[key] is not None:
            return data[key]
    return default


def _normalize_receiver(raw: Any, session_id: str) -> dict[str, Any]:
    if isinstance(raw, str):
        receiver_id = raw
        entry: dict[str, Any] = {}
    elif isinstance(raw, dict):
        entry = raw
        receiver_id = _first_key(entry, ("receiver_id", "canonical_receiver_id", "screen_id", "id"))
    else:
        raise ScreenError(E_SCHEMA, "接收端角色项必须是字符串或对象", {"session_id": session_id, "value": raw})
    if not isinstance(receiver_id, str) or not receiver_id.strip():
        raise ScreenError(E_SCHEMA, "接收端标识缺失或非法", {"session_id": session_id})
    aliases = entry.get("aliases", []) if isinstance(entry, dict) else []
    if isinstance(aliases, str):
        aliases = [aliases]
    if not isinstance(aliases, list) or not all(isinstance(item, str) and item for item in aliases):
        raise ScreenError(E_SCHEMA, "接收端 aliases 必须是非空字符串数组", {"receiver_id": receiver_id})
    tolerance = _first_key(entry, ("tolerance_ms", "late_tolerance_ms", "switch_tolerance_ms"))
    if tolerance is not None:
        require_nonnegative_int(tolerance, E_SCHEMA, "receiver.tolerance_ms", receiver_id=receiver_id)
    return {
        "receiver_id": receiver_id,
        "session_id": session_id,
        "role": entry.get("role", "receiver") if isinstance(entry, dict) else "receiver",
        "aliases": sorted(set(aliases)),
        "tolerance_ms": tolerance,
    }


def normalize_manifest(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise ScreenError(E_SCHEMA, "会话清单根节点必须是 JSON 对象")
    raw_sessions = data.get("sessions", data if isinstance(data.get("session_id"), str) else [])
    if isinstance(raw_sessions, dict):
        raw_sessions = [raw_sessions]
    if not isinstance(raw_sessions, list) or not raw_sessions:
        raise ScreenError(E_SCHEMA, "sessions 必须是非空数组")

    sessions: list[dict[str, Any]] = []
    session_index: dict[str, dict[str, Any]] = {}
    receiver_index: dict[str, dict[str, Any]] = {}
    alias_index: dict[str, str] = {}

    for raw_session in raw_sessions:
        if not isinstance(raw_session, dict):
            raise ScreenError(E_SCHEMA, "会话项必须是对象", {"value": raw_session})
        session_id = _first_key(raw_session, ("session_id", "id"))
        if not isinstance(session_id, str) or not session_id.strip():
            raise ScreenError(E_SCHEMA, "session_id 缺失或非法", {"session": raw_session})
        if session_id in session_index:
            raise ScreenError(E_DUP_SESSION, "会话标识重复", {"session_id": session_id})

        protocol = str(_first_key(raw_session, ("protocol_version", "protocol"), "S2")).upper()
        if protocol not in {"S1", "S2"}:
            raise ScreenError(E_SCHEMA, "协议版本必须是 S1 或 S2", {"session_id": session_id, "protocol": protocol})

        geometry = raw_session.get("geometry")
        if not isinstance(geometry, dict):
            raise ScreenError(E_GEOMETRY, "缺少屏幕几何 geometry", {"session_id": session_id})
        width = require_finite_positive(
            _first_key(geometry, ("width_px", "width", "widthPixels")), "geometry.width_px", session_id=session_id
        )
        height = require_finite_positive(
            _first_key(geometry, ("height_px", "height", "heightPixels")), "geometry.height_px", session_id=session_id
        )
        scale = require_finite_positive(
            _first_key(geometry, ("scale", "scale_factor", "scaleFactor")), "geometry.scale", session_id=session_id
        )

        start_ms = _first_key(raw_session, ("start_ms", "start_logical_ms", "base_ms"), 0)
        require_nonnegative_int(start_ms, E_CLOCK, "start_ms", session_id=session_id)
        multicast_group = _first_key(raw_session, ("multicast_group", "group"), "")
        if not isinstance(multicast_group, str) or not multicast_group.strip():
            raise ScreenError(E_SCHEMA, "multicast_group 必须是非空字符串", {"session_id": session_id})
        tolerance_ms = _first_key(
            raw_session, ("switch_tolerance_ms", "generation_tolerance_ms", "tolerance_ms"), 500
        )
        require_nonnegative_int(tolerance_ms, E_SCHEMA, "switch_tolerance_ms", session_id=session_id)
        breach_switch_generation = raw_session.get(
            "breach_switch_generation", raw_session.get("next_generation_on_breach")
        )
        if breach_switch_generation is not None:
            require_positive_int(
                breach_switch_generation, E_SCHEMA, "breach_switch_generation", session_id=session_id
            )

        sender_raw = raw_session.get("sender", {"sender_id": raw_session.get("sender_id", "sender")})
        if isinstance(sender_raw, str):
            sender = {"sender_id": sender_raw, "role": "sender"}
        elif isinstance(sender_raw, dict):
            sender = {
                "sender_id": _first_key(sender_raw, ("sender_id", "device_id", "id"), "sender"),
                "role": sender_raw.get("role", "sender"),
            }
        else:
            raise ScreenError(E_SCHEMA, "sender 必须是字符串或对象", {"session_id": session_id})

        raw_receivers = raw_session.get("receivers", raw_session.get("receiver_roles", []))
        if not isinstance(raw_receivers, list) or not raw_receivers:
            raise ScreenError(E_SCHEMA, "receivers 必须是非空数组", {"session_id": session_id})
        receivers = [_normalize_receiver(item, session_id) for item in raw_receivers]
        for receiver in receivers:
            rid = receiver["receiver_id"]
            if rid in receiver_index:
                raise ScreenError(E_DUP_RECEIVER, "接收端标识重复", {"receiver_id": rid})
            receiver_index[rid] = receiver
            if rid in alias_index and alias_index[rid] != rid:
                raise ScreenError(E_DUP_RECEIVER, "接收端规范标识与别名冲突", {"receiver_id": rid})
            alias_index[rid] = rid
            for alias in receiver["aliases"]:
                if alias in alias_index and alias_index[alias] != rid:
                    raise ScreenError(E_DUP_RECEIVER, "接收端别名重复", {"alias": alias})
                alias_index[alias] = rid
            if receiver["tolerance_ms"] is None:
                receiver["tolerance_ms"] = tolerance_ms

        session = {
            "session_id": session_id,
            "multicast_group": multicast_group,
            "protocol_version": protocol,
            "sender": sender,
            "receivers": receivers,
            "geometry": {"width_px": width, "height_px": height, "scale": scale},
            "start_ms": start_ms,
            "switch_tolerance_ms": tolerance_ms,
            "breach_switch_generation": breach_switch_generation,
        }
        sessions.append(session)
        session_index[session_id] = session

    return {
        "format": data.get("format", "multiscreen.manifest"),
        "seed": data.get("seed"),
        "sessions": sessions,
        "session_index": session_index,
        "receiver_index": receiver_index,
        "alias_index": alias_index,
    }


def load_manifest(path: str | Path) -> dict[str, Any]:
    return normalize_manifest(load_json(path, what="会话清单"))


FRAME_TYPE_ALIASES = {
    "key": "key",
    "keyframe": "key",
    "i": "key",
    "iframe": "key",
    "关键": "key",
    "关键帧": "key",
    "delta": "delta",
    "increment": "delta",
    "incremental": "delta",
    "p": "delta",
    "pframe": "delta",
    "增量": "delta",
    "增量帧": "delta",
    "discard": "discard",
    "discarded": "discard",
    "drop": "discard",
    "dropped": "discard",
    "丢弃": "discard",
    "丢弃帧": "discard",
}


def normalize_frame_type(raw_type: Any, key_marker: Any, context: dict[str, Any]) -> tuple[str, bool]:
    frame_type: str | None = None
    if raw_type is not None:
        key = str(raw_type).strip().lower()
        frame_type = FRAME_TYPE_ALIASES.get(key)
        if frame_type is None:
            raise ScreenError(E_FRAME_TYPE, "未知帧类型", {**context, "frame_type": raw_type})

    marker: bool | None = None
    if key_marker is not None:
        if not isinstance(key_marker, bool):
            raise ScreenError(E_FRAME_TYPE, "关键帧标记必须是布尔值", {**context, "keyframe": key_marker})
        marker = key_marker

    if frame_type is None and marker is None:
        raise ScreenError(E_FRAME_TYPE, "帧类型与关键帧标记至少提供一个", context)
    if frame_type is None:
        frame_type = "key" if marker else "delta"
    expected_marker = frame_type == "key"
    if marker is not None and marker != expected_marker:
        raise ScreenError(
            E_FRAME_TYPE,
            "帧类型与关键帧标记不一致",
            {**context, "frame_type": frame_type, "keyframe": marker, "expected_keyframe": expected_marker},
        )
    if frame_type == "discard" and expected_marker:
        raise ScreenError(E_FRAME_TYPE, "丢弃帧不能标记为关键帧", context)
    return frame_type, expected_marker


def _read_event_records(path: Path) -> list[dict[str, Any]]:
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".jsonl":
        records = []
        for line_number, line in enumerate(text.splitlines(), 1):
            if not line.strip():
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ScreenError(E_SCHEMA, "JSONL 事件不可解析", {"path": str(path), "line": line_number}) from exc
            if not isinstance(item, dict):
                raise ScreenError(E_SCHEMA, "事件行必须是 JSON 对象", {"line": line_number})
            item["_line"] = line_number
            records.append(item)
        return records
    data = json.loads(text)
    if isinstance(data, dict):
        records = data.get("events", data.get("frames", []))
    else:
        records = data
    if not isinstance(records, list):
        raise ScreenError(E_SCHEMA, "事件流必须是数组或含 events 数组")
    return [item for item in records if isinstance(item, dict)]


def normalize_events(data: Any, manifest: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    if isinstance(data, (str, Path)):
        raw_records = _read_event_records(Path(data))
    elif isinstance(data, list):
        raw_records = data
    else:
        raise ScreenError(E_SCHEMA, "事件流输入非法")

    alias_index = manifest.get("alias_index", {}) if manifest else {}
    session_index = manifest.get("session_index", {}) if manifest else {}
    events: list[dict[str, Any]] = []
    seen_event_seqs: set[int] = set()

    for raw in raw_records:
        event_seq = _first_key(raw, ("event_seq", "seq", "sequence"))
        require_nonnegative_int(event_seq, E_SCHEMA, "event_seq", source_line=raw.get("_line"))
        if event_seq in seen_event_seqs:
            raise ScreenError(E_SCHEMA, "事件序号重复", {"event_seq": event_seq})
        seen_event_seqs.add(event_seq)

        session_id = _first_key(raw, ("session_id", "session"))
        if manifest and session_id not in session_index:
            raise ScreenError(E_REFERENCE, "事件引用了不存在的会话", {"event_seq": event_seq, "session_id": session_id})

        raw_receiver = _first_key(raw, ("receiver_id", "canonical_receiver_id", "screen_id", "receiver"))
        receiver_id = alias_index.get(raw_receiver, raw_receiver) if isinstance(raw_receiver, str) else raw_receiver
        if manifest:
            if not isinstance(receiver_id, str) or receiver_id not in manifest["receiver_index"]:
                raise ScreenError(
                    E_REFERENCE,
                    "事件引用了不存在的接收端",
                    {"event_seq": event_seq, "receiver_id": raw_receiver},
                )
            expected_session = manifest["receiver_index"][receiver_id]["session_id"]
            if session_id != expected_session:
                raise ScreenError(
                    E_REFERENCE,
                    "事件会话与接收端所属会话不一致",
                    {"event_seq": event_seq, "session_id": session_id, "expected_session_id": expected_session},
                )

        sample_ms = _first_key(raw, ("sample_ms", "sample_ts_ms", "timestamp_ms", "ts_ms", "at_ms"))
        require_nonnegative_int(sample_ms, E_CLOCK, "sample_ms", event_seq=event_seq)

        record_kind = str(_first_key(raw, ("record_type", "event_type", "kind"), "frame")).lower()
        if record_kind in {"switch", "generation_switch", "marker"}:
            to_generation = _first_key(raw, ("to_generation", "new_generation", "generation"))
            require_positive_int(to_generation, E_SCHEMA, "to_generation", event_seq=event_seq)
            from_generation = raw.get("from_generation", raw.get("old_generation"))
            if from_generation is not None:
                require_positive_int(from_generation, E_SCHEMA, "from_generation", event_seq=event_seq)
            events.append(
                {
                    "event_seq": event_seq,
                    "record_type": "switch",
                    "session_id": session_id,
                    "receiver_id": receiver_id,
                    "sample_ms": sample_ms,
                    "from_generation": from_generation,
                    "to_generation": to_generation,
                    "reason": raw.get("reason", "explicit_marker"),
                }
            )
            continue

        frame_seq = _first_key(raw, ("frame_seq", "frame_no", "frame_index"))
        require_positive_int(frame_seq, E_SCHEMA, "frame_seq", event_seq=event_seq)
        generation = _first_key(raw, ("generation", "session_generation", "gen"), 1)
        require_positive_int(generation, E_SCHEMA, "generation", event_seq=event_seq)
        frame_type, keyframe = normalize_frame_type(
            _first_key(raw, ("frame_type", "type", "coding_type")),
            _first_key(raw, ("keyframe", "is_keyframe", "is_key_frame")),
            {"event_seq": event_seq, "receiver_id": receiver_id, "frame_seq": frame_seq},
        )
        coded_width = _first_key(raw, ("coded_width_px", "coded_width", "width"))
        coded_height = _first_key(raw, ("coded_height_px", "coded_height", "height"))
        if coded_width is not None:
            require_positive_int(coded_width, E_SCHEMA, "coded_width_px", event_seq=event_seq)
        if coded_height is not None:
            require_positive_int(coded_height, E_SCHEMA, "coded_height_px", event_seq=event_seq)
        payload_length = _first_key(raw, ("payload_length", "payload_bytes", "payload_len"), 0)
        require_nonnegative_int(payload_length, E_SCHEMA, "payload_length", event_seq=event_seq)

        events.append(
            {
                "event_seq": event_seq,
                "record_type": "frame",
                "session_id": session_id,
                "receiver_id": receiver_id,
                "frame_seq": frame_seq,
                "sample_ms": sample_ms,
                "generation": generation,
                "frame_type": frame_type,
                "keyframe": keyframe,
                "coded_width_px": coded_width,
                "coded_height_px": coded_height,
                "payload_length": payload_length,
            }
        )

    events.sort(key=lambda item: item["event_seq"])
    last_frame_seq: dict[str, int] = {}
    for event in events:
        if event["record_type"] != "frame":
            continue
        rid = event["receiver_id"]
        previous = last_frame_seq.get(rid)
        if previous is not None and event["frame_seq"] <= previous:
            raise ScreenError(
                E_FRAME_ORDER,
                "同一接收端的帧序号必须按事件序号严格递增",
                {
                    "receiver_id": rid,
                    "event_seq": event["event_seq"],
                    "previous_frame_seq": previous,
                    "frame_seq": event["frame_seq"],
                },
            )
        last_frame_seq[rid] = event["frame_seq"]
    return events


def load_events(path: str | Path, manifest: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    try:
        return normalize_events(path, manifest)
    except FileNotFoundError as exc:
        raise ScreenError(E_SCHEMA, "帧事件流不存在", {"path": str(path)}) from exc
    except OSError as exc:
        raise ScreenError(E_SCHEMA, "帧事件流不可读取", {"path": str(path), "cause": str(exc)}) from exc


def relative_seconds_to_ms(value: Any, start_ms: int, *, receiver_id: str, source: str) -> int:
    if isinstance(value, bool):
        raise ScreenError(E_TIMELINE, "v1 相对秒数非法", {"receiver_id": receiver_id, "source": source, "value": value})
    try:
        seconds = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ScreenError(E_TIMELINE, "v1 相对秒数无法换算", {"receiver_id": receiver_id, "value": value}) from exc
    if not seconds.is_finite() or seconds < 0:
        raise ScreenError(E_TIMELINE, "v1 相对秒数必须是非负有限数", {"receiver_id": receiver_id, "value": value})
    relative_ms = seconds * Decimal(1000)
    if relative_ms != relative_ms.to_integral_value():
        raise ScreenError(
            E_TIMELINE,
            "v1 相对秒数换算后不是整数逻辑毫秒",
            {"receiver_id": receiver_id, "value": value, "absolute_ms": str(relative_ms)},
        )
    require_nonnegative_int(start_ms, E_CLOCK, "start_ms", receiver_id=receiver_id)
    return start_ms + int(relative_ms)
