"""数据模型、清单校验与归属/越界/重叠判定。

帧区间一律采用半开区间 [start_frame, end_frame)，即结束帧本身不属于该区间。
镜头时间轴区间：[shot.start_frame, shot.end_frame)。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from .errors import ManifestError

# 业务状态常量（同时作为 CSV 中的状态列取值）
VALID = "有效"
OUT_OF_BOUNDS = "越界"
OVERLAP = "重叠"
# 无效字幕的 CSV 状态列为空字符串（见需求 7）
INVALID_STATUS = ""
INVALID_REASON = "起止帧无效"

SHOT_REQUIRED = ("shot_id", "start_frame", "end_frame")
SUBTITLE_REQUIRED = ("id", "text", "start_frame", "end_frame")


# --------------------------------------------------------------------------- #
# 数据模型
# --------------------------------------------------------------------------- #
@dataclass
class Shot:
    shot_id: str
    start_frame: int
    end_frame: int
    fps: float
    order: int  # 在清单中的原始序号（0 起），用于警告/稳定性排序
    skipped: bool = False  # 与前序镜头重叠而被剔除时为 True


@dataclass
class Subtitle:
    sid: str
    text: str
    start_frame: int
    end_frame: int
    order: int  # 在清单中的原始序号（0 起）


@dataclass
class AuditRow:
    """一条字幕的完整审计结果，同时驱动 CSV 与 PNG。"""

    subtitle: Subtitle
    valid_input: bool
    invalid_reason: str = ""
    owner: Optional[Shot] = None
    fps: Optional[float] = None
    status: str = INVALID_STATUS          # "" / 有效 / 越界 / 重叠
    conflict: str = ""                     # CSV 冲突描述
    # 归属候选镜头（可能与 owner 不同：越界字幕保留候选用于毫秒换算与排序）
    candidate: Optional[Shot] = None
    # 重叠对信息：(对方字幕, 重叠帧数)
    overlaps: list[tuple[Subtitle, int]] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #
def natural_key(value: str) -> list[Any]:
    """自然排序键：S2 排在 S10 之前。"""
    parts: list[Any] = []
    buf = ""
    is_digit = False
    for ch in str(value):
        if ch.isdigit():
            if not is_digit:
                parts.append(buf)
                buf = ""
                is_digit = True
            buf += ch
        else:
            if is_digit:
                parts.append(int(buf))
                buf = ""
                is_digit = False
            buf += ch
    parts.append(int(buf) if is_digit and buf else buf)
    return parts


def frame_to_ms(frame: int, fps: float) -> float:
    """帧号 -> 毫秒（保留 3 位小数）。"""
    return round(frame * 1000.0 / fps, 3)


def _is_int(value: Any) -> bool:
    # bool 是 int 的子类，清单里 true/false 不能当作合法帧数
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _require_key(obj: dict[str, Any], key: str, file_label: str, where: str) -> Any:
    if key not in obj:
        raise ManifestError(
            f"文件 {file_label} 中{where}缺少键 \"{key}\""
        )
    return obj[key]


# --------------------------------------------------------------------------- #
# 清单读取与字段校验（需求 1）
# --------------------------------------------------------------------------- #
def _load_json(path: str | Path) -> Any:
    p = Path(path)
    if not p.exists():
        raise ManifestError(f"文件 {p} 不存在或无法访问")
    try:
        with p.open("r", encoding="utf-8-sig") as fh:
            return json.load(fh)
    except json.JSONDecodeError as exc:
        raise ManifestError(f"文件 {p} 不是合法 JSON：第 {exc.lineno} 行第 {exc.colno} 列 {exc.msg}") from exc
    except OSError as exc:
        raise ManifestError(f"文件 {p} 读取失败：{exc}") from exc


def _as_list(payload: Any, path: Path, wrapper_key: str) -> list[Any]:
    """支持顶层为数组，或 {"shots": [...]} / {"subtitles": [...]} 包装形式。"""
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict) and isinstance(payload.get(wrapper_key), list):
        return payload[wrapper_key]
    raise ManifestError(
        f"文件 {path} 顶层结构错误：应为数组，或含 \"{wrapper_key}\" 数组字段的对象"
    )


def _resolve_fps(entry: dict[str, Any], payload: Any, path: Path, kind: str) -> float:
    if "fps" in entry:
        fps = entry["fps"]
        if not _is_number(fps) or fps <= 0:
            raise ManifestError(
                f"文件 {path} 中第{kind}的键 \"fps\" 必须为正数，实际为 {fps!r}"
            )
        return float(fps)
    if isinstance(payload, dict) and _is_number(payload.get("fps")) and payload["fps"] > 0:
        return float(payload["fps"])
    raise ManifestError(
        f"文件 {path} 中第{kind}缺少帧率信息（条目键 \"fps\" 或顶层键 \"fps\" 均缺失/非法）"
    )


def load_shots(path: str | Path) -> list[Shot]:
    p = Path(path)
    payload = _load_json(p)
    items = _as_list(payload, p, "shots")
    shots: list[Shot] = []
    for idx, raw in enumerate(items):
        where = f"第 {idx + 1} 个镜头条目"
        if not isinstance(raw, dict):
            raise ManifestError(f"文件 {p} 中{where}必须为 JSON 对象")
        for key in SHOT_REQUIRED:
            _require_key(raw, key, p, where)
        shot_id = raw["shot_id"]
        if not isinstance(shot_id, str) or not shot_id.strip():
            raise ManifestError(f"文件 {p} 中{where}的键 \"shot_id\" 必须为非空字符串，实际为 {shot_id!r}")
        for key in ("start_frame", "end_frame"):
            if not _is_int(raw[key]):
                raise ManifestError(
                    f"文件 {p} 中{where}（shot_id={shot_id!r}）的键 \"{key}\" 必须为整数，实际为 {raw[key]!r}"
                )
        if raw["start_frame"] < 0 or raw["end_frame"] < 0:
            raise ManifestError(
                f"文件 {p} 中{where}（shot_id={shot_id!r}）的帧号不能为负"
            )
        if raw["start_frame"] >= raw["end_frame"]:
            raise ManifestError(
                f"文件 {p} 中{where}（shot_id={shot_id!r}）区间非法：start_frame 必须小于 end_frame"
            )
        fps = _resolve_fps(raw, payload, p, where)
        shots.append(Shot(shot_id, raw["start_frame"], raw["end_frame"], fps, idx))
    if not shots:
        raise ManifestError(f"文件 {p} 的镜头清单为空")
    return shots


def load_subtitles(path: str | Path) -> list[Subtitle]:
    p = Path(path)
    payload = _load_json(p)
    items = _as_list(payload, p, "subtitles")
    subs: list[Subtitle] = []
    seen_ids: set[str] = set()
    for idx, raw in enumerate(items):
        where = f"第 {idx + 1} 个字幕条目"
        if not isinstance(raw, dict):
            raise ManifestError(f"文件 {p} 中{where}必须为 JSON 对象")
        for key in SUBTITLE_REQUIRED:
            _require_key(raw, key, p, where)
        sid = raw["id"]
        if not isinstance(sid, str) or not sid.strip():
            raise ManifestError(f"文件 {p} 中{where}的键 \"id\" 必须为非空字符串，实际为 {sid!r}")
        if sid in seen_ids:
            raise ManifestError(f"文件 {p} 中字幕 id \"{sid}\" 重复")
        seen_ids.add(sid)
        if not isinstance(raw["text"], str):
            raise ManifestError(
                f"文件 {p} 中字幕 {sid!r} 的键 \"text\" 必须为字符串，实际为 {raw['text']!r}"
            )
        for key in ("start_frame", "end_frame"):
            if not _is_int(raw[key]):
                raise ManifestError(
                    f"文件 {p} 中字幕 {sid!r} 的键 \"{key}\" 必须为整数，实际为 {raw[key]!r}"
                )
        if raw["start_frame"] < 0 or raw["end_frame"] < 0:
            raise ManifestError(f"文件 {p} 中字幕 {sid!r} 的帧号不能为负")
        subs.append(Subtitle(sid, raw["text"], raw["start_frame"], raw["end_frame"], idx))
    return subs


# --------------------------------------------------------------------------- #
# 镜头清单冲突检查（需求 8）
# --------------------------------------------------------------------------- #
def check_shot_conflicts(shots: list[Shot]) -> list[str]:
    """按起始帧升序检查乱序/重叠，返回中文警告列表。

    重叠的后序镜头标记 skipped=True，后续归属检查不再使用它。
    仅乱序（不重叠）只警告、不剔除。
    """
    warnings: list[str] = []
    ordered = sorted(shots, key=lambda s: (s.start_frame, s.order))
    prev_in_input: Optional[Shot] = None
    for shot in shots:
        if prev_in_input is not None and shot.start_frame < prev_in_input.start_frame:
            warnings.append(
                f"警告：镜头区间乱序，镜头 {prev_in_input.shot_id}[{prev_in_input.start_frame},"
                f"{prev_in_input.end_frame}) 之后出现更早起点的镜头 {shot.shot_id}[{shot.start_frame},"
                f"{shot.end_frame})；已按起始帧重新排序处理"
            )
        prev_in_input = shot

    kept: list[Shot] = []
    for shot in ordered:
        for other in kept:
            if shot.start_frame < other.end_frame and other.start_frame < shot.end_frame:
                shot.skipped = True
                overlap = min(shot.end_frame, other.end_frame) - max(shot.start_frame, other.start_frame)
                warnings.append(
                    f"警告：镜头 {shot.shot_id}[{shot.start_frame},{shot.end_frame}) 与已保留镜头 "
                    f"{other.shot_id}[{other.start_frame},{other.end_frame}) 重叠 {overlap} 帧；"
                    f"镜头 {shot.shot_id} 不参与字幕归属检查，其余镜头继续处理"
                )
                break
        if not shot.skipped:
            kept.append(shot)
    return warnings


def usable_shots(shots: list[Shot]) -> list[Shot]:
    return [s for s in shots if not s.skipped]


# --------------------------------------------------------------------------- #
# 归属与越界判定（需求 2、3）
# --------------------------------------------------------------------------- #
def pick_candidate(sub: Subtitle, shots: list[Shot]) -> Optional[Shot]:
    """挑选与字幕区间关系最密切的镜头：

    1. 重叠帧数最多；
    2. 重叠相同则区间中点距离最近；
    3. 再相同则起始帧较小（确定性）。
    """
    best: Optional[Shot] = None
    best_key: Optional[tuple[int, float, int]] = None
    sub_mid = (sub.start_frame + sub.end_frame) / 2
    for shot in shots:
        overlap = max(0, min(sub.end_frame, shot.end_frame) - max(sub.start_frame, shot.start_frame))
        shot_mid = (shot.start_frame + shot.end_frame) / 2
        key = (overlap, -abs(sub_mid - shot_mid), -shot.start_frame)
        if best_key is None or key > best_key:
            best_key = key
            best = shot
    return best


def _oob_description(sub: Subtitle, shot: Shot) -> str:
    """越界方向 + 越界帧 + 允许的最大起止帧范围。"""
    early = max(0, shot.start_frame - sub.start_frame)
    late = max(0, sub.end_frame - shot.end_frame)
    allowed = f"[{shot.start_frame},{shot.end_frame})"
    parts: list[str] = []
    if early > 0:
        parts.append(f"早于镜头起点：越界起于第 {sub.start_frame} 帧，超出起点 {early} 帧")
    if late > 0:
        parts.append(f"晚于镜头终点：越界止于第 {sub.end_frame} 帧，超出终点 {late} 帧")
    return f"字幕区间[{sub.start_frame},{sub.end_frame}){('；'.join(parts))}；允许范围为 {allowed}"


# --------------------------------------------------------------------------- #
# 主审计流程
# --------------------------------------------------------------------------- #
def audit(shots: list[Shot], subs: list[Subtitle]) -> list[AuditRow]:
    active = usable_shots(shots)
    rows: list[AuditRow] = []

    # 第一遍：输入合法性（需求 7）+ 归属/越界（需求 2、3）
    for sub in subs:
        row = AuditRow(subtitle=sub, valid_input=True)
        if sub.start_frame == sub.end_frame:
            row.valid_input = False
            row.invalid_reason = f"{INVALID_REASON}：起止帧相同（均为第 {sub.start_frame} 帧）"
        elif sub.end_frame < sub.start_frame:
            row.valid_input = False
            row.invalid_reason = (
                f"{INVALID_REASON}：结束帧 {sub.end_frame} 小于开始帧 {sub.start_frame}"
            )
        rows.append(row)
        if not row.valid_input:
            continue

        candidate = pick_candidate(sub, active)
        row.candidate = candidate
        if candidate is None:
            row.status = OUT_OF_BOUNDS
            row.fps = shots[0].fps if shots else None
            row.conflict = (
                f"字幕区间[{sub.start_frame},{sub.end_frame})无可用镜头（镜头清单全部冲突被剔除）"
            )
            continue
        row.fps = candidate.fps
        inside = candidate.start_frame <= sub.start_frame and sub.end_frame <= candidate.end_frame
        if inside:
            row.owner = candidate
            row.status = VALID
        else:
            row.owner = candidate
            row.status = OUT_OF_BOUNDS
            row.conflict = _oob_description(sub, candidate)

    # 第二遍：同一镜头内有效字幕两两重叠检查（需求 4）
    groups: dict[str, list[AuditRow]] = {}
    for row in rows:
        if row.valid_input and row.status == VALID and row.owner is not None:
            groups.setdefault(row.owner.shot_id, []).append(row)

    for shot_id, group in groups.items():
        group.sort(key=lambda r: (r.subtitle.start_frame, natural_key(r.subtitle.sid)))
        for i in range(len(group)):
            for j in range(i + 1, len(group)):
                a, b = group[i].subtitle, group[j].subtitle
                overlap_frames = min(a.end_frame, b.end_frame) - max(a.start_frame, b.start_frame)
                if overlap_frames > 0:
                    group[i].status = OVERLAP
                    group[j].status = OVERLAP
                    group[i].overlaps.append((b, overlap_frames))
                    group[j].overlaps.append((a, overlap_frames))

    for row in rows:
        if row.overlaps:
            descs = [
                f"与字幕 {other.sid}（\"{other.text}\"，[{other.start_frame},{other.end_frame})）重叠 {n} 帧"
                for other, n in sorted(row.overlaps, key=lambda x: (x[0].start_frame, natural_key(x[0].sid)))
            ]
            prefix = f"归属镜头 {row.owner.shot_id}；" if row.owner else ""
            row.conflict = prefix + "重叠冲突：" + "；".join(descs)

    rows.sort(key=_row_sort_key)
    return rows


def _row_sort_key(row: AuditRow) -> tuple[Any, ...]:
    """需求 9：镜头编号升序（自然序），同镜头按字幕起始帧升序。

    越界字幕挂在候选镜头之后、有效字幕排完；无效输入字幕排在最后。
    顺序不受输入顺序影响，最终用字幕自然 ID 兜底保证确定性。
    """
    sub = row.subtitle
    anchor = row.owner or row.candidate
    if not row.valid_input:
        group_rank, shot_key = (3, [])
    elif row.status == OUT_OF_BOUNDS:
        group_rank, shot_key = (1, natural_key(anchor.shot_id) if anchor else [])
    else:
        group_rank, shot_key = (0, natural_key(anchor.shot_id) if anchor else [])
    return (group_rank, shot_key, sub.start_frame, natural_key(sub.sid))


def summarize(rows: list[AuditRow]) -> dict[str, int]:
    """需求 11 的四类计数。"""
    counts = {"有效": 0, "越界": 0, "重叠": 0, "无效": 0}
    for row in rows:
        if not row.valid_input:
            counts["无效"] += 1
        elif row.status in counts:
            counts[row.status] += 1
    return counts
