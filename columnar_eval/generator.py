from __future__ import annotations

import copy
import random
from dataclasses import dataclass
from typing import Any

from .model import BOOL, DType, INT64, STRING, mask_to_bitstring


CANONICAL_FIELDS: dict[str, DType] = {
    "district": STRING,
    "population": INT64,
    "avg_speed": DType("decimal", 3),
    "pm25": DType("decimal", 3),
    "is_weekend": BOOL,
    "rainfall": DType("decimal", 3),
}

FIELD_MAPS: dict[str, dict[str, str]] = {
    "v1": {
        "district": "district",
        "population": "residents",
        "avg_speed": "mean_speed",
        "pm25": "pm25_ugm3",
        "is_weekend": "weekend_flag",
        "rainfall": "rain_mm",
    },
    "v2": {
        "district": "STAT_REGION",
        "population": "STAT_POP_TOTAL",
        "avg_speed": "STAT_SPEED_AVG",
        "pm25": "STAT_PM25",
        "is_weekend": "STAT_WEEKEND",
        "rainfall": "STAT_RAIN_MM",
    },
    "v3": {
        "district": "districtName",
        "population": "populationCount",
        "avg_speed": "averageSpeed",
        "pm25": "pm25Level",
        "is_weekend": "weekendFlag",
        "rainfall": "rainfallMm",
    },
}

PHYSICAL_SCALES: dict[str, int] = {"v1": 1, "v2": 2, "v3": 3}


@dataclass
class GeneratedSample:
    seed: int
    row_count: int
    schemas: dict[str, dict[str, Any]]
    expressions: dict[str, list[dict[str, Any]]]
    logical_columns: dict[str, dict[str, Any]]

    def to_json(self) -> dict[str, Any]:
        return {
            "seed": self.seed,
            "row_count": self.row_count,
            "logical_columns": copy.deepcopy(self.logical_columns),
            "schemas": copy.deepcopy(self.schemas),
            "expressions": copy.deepcopy(self.expressions),
        }


def _make_masks(row_count: int, rng: random.Random) -> dict[str, list[bool]]:
    masks = {
        "district": [rng.random() < 0.055 for _ in range(row_count)],
        "population": [rng.random() < 0.075 for _ in range(row_count)],
        "avg_speed": [rng.random() < 0.10 for _ in range(row_count)],
        "pm25": [rng.random() < 0.08 for _ in range(row_count)],
        "is_weekend": [rng.random() < 0.04 for _ in range(row_count)],
        "rainfall": [rng.random() < 0.12 for _ in range(row_count)],
    }
    return masks


