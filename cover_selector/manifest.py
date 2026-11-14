"""读取候选时间点清单。"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .errors import SetupError


@dataclass
class ManifestEntry:
    """清单中的一条候选记录。``ok`` 为 False 时 ``error`` 说明跳过原因。"""

    position: int
    time: float | None = None
    label: str | None = None
    raw: Any = None
    extra: dict[str, Any] = field(default_factory=dict)
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None

    @property
    def display(self) -> str:
        if self.label:
            return f"{self.label} ({self.position})"
        return f"#{self.position}"


def _coerce_time(value: Any) -> tuple[float | None, str | None]:
    if isinstance(value, bool) or value is None:
        return None, f"时间字段不是数值：{value!r}"
    if isinstance(value, (int, float)):
        number = float(value)
        if not math.isfinite(number):
            return None, f"时间字段不是有限数值：{value!r}"
        return number, None
    if isinstance(value, str):
        text = value.strip()
        try:
            number = float(text)
        except ValueError:
            return None, f"时间字段不是数值：{value!r}"
        if not math.isfinite(number):
            return None, f"时间字段不是有限数值：{value!r}"
        return number, None
    return None, f"时间字段不是数值：{value!r}"


def load_manifest(path: Path) -> list[ManifestEntry]:
    """读取 JSON 清单。条目级问题只记录原因，不影响其余条目。

    支持的清单格式：
    - ``{"items": [...]}``
    - ``{"candidates": [...]}``
    - ``[...]``
    条目可以是 ``{"time": 1.2, "label": "..."}``，也可以直接是数值时间。
    """

    try:
        with path.open("r", encoding="utf-8-sig") as handle:
            raw = json.load(handle)
    except FileNotFoundError as exc:
        raise SetupError(f"候选清单不存在：{path}") from exc
    except OSError as exc:
        raise SetupError(f"无法读取候选清单 {path}：{exc}") from exc
    except json.JSONDecodeError as exc:
        raise SetupError(f"候选清单不是合法 JSON：{path}（第 {exc.lineno} 行）") from exc

    if isinstance(raw, dict):
        for key in ("items", "candidates", "times"):
            if key in raw:
                raw = raw[key]
                break
        else:
            raise SetupError("候选清单是 JSON 对象，但缺少 items / candidates / times 数组")
    if not isinstance(raw, list):
        raise SetupError("候选清单顶层必须是数组，或包含 items 数组的对象")
    if not raw:
        raise SetupError("候选清单为空，没有可处理的时间点")

    entries: list[ManifestEntry] = []
    for position, item in enumerate(raw, start=1):
        entry = ManifestEntry(position=position, raw=item)
        entries.append(entry)
        if isinstance(item, dict):
            label = item.get("label")
            entry.label = None if label in (None, "") else str(label)
            entry.extra = {k: v for k, v in item.items() if k not in ("time", "label")}
            if "time" not in item:
                entry.error = "清单条目缺少 time 字段"
                continue
            entry.time, error = _coerce_time(item["time"])
            entry.error = error
        elif isinstance(item, (int, float, str)) and not isinstance(item, bool):
            entry.time, entry.error = _coerce_time(item)
        else:
            entry.error = f"清单条目不是对象或数值：{type(item).__name__}"
    return entries
