from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any

import numpy as np

from .encoding import canonical_digest
from .errors import NarrativeError
from .fixedpoint import fixed_to_decimal, fixed_to_float64
from .schema import ConditionSpec, EffectSpec, Script


COMPARATORS = {
    "==": lambda left, right: left == right,
    "!=": lambda left, right: left != right,
    ">": lambda left, right: left > right,
    ">=": lambda left, right: left >= right,
    "<": lambda left, right: left < right,
    "<=": lambda left, right: left <= right,
}


class DomainError(NarrativeError):
    code = "E_DOMAIN"
    exit_code = 5


@dataclass
class WorldState:
    fixed: dict[str, int]
    integers: dict[str, int]
    booleans: dict[str, bool]

    @classmethod
    def fresh(cls, script: Script) -> "WorldState":
        return cls(
            fixed=dict(script.initial_fixed),
            integers=dict(script.initial_integers),
            booleans=dict(script.initial_booleans),
        )

    def clone(self) -> "WorldState":
        return WorldState(
            fixed=dict(self.fixed),
            integers=dict(self.integers),
            booleans=dict(self.booleans),
        )

    def exact(self) -> dict[str, dict[str, Any]]:
        return {
            "fixed": {key: self.fixed[key] for key in sorted(self.fixed)},
            "integers": {key: self.integers[key] for key in sorted(self.integers)},
            "booleans": {key: self.booleans[key] for key in sorted(self.booleans)},
        }

    def digest(self) -> str:
        return canonical_digest(self.exact())

    def decimal_values(self, script: Script) -> dict[str, str]:
        result: dict[str, str] = {}
        for key, value in self.fixed.items():
            result[key] = format(fixed_to_decimal(value, script.scale), "f")
        for key, value in self.integers.items():
            result[key] = str(value)
        for key, value in self.booleans.items():
            result[key] = "true" if value else "false"
        return dict(sorted(result.items()))

    def native_values(self, script: Script) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in self.fixed.items():
            result[key] = float(fixed_to_float64(value, script.scale))
        result.update(self.integers)
        result.update(self.booleans)
        return dict(sorted(result.items()))


class FloatTrace:
    """Sequential float64 reference path used only for diagnostics."""

    def __init__(self, values: dict[str, np.float64], scale: int):
        self.values = values
        self.scale = scale

    @classmethod
    def fresh(cls, script: Script, state: WorldState) -> "FloatTrace":
        values = {
            key: fixed_to_float64(state.fixed[key], script.scale)
            for key in script.states
            if script.states[key].base == "fixed"
        }
        return cls(values, script.scale)

    def clone(self) -> "FloatTrace":
        return FloatTrace({key: np.float64(value) for key, value in self.values.items()}, self.scale)

    def apply_effect(self, effect: EffectSpec) -> None:
        if effect.key not in self.values or effect.fixed_value is None:
            return
        amount = fixed_to_float64(effect.fixed_value, self.scale)
        current = np.float64(self.values[effect.key])
        if effect.op == "add":
            updated = np.float64(current + amount)
        elif effect.op == "subtract":
            updated = np.float64(current - amount)
        elif effect.op == "set":
            updated = amount
        else:
            return
        self.values[effect.key] = np.float64(updated)

    def difference(self, key: str, state: WorldState) -> float:
        fixed_reference = fixed_to_float64(state.fixed[key], self.scale)
        return float(abs(np.float64(fixed_reference) - np.float64(self.values[key])))

    def condition_view(self, condition: ConditionSpec) -> dict[str, Any] | None:
        if condition.expected_fixed is None or condition.key not in self.values:
            return None
        actual = np.float64(self.values[condition.key])
        expected = fixed_to_float64(condition.expected_fixed, self.scale)
        return {
            "path": "float64_reference_only",
            "actual": float(actual),
            "expected": float(expected),
            "would_pass": bool(COMPARATORS[condition.op](actual, expected)),
        }

    def snapshot(self) -> dict[str, float]:
        return {key: float(value) for key, value in sorted(self.values.items())}


