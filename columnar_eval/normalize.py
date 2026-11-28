from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any

from .model import (
    ColumnVector,
    DType,
    EvalBatch,
    mask_to_bitstring,
)


UNIFIED_DECIMAL_SCALE = 3


def _parse_decimal_scale(payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    scale_payload = payload.get("scale")
    if isinstance(scale_payload, int):
        return scale_payload, {"mode": "integer", "value": scale_payload}
    if not isinstance(scale_payload, dict):
        raise ValueError(f"invalid decimal scale payload: {scale_payload!r}")
    mode = scale_payload.get("mode")
    if mode == "fractional_digits":
        scale = int(scale_payload["digits"])
    elif mode == "power_of_ten_multiplier":
        multiplier = int(scale_payload["multiplier"])
        if multiplier <= 0 or multiplier % 10:
            raise ValueError(f"invalid power-of-ten multiplier: {multiplier}")
        scale = 0
        power = 1
        while power < multiplier:
            power *= 10
            scale += 1
        if power != multiplier:
            raise ValueError(f"multiplier is not an exact power of ten: {multiplier}")
    elif mode == "negative_exponent":
        exponent = int(scale_payload["exponent"])
        scale = -exponent
        if scale < 0:
            raise ValueError(f"negative exponent must be <= 0, got {exponent}")
    else:
        raise ValueError(f"unsupported decimal scale mode: {mode}")
    if scale < 0:
        raise ValueError(f"decimal scale must be non-negative, got {scale}")
    return scale, copy.deepcopy(scale_payload)


def _parse_column_dtype(payload: dict[str, Any]) -> tuple[DType, dict[str, Any]]:
    kind = payload["kind"]
    if kind == "decimal":
        scale, encoding = _parse_decimal_scale(payload)
        return DType("decimal", scale), {"scale": encoding}
    if kind in {"int64", "string", "bool"}:
        return DType(kind), {}
    raise ValueError(f"unsupported schema type: {kind}")


def _decode_bit_segment(segment: str, length: int, bit_order: str) -> list[bool]:
    if len(segment) < length:
        raise ValueError(f"bit segment has {len(segment)} bits, expected {length}")
    segment = segment[:length]
    if bit_order == "msb_first":
        return [bit == "1" for bit in segment]
    if bit_order == "lsb_first":
        return [bit == "1" for bit in reversed(segment)]
    raise ValueError(f"unsupported bit order: {bit_order}")


def _decode_global_bit_cursor(
    codec: dict[str, Any], fields: list[str], row_count: int
) -> dict[str, list[bool]]:
    offsets = codec["offsets"]
    lengths = codec.get("lengths", [row_count] * len(fields))
    bits = codec["concat_bits"]
    if len(offsets) != len(fields) or len(lengths) != len(fields):
        raise ValueError("global cursor offsets/lengths do not match column count")
    masks: dict[str, list[bool]] = {}
    bit_order = codec.get("bit_order", "msb_first")
    for field, offset, length in zip(fields, offsets, lengths):
        segments = bits[offset : offset + length]
        masks[field] = _decode_bit_segment(segments, length, bit_order)
    return masks


def _decode_global_hex_cursor(
    codec: dict[str, Any], fields: list[str], row_count: int
) -> dict[str, list[bool]]:
    offsets = codec["offsets"]
    lengths_bytes = codec["lengths_bytes"]
    raw = bytes.fromhex(codec["concat_hex"])
    if len(offsets) != len(fields) or len(lengths_bytes) != len(fields):
        raise ValueError("global hex cursor offsets/lengths do not match column count")
    masks: dict[str, list[bool]] = {}
    bit_order = codec.get("bit_order", "lsb_first")
    for field, offset, length_bytes in zip(fields, offsets, lengths_bytes):
        segment = raw[offset : offset + length_bytes]
        if bit_order == "lsb_first":
            mask = [(segment[idx // 8] >> (idx % 8)) & 1 == 1 for idx in range(row_count)]
        elif bit_order == "msb_first":
            mask = [(segment[idx // 8] >> (7 - (idx % 8))) & 1 == 1 for idx in range(row_count)]
        else:
            raise ValueError(f"unsupported bit order: {bit_order}")
        masks[field] = mask
    return masks


def _decode_nulls(
    schema: dict[str, Any],
    columns: list[dict[str, Any]],
    fields: list[str],
    row_count: int,
) -> tuple[dict[str, list[bool]], dict[str, Any]]:
    codec = schema["null_codec"]
    kind = codec["kind"]
    if kind == "per_column_bitstring":
        bit_order = codec.get("bit_order", "msb_first")
        masks: dict[str, list[bool]] = {}
        for column, field in zip(columns, fields):
            bitmap = column.get("null_bitmap")
            if bitmap is None:
                bitmap = codec.get("column_bitmaps", {}).get(field)
            if bitmap is None:
                raise ValueError(f"missing null bitmap for column {field}")
            masks[field] = _decode_bit_segment(bitmap, row_count, bit_order)
        return masks, {
            "kind": "per_column_bitstring",
            "bit_order": bit_order,
            "null_bit": codec.get("null_bit", "1"),
        }
    if kind == "global_cursor":
        storage = codec.get("storage", "bit_string")
        if storage == "bit_string":
            masks = _decode_global_bit_cursor(codec, fields, row_count)
        elif storage == "hex_bytes":
            masks = _decode_global_hex_cursor(codec, fields, row_count)
        else:
            raise ValueError(f"unsupported global cursor storage: {storage}")
        return masks, {
            "kind": "global_cursor",
            "storage": storage,
            "bit_order": codec.get("bit_order"),
            "null_bit": codec.get("null_bit", "1"),
            "offsets": copy.deepcopy(codec.get("offsets")),
        }
    raise ValueError(f"unsupported null codec: {kind}")


def _rescale_raw(raw: int, source_scale: int, target_scale: int) -> int:
    if source_scale == target_scale:
        return raw
    if target_scale > source_scale:
        return raw * 10 ** (target_scale - source_scale)
    divisor = 10 ** (source_scale - target_scale)
    if divisor and raw % divisor:
        raise ValueError(
            f"raw value {raw} at scale {source_scale} cannot align exactly to scale {target_scale}"
        )
    return raw // divisor


def _normalize_expression_node(
    node: dict[str, Any],
    original_to_canonical: dict[str, str],
    rewrite_log: list[dict[str, str]],
) -> dict[str, Any]:
    normalized = copy.deepcopy(node)
    if normalized["op"] == "column":
        original = normalized["field"]
        if original not in original_to_canonical:
            raise ValueError(f"expression references unknown field: {original}")
        canonical = original_to_canonical[original]
        normalized["field"] = canonical
        rewrite_log.append(
            {
                "node_id": normalized["node_id"],
                "original_field": original,
                "canonical_field": canonical,
            }
        )
    for idx, arg in enumerate(normalized.get("args", [])):
        normalized["args"][idx] = _normalize_expression_node(
            arg, original_to_canonical, rewrite_log
        )
    return normalized


@dataclass
class NormalizedModel:
    version: str
    schema_version: str
    table: str
    row_count: int
    columns: dict[str, ColumnVector]
    original_to_canonical: dict[str, str]
    canonical_to_original: dict[str, str]
    scale_alignments: list[dict[str, Any]]
    normalized_null_codec: dict[str, Any]
    expressions: list[dict[str, Any]]
    expression_rewrites: list[dict[str, Any]]
    version_manifest: dict[str, Any]
    source_schema: dict[str, Any] = field(repr=False)

    def column_width(self) -> int:
        return len(self.columns)

    def batch(self, *, batch_index: int, row_start: int, row_count: int) -> EvalBatch:
        if row_start < 0 or row_count < 0 or row_start + row_count > self.row_count:
            raise ValueError(
                f"batch range [{row_start}, {row_start + row_count}) "
                f"outside row_count={self.row_count}"
            )
        sliced = {
            name: ColumnVector(
                dtype=vector.dtype,
                values=vector.values[row_start : row_start + row_count],
                null_mask=vector.null_mask[row_start : row_start + row_count],
                source_field=vector.source_field,
            )
            for name, vector in self.columns.items()
        }
        return EvalBatch(
            batch_index=batch_index,
            row_start=row_start,
            row_count=row_count,
            columns=sliced,
        )

    def canonical_signature(self) -> dict[str, Any]:
        return {
            "row_count": self.row_count,
            "columns": {
                name: {
                    "dtype": vector.dtype.to_json(),
                    "raw_values": list(vector.values),
                    "null_mask": list(vector.null_mask),
                    "null_bitmap": mask_to_bitstring(vector.null_mask, "msb_first"),
                }
                for name, vector in self.columns.items()
            },
        }

    def to_json(self, include_schema_data: bool = True) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "version": self.version,
            "schema_version": self.schema_version,
            "table": self.table,
            "row_count": self.row_count,
            "original_to_canonical": copy.deepcopy(self.original_to_canonical),
            "canonical_to_original": copy.deepcopy(self.canonical_to_original),
            "scale_alignments": copy.deepcopy(self.scale_alignments),
            "normalized_null_codec": copy.deepcopy(self.normalized_null_codec),
            "columns": {
                name: vector.to_json(include_display=False)
                for name, vector in self.columns.items()
            },
            "expressions": copy.deepcopy(self.expressions),
            "expression_field_rewrites": copy.deepcopy(self.expression_rewrites),
            "version_manifest": copy.deepcopy(self.version_manifest),
        }
        if include_schema_data:
            payload["source_schema"] = copy.deepcopy(self.source_schema)
        return payload


def normalize_schema_version(
    schema: dict[str, Any],
    expressions: list[dict[str, Any]],
    *,
    unified_decimal_scale: int = UNIFIED_DECIMAL_SCALE,
) -> NormalizedModel:
    version = schema["schema_version"]
    row_count = int(schema["row_count"])
    columns_payload = schema["columns"]
    logical_field_names = [column["logical_name"] for column in columns_payload]
    names = [column["name"] for column in columns_payload]
    if len(set(names)) != len(names):
        raise ValueError("schema contains duplicate physical field names")
    if len(set(logical_field_names)) != len(logical_field_names):
        raise ValueError("schema contains duplicate logical field names")

    masks, source_null_codec = _decode_nulls(
        schema, columns_payload, logical_field_names, row_count
    )

    original_to_canonical = {
        column["name"]: column["logical_name"] for column in columns_payload
    }
    canonical_to_original = {value: key for key, value in original_to_canonical.items()}

    parsed_columns: list[dict[str, Any]] = []
    observed_scales: list[int] = []
    for column in columns_payload:
        dtype, scale_encoding = _parse_column_dtype(column["type"])
        if dtype.kind == "decimal":
            observed_scales.append(int(dtype.scale or 0))
        parsed_columns.append(
            {
                "physical_name": column["name"],
                "canonical_name": column["logical_name"],
                "dtype": dtype,
                "scale_encoding": scale_encoding,
                "data": list(column["data"]),
            }
        )

    target_scale = max([unified_decimal_scale, *observed_scales])
    normalized_columns: dict[str, ColumnVector] = {}
    scale_alignments: list[dict[str, Any]] = []
    for parsed in parsed_columns:
        dtype: DType = parsed["dtype"]
        canonical_name = parsed["canonical_name"]
        if len(parsed["data"]) != row_count:
            raise ValueError(
                f"column {parsed['physical_name']} has {len(parsed['data'])} values, "
                f"expected {row_count}"
            )
        masks_for_column = masks[canonical_name]
        if len(masks_for_column) != row_count:
            raise ValueError(
                f"column {parsed['physical_name']} null mask has {len(masks_for_column)} "
                f"bits, expected {row_count}"
            )
        target_dtype = dtype
        if dtype.kind == "decimal":
            source_scale = int(dtype.scale or 0)
            target_dtype = DType("decimal", target_scale)
            factor = 10 ** (target_scale - source_scale)
            scale_alignments.append(
                {
                    "physical_name": parsed["physical_name"],
                    "canonical_name": canonical_name,
                    "source_scale": source_scale,
                    "target_scale": target_scale,
                    "multiplier": 10 ** max(target_scale - source_scale, 0),
                    "divisor": 10 ** max(source_scale - target_scale, 0),
                    "source_scale_encoding": parsed["scale_encoding"],
                    "rule": "exact integer rescaling; no rounding or truncation",
                    "factor_check": factor,
                }
            )
        values: list[Any] = []
        for raw, is_null in zip(parsed["data"], masks_for_column):
            if is_null:
                values.append(None)
                continue
            if dtype.kind == "decimal":
                values.append(
                    _rescale_raw(int(raw), int(dtype.scale or 0), int(target_dtype.scale or 0))
                )
            elif dtype.kind == "int64":
                values.append(int(raw))
            elif dtype.kind == "bool":
                values.append(bool(raw))
            elif dtype.kind == "string":
                values.append(str(raw))
            else:
                raise ValueError(f"unsupported normalized type: {dtype.kind}")
        normalized_columns[canonical_name] = ColumnVector(
            dtype=target_dtype,
            values=values,
            null_mask=list(masks_for_column),
            source_field=parsed["physical_name"],
        )

    expression_rewrites: list[dict[str, Any]] = []
    normalized_expressions: list[dict[str, Any]] = []
    for expression in expressions:
        rewrite_log: list[dict[str, str]] = []
        root = _normalize_expression_node(
            expression["root"], original_to_canonical, rewrite_log
        )
        normalized_expression = {
            "expression_id": expression["expression_id"],
            "purpose": expression.get("purpose", ""),
            "root": root,
        }
        expression_rewrites.append(
            {
                "expression_id": expression["expression_id"],
                "assignments": rewrite_log,
            }
        )
        normalized_expressions.append(normalized_expression)

    null_bitmaps = {
        name: mask_to_bitstring(vector.null_mask, "msb_first")
        for name, vector in normalized_columns.items()
    }
    normalized_null_codec = {
        "kind": "explicit_per_column_bitmap",
        "bit_order": "msb_first",
        "null_bit": 1,
        "non_null_bit": 0,
        "column_bitmaps": null_bitmaps,
    }
    version_manifest = {
        "version": version,
        "schema_version": schema["schema_version"],
        "source_system": schema["source_system"],
        "field_names": {
            "rule": schema["name_rule"],
            "original_to_canonical": copy.deepcopy(original_to_canonical),
        },
        "decimal_scales": {
            "encoding_rule": schema["physical_scale_encoding"],
            "physical_scale": schema["physical_decimal_scale"],
            "unified_scale": target_scale,
            "alignments": copy.deepcopy(scale_alignments),
        },
        "null_bitmap": {
            "source_codec": copy.deepcopy(source_null_codec),
            "normalized_codec": "explicit per-column mask, 1=null, msb_first",
        },
        "expressions": copy.deepcopy(expression_rewrites),
        "normalization_invariants": {
            "row_count": row_count,
            "decimal_alignment_is_exact": True,
            "canonical_columns": list(normalized_columns),
        },
    }
    return NormalizedModel(
        version=version,
        schema_version=schema["schema_version"],
        table=schema["table"],
        row_count=row_count,
        columns=normalized_columns,
        original_to_canonical=original_to_canonical,
        canonical_to_original=canonical_to_original,
        scale_alignments=scale_alignments,
        normalized_null_codec=normalized_null_codec,
        expressions=normalized_expressions,
        expression_rewrites=expression_rewrites,
        version_manifest=version_manifest,
        source_schema=schema,
    )


def normalize_all(
    sample: dict[str, Any] | Any,
    *,
    unified_decimal_scale: int = UNIFIED_DECIMAL_SCALE,
) -> dict[str, NormalizedModel]:
    if hasattr(sample, "schemas"):
        schemas = sample.schemas
        expressions = sample.expressions
    else:
        schemas = sample["schemas"]
        expressions = sample["expressions"]
    return {
        version: normalize_schema_version(
            schemas[version],
            expressions[version],
            unified_decimal_scale=unified_decimal_scale,
        )
        for version in ("v1", "v2", "v3")
    }


def build_version_difference_manifest(models: dict[str, NormalizedModel]) -> dict[str, Any]:
    versions = sorted(models)
    differences: list[dict[str, Any]] = []

    aspects = [
        (
            "field_naming",
            lambda model: {
                "rule": model.version_manifest["field_names"]["rule"],
                "mapping": model.original_to_canonical,
            },
        ),
        (
            "decimal_scale_representation",
            lambda model: {
                "rule": model.version_manifest["decimal_scales"]["encoding_rule"],
                "physical_scale": model.version_manifest["decimal_scales"]["physical_scale"],
                "alignments": model.scale_alignments,
            },
        ),
        (
            "null_bitmap_encoding",
            lambda model: {
                "source": model.version_manifest["null_bitmap"]["source_codec"],
                "normalized": model.version_manifest["null_bitmap"]["normalized_codec"],
            },
        ),
    ]
    for aspect, extractor in aspects:
        values = {version: extractor(models[version]) for version in versions}
        differences.append(
            {
                "aspect": aspect,
                "distinct": len({repr(value) for value in values.values()}) > 1,
                "versions": values,
            }
        )

    canonical_columns = {
        version: sorted(models[version].columns) for version in versions
    }
    differences.append(
        {
            "aspect": "canonical_column_set",
            "distinct": len({tuple(value) for value in canonical_columns.values()}) > 1,
            "versions": canonical_columns,
        }
    )
    expected_aspects = {
        "field_naming",
        "decimal_scale_representation",
        "null_bitmap_encoding",
    }
    changed_aspects = {
        entry["aspect"] for entry in differences if entry["distinct"]
    }
    unexpected_changes = sorted(changed_aspects - expected_aspects)
    return {
        "versions": versions,
        "expected_difference_aspects": sorted(expected_aspects),
        "unexpected_changes": unexpected_changes,
        "differences": differences,
        "statement": (
            "版本差异仅允许出现在字段命名、定点刻度表示与空值位图编码；"
            "归一后的逻辑列、类型、值与空值掩码必须一致。"
        ),
    }
