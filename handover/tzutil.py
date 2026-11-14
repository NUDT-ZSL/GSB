"""时区解析与日期规范化。

设计要点：
- 优先使用标准库 zoneinfo（Windows 上依赖 tzdata 包，本机已安装）；
- 对常见 "UTC+8" 写法与少量常用名提供内置兜底，避免现场缺 tzdata 时整体失败；
- 不引入任何第三方依赖。
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone, tzinfo
from zoneinfo import ZoneInfo

from .errors import ConfigError

# 常用时区名兜底（zoneinfo 不可用时仍可工作）
_FALLBACK_ZONES: dict[str, tzinfo] = {
    "UTC": timezone.utc,
    "GMT": timezone.utc,
    "Z": timezone.utc,
    "ASIA/SHANGHAI": timezone(timedelta(hours=8)),
    "ASIA/BEIJING": timezone(timedelta(hours=8)),
    "ASIA/HONG_KONG": timezone(timedelta(hours=8)),
    "ASIA/SINGAPORE": timezone(timedelta(hours=8)),
    "ASIA/TOKYO": timezone(timedelta(hours=9)),
    "EUROPE/LONDON": timezone(timedelta(hours=0)),
    "AMERICA/NEW_YORK": timezone(timedelta(hours=-5)),
    "AMERICA/LOS_ANGELES": timezone(timedelta(hours=-8)),
}

_OFFSET_RE = re.compile(r"^UTC([+-])(\d{1,2})(?::?(\d{2}))?$", re.IGNORECASE)


def resolve_timezone(name: str, *, where: str) -> tzinfo:
    """把配置中的时区名称解析为 tzinfo。

    where 用于错误信息，标明出错的配置位置。
    """
    if not isinstance(name, str) or not name.strip():
        raise ConfigError(f"{where}: 时区名称必须是非空字符串，实际为 {name!r}")
    key = name.strip()
    upper = key.upper()

    m = _OFFSET_RE.match(key)
    if m:
        sign, hours, minutes = m.groups()
        delta = timedelta(hours=int(hours), minutes=int(minutes or 0))
        if sign == "-":
            delta = -delta
        return timezone(delta, name=key)

    try:
        return ZoneInfo(key)
    except Exception:
        pass

    if upper in _FALLBACK_ZONES:
        return _FALLBACK_ZONES[upper]

    raise ConfigError(
        f"{where}: 无法解析时区 {name!r}（请使用 IANA 名称如 Asia/Shanghai，"
        "或 UTC+8 形式的固定偏移）"
    )


# 标准库 fromisoformat 在 3.10 不支持末尾 Z，统一预处理
_ISO_Z_RE = re.compile(r"Z$", re.IGNORECASE)


def parse_datetime(
    raw: object,
    formats: list[str],
    default_tz: tzinfo,
    *,
    where: str,
) -> tuple[datetime, str]:
    """解析来源日期值，返回 (aware datetime, 规范字符串)。

    - 带时区标记的值：按其自身时区解析，再换算到 default_tz；
    - 不带时区标记的值：按 default_tz 解释；
    - 依次尝试 ISO 8601 与配置中声明的 strptime 格式。
    """
    if not isinstance(raw, str) or not raw.strip():
        raise ConfigError(f"{where}: 日期值必须是非空字符串，实际为 {raw!r}")
    text = raw.strip()

    dt: datetime | None = None
    iso_candidate = _ISO_Z_RE.sub("+00:00", text)
    try:
        dt = datetime.fromisoformat(iso_candidate)
    except ValueError:
        dt = None

    if dt is None:
        for fmt in formats:
            try:
                dt = datetime.strptime(text, fmt)
                break
            except ValueError:
                continue

    if dt is None:
        raise ConfigError(
            f"{where}: 日期 {text!r} 无法用 ISO 8601 或配置格式 "
            f"{formats!r} 解析"
        )

    if dt.tzinfo is None:
        # 需求 2：时区标记缺失时按配置默认时区解释
        dt = dt.replace(tzinfo=default_tz)
    else:
        # 统一换算到默认时区，保证同一时刻输出完全一致的规范字符串
        dt = dt.astimezone(default_tz)

    return dt, format_canonical(dt)


def format_canonical(dt: datetime) -> str:
    """规范日期格式：偏移型 ISO 8601，秒精度，例如 2026-11-14T09:30:00+08:00。"""
    if dt.tzinfo is None:
        raise ValueError("format_canonical 要求带时区的 datetime")
    return dt.isoformat(timespec="seconds")