def _logical_values(
    row_count: int, rng: random.Random, masks: dict[str, list[bool]]
) -> dict[str, dict[str, Any]]:
    districts = ["滨江", "临湖", "高新", "老城", "北站", "湿地"]
    columns: dict[str, dict[str, Any]] = {}

    district_values = [districts[(idx * 3 + rng.randint(0, 2)) % len(districts)] for idx in range(row_count)]
    population_values = [rng.randint(120_000, 760_000) for _ in range(row_count)]
    speed_values = [rng.randint(12, 74) * 500 for _ in range(row_count)]
    pm25_values = [rng.randint(8, 86) * 1_000 for _ in range(row_count)]
    weekend_values = [((idx // 5) + rng.randint(0, 1)) % 2 == 0 for idx in range(row_count)]
    rainfall_values = [rng.randint(0, 24) * 500 for _ in range(row_count)]

    overrides = {
        5: (False, 12_000, None, 50_000, True, 5_000),
        6: (False, 22_000, 20_000, 10_000, False, None),
        7: (False, 31_000, 20_000, 50_000, True, None),
        8: (False, 18_000, None, 10_000, False, 5_000),
        9: (True, 25_000, 25_000, None, False, 0),
    }
    for idx, (district_null, population, speed, pm25, weekend, rainfall) in overrides.items():
        if 0 <= idx < row_count:
            masks["district"][idx] = district_null
            masks["population"][idx] = population is None
            masks["avg_speed"][idx] = speed is None
            masks["pm25"][idx] = pm25 is None
            masks["is_weekend"][idx] = weekend is None
            masks["rainfall"][idx] = rainfall is None
            if population is not None:
                population_values[idx] = population
            if speed is not None:
                speed_values[idx] = speed
            if pm25 is not None:
                pm25_values[idx] = pm25
            if weekend is not None:
                weekend_values[idx] = weekend
            if rainfall is not None:
                rainfall_values[idx] = rainfall

    columns["district"] = {
        "canonical_name": "district",
        "dtype": STRING.to_json(),
        "values": district_values,
        "null_mask": masks["district"],
    }
    columns["population"] = {
        "canonical_name": "population",
        "dtype": INT64.to_json(),
        "values": population_values,
        "null_mask": masks["population"],
    }
    columns["avg_speed"] = {
        "canonical_name": "avg_speed",
        "dtype": {"kind": "decimal", "scale": 3},
        "values": speed_values,
        "null_mask": masks["avg_speed"],
    }
    columns["pm25"] = {
        "canonical_name": "pm25",
        "dtype": {"kind": "decimal", "scale": 3},
        "values": pm25_values,
        "null_mask": masks["pm25"],
    }
    columns["is_weekend"] = {
        "canonical_name": "is_weekend",
        "dtype": BOOL.to_json(),
        "values": weekend_values,
        "null_mask": masks["is_weekend"],
    }
    columns["rainfall"] = {
        "canonical_name": "rainfall",
        "dtype": {"kind": "decimal", "scale": 3},
        "values": rainfall_values,
        "null_mask": masks["rainfall"],
    }
    return columns


def _rescale_raw(raw: int, source_scale: int, target_scale: int) -> int:
    if source_scale == target_scale:
        return raw
    if target_scale > source_scale:
        return raw * (10 ** (target_scale - source_scale))
    divisor = 10 ** (source_scale - target_scale)
    if divisor and raw % divisor:
        raise ValueError(
            f"cannot exactly rescale raw={raw} from scale {source_scale} to {target_scale}"
        )
    return raw // divisor


def _physical_type_payload(version: str, dtype: DType) -> dict[str, Any]:
    if dtype.kind != "decimal":
        return dtype.to_json()
    scale = PHYSICAL_SCALES[version]
    if version == "v1":
        scale_payload: dict[str, Any] = {
            "mode": "fractional_digits",
            "digits": scale,
        }
    elif version == "v2":
        scale_payload = {
            "mode": "power_of_ten_multiplier",
            "multiplier": 10**scale,
        }
    else:
        scale_payload = {
            "mode": "negative_exponent",
            "exponent": -scale,
        }
    return {"kind": "decimal", "scale": scale_payload}


def _encode_per_column_null(masks: dict[str, list[bool]], fields: list[str]) -> dict[str, Any]:
    return {
        "kind": "per_column_bitstring",
        "bit_order": "msb_first",
        "null_bit": "1",
        "column_bitmaps": {
            field: mask_to_bitstring(masks[field], "msb_first") for field in fields
        },
    }


def _encode_global_bit_cursor(masks: dict[str, list[bool]], fields: list[str], row_count: int) -> dict[str, Any]:
    offsets: list[int] = []
    lengths: list[int] = []
    cursor = 0
    chunks: list[str] = []
    for field in fields:
        offsets.append(cursor)
        lengths.append(row_count)
        bits = mask_to_bitstring(masks[field], "msb_first")
        chunks.append(bits)
        cursor += len(bits)
    return {
        "kind": "global_cursor",
        "storage": "bit_string",
        "bit_order": "msb_first",
        "null_bit": "1",
        "offsets": offsets,
        "lengths": lengths,
        "concat_bits": "".join(chunks),
    }


def _encode_global_hex_cursor(masks: dict[str, list[bool]], fields: list[str], row_count: int) -> dict[str, Any]:
    offsets: list[int] = []
    lengths_bytes: list[int] = []
    byte_chunks: list[bytes] = []
    cursor = 0
    width = (row_count + 7) // 8
    for field in fields:
        mask = masks[field]
        raw = bytearray(width)
        for idx, is_null in enumerate(mask):
            if is_null:
                raw[idx // 8] |= 1 << (idx % 8)
        offsets.append(cursor)
        lengths_bytes.append(width)
        byte_chunks.append(bytes(raw))
        cursor += width
    return {
        "kind": "global_cursor",
        "storage": "hex_bytes",
        "bit_order": "lsb_first",
        "null_bit": "1",
        "offsets": offsets,
        "lengths_bytes": lengths_bytes,
        "concat_hex": b"".join(byte_chunks).hex(),
    }


def _build_schema(
    version: str,
    row_count: int,
    logical_columns: dict[str, dict[str, Any]],
    masks: dict[str, list[bool]],
) -> dict[str, Any]:
    field_map = FIELD_MAPS[version]
    fields = list(FIELD_MAPS[version])
    physical_scale = PHYSICAL_SCALES[version]
    columns: list[dict[str, Any]] = []
    for field in fields:
        logical = logical_columns[field]
        dtype = DType.from_json(logical["dtype"])
        values = copy.deepcopy(logical["values"])
        if dtype.kind == "decimal":
            values = [_rescale_raw(value, int(dtype.scale or 0), physical_scale) for value in values]
        for idx, is_null in enumerate(masks[field]):
            if is_null:
                values[idx] = 0 if dtype.kind == "int64" or dtype.kind == "decimal" else (
                    False if dtype.kind == "bool" else ""
                )
        column: dict[str, Any] = {
            "name": field_map[field],
            "logical_name": field,
            "type": _physical_type_payload(version, dtype),
            "data": values,
        }
        columns.append(column)

    null_payload: dict[str, Any]
    if version == "v1":
        null_payload = _encode_per_column_null(masks, fields)
        for column, field in zip(columns, fields):
            column["null_bitmap"] = null_payload["column_bitmaps"][field]
    elif version == "v2":
        null_payload = _encode_global_bit_cursor(masks, fields, row_count)
    else:
        null_payload = _encode_global_hex_cursor(masks, fields, row_count)

    if version == "v1":
        name_rule = "snake_case_business_aliases"
        physical_scale_encoding = "fractional digit count in each decimal type"
    elif version == "v2":
        name_rule = "uppercase_prefixed_legacy_names"
        physical_scale_encoding = "power-of-ten multiplier in each decimal type"
    else:
        name_rule = "camel_case_catalog_names"
        physical_scale_encoding = "negative decimal exponent in each decimal type"

    return {
        "schema_version": version,
        "source_system": {
            "v1": "department_a_legacy",
            "v2": "department_b_archive",
            "v3": "catalog_current",
        }[version],
        "table": "municipal_metrics",
        "row_count": row_count,
        "name_rule": name_rule,
        "physical_decimal_scale": physical_scale,
        "physical_scale_encoding": physical_scale_encoding,
        "field_map": {field_map[field]: field for field in fields},
        "columns": columns,
        "null_codec": null_payload,
    }


def _column(node_id: str, field: str) -> dict[str, Any]:
    return {"node_id": node_id, "op": "column", "field": field}


def _int_literal(node_id: str, value: int) -> dict[str, Any]:
    return {"node_id": node_id, "op": "literal", "dtype": {"kind": "int64"}, "value": value}


def _decimal_literal(node_id: str, raw: int, scale: int) -> dict[str, Any]:
    return {
        "node_id": node_id,
        "op": "literal",
        "dtype": {"kind": "decimal", "scale": scale},
        "raw": raw,
    }


def _op(node_id: str, op: str, *args: dict[str, Any]) -> dict[str, Any]:
    return {"node_id": node_id, "op": op, "args": list(args)}


def _canonical_expressions() -> list[dict[str, Any]]:
    return [
        {
            "expression_id": "expr01_int_plus_decimal",
            "purpose": "整型与定点小数相加，验证 int64 -> decimal(scale=3) 提升",
            "root": _op(
                "expr01.add",
                "add",
                _column("expr01.population", "population"),
                _column("expr01.avg_speed", "avg_speed"),
            ),
        },
        {
            "expression_id": "expr02_decimal_scale_alignment",
            "purpose": "列刻度 3 与字面量刻度 2 对齐后相加",
            "root": _op(
                "expr02.add",
                "add",
                _column("expr02.rainfall", "rainfall"),
                _decimal_literal("expr02.literal", 125, 2),
            ),
        },
        {
            "expression_id": "expr03_bool_int_compare",
            "purpose": "布尔与整型比较，bool 提升为 int64 后比较",
            "root": _op(
                "expr03.gt",
                "gt",
                _column("expr03.is_weekend", "is_weekend"),
                _int_literal("expr03.literal", 0),
            ),
        },
        {
            "expression_id": "expr04_three_valued_logic",
            "purpose": "AND/OR 三值逻辑组合，覆盖假与空、真与空",
            "root": _op(
                "expr04.and",
                "and",
                _op(
                    "expr04.pm_gt",
                    "gt",
                    _column("expr04.pm25", "pm25"),
                    _decimal_literal("expr04.pm_limit", 3500, 2),
                ),
                _op(
                    "expr04.or",
                    "or",
                    _column("expr04.is_weekend", "is_weekend"),
                    _op(
                        "expr04.rain_gt",
                        "gt",
                        _column("expr04.rainfall", "rainfall"),
                        _decimal_literal("expr04.rain_limit", 0, 1),
                    ),
                ),
            ),
        },
        {
            "expression_id": "expr05_abs_function",
            "purpose": "函数调用 abs 与小数减法，验证整列函数求值",
            "root": _op(
                "expr05.abs",
                "abs",
                _op(
                    "expr05.sub",
                    "sub",
                    _column("expr05.avg_speed", "avg_speed"),
                    _column("expr05.pm25", "pm25"),
                ),
            ),
        },
        {
            "expression_id": "expr06_conditional_branch",
            "purpose": "条件表达式分离真假分支，未选中分支空值不得污染结果",
            "root": _op(
                "expr06.conditional",
                "conditional",
                _op(
                    "expr06.cond",
                    "gt",
                    _column("expr06.pm25", "pm25"),
                    _decimal_literal("expr06.threshold", 3500, 2),
                ),
                _column("expr06.true_value", "avg_speed"),
                _column("expr06.false_value", "rainfall"),
            ),
        },
        {
            "expression_id": "expr07_coalesce_function",
            "purpose": "coalesce 函数按统一 decimal(scale=3) 处理空值",
            "root": _op(
                "expr07.coalesce",
                "coalesce",
                _column("expr07.pm25", "pm25"),
                _decimal_literal("expr07.fallback", 0, 0),
            ),
        },
        {
            "expression_id": "expr08_string_functions",
            "purpose": "lower/length 嵌套函数，验证字符串列整列处理",
            "root": _op(
                "expr08.length",
                "length",
                _op(
                    "expr08.lower",
                    "lower",
                    _column("expr08.district", "district"),
                ),
            ),
        },
        {
            "expression_id": "expr09_decimal_multiply",
            "purpose": "定点小数与整型相乘，小数刻度保持不变",
            "root": _op(
                "expr09.mul",
                "mul",
                _column("expr09.avg_speed", "avg_speed"),
                _int_literal("expr09.factor", 2),
            ),
        },
    ]


def _rename_expression_fields(node: dict[str, Any], version: str) -> dict[str, Any]:
    renamed = copy.deepcopy(node)
    if renamed["op"] == "column":
        renamed["field"] = FIELD_MAPS[version][renamed["field"]]
    for idx, arg in enumerate(renamed.get("args", [])):
        renamed["args"][idx] = _rename_expression_fields(arg, version)
    return renamed


def generate_sample(seed: int = 20261128, row_count: int = 257) -> GeneratedSample:
    """Generate the same logical table in three deliberately incompatible schemas."""
    if row_count <= 0:
        raise ValueError("row_count must be positive")
    rng = random.Random(seed)
    masks = _make_masks(row_count, rng)
    logical_columns = _logical_values(row_count, rng, masks)
    schemas = {
        version: _build_schema(version, row_count, logical_columns, masks)
        for version in ("v1", "v2", "v3")
    }
    canonical_expressions = _canonical_expressions()
    expressions = {
        version: [
            {
                **{
                    key: copy.deepcopy(value)
                    for key, value in expression.items()
                    if key != "root"
                },
                "root": _rename_expression_fields(expression["root"], version),
            }
            for expression in canonical_expressions
        ]
        for version in ("v1", "v2", "v3")
    }
    return GeneratedSample(
        seed=seed,
        row_count=row_count,
        schemas=schemas,
        expressions=expressions,
        logical_columns=logical_columns,
    )