def evaluate_condition(
    condition: ConditionSpec,
    state: WorldState,
    float_trace: FloatTrace | None = None,
) -> dict[str, Any]:
    comparator = COMPARATORS[condition.op]
    result: dict[str, Any] = {
        "key": condition.key,
        "op": condition.op,
    }
    if condition.expected_fixed is not None:
        actual = state.fixed[condition.key]
        expected = condition.expected_fixed
        passed = bool(comparator(actual, expected))
        result.update(
            {
                "base": "fixed",
                "actual_fixed": actual,
                "expected_fixed": expected,
                "passed": passed,
            }
        )
        if float_trace is not None:
            float_view = float_trace.condition_view(condition)
            if float_view is not None:
                result["float64_reference"] = float_view
        return result

    if condition.expected_int is not None:
        actual_int = state.integers[condition.key]
        expected_int = condition.expected_int
        result.update(
            {
                "base": "integer",
                "actual_integer": actual_int,
                "expected_integer": expected_int,
                "passed": bool(comparator(actual_int, expected_int)),
            }
        )
        return result

    actual_bool = state.booleans[condition.key]
    expected_bool = bool(condition.expected_bool)
    passed = bool(comparator(actual_bool, expected_bool))
    result.update(
        {
            "base": "boolean",
            "actual_boolean": actual_bool,
            "expected_boolean": expected_bool,
            "passed": passed,
        }
    )
    return result


def conditions_pass(
    conditions: tuple[ConditionSpec, ...],
    state: WorldState,
    float_trace: FloatTrace | None = None,
) -> tuple[bool, list[dict[str, Any]]]:
    evaluations = [evaluate_condition(condition, state, float_trace) for condition in conditions]
    return all(item["passed"] for item in evaluations), evaluations


def apply_effect(
    effect: EffectSpec,
    script: Script,
    state: WorldState,
    float_trace: FloatTrace | None = None,
) -> dict[str, Any]:
    spec = script.states[effect.key]
    change: dict[str, Any] = {
        "key": effect.key,
        "op": effect.op,
        "base": spec.base,
    }

    if spec.base == "fixed":
        before = state.fixed[effect.key]
        if effect.op == "add":
            after = before + int(effect.fixed_value)
        elif effect.op == "subtract":
            after = before - int(effect.fixed_value)
        else:
            after = int(effect.fixed_value)
        if spec.min_fixed is not None and spec.max_fixed is not None and not (spec.min_fixed <= after <= spec.max_fixed):
            raise DomainError(
                f"effect on {effect.key!r} violates declared domain",
                details={
                    "key": effect.key,
                    "after_fixed": after,
                    "min_fixed": spec.min_fixed,
                    "max_fixed": spec.max_fixed,
                },
            )
        state.fixed[effect.key] = after
        if float_trace is not None:
            float_trace.apply_effect(effect)
        before_decimal = format(fixed_to_decimal(before, script.scale), "f")
        after_decimal = format(fixed_to_decimal(after, script.scale), "f")
        change.update(
            {
                "before_fixed": before,
                "after_fixed": after,
                "before_decimal": before_decimal,
                "after_decimal": after_decimal,
                "before_value": float(fixed_to_float64(before, script.scale)),
                "after_value": float(fixed_to_float64(after, script.scale)),
                "changed": before != after,
            }
        )
        return change

    if spec.base == "integer":
        before_int = state.integers[effect.key]
        if effect.op == "add":
            after_int = before_int + int(effect.int_value)
        elif effect.op == "subtract":
            after_int = before_int - int(effect.int_value)
        else:
            after_int = int(effect.int_value)
        min_int = int(spec.min_value) if spec.min_value is not None else None
        max_int = int(spec.max_value) if spec.max_value is not None else None
        if min_int is not None and max_int is not None and not (min_int <= after_int <= max_int):
            raise DomainError(
                f"effect on {effect.key!r} violates integer domain",
                details={"key": effect.key, "after": after_int, "min": min_int, "max": max_int},
            )
        state.integers[effect.key] = after_int
        change.update(
            {
                "before_integer": before_int,
                "after_integer": after_int,
                "before_value": before_int,
                "after_value": after_int,
                "changed": before_int != after_int,
            }
        )
        return change

    before_bool = state.booleans[effect.key]
    if effect.op == "toggle":
        after_bool = not before_bool
    else:
        after_bool = bool(effect.bool_value)
    state.booleans[effect.key] = after_bool
    change.update(
        {
            "before_boolean": before_bool,
            "after_boolean": after_bool,
            "before_value": before_bool,
            "after_value": after_bool,
            "changed": before_bool != after_bool,
        }
    )
    return change


def clone_records(value: Any) -> Any:
    return copy.deepcopy(value)
