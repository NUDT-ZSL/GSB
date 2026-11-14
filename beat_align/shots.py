"""镜头清单读取与原始起点推导（需求 3、6）。

清单为 JSON 文件，顶层是数组，每条至少包含 ``title`` 与 ``duration_ms``::

    [
      {"title": "开场航拍", "duration_ms": 2500},
      {"title": "人物特写", "duration_ms": 0},
      {"title": "产品全景"}
    ]

规则：

* 镜头起点不允许手工指定，一律由前面各镜头时长累计推导；
* ``duration_ms`` 缺失、为 null、为 0 或无法解析为非负数时，该镜头标记为
  跳过并记录原因，但仍保留在结果顺序中，不影响后续镜头编号；
* 全部镜头时长之和与配乐时长比较，超出则设置 ``total_exceeds_audio`` 标记，
  被跳过的镜头仍参与对齐流程之外的结果记录（需求 3、6、10）。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

from .errors import ShotListError

STATUS_ALIGNED = "aligned"
STATUS_SKIPPED = "skipped"


@dataclass
class ShotEntry:
    """镜头清单中的一条记录（跳过的镜头同样保留）。"""

    index: int
    """从 1 开始的镜头序号，与清单顺序一致，跳过也不重排。"""
    title: str
    duration_ms: int | None
    original_start_ms: int
    """由累计时长推导的原始起点（毫秒）。"""
    skipped: bool = False
    skip_reason: str = ""
    extras: dict = field(default_factory=dict)
    """清单中未识别的附加字段原样保留，方便回查。"""


def load_shots(path: str) -> list[ShotEntry]:
    """读取镜头清单 JSON 并推导累计起点。"""
    if not os.path.exists(path):
        raise ShotListError(f"镜头清单文件不存在：{path}")
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except json.JSONDecodeError as exc:
        raise ShotListError(f"镜头清单不是合法 JSON：{path}（第 {exc.lineno} 行：{exc.msg}）") from exc
    except OSError as exc:
        raise ShotListError(f"镜头清单读取失败：{path}（{exc}）") from exc

    if not isinstance(data, list) or not data:
        raise ShotListError("镜头清单顶层必须是非空数组，每条含 title 与 duration_ms")

    entries: list[ShotEntry] = []
    cursor = 0
    known_keys = {"title", "duration_ms"}

    for i, raw in enumerate(data, start=1):
        if not isinstance(raw, dict):
            raise ShotListError(f"镜头清单第 {i} 条必须是 JSON 对象，实际为 {type(raw).__name__}")

        title = raw.get("title")
        if not isinstance(title, str) or not title.strip():
            raise ShotListError(f"镜头清单第 {i} 条缺少有效 title 字段")
        title = title.strip()

        entry = ShotEntry(
            index=i,
            title=title,
            duration_ms=None,
            original_start_ms=cursor,
            extras={k: v for k, v in raw.items() if k not in known_keys},
        )

        if "duration_ms" not in raw:
            entry.skipped = True
            entry.skip_reason = "缺少 duration_ms 字段"
        else:
            duration = raw["duration_ms"]
            if isinstance(duration, bool) or not isinstance(duration, (int, float)):
                entry.skipped = True
                entry.skip_reason = f"duration_ms 不是数值（实际：{duration!r}）"
            elif duration <= 0:
                entry.skipped = True
                entry.skip_reason = f"duration_ms 为 {duration}，时长必须大于 0"
            elif duration != int(duration):
                entry.skipped = True
                entry.skip_reason = f"duration_ms 必须是整数毫秒（实际：{duration}）"
            else:
                entry.duration_ms = int(duration)

        # 只有有效时长才推进累计游标；跳过镜头的起点仍记录在当前游标处
        if entry.duration_ms is not None:
            cursor += entry.duration_ms
        entries.append(entry)

    return entries


def total_duration_ms(entries: list[ShotEntry]) -> int:
    """有效镜头时长之和（跳过镜头不计入）。"""
    return sum(e.duration_ms for e in entries if e.duration_ms is not None)


def mark_overhang(entries: list[ShotEntry], audio_duration_ms: int) -> dict:
    """比较清单总长与配乐时长，返回超限标记信息（需求 3）。"""
    total = total_duration_ms(entries)
    exceeds = total > audio_duration_ms
    return {
        "shots_total_ms": total,
        "audio_duration_ms": audio_duration_ms,
        "total_exceeds_audio": exceeds,
        "overhang_ms": max(0, total - audio_duration_ms),
        "message": (
            f"镜头总长 {total} ms 超出配乐时长 {audio_duration_ms} ms，"
            f"超出 {total - audio_duration_ms} ms，超出部分仍参与对齐但无配乐覆盖"
            if exceeds
            else f"镜头总长 {total} ms 未超过配乐时长 {audio_duration_ms} ms"
        ),
    }
