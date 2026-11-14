"""Loading and validation of the frame table and dialogue list.

Both inputs are JSON.  A malformed *entry* is recorded as an issue and never
aborts the whole run; only a malformed *document* (unreadable file, invalid
JSON, wrong top-level shape) raises :class:`InputError`.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Accepted aliases keep the tool usable with slightly different export shapes.
_FRAME_LIST_KEYS = ("frames", "rows", "entries", "lines")
_DIALOGUE_LIST_KEYS = ("dialogues", "lines", "entries")
_LINE_ID_KEYS = ("line_id", "id")
_MOUTH_KEYS = ("mouth_time_ms", "time_ms", "mouth_ms", "t_ms", "time")
_WAV_KEYS = ("wav_path", "audio_path", "wav", "wav_file")


class InputError(Exception):
    """Fatal problem with an input document."""


@dataclass
class FrameTable:
    times_by_line: dict[str, list[float]] = field(default_factory=dict)
    issues: list[dict[str, Any]] = field(default_factory=list)

    def earliest(self, line_id: str) -> float | None:
        times = self.times_by_line.get(line_id)
        return min(times) if times else None


@dataclass
class DialogueEntry:
    line_id: str
    wav_path: str | None
    resolved_path: Path | None
    text: str | None
    path_error: str | None


@dataclass
class DialogueList:
    entries: dict[str, DialogueEntry] = field(default_factory=dict)
    issues: list[dict[str, Any]] = field(default_factory=list)


def _load_json(path: Path) -> Any:
    if not path.is_file():
        raise InputError(f"输入文件不存在: {path}")
    try:
        with path.open("r", encoding="utf-8") as fh:
            return json.load(fh)
    except json.JSONDecodeError as exc:
        raise InputError(f"JSON 解析失败: {path} ({exc.msg}, 行 {exc.lineno})") from exc
    except OSError as exc:
        raise InputError(f"无法读取输入文件: {path} ({exc})") from exc


def _extract_rows(document: Any, candidate_keys: tuple[str, ...], label: str) -> list[Any]:
    if isinstance(document, list):
        return document
    if isinstance(document, dict):
        for key in candidate_keys:
            if key in document:
                rows = document[key]
                if not isinstance(rows, list):
                    raise InputError(f"{label} 的 `{key}` 字段必须是数组")
                return rows
    raise InputError(f"{label} 顶层必须是数组，或包含下列任一数组字段: {', '.join(candidate_keys)}")


def normalize_line_id(value: Any) -> str | None:
    """Return a canonical non-empty string id, or None if invalid."""
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        text = value.strip()
        return text or None
    if isinstance(value, int):
        return str(value)
    return None


def normalize_mouth_ms(value: Any) -> float | None:
    """Return a finite, non-negative millisecond value, or None if invalid."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not math.isfinite(number) or number < 0:
        return None
    return number


def load_frame_table(path: str | Path) -> FrameTable:
    p = Path(path)
    document = _load_json(p)
    rows = _extract_rows(document, _FRAME_LIST_KEYS, "帧表")
    table = FrameTable()

    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            table.issues.append({"source": "frame_table", "row": index, "reason": "帧记录不是对象"})
            continue

        line_id = None
        for key in _LINE_ID_KEYS:
            if key in row:
                line_id = normalize_line_id(row.get(key))
                break
        if line_id is None:
            table.issues.append({"source": "frame_table", "row": index, "reason": "语句标识缺失或非法"})
            continue

        raw_time = None
        for key in _MOUTH_KEYS:
            if key in row:
                raw_time = row.get(key)
                break
        mouth_ms = normalize_mouth_ms(raw_time)
        if mouth_ms is None:
            table.issues.append(
                {
                    "source": "frame_table",
                    "row": index,
                    "line_id": line_id,
                    "reason": "毫秒时间缺失或非法",
                }
            )
            continue

        table.times_by_line.setdefault(line_id, []).append(mouth_ms)

    # De-duplicate identical timestamps while keeping deterministic order.
    for line_id, times in table.times_by_line.items():
        table.times_by_line[line_id] = sorted(set(times))
    return table


def load_dialogues(path: str | Path) -> DialogueList:
    p = Path(path)
    document = _load_json(p)
    rows = _extract_rows(document, _DIALOGUE_LIST_KEYS, "对白清单")
    base_dir = p.parent
    dialogues = DialogueList()

    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            dialogues.issues.append({"source": "dialogue_list", "row": index, "reason": "对白记录不是对象"})
            continue

        line_id = None
        for key in _LINE_ID_KEYS:
            if key in row:
                line_id = normalize_line_id(row.get(key))
                break
        if line_id is None:
            dialogues.issues.append({"source": "dialogue_list", "row": index, "reason": "语句标识缺失或非法"})
            continue

        if line_id in dialogues.entries:
            dialogues.issues.append(
                {
                    "source": "dialogue_list",
                    "row": index,
                    "line_id": line_id,
                    "reason": "语句标识重复，已保留首条记录",
                }
            )
            continue

        raw_wav: Any = None
        for key in _WAV_KEYS:
            if key in row:
                raw_wav = row.get(key)
                break

        text = row.get("text") if isinstance(row.get("text"), str) else None
        wav_path: str | None = None
        resolved: Path | None = None
        path_error: str | None = None

        if raw_wav is None or (isinstance(raw_wav, str) and not raw_wav.strip()):
            path_error = "对白清单缺少有效 WAV 路径"
        elif not isinstance(raw_wav, str):
            path_error = "WAV 路径字段不是字符串"
        else:
            wav_path = raw_wav.strip()
            resolved = Path(wav_path)
            if not resolved.is_absolute():
                resolved = base_dir / resolved
            resolved = resolved.resolve()

        dialogues.entries[line_id] = DialogueEntry(
            line_id=line_id,
            wav_path=wav_path,
            resolved_path=resolved,
            text=text,
            path_error=path_error,
        )

    return dialogues
