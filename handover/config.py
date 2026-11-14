"""映射配置加载与严格校验。

任何校验失败都抛出 ConfigError，错误信息必须指出具体键名 / 数组下标，
由调用方保证在写出任何产物之前终止（需求 9：不产生半成品输出）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .errors import ConfigError
from .tzutil import resolve_timezone


@dataclass(frozen=True)
class SourceConfig:
    name: str
    priority: int
    file_match: str
    mappings: dict[str, str]  # 来源字段名 -> 规范字段名
    input_date_formats: list[str]
    default_tz_name: str


@dataclass(frozen=True)
class Config:
    batch_id: str
    primary_key: str
    canonical_fields: tuple[str, ...]
    required_fields: tuple[str, ...]
    date_fields: tuple[str, ...]
    default_timezone_name: str
    sources: tuple[SourceConfig, ...]

    def source_for_file(self, file_name: str) -> SourceConfig | None:
        """按 file_match 前缀匹配来源；多来源命中时取最长前缀。"""
        best: SourceConfig | None = None
        for src in self.sources:
            if file_name.startswith(src.file_match):
                if best is None or len(src.file_match) > len(best.file_match):
                    best = src
        return best


def _require_object(value: object, path: str) -> dict:
    if not isinstance(value, dict):
        raise ConfigError(f"{path}: 期望为 JSON 对象，实际类型为 {type(value).__name__}")
    return value


def _require_string(value: object, path: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{path}: 必须是非空字符串，实际为 {value!r}")
    return value.strip()


def _require_string_list(value: object, path: str) -> list[str]:
    if not isinstance(value, list) or not value:
        raise ConfigError(f"{path}: 必须是非空字符串数组")
    result: list[str] = []
    for i, item in enumerate(value):
        if not isinstance(item, str) or not item.strip():
            raise ConfigError(f"{path}[{i}]: 必须是非空字符串，实际为 {item!r}")
        result.append(item.strip())
    return result


def _validate_strptime(formats: list[str], path: str) -> None:
    probe = datetime(2000, 1, 2, 3, 4, 5)
    for i, fmt in enumerate(formats):
        try:
            probe.strftime(fmt)
        except ValueError as exc:
            raise ConfigError(f"{path}[{i}]: 非法 strptime 格式 {fmt!r}（{exc}）") from None


def load_config(config_path: str | Path) -> Config:
    """加载并完整校验映射配置。"""
    path = Path(config_path)
    if not path.is_file():
        raise ConfigError(f"配置文件不存在: {path}")

    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise ConfigError(
            f"配置文件 {path.name} 不是合法 JSON：第 {exc.lineno} 行第 {exc.colno} 列 "
            f"附近 {exc.msg}"
        ) from None

    root = _require_object(raw, "$")

    for key in ("batch_id", "primary_key", "canonical_fields", "required_fields",
                "date_fields", "default_timezone", "sources"):
        if key not in root:
            raise ConfigError(f"$.{key}: 缺少必填键")

    batch_id = _require_string(root["batch_id"], "$.batch_id")
    primary_key = _require_string(root["primary_key"], "$.primary_key")
    canonical_fields = tuple(_require_string_list(root["canonical_fields"], "$.canonical_fields"))
    required_fields = tuple(_require_string_list(root["required_fields"], "$.required_fields"))
    date_fields = tuple(_require_string_list(root["date_fields"], "$.date_fields"))
    tz_name = _require_string(root["default_timezone"], "$.default_timezone")

    if len(set(canonical_fields)) != len(canonical_fields):
        raise ConfigError("$.canonical_fields: 存在重复字段名")
    canonical_set = set(canonical_fields)

    if primary_key not in canonical_set:
        raise ConfigError(
            f"$.primary_key: 主键字段 {primary_key!r} 不在 canonical_fields 中"
        )
    for i, name in enumerate(required_fields):
        if name not in canonical_set:
            raise ConfigError(
                f"$.required_fields[{i}]: 字段 {name!r} 不在 canonical_fields 中"
            )
    if primary_key not in required_fields:
        raise ConfigError("$.required_fields: 必须包含 primary_key 指向的主键字段")
    for i, name in enumerate(date_fields):
        if name not in canonical_set:
            raise ConfigError(
                f"$.date_fields[{i}]: 字段 {name!r} 不在 canonical_fields 中"
            )

    # 显式解析一次，非法时区在此处直接拒绝
    resolve_timezone(tz_name, where="$.default_timezone")

    sources_raw = root["sources"]
    if not isinstance(sources_raw, list) or not sources_raw:
        raise ConfigError("$.sources: 必须是非空数组")

    sources: list[SourceConfig] = []
    seen_names: set[str] = set()
    seen_priorities: set[int] = set()
    seen_matches: set[str] = set()

    for i, item in enumerate(sources_raw):
        spath = f"$.sources[{i}]"
        src = _require_object(item, spath)
        for key in ("name", "priority", "file_match", "mappings"):
            if key not in src:
                raise ConfigError(f"{spath}.{key}: 缺少必填键")

        name = _require_string(src["name"], f"{spath}.name")
        if name in seen_names:
            raise ConfigError(f"{spath}.name: 来源名 {name!r} 重复")
        seen_names.add(name)

        priority = src["priority"]
        if not isinstance(priority, int) or isinstance(priority, bool) or priority <= 0:
            raise ConfigError(f"{spath}.priority: 必须是正整数，实际为 {priority!r}")
        if priority in seen_priorities:
            raise ConfigError(f"{spath}.priority: 优先级 {priority} 与其他来源重复")
        seen_priorities.add(priority)

        file_match = _require_string(src["file_match"], f"{spath}.file_match")
        if file_match in seen_matches:
            raise ConfigError(f"{spath}.file_match: 前缀 {file_match!r} 与其他来源重复")
        seen_matches.add(file_match)

        mappings_raw = _require_object(src["mappings"], f"{spath}.mappings")
        if not mappings_raw:
            raise ConfigError(f"{spath}.mappings: 至少需要一条字段映射")
        mappings: dict[str, str] = {}
        for src_field, target in mappings_raw.items():
            mpath = f"{spath}.mappings.{src_field}"
            if not isinstance(src_field, str) or not src_field.strip():
                raise ConfigError(f"{spath}.mappings: 存在非法来源字段名 {src_field!r}")
            if not isinstance(target, str) or not target.strip():
                raise ConfigError(f"{mpath}: 映射目标必须是非空字符串")
            # 需求 9：映射目标不存在于规范字段时整体拒绝
            if target not in canonical_set:
                raise ConfigError(
                    f"{mpath}: 映射目标字段 {target!r} 不存在于 canonical_fields 中"
                )
            if src_field in mappings:
                raise ConfigError(f"{spath}.mappings: 来源字段 {src_field!r} 重复映射")
            mappings[src_field] = target

        if primary_key not in mappings.values():
            raise ConfigError(
                f"{spath}.mappings: 必须映射主键字段 {primary_key!r}，"
                f"否则该来源记录无法归属"
            )

        # 声明为日期的规范字段必须在该来源有映射来源，否则日期转换无法执行
        mapped_targets = set(mappings.values())
        for j, date_field in enumerate(date_fields):
            if date_field not in mapped_targets:
                raise ConfigError(
                    f"{spath}.mappings: date_fields[{j}] 声明的日期字段 "
                    f"{date_field!r} 在该来源没有任何映射来源"
                )

        # 业务时间取 date_fields[0]，它同时必须是必填字段，保证合并次序可计算
        if date_fields and date_fields[0] not in required_fields:
            raise ConfigError(
                "$.required_fields: 业务时间字段 "
                f"{date_fields[0]!r}（date_fields[0]）必须为必填字段"
            )

        date_formats = src.get("input_date_formats", [])
        if date_formats is None:
            date_formats = []
        if not isinstance(date_formats, list) or any(
            not isinstance(f, str) or not f.strip() for f in date_formats
        ):
            raise ConfigError(f"{spath}.input_date_formats: 必须是字符串数组")
        _validate_strptime(date_formats, f"{spath}.input_date_formats")

        src_tz = src.get("default_timezone", tz_name)
        if not isinstance(src_tz, str) or not src_tz.strip():
            raise ConfigError(f"{spath}.default_timezone: 必须是非空字符串")
        resolve_timezone(src_tz, where=f"{spath}.default_timezone")

        sources.append(
            SourceConfig(
                name=name,
                priority=priority,
                file_match=file_match,
                mappings=mappings,
                input_date_formats=list(date_formats),
                default_tz_name=src_tz,
            )
        )

    return Config(
        batch_id=batch_id,
        primary_key=primary_key,
        canonical_fields=canonical_fields,
        required_fields=required_fields,
        date_fields=date_fields,
        default_timezone_name=tz_name,
        sources=tuple(sources),
    )
