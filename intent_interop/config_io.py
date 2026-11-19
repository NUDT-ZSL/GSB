"""Readers for v1, v2 and v3 intent configuration forms."""

from __future__ import annotations

import hashlib
import json
import math
import re
from pathlib import Path
from typing import Any

from .errors import IntentError
from .models import (
    DEFAULT_FALLBACK,
    ConfigDocument,
    IntentSpec,
    SlotSpec,
    SUPPORTED_VERSIONS,
)

_META_RE = re.compile(r"^\s*#\s*([A-Za-z_][A-Za-z0-9_-]*)\s*[:=]\s*(.*?)\s*$")
_JSON_INTENT_LINE_RE = re.compile(r'"intent"\s*:')
_V2_REQUIRED_KEYS = ("intent", "keywords", "threshold", "fallback_intent")
_V3_INTENT_KEYS = (
    "intent",
    "keywords",
    "threshold",
    "fallback_intent",
    "label_path",
    "slots",
    "weight_vector",
)


def _read_bytes(path: str | Path) -> bytes:
    try:
        return Path(path).read_bytes()
    except OSError as exc:
        raise IntentError(
            "E_CONFIG_INVALID",
            f"无法读取配置：{path}",
            stage="read",
            field="path",
            path=str(path),
            reason=str(exc),
        ) from exc


def _decode_utf8(raw: bytes, path: str) -> str:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise IntentError(
            "E_FORMAT_CORRUPT",
            "配置不是合法 UTF-8",
            stage="read",
            field="encoding",
            field_offset=exc.start,
            path=path,
        ) from exc
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _metadata(text: str) -> tuple[dict[str, str], dict[str, tuple[int, int]]]:
    values: dict[str, str] = {}
    positions: dict[str, tuple[int, int]] = {}
    byte_offset = 0
    for line_no, line in enumerate(text.splitlines(keepends=True), start=1):
        match = _META_RE.match(line)
        if match:
            key = match.group(1)
            if key in values:
                raise IntentError(
                    "E_FORMAT_CORRUPT",
                    f"元数据键重复：{key}",
                    stage="read",
                    line_no=line_no,
                    field=key,
                )
            values[key] = match.group(2).strip()
            positions[key] = (line_no, byte_offset)
        byte_offset += len(line.encode("utf-8"))
    return values, positions


def _data_lines(text: str) -> list[tuple[int, str]]:
    lines: list[tuple[int, str]] = []
    for line_no, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        lines.append((line_no, stripped))
    return lines


def _first_data_line(text: str) -> int:
    rows = _data_lines(text)
    return rows[0][0] if rows else 1


def detect_version(text: str) -> str:
    stripped = text.lstrip()
    if stripped.startswith("{"):
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise IntentError(
                "E_FORMAT_CORRUPT",
                "v3 JSON 无法解析",
                stage="read",
                line_no=exc.lineno,
                field="json",
                field_offset=exc.pos,
            ) from exc
        if not isinstance(payload, dict):
            raise IntentError(
                "E_FORMAT_CORRUPT",
                "v3 配置根节点必须是对象",
                stage="read",
                field="root",
            )
        version = payload.get("version")
        if version not in SUPPORTED_VERSIONS:
            raise IntentError(
                "E_VERSION_UNSUPPORTED",
                f"不支持的配置版本：{version!r}",
                stage="read",
                version=version,
            )
        return str(version)

    meta, _ = _metadata(text)
    version = meta.get("version")
    if version is not None:
        if version not in SUPPORTED_VERSIONS:
            raise IntentError(
                "E_VERSION_UNSUPPORTED",
                f"不支持的配置版本：{version!r}",
                stage="read",
                version=version,
            )
        return version

    rows = _data_lines(text)
    if rows and all(len(row.split("|")) == 3 for _, row in rows):
        return "v1"
    if rows:
        field_sets: list[set[str]] = []
        for _, row in rows:
            parts = row.split("|")
            keys = {
                part.split("=", 1)[0].strip()
                for part in parts
                if "=" in part
            }
            field_sets.append(keys)
        if field_sets and all(set(_V2_REQUIRED_KEYS) <= keys for keys in field_sets):
            return "v2"
    raise IntentError(
        "E_VERSION_UNSUPPORTED",
        "无法从版本头或字段集判定配置版本",
        stage="read",
        version=None,
    )


