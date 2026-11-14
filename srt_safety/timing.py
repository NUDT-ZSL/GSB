"""SRT 时间码解析与帧号换算（纯整数/确定性浮点运算）。"""
from __future__ import annotations

import math
import re

# HH:MM:SS,mmm 或 HH:MM:SS.mmm，小时 1-2 位，分秒 00-59，毫秒 1-3 位
TIMECODE_RE = re.compile(r"^(\d{1,2}):([0-5]\d):([0-5]\d)[,.](\d{1,3})$")


def parse_timecode(token: str) -> int:
    """把 SRT 时间码解析为毫秒整数，非法时抛出 ValueError。"""
    matched = TIMECODE_RE.match(token.strip())
    if not matched:
        raise ValueError(f"时间码格式非法: {token!r}，应为 HH:MM:SS,mmm")
    hours, minutes, seconds, millis = matched.groups()
    return (
        (int(hours) * 3600 + int(minutes) * 60 + int(seconds)) * 1000
        + int(millis.ljust(3, "0"))
    )


def frame_number(milliseconds: int, fps: float) -> int:
    """时间(毫秒)乘帧率向下取整得到帧号。"""
    return math.floor(milliseconds * fps / 1000)
