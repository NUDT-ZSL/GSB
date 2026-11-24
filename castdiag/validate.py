"""输入解析与校验.

校验规则(任一失败抛出带 E_xxx 业务码的 DiagError, 调用方不得产生输出):
  - 会话标识唯一                       -> E_DUP_SESSION
  - 屏幕几何为有限正数                 -> E_GEOMETRY
  - 帧序号按 (会话, 接收屏, 代数) 严格单调 -> E_FRAME_ORDER
  - 逻辑时钟毫秒为非负整数             -> E_CLOCK
  - v1 相对秒可换算到 v2 绝对毫秒时间轴 -> E_TIMELINE
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np

from .errors import DiagError, E_CLOCK, E_DUP_SESSION, E_FRAME_ORDER, E_GEOMETRY, E_SCHEMA, E_TIMELINE


def _is_nonneg_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def load_manifest(path) -> dict:
    try:
        manifest = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DiagError(E_SCHEMA, "会话清单不可解析", {"path": str(path), "cause": str(exc)})
    if not isinstance(manifest, dict) or not isinstance(manifest.get("sessions"), list):
        raise DiagError(E_SCHEMA, "会话清单缺少 sessions 数组", {"path": str(path)})
    seen: set[str] = set()
    for idx, sess in enumerate(manifest["sessions"]):
        sid = sess.get("session_id") if isinstance(sess, dict) else None
        if not isinstance(sid, str) or not sid:
            raise DiagError(E_SCHEMA, "会话缺少 session_id", {"index": idx})
        if sid in seen:
            raise DiagError(E_DUP_SESSION, "会话标识重复", {"session_id": sid})
        seen.add(sid)
        if not _is_nonneg_int(sess.get("start_ms")):
            raise DiagError(E_CLOCK, "起点逻辑毫秒必须为非负整数",
                            {"session_id": sid, "start_ms": sess.get("start_ms")})
        geo = sess.get("geometry")
        if not isinstance(geo, dict):
            raise DiagError(E_GEOMETRY, "缺少屏幕几何", {"session_id": sid})
        for field in ("width_px", "height_px", "scale"):
            value = geo.get(field)
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                raise DiagError(E_GEOMETRY, "屏幕几何字段必须为数值",
                                {"session_id": sid, "field": field, "value": value})
            if not np.isfinite(value) or value <= 0:
                raise DiagError(E_GEOMETRY, "屏幕几何必须为有限正数",
                                {"session_id": sid, "field": field, "value": value})
    return manifest


def load_events(path) -> list[dict]:
    events: list[dict] = []
    try:
        fh = open(path, "r", encoding="utf-8")
    except OSError as exc:
        raise DiagError(E_SCHEMA, "帧事件流不可读", {"path": str(path), "cause": str(exc)})
    with fh:
        for line_no, line in enumerate(fh, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError as exc:
                raise DiagError(E_SCHEMA, "帧事件行不可解析", {"line": line_no, "cause": str(exc)})
            events.append(ev)
    last_frame_no: dict[tuple, int] = {}
    seen_seq: set[int] = set()
    for ev in events:
        seq = ev.get("seq")
        if not _is_nonneg_int(seq):
            raise DiagError(E_CLOCK, "事件序号必须为非负整数", {"seq": seq})
        if seq in seen_seq:
            raise DiagError(E_SCHEMA, "事件序号重复", {"seq": seq})
        seen_seq.add(seq)
        sample_ms = ev.get("sample_ms")
        if not _is_nonneg_int(sample_ms):
            raise DiagError(E_CLOCK, "采样时刻必须为非负整数毫秒",
                            {"seq": seq, "sample_ms": sample_ms})
        key = (ev.get("session_id"), ev.get("screen_id"), ev.get("generation"))
        frame_no = ev.get("frame_no")
        if not isinstance(frame_no, int) or isinstance(frame_no, bool) or frame_no <= 0:
            raise DiagError(E_FRAME_ORDER, "帧序号必须为正整数", {"seq": seq, "frame_no": frame_no})
        prev = last_frame_no.get(key)
        if prev is not None and frame_no <= prev:
            raise DiagError(E_FRAME_ORDER, "帧序号非单调递增",
                            {"session_id": key[0], "screen_id": key[1],
                             "prev_frame_no": prev, "frame_no": frame_no, "seq": seq})
        last_frame_no[key] = frame_no
    return events


def v1_rel_to_abs_ms(base_epoch_ms, rel_start_s, screen_id: str) -> int:
    """v1 相对起始秒数 -> 绝对逻辑毫秒; 不可换算时抛 E_TIMELINE."""
    if not _is_nonneg_int(base_epoch_ms):
        raise DiagError(E_TIMELINE, "v1 base_epoch_ms 必须为非负整数毫秒",
                        {"base_epoch_ms": base_epoch_ms})
    bad_rel = not isinstance(rel_start_s, (int, float)) or isinstance(rel_start_s, bool)
    if bad_rel or not math.isfinite(rel_start_s) or rel_start_s < 0:
        raise DiagError(E_TIMELINE, "v1 相对起始秒数必须为有限非负数",
                        {"screen_id": screen_id, "rel_start_s": rel_start_s})
    scaled = rel_start_s * 1000.0
    rounded = round(scaled)
    if abs(scaled - rounded) > 1e-6:
        raise DiagError(E_TIMELINE, "v1 相对秒数无法精确换算到整数毫秒时间轴",
                        {"screen_id": screen_id, "rel_start_s": rel_start_s})
    abs_ms = base_epoch_ms + rounded
    if abs_ms < 0:
        raise DiagError(E_TIMELINE, "换算后的绝对毫秒为负",
                        {"screen_id": screen_id, "abs_ms": abs_ms})
    return abs_ms


def validate_timeline(v1: dict, v2_rows: list[dict]) -> None:
    """校验两代快照可换算到同一时间轴."""
    base = v1.get("base_epoch_ms")
    max_v2_ts = max((row["ts_ms"] for row in v2_rows), default=0)
    max_tol = max((row["tolerance_ms"] for row in v2_rows), default=0)
    for screen_id, entry in v1.get("screens", {}).items():
        abs_ms = v1_rel_to_abs_ms(base, entry.get("rel_start_s"), screen_id)
        if v2_rows and abs_ms > max_v2_ts + max_tol:
            raise DiagError(E_TIMELINE, "v1 换算时刻超出 v2 时间轴可达范围",
                            {"screen_id": screen_id, "abs_ms": abs_ms,
                             "max_v2_ts_ms": max_v2_ts, "tolerance_ms": max_tol})
