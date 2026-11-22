from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable

from .errors import BusinessError, E_MANIFEST_INVALID
from .serialization import canonical_json_bytes, content_checksum


REQUIRED_FIELDS = (
    "model_tag",
    "layer_id",
    "layer_index",
    "layer_shape",
    "weight_revision",
    "quant_dtype",
    "scale_hint",
    "zero_point",
    "calib_seed",
    "block_policy",
)


def _manifest_error(message: str, **details: Any) -> BusinessError:
    return BusinessError(
        E_MANIFEST_INVALID,
        message,
        stage="model_validation",
        layer_id=details.get("layer_id"),
        layer_index=details.get("layer_index"),
        details=details,
    )


def validate_manifest(entries: list[dict[str, Any]]) -> None:
    if not entries:
        raise _manifest_error("模型包清单为空")
    seen_ids: set[str] = set()
    seen_indices: set[int] = set()
    previous_output: int | None = None
    model_tag = entries[0].get("model_tag")
    for position, entry in enumerate(entries):
        missing = [field for field in REQUIRED_FIELDS if field not in entry]
        if missing:
            raise _manifest_error(
                f"第 {position + 1} 行缺少字段: {', '.join(missing)}",
                line=position + 1,
                missing=missing,
            )
        layer_id = entry["layer_id"]
        if not isinstance(layer_id, str) or not layer_id:
            raise _manifest_error("layer_id 必须为非空字符串", line=position + 1)
        index = entry["layer_index"]
        if isinstance(index, bool) or not isinstance(index, int) or index < 0:
            raise _manifest_error(
                "layer_index 必须为非负整数",
                layer_id=layer_id,
                line=position + 1,
            )
        if layer_id in seen_ids:
            raise _manifest_error("layer_id 重复", layer_id=layer_id)
        if index in seen_indices:
            raise _manifest_error("layer_index 重复", layer_id=layer_id, layer_index=index)
        seen_ids.add(layer_id)
        seen_indices.add(index)
        shape = entry["layer_shape"]
        if (
            not isinstance(shape, list)
            or len(shape) != 2
            or any(isinstance(v, bool) or not isinstance(v, int) or v < 1 for v in shape)
        ):
            raise _manifest_error(
                "layer_shape 必须为两个正整数",
                layer_id=layer_id,
                layer_index=index,
            )
        if previous_output is not None and shape[1] != previous_output:
            raise _manifest_error(
                "层输入维度必须等于上一层输出维度",
                layer_id=layer_id,
                layer_index=index,
                expected_input=previous_output,
                actual_input=shape[1],
            )
        previous_output = shape[0]
        if entry["model_tag"] != model_tag:
            raise _manifest_error(
                "同一清单的 model_tag 必须一致",
                layer_id=layer_id,
                layer_index=index,
            )
        revision = entry["weight_revision"]
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
            raise _manifest_error(
                "weight_revision 必须为正整数",
                layer_id=layer_id,
                layer_index=index,
            )
        if not isinstance(entry["quant_dtype"], str) or not entry["quant_dtype"]:
            raise _manifest_error(
                "quant_dtype 必须为非空字符串",
                layer_id=layer_id,
                layer_index=index,
            )
        hint = entry["scale_hint"]
        if isinstance(hint, bool) or not isinstance(hint, (int, float)):
            raise _manifest_error(
                "scale_hint 必须为数值",
                layer_id=layer_id,
                layer_index=index,
            )
        zero_point = entry["zero_point"]
        if isinstance(zero_point, bool) or not isinstance(zero_point, int):
            raise _manifest_error(
                "zero_point 必须为整数",
                layer_id=layer_id,
                layer_index=index,
            )
        calib_seed = entry["calib_seed"]
        if isinstance(calib_seed, bool) or not isinstance(calib_seed, int):
            raise _manifest_error(
                "calib_seed 必须为整数",
                layer_id=layer_id,
                layer_index=index,
            )
        if not isinstance(entry["block_policy"], dict):
            raise _manifest_error(
                "block_policy 必须为对象",
                layer_id=layer_id,
                layer_index=index,
            )
    indices = sorted(seen_indices)
    if indices != list(range(len(indices))):
        raise _manifest_error(
            "layer_index 必须从 0 连续排列",
            layer_indices=indices,
        )
    if [entry["layer_index"] for entry in entries] != indices:
        raise _manifest_error("清单必须按 layer_index 升序排列")


def parse_manifest_text(text: str) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise _manifest_error(
                f"清单第 {line_number} 行不是合法 JSON",
                line=line_number,
                reason=str(exc),
            ) from exc
        if not isinstance(value, dict):
            raise _manifest_error(
                f"清单第 {line_number} 行必须是对象",
                line=line_number,
            )
        entries.append(value)
    validate_manifest(entries)
    return entries


def parse_manifest_file(path: Path) -> list[dict[str, Any]]:
    try:
        return parse_manifest_text(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise _manifest_error("无法读取模型包清单", path=str(path), reason=str(exc)) from exc


def manifest_bytes(entries: Iterable[dict[str, Any]]) -> bytes:
    lines = [canonical_json_bytes(dict(entry)).decode("utf-8") for entry in entries]
    return ("\n".join(lines) + "\n").encode("utf-8")


def manifest_digest(entries: list[dict[str, Any]]) -> str:
    return content_checksum(entries)


def manifest_by_index(entries: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    return {int(entry["layer_index"]): entry for entry in entries}