def _checksum_text(text: str) -> str:
    kept: list[str] = []
    for line in text.splitlines(keepends=True):
        match = _META_RE.match(line)
        if match and match.group(1) == "checksum":
            continue
        kept.append(line)
    return "sha256:" + hashlib.sha256("".join(kept).encode("utf-8")).hexdigest()


def _checksum_json(payload: dict[str, Any]) -> str:
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _normalize_checksum(value: str) -> str:
    value = value.strip().lower()
    if value.startswith("sha256:"):
        value = value[7:]
    if not re.fullmatch(r"[0-9a-f]{64}", value):
        raise IntentError(
            "E_CONFIG_INVALID",
            "checksum 必须为 sha256 十六进制值",
            stage="read",
            field="checksum",
        )
    return "sha256:" + value


def _snapshot_metadata(
    values: dict[str, str],
    *,
    fallback_required: bool,
) -> tuple[int, frozenset[str], str, str]:
    raw_version = values.get("snapshot_version") or values.get("config_version")
    if raw_version is None:
        raise IntentError(
            "E_CONFIG_INVALID",
            "缺少 snapshot_version",
            stage="read",
            field="snapshot_version",
        )
    try:
        version = int(raw_version)
    except ValueError as exc:
        raise IntentError(
            "E_CONFIG_INVALID",
            "snapshot_version 必须为整数",
            stage="read",
            field="snapshot_version",
            value=raw_version,
        ) from exc
    if version < 1:
        raise IntentError(
            "E_CONFIG_INVALID",
            "snapshot_version 必须大于等于 1",
            stage="read",
            field="snapshot_version",
            value=version,
        )

    raw_labels = values.get("controlled_labels")
    if raw_labels is None:
        raise IntentError(
            "E_CONFIG_INVALID",
            "缺少 controlled_labels",
            stage="read",
            field="controlled_labels",
        )
    labels = [part.strip() for part in raw_labels.split(",") if part.strip()]
    if not labels:
        raise IntentError(
            "E_CONFIG_INVALID",
            "controlled_labels 不能为空",
            stage="read",
            field="controlled_labels",
        )
    if len(labels) != len(set(labels)):
        raise IntentError(
            "E_CONFIG_INVALID",
            "controlled_labels 含重复标签",
            stage="read",
            field="controlled_labels",
        )

    fallback = values.get("fallback_intent", DEFAULT_FALLBACK).strip()
    if not fallback:
        raise IntentError(
            "E_CONFIG_INVALID",
            "fallback_intent 不能为空",
            stage="read",
            field="fallback_intent",
        )
    if fallback_required and "fallback_intent" not in values:
        raise IntentError(
            "E_CONFIG_INVALID",
            "v1 配置缺少全局 fallback_intent",
            stage="read",
            field="fallback_intent",
        )
    if fallback not in labels:
        raise IntentError(
            "E_CONFIG_INVALID",
            "全局 fallback_intent 必须位于 controlled_labels",
            stage="read",
            field="fallback_intent",
            fallback_intent=fallback,
        )

    checksum_raw = values.get("checksum")
    if checksum_raw is None:
        raise IntentError(
            "E_CONFIG_INVALID",
            "缺少 checksum",
            stage="read",
            field="checksum",
        )
    checksum = _normalize_checksum(checksum_raw)
    return version, frozenset(labels), fallback, checksum


def _verify_checksum(
    *,
    provided: str,
    actual: str,
    snapshot_version: int,
    field_offset: int,
) -> None:
    if provided != actual:
        raise IntentError(
            "E_SNAPSHOT_CORRUPT",
            "配置内容校验和不符",
            stage="snapshot",
            version=snapshot_version,
            field="checksum",
            field_offset=field_offset,
            provided=provided,
            actual=actual,
        )


def _parse_keywords(raw: str, line_no: int) -> tuple[str, ...]:
    normalized = raw.replace("，", ",")
    keywords = tuple(part.strip() for part in normalized.split(","))
    if not raw.strip() or any(not keyword for keyword in keywords):
        raise IntentError(
            "E_CONFIG_INVALID",
            "关键词不能为空",
            stage="read",
            line_no=line_no,
            field="keywords",
        )
    if len(keywords) != len(set(keywords)):
        raise IntentError(
            "E_CONFIG_INVALID",
            "同一意图内关键词重复",
            stage="read",
            line_no=line_no,
            field="keywords",
        )
    return keywords


