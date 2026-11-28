from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .model import BOOL, INT64, NULL, STRING, ColumnVector, DType, EvaluationError


NUMERIC_KINDS = {"int64", "decimal", "bool"}
COMPARISON_OPS = {"eq", "ne", "lt", "le", "gt", "ge"}
ARITHMETIC_OPS = {"add", "sub", "mul", "div"}
LOGICAL_OPS = {"and", "or"}


def is_numeric(dtype: DType) -> bool:
    return dtype.kind in NUMERIC_KINDS


def _scale_of(dtype: DType) -> int:
    if dtype.kind == "decimal":
        return int(dtype.scale or 0)
    return 0


def numeric_common_type(types: list[DType], *, arithmetic: bool = False) -> DType:
    present = [dtype for dtype in types if dtype.kind != "null"]
    if not present:
        return NULL
    if not all(is_numeric(dtype) for dtype in present):
        raise EvaluationError(
            f"numeric promotion requires numeric operands, got {[dtype.label for dtype in present]}"
        )
    if any(dtype.kind == "decimal" for dtype in present):
        return DType("decimal", max(_scale_of(dtype) for dtype in present))
    if all(dtype.kind == "bool" for dtype in present) and not arithmetic:
        return BOOL
    return INT64


def common_type(types: list[DType], *, context: str) -> DType:
    present = [dtype for dtype in types if dtype.kind != "null"]
    if not present:
        return NULL
    kinds = {dtype.kind for dtype in present}
    if len(kinds) == 1:
        if kinds == {"decimal"}:
            return DType("decimal", max(_scale_of(dtype) for dtype in present))
        if kinds == {"string"}:
            return STRING
        if kinds == {"bool"}:
            return BOOL
        if kinds == {"int64"}:
            return INT64
    if kinds <= NUMERIC_KINDS:
        return numeric_common_type(present)
    raise EvaluationError(
        f"{context}: no common type for {[dtype.label for dtype in present]}"
    )


@dataclass(frozen=True)
class PromotionRecord:
    operand_types: list[DType]
    operand_targets: list[DType]
    result_type: DType
    basis: str
    conversions: list[dict[str, Any]]

    def to_json(self) -> dict[str, Any]:
        return {
            "operand_types": [dtype.label for dtype in self.operand_types],
            "operand_targets": [dtype.label for dtype in self.operand_targets],
            "result_type": self.result_type.label,
            "basis": self.basis,
            "conversions": self.conversions,
        }


def _conversion_entry(source: DType, target: DType) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "from": source.label,
        "to": target.label,
        "changed": source != target,
    }
    if source.kind == "bool" and target.kind == "int64":
        entry["rule"] = "false -> 0, true -> 1"
    elif source.kind == "bool" and target.kind == "decimal":
        entry["rule"] = "false -> 0 at target scale, true -> 10^scale"
    elif source.kind == "int64" and target.kind == "decimal":
        entry["rule"] = f"raw * 10^{target.scale}"
    elif source.kind == "decimal" and target.kind == "decimal":
        if int(source.scale or 0) < int(target.scale or 0):
            entry["rule"] = f"raw * 10^{int(target.scale or 0) - int(source.scale or 0)}"
        elif int(source.scale or 0) > int(target.scale or 0):
            entry["rule"] = (
                f"raw / 10^{int(source.scale or 0) - int(target.scale or 0)}; "
                "exact divisibility is required"
            )
        else:
            entry["rule"] = "no change"
    elif source.kind == "null":
        entry["rule"] = "null-only vector adopts the target type"
    return entry


