"""按格式头或字段集分派解析/序列化路径；未知版本报 E_FORMAT_VERSION_UNSUPPORTED。"""
from __future__ import annotations

import json
import re

from ..errors import FormatVersionUnsupported, FormatCorrupt
from . import v1, v2, v3

_HEADER_RE = re.compile(r"^#format:\s*asr-slices-v(\d+)\s*$")
_PARSERS = {1: v1, 2: v2, 3: v3}


def detect_format(text: str) -> int:
    stripped = text.lstrip()
    if stripped.startswith("{"):
        try:
            doc = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise FormatCorrupt(f"JSON 解析失败: {exc}",
                                location=f"line {exc.lineno}") from None
        version = doc.get("format_version")
        if version == 3:
            return 3
        raise FormatVersionUnsupported(
            f"未知 JSON 格式版本: {version!r}", location="$.format_version")
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        match = _HEADER_RE.match(line)
        if match:
            version = int(match.group(1))
            if version in _PARSERS:
                return version
            raise FormatVersionUnsupported(
                f"未知格式版本: v{version}", location=f"line: {line!r}")
        if line.startswith("#"):
            continue
        first_field = line.split("|", 1)[0]
        return 2 if "=" in first_field else 1
    raise FormatCorrupt("空切片文件", location="line 1")


def load_slices(text: str, clip_id: str):
    version = detect_format(text)
    return _PARSERS[version].parse(text, clip_id)


def dump_slices(slice_set, version: int) -> str:
    if version not in _PARSERS:
        raise FormatVersionUnsupported(f"未知格式版本: v{version}")
    return _PARSERS[version].dump(slice_set)