def _parse_threshold(raw: str | float | int, line_no: int) -> float:
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise IntentError(
            "E_CONFIG_INVALID",
            "threshold 必须为数值",
            stage="read",
            line_no=line_no,
            field="threshold",
            value=raw,
        ) from exc
    if not math.isfinite(value) or value < 0.0 or value > 1.0:
        raise IntentError(
            "E_CONFIG_INVALID",
            "threshold 必须位于 [0, 1]",
            stage="read",
            line_no=line_no,
            field="threshold",
            value=value,
        )
    return value


def _register_intent(
    seen: dict[str, int],
    spec: IntentSpec,
    *,
    line_no: int,
) -> None:
    if not spec.intent:
        raise IntentError(
            "E_CONFIG_INVALID",
            "intent 不能为空",
            stage="read",
            line_no=line_no,
            field="intent",
        )
    if spec.intent in seen:
        raise IntentError(
            "E_DUP_INTENT",
            f"意图标签重复：{spec.intent}",
            stage="read",
            line_no=line_no,
            field="intent",
            intent=spec.intent,
            first_line_no=seen[spec.intent],
        )
    seen[spec.intent] = line_no


def _parse_v1(
    text: str,
    *,
    snapshot_version: int,
    controlled_labels: frozenset[str],
    fallback: str,
    checksum: str,
) -> tuple[IntentSpec, ...]:
    specs: list[IntentSpec] = []
    seen: dict[str, int] = {}
    for line_no, row in _data_lines(text):
        fields = row.split("|")
        if len(fields) != 3:
            raise IntentError(
                "E_FORMAT_CORRUPT",
                f"v1 行字段数应为 3，实际为 {len(fields)}",
                stage="read",
                line_no=line_no,
                field="v1_fields",
                expected=3,
                actual=len(fields),
            )
        intent, keyword_text, priority_text = (field.strip() for field in fields)
        try:
            priority = int(priority_text)
        except ValueError as exc:
            raise IntentError(
                "E_CONFIG_INVALID",
                "v1 priority 必须为整数",
                stage="read",
                line_no=line_no,
                field="priority",
                value=priority_text,
            ) from exc
        spec = IntentSpec(
            intent=intent,
            keywords=_parse_keywords(keyword_text, line_no),
            priority=priority,
            line_no=line_no,
        )
        _register_intent(seen, spec, line_no=line_no)
        specs.append(spec)
    if not specs:
        raise IntentError(
            "E_CONFIG_INVALID",
            "配置不包含意图",
            stage="read",
            field="intents",
        )
    return tuple(specs)


def _parse_v2(
    text: str,
    *,
    snapshot_version: int,
    controlled_labels: frozenset[str],
    fallback: str,
    checksum: str,
) -> tuple[IntentSpec, ...]:
    specs: list[IntentSpec] = []
    seen: dict[str, int] = {}
    for line_no, row in _data_lines(text):
        fields: dict[str, str] = {}
        for part in row.split("|"):
            if "=" not in part:
                raise IntentError(
                    "E_FORMAT_CORRUPT",
                    "v2 行内字段缺少键值分隔符",
                    stage="read",
                    line_no=line_no,
                    field="key_value",
                    fragment=part,
                )
            key, value = part.split("=", 1)
            key = key.strip()
            value = value.strip()
            if not key:
                raise IntentError(
                    "E_FORMAT_CORRUPT",
                    "v2 字段名为空",
                    stage="read",
                    line_no=line_no,
                    field="key",
                )
            if key in fields:
                raise IntentError(
                    "E_FORMAT_CORRUPT",
                    "v2 行内字段名重复",
                    stage="read",
                    line_no=line_no,
                    field=key,
                )
            fields[key] = value
        missing = [key for key in _V2_REQUIRED_KEYS if key not in fields]
        if missing:
            raise IntentError(
                "E_FORMAT_CORRUPT",
                f"v2 行缺少键：{','.join(missing)}",
                stage="read",
                line_no=line_no,
                field=", ".join(missing),
                missing_keys=missing,
            )
        unexpected = sorted(set(fields) - set(_V2_REQUIRED_KEYS))
        if unexpected:
            raise IntentError(
                "E_FORMAT_CORRUPT",
                f"v2 行包含未知键：{','.join(unexpected)}",
                stage="read",
                line_no=line_no,
                field=", ".join(unexpected),
                unexpected_keys=unexpected,
            )

        intent = fields["intent"].strip()
        keywords = _parse_keywords(fields["keywords"], line_no)
        threshold = _parse_threshold(fields["threshold"], line_no)
        fallback_intent = fields["fallback_intent"].strip()
        if not fallback_intent:
            raise IntentError(
                "E_FORMAT_CORRUPT",
                "v2 fallback_intent 不能为空",
                stage="read",
                line_no=line_no,
                field="fallback_intent",
            )
        if fallback_intent not in controlled_labels:
            raise IntentError(
                "E_CONFIG_INVALID",
                "意图 fallback_intent 必须位于 controlled_labels",
                stage="read",
                line_no=line_no,
                field="fallback_intent",
                intent=intent,
                fallback_intent=fallback_intent,
            )
        spec = IntentSpec(
            intent=intent,
            keywords=keywords,
            threshold=threshold,
            fallback_intent=fallback_intent,
            line_no=line_no,
        )
        _register_intent(seen, spec, line_no=line_no)
        specs.append(spec)
    if not specs:
        raise IntentError(
            "E_CONFIG_INVALID",
            "配置不包含意图",
            stage="read",
            field="intents",
        )
    return tuple(specs)