def plan_promotion(op: str, operand_types: list[DType]) -> PromotionRecord:
    if op in ARITHMETIC_OPS:
        present = [dtype for dtype in operand_types if dtype.kind != "null"]
        if not all(is_numeric(dtype) for dtype in present):
            raise EvaluationError(
                f"{op} requires numeric operands, got "
                f"{[dtype.label for dtype in operand_types]}"
            )
        if op == "mul":
            if any(dtype.kind == "decimal" for dtype in present):
                result_scale = sum(_scale_of(dtype) for dtype in present)
                result_type = DType("decimal", result_scale)
            else:
                result_type = INT64
            operand_targets: list[DType] = []
            for dtype in operand_types:
                if dtype.kind == "null":
                    operand_targets.append(INT64)
                elif dtype.kind == "bool":
                    operand_targets.append(INT64)
                else:
                    operand_targets.append(dtype)
            basis = (
                "multiplication keeps raw integer products; decimal result scale is "
                "the sum of operand decimal scales; bool is first promoted to int64"
            )
        elif op == "div":
            numeric_type = numeric_common_type(operand_types, arithmetic=True)
            if numeric_type.kind == "decimal":
                result_type = numeric_type
            else:
                result_type = DType("decimal", 3)
            operand_targets = [
                INT64 if dtype.kind == "null" else result_type for dtype in operand_types
            ]
            basis = (
                "division aligns operands to a decimal working scale "
                "(at least scale 3); integer division is promoted to decimal(scale=3); "
                "digits beyond the working scale are truncated toward zero"
            )
        else:
            result_type = numeric_common_type(operand_types, arithmetic=True)
            operand_targets = [
                INT64 if dtype.kind == "null" and result_type.kind != "decimal" else result_type
                for dtype in operand_types
            ]
            basis = (
                "flat arithmetic aligns decimal operands to the maximum scale; "
                "integer and boolean operands are promoted to that scale; "
                "booleans use false=0, true=1"
            )
        conversions = [
            _conversion_entry(source, target)
            for source, target in zip(operand_types, operand_targets)
        ]
        return PromotionRecord(
            operand_types=operand_types,
            operand_targets=operand_targets,
            result_type=result_type,
            basis=basis,
            conversions=conversions,
        )

    if op in COMPARISON_OPS:
        present = [dtype for dtype in operand_types if dtype.kind != "null"]
        if not present:
            target = NULL
            basis = "all-null comparison"
        elif all(is_numeric(dtype) for dtype in present):
            target = numeric_common_type(operand_types, arithmetic=False)
            if any(dtype.kind == "bool" for dtype in present) and any(
                dtype.kind in {"int64", "decimal"} for dtype in present
            ):
                basis = (
                    "mixed boolean/integer comparison promotes bool to int64 first "
                    "(false=0, true=1), then applies decimal scale alignment if needed"
                )
            else:
                basis = "numeric comparison uses a common aligned numeric type"
        elif all(dtype.kind == "string" for dtype in present):
            target = STRING
            basis = "string comparison uses lexicographic order"
        elif all(dtype.kind == "bool" for dtype in present):
            target = BOOL
            basis = "boolean comparison keeps bool ordering false < true"
        else:
            raise EvaluationError(
                f"{op} has incompatible comparison operands "
                f"{[dtype.label for dtype in operand_types]}"
            )
        operand_targets = [
            target if dtype.kind != "null" else target for dtype in operand_types
        ]
        conversions = [
            _conversion_entry(source, target_dtype)
            for source, target_dtype in zip(operand_types, operand_targets)
        ]
        return PromotionRecord(
            operand_types=operand_types,
            operand_targets=operand_targets,
            result_type=BOOL,
            basis=basis,
            conversions=conversions,
        )

    if op in LOGICAL_OPS:
        for dtype in operand_types:
            if dtype.kind not in {"bool", "null"}:
                raise EvaluationError(
                    f"{op} requires bool operands, got {[item.label for item in operand_types]}"
                )
        return PromotionRecord(
            operand_types=operand_types,
            operand_targets=[BOOL for _ in operand_types],
            result_type=BOOL,
            basis="logical operators use standard three-valued AND/OR truth tables",
            conversions=[
                _conversion_entry(source, BOOL) for source in operand_types
            ],
        )

    if op == "not":
        if len(operand_types) != 1 or operand_types[0].kind not in {"bool", "null"}:
            raise EvaluationError("not requires one bool operand")
        return PromotionRecord(
            operand_types=operand_types,
            operand_targets=[BOOL],
            result_type=BOOL,
            basis="three-valued NOT: null remains null",
            conversions=[_conversion_entry(operand_types[0], BOOL)],
        )

    if op == "abs":
        if len(operand_types) != 1 or not is_numeric(operand_types[0]):
            raise EvaluationError("abs requires one numeric operand")
        if operand_types[0].kind == "bool":
            result_type = INT64
        else:
            result_type = operand_types[0]
        return PromotionRecord(
            operand_types=operand_types,
            operand_targets=[result_type],
            result_type=result_type,
            basis="abs preserves decimal scale or int64; bool is promoted to int64",
            conversions=[_conversion_entry(operand_types[0], result_type)],
        )

    if op == "coalesce":
        result_type = common_type(operand_types, context="coalesce")
        return PromotionRecord(
            operand_types=operand_types,
            operand_targets=[result_type for _ in operand_types],
            result_type=result_type,
            basis=(
                "coalesce first unifies all arguments to a common type; "
                "decimal arguments align to the maximum scale"
            ),
            conversions=[
                _conversion_entry(source, result_type) for source in operand_types
            ],
        )

    if op == "lower":
        if len(operand_types) != 1 or operand_types[0].kind not in {"string", "null"}:
            raise EvaluationError("lower requires one string operand")
        return PromotionRecord(
            operand_types=operand_types,
            operand_targets=[STRING],
            result_type=STRING,
            basis="lower is a string function; null propagates",
            conversions=[_conversion_entry(operand_types[0], STRING)],
        )

    if op == "length":
        if len(operand_types) != 1 or operand_types[0].kind not in {"string", "null"}:
            raise EvaluationError("length requires one string operand")
        return PromotionRecord(
            operand_types=operand_types,
            operand_targets=[STRING],
            result_type=INT64,
            basis="length returns int64; null propagates",
            conversions=[_conversion_entry(operand_types[0], STRING)],
        )

    if op == "is_null":
        if len(operand_types) != 1:
            raise EvaluationError("is_null requires one operand")
        return PromotionRecord(
            operand_types=operand_types,
            operand_targets=operand_types,
            result_type=BOOL,
            basis="is_null inspects the explicit mask and never propagates null",
            conversions=[],
        )

    raise EvaluationError(f"unsupported operator for type promotion: {op}")