def _json_intent_line_numbers(text: str) -> list[int]:
    return [
        line_no
        for line_no, line in enumerate(text.splitlines(), start=1)
        if _JSON_INTENT_LINE_RE.search(line)
    ]


def _require_mapping(
    value: Any,
    *,
    field: str,
    line_no: int,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise IntentError(
            "E_FORMAT_CORRUPT",
            f"{field} 必须是对象",
            stage="read",
            line_no=line_no,
            field=field,
        )
    return value


def _v3_label_path(value: Any, *, line_no: int) -> tuple[str, ...]:
    if (
        not isinstance(value, list)
        or not value
        or any(not isinstance(part, str) for part in value)
    ):
        raise IntentError(
            "E_FORMAT_CORRUPT",
            "label_path 必须是非空字符串数组",
            stage="read",
            line_no=line_no,
            field="label_path",
        )
    path = tuple(str(part).strip() for part in value)
    if any(not part for part in path):
        raise IntentError(
            "E_FORMAT_CORRUPT",
            "label_path 不能包含空片段",
            stage="read",
            line_no=line_no,
            field="label_path",
        )
    return path


def _v3_slots(value: Any, *, line_no: int) -> tuple[SlotSpec, ...]:
    if not isinstance(value, list):
        raise IntentError(
            "E_FORMAT_CORRUPT",
            "slots 必须是数组",
            stage="read",
            line_no=line_no,
            field="slots",
        )
    slots: list[SlotSpec] = []
    names: set[str] = set()
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise IntentError(
                "E_FORMAT_CORRUPT",
                "slot 必须是对象",
                stage="read",
                line_no=line_no,
                field=f"slots[{index}]",
            )
        raw_name = item.get("name")
        raw_placeholder = item.get("placeholder")
        if not isinstance(raw_name, str) or not isinstance(raw_placeholder, str):
            raise IntentError(
                "E_CONFIG_INVALID",
                "slot name 与 placeholder 必须为字符串",
                stage="read",
                line_no=line_no,
                field=f"slots[{index}]",
            )
        name = raw_name.strip()
        placeholder = raw_placeholder.strip()
        if not name or not placeholder:
            raise IntentError(
                "E_FORMAT_CORRUPT",
                "slot 必须包含非空 name 与 placeholder",
                stage="read",
                line_no=line_no,
                field=f"slots[{index}]",
            )
        if name in names:
            raise IntentError(
                "E_CONFIG_INVALID",
                "slot name 重复",
                stage="read",
                line_no=line_no,
                field="slots",
                slot=name,
            )
        names.add(name)
        slots.append(SlotSpec(name=name, placeholder=placeholder))
    return tuple(slots)


def _v3_weights(value: Any, *, keyword_count: int, line_no: int) -> tuple[float, ...]:
    if not isinstance(value, list):
        raise IntentError(
            "E_FORMAT_CORRUPT",
            "weight_vector 必须是数组",
            stage="read",
            line_no=line_no,
            field="weight_vector",
        )
    if len(value) != keyword_count:
        raise IntentError(
            "E_CONFIG_INVALID",
            "weight_vector 长度必须等于关键词数量",
            stage="read",
            line_no=line_no,
            field="weight_vector",
            expected=keyword_count,
            actual=len(value),
        )
    weights: list[float] = []
    for index, raw in enumerate(value):
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            raise IntentError(
                "E_CONFIG_INVALID",
                "weight_vector 必须只包含数值",
                stage="read",
                line_no=line_no,
                field=f"weight_vector[{index}]",
            )
        weight = float(raw)
        if not math.isfinite(weight) or weight < 0.0:
            raise IntentError(
                "E_CONFIG_INVALID",
                "weight_vector 权重必须为非负有限数",
                stage="read",
                line_no=line_no,
                field=f"weight_vector[{index}]",
                value=weight,
            )
        weights.append(weight)
    return tuple(weights)


def _parse_v3(
    text: str,
    payload: dict[str, Any],
    *,
    snapshot_version: int,
    controlled_labels: frozenset[str],
    fallback: str,
    checksum: str,
) -> tuple[IntentSpec, ...]:
    intents = payload.get("intents")
    if not isinstance(intents, list):
        raise IntentError(
            "E_FORMAT_CORRUPT",
            "v3 intents 必须是数组",
            stage="read",
            field="intents",
        )
    line_numbers = _json_intent_line_numbers(text)
    specs: list[IntentSpec] = []
    seen: dict[str, int] = {}
    for index, raw_item in enumerate(intents):
        line_no = line_numbers[index] if index < len(line_numbers) else index + 1
        item = _require_mapping(raw_item, field=f"intents[{index}]", line_no=line_no)
        missing = [key for key in _V3_INTENT_KEYS if key not in item]
        if missing:
            raise IntentError(
                "E_FORMAT_CORRUPT",
                f"v3 意图缺少键：{','.join(missing)}",
                stage="read",
                line_no=line_no,
                field=", ".join(missing),
                missing_keys=missing,
            )
        if not isinstance(item["intent"], str):
            raise IntentError(
                "E_CONFIG_INVALID",
                "v3 intent 必须为字符串",
                stage="read",
                line_no=line_no,
                field="intent",
            )
        intent = item["intent"].strip()
        raw_keywords = item["keywords"]
        if not isinstance(raw_keywords, list):
            raise IntentError(
                "E_FORMAT_CORRUPT",
                "v3 keywords 必须是数组",
                stage="read",
                line_no=line_no,
                field="keywords",
            )
        if any(not isinstance(keyword, str) for keyword in raw_keywords):
            raise IntentError(
                "E_CONFIG_INVALID",
                "v3 keywords 只能包含字符串",
                stage="read",
                line_no=line_no,
                field="keywords",
            )
        keyword_text = ",".join(str(keyword) for keyword in raw_keywords)
        keywords = _parse_keywords(keyword_text, line_no)
        threshold = _parse_threshold(item["threshold"], line_no)
        if not isinstance(item["fallback_intent"], str):
            raise IntentError(
                "E_CONFIG_INVALID",
                "v3 fallback_intent 必须为字符串",
                stage="read",
                line_no=line_no,
                field="fallback_intent",
            )
        fallback_intent = item["fallback_intent"].strip()
        if not fallback_intent:
            raise IntentError(
                "E_FORMAT_CORRUPT",
                "v3 fallback_intent 不能为空",
                stage="read",
                line_no=line_no,
                field="fallback_intent",
            )
        if fallback_intent not in controlled_labels:
            raise IntentError(
                "E_CONFIG_INVALID",
                "意图 fallback_intent 必须位于 controlled_labels",
                stage="read",
                line_no=line_no,
                field="fallback_intent",
                intent=intent,
                fallback_intent=fallback_intent,
            )
        label_path = _v3_label_path(item["label_path"], line_no=line_no)
        slots = _v3_slots(item["slots"], line_no=line_no)
        weights = _v3_weights(
            item["weight_vector"],
            keyword_count=len(keywords),
            line_no=line_no,
        )
        spec = IntentSpec(
            intent=intent,
            keywords=keywords,
            threshold=threshold,
            fallback_intent=fallback_intent,
            label_path=label_path,
            slots=slots,
            weight_vector=weights,
            line_no=line_no,
        )
        _register_intent(seen, spec, line_no=line_no)
        specs.append(spec)
    if not specs:
        raise IntentError(
            "E_CONFIG_INVALID",
            "配置不包含意图",
            stage="read",
            field="intents",
        )
    return tuple(specs)


def parse_config_text(
    text: str,
    *,
    source_path: str | None = None,
    verify_checksum: bool = True,
    injector: Any | None = None,
) -> ConfigDocument:
    """Parse one config string and dispatch by version header/field set."""

    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    version = detect_version(normalized)
    path = source_path or "<memory>"

    if injector is not None:
        injector.fire(
            "read",
            {
                "path": path,
                "line_no": _first_data_line(normalized),
                "checksum": None,
            },
        )

    if version == "v3":
        payload = json.loads(normalized)
        raw_labels = payload.get("controlled_labels")
        if (
            not isinstance(raw_labels, list)
            or not raw_labels
            or any(not isinstance(label, str) for label in raw_labels)
        ):
            raise IntentError(
                "E_CONFIG_INVALID",
                "controlled_labels 必须是非空字符串数组",
                stage="read",
                field="controlled_labels",
            )
        if not isinstance(payload.get("fallback_intent"), str):
            raise IntentError(
                "E_CONFIG_INVALID",
                "fallback_intent 必须为字符串",
                stage="read",
                field="fallback_intent",
            )
        values = {
            key: (
                ",".join(str(part) for part in value)
                if isinstance(value, list)
                else str(value)
            )
            for key, value in payload.items()
            if key != "intents"
        }
        values["controlled_labels"] = ",".join(raw_labels)
        snapshot_version, controlled_labels, fallback, checksum = _snapshot_metadata(
            values,
            fallback_required=True,
        )
        if len(controlled_labels) != len(raw_labels or []):
            raise IntentError(
                "E_CONFIG_INVALID",
                "controlled_labels 含重复或空标签",
                stage="read",
                field="controlled_labels",
            )
        payload_without_checksum = dict(payload)
        provided_raw = payload_without_checksum.pop("checksum", None)
        if provided_raw is None:
            raise IntentError(
                "E_CONFIG_INVALID",
                "缺少 checksum",
                stage="read",
                field="checksum",
            )
        provided = _normalize_checksum(str(provided_raw))
        actual = _checksum_json(payload_without_checksum)
        if verify_checksum:
            offset = normalized.encode("utf-8").find(b'"checksum"')
            _verify_checksum(
                provided=provided,
                actual=actual,
                snapshot_version=snapshot_version,
                field_offset=max(offset, 0),
            )
        specs = _parse_v3(
            normalized,
            payload,
            snapshot_version=snapshot_version,
            controlled_labels=controlled_labels,
            fallback=fallback,
            checksum=provided,
        )
        return ConfigDocument(
            source_version="v3",
            snapshot_version=snapshot_version,
            controlled_labels=controlled_labels,
            global_fallback=fallback,
            intents=specs,
            checksum=provided,
            source_path=source_path,
            metadata=values,
            raw_text=normalized,
        )

    meta, positions = _metadata(normalized)
    snapshot_version, controlled_labels, fallback, checksum = _snapshot_metadata(
        meta,
        fallback_required=(version == "v1"),
    )
    if verify_checksum:
        actual = _checksum_text(normalized)
        checksum_position = positions.get("checksum", (0, 0))[1]
        _verify_checksum(
            provided=checksum,
            actual=actual,
            snapshot_version=snapshot_version,
            field_offset=checksum_position,
        )
    parser = _parse_v1 if version == "v1" else _parse_v2
    specs = parser(
        normalized,
        snapshot_version=snapshot_version,
        controlled_labels=controlled_labels,
        fallback=fallback,
        checksum=checksum,
    )
    return ConfigDocument(
        source_version=version,
        snapshot_version=snapshot_version,
        controlled_labels=controlled_labels,
        global_fallback=fallback,
        intents=specs,
        checksum=checksum,
        source_path=source_path,
        metadata=meta,
        raw_text=normalized,
    )


def read_config_document(
    path: str | Path,
    *,
    verify_checksum: bool = True,
    injector: Any | None = None,
) -> ConfigDocument:
    raw = _read_bytes(path)
    text = _decode_utf8(raw, str(path))
    document = parse_config_text(
        text,
        source_path=str(path),
        verify_checksum=verify_checksum,
        injector=injector,
    )
    return document


def sha256_file(path: str | Path) -> str:
    try:
        digest = hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except OSError as exc:
        raise IntentError(
            "E_CONFIG_INVALID",
            f"无法计算文件校验和：{path}",
            stage="input",
            field="path",
            path=str(path),
        ) from exc
    return "sha256:" + digest