def cast_vector(vector: ColumnVector, target: DType) -> ColumnVector:
    if vector.dtype == target:
        return ColumnVector(
            dtype=target,
            values=list(vector.values),
            null_mask=list(vector.null_mask),
            source_field=vector.source_field,
        )
    source = vector.dtype
    values: list[Any] = []
    if target.kind == "decimal":
        target_scale = int(target.scale or 0)
        if source.kind == "null":
            values = [None] * len(vector)
        elif source.kind in {"int64", "bool"}:
            factor = 10**target_scale
            values = [
                None if is_null else int(value) * factor
                for value, is_null in zip(vector.values, vector.null_mask)
            ]
        elif source.kind == "decimal":
            source_scale = int(source.scale or 0)
            if target_scale >= source_scale:
                factor = 10 ** (target_scale - source_scale)
                values = [
                    None if is_null else int(value) * factor
                    for value, is_null in zip(vector.values, vector.null_mask)
                ]
            else:
                divisor = 10 ** (source_scale - target_scale)
                for value, is_null in zip(vector.values, vector.null_mask):
                    if is_null:
                        values.append(None)
                    elif int(value) % divisor:
                        raise EvaluationError(
                            f"cannot cast {source.label} raw value {value} to "
                            f"{target.label} without rounding"
                        )
                    else:
                        values.append(int(value) // divisor)
        else:
            raise EvaluationError(f"cannot cast {source.label} to {target.label}")
    elif target.kind == "int64":
        if source.kind == "bool":
            values = [
                None if is_null else int(value)
                for value, is_null in zip(vector.values, vector.null_mask)
            ]
        elif source.kind == "null":
            values = [None] * len(vector)
        elif source.kind == "int64":
            values = list(vector.values)
        else:
            raise EvaluationError(f"cannot cast {source.label} to {target.label}")
    elif target.kind == "string":
        if source.kind == "null":
            values = [None] * len(vector)
        elif source.kind == "string":
            values = list(vector.values)
        else:
            raise EvaluationError(f"cannot cast {source.label} to {target.label}")
    elif target.kind == "bool":
        if source.kind in {"bool", "null"}:
            values = list(vector.values)
        else:
            raise EvaluationError(f"cannot cast {source.label} to {target.label}")
    elif target.kind == "null":
        values = [None] * len(vector)
    else:
        raise EvaluationError(f"unsupported cast target: {target.label}")
    mask = list(vector.null_mask)
    if target.kind == "null":
        mask = [True] * len(vector)
    return ColumnVector(
        dtype=target,
        values=values,
        null_mask=mask,
        source_field=vector.source_field,
    )
