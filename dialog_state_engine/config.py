from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any

from .canonical import content_hash, read_json
from .errors import E_CONFIG_INVALID, E_FORMAT_CORRUPT, fail


def _require_number(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        fail(E_CONFIG_INVALID, "configuration value must be numeric", field=field)
    number = float(value)
    if number != number or number in (float("inf"), float("-inf")):
        fail(E_CONFIG_INVALID, "configuration value must be finite", field=field)
    return number


def _require_int(value: Any, field: str, minimum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        fail(E_CONFIG_INVALID, "configuration value must be an integer", field=field)
    if minimum is not None and value < minimum:
        fail(E_CONFIG_INVALID, f"configuration value must be >= {minimum}", field=field)
    return value


@dataclass(frozen=True)
class Rules:
    config_version: str
    time_origin_ms: int
    turn_step_ms: int
    default_state_generation: int
    confidence_weight: float
    turn_weight: float
    score_precision: int
    clarification_threshold: float
    clarification_pattern: str
    slot_key_pattern: str
    merge_strategy: str
    source_path: str | None = None

    @property
    def digest(self) -> str:
        return content_hash(self.to_dict())

    @property
    def clarification_regex(self) -> re.Pattern[str]:
        return re.compile(self.clarification_pattern)

    def to_dict(self) -> dict[str, Any]:
        return {
            "config_version": self.config_version,
            "time_origin_ms": self.time_origin_ms,
            "turn_step_ms": self.turn_step_ms,
            "default_state_generation": self.default_state_generation,
            "scoring": {
                "confidence_weight": self.confidence_weight,
                "turn_weight": self.turn_weight,
                "precision": self.score_precision,
            },
            "clarification": {
                "threshold": self.clarification_threshold,
                "pattern": self.clarification_pattern,
            },
            "slot_key_pattern": self.slot_key_pattern,
            "merge": {"strategy": self.merge_strategy},
        }

    def timestamp_for_turn(self, turn_no: int) -> int:
        return self.time_origin_ms + turn_no * self.turn_step_ms

    def relative_seconds(self, timestamp_ms: int) -> int | float:
        delta = timestamp_ms - self.time_origin_ms
        if delta % 1000 == 0:
            return delta // 1000
        return float((Decimal(delta) / Decimal(1000)).quantize(Decimal("0.001")))

    def milliseconds_from_relative(self, seconds: Any, field: str) -> int:
        number = _require_number(seconds, field)
        milliseconds = (
            Decimal(str(number)) * Decimal(1000)
        ).to_integral_value(rounding=ROUND_HALF_UP)
        return self.time_origin_ms + int(milliseconds)

    @classmethod
    def from_dict(cls, data: Any, source_path: str | None = None) -> "Rules":
        if not isinstance(data, dict):
            fail(E_CONFIG_INVALID, "configuration must be a JSON object", field="$")
        try:
            config_version = data["config_version"]
            time_origin_ms = _require_int(data["time_origin_ms"], "time_origin_ms", 0)
            turn_step_ms = _require_int(data["turn_step_ms"], "turn_step_ms", 1)
            default_generation = _require_int(
                data["default_state_generation"], "default_state_generation", 0
            )
            scoring = data["scoring"]
            clarification = data["clarification"]
            merge = data["merge"]
            slot_key_pattern = data["slot_key_pattern"]
        except KeyError as exc:
            fail(
                E_CONFIG_INVALID,
                "configuration field is missing",
                field=str(exc.args[0]),
            )
        if not isinstance(config_version, str) or not config_version:
            fail(E_CONFIG_INVALID, "config_version must be a non-empty string", field="config_version")
        if not isinstance(scoring, dict):
            fail(E_CONFIG_INVALID, "scoring must be an object", field="scoring")
        if not isinstance(clarification, dict):
            fail(E_CONFIG_INVALID, "clarification must be an object", field="clarification")
        if not isinstance(merge, dict):
            fail(E_CONFIG_INVALID, "merge must be an object", field="merge")

        confidence_weight = _require_number(
            scoring.get("confidence_weight"), "scoring.confidence_weight"
        )
        turn_weight = _require_number(scoring.get("turn_weight"), "scoring.turn_weight")
        precision = _require_int(scoring.get("precision"), "scoring.precision", 0)
        threshold = _require_number(
            clarification.get("threshold"), "clarification.threshold"
        )
        pattern = clarification.get("pattern")
        strategy = merge.get("strategy")

        if confidence_weight < 0 or turn_weight < 0:
            fail(E_CONFIG_INVALID, "scoring weights must be non-negative", field="scoring")
        if confidence_weight + turn_weight <= 0:
            fail(E_CONFIG_INVALID, "at least one scoring weight must be positive", field="scoring")
        if precision > 12:
            fail(E_CONFIG_INVALID, "scoring.precision must be <= 12", field="scoring.precision")
        if not 0 <= threshold <= 1:
            fail(E_CONFIG_INVALID, "clarification threshold must be in [0, 1]", field="clarification.threshold")
        if not isinstance(pattern, str) or not pattern:
            fail(E_CONFIG_INVALID, "clarification pattern must be a non-empty string", field="clarification.pattern")
        try:
            regex = re.compile(pattern)
        except re.error as exc:
            fail(
                E_CONFIG_INVALID,
                f"clarification pattern is invalid: {exc}",
                field="clarification.pattern",
            )
        if regex.groups != 1:
            fail(
                E_CONFIG_INVALID,
                "clarification pattern must have exactly one capture group",
                field="clarification.pattern",
            )
        if not isinstance(slot_key_pattern, str) or not slot_key_pattern:
            fail(E_CONFIG_INVALID, "slot_key_pattern must be non-empty", field="slot_key_pattern")
        try:
            re.compile(slot_key_pattern)
        except re.error as exc:
            fail(
                E_CONFIG_INVALID,
                f"slot key pattern is invalid: {exc}",
                field="slot_key_pattern",
            )
        if strategy != "source_turn_then_score_then_value_lexicographic":
            fail(
                E_CONFIG_INVALID,
                "unsupported slot merge strategy",
                field="merge.strategy",
                details={"supported": "source_turn_then_score_then_value_lexicographic"},
            )
        return cls(
            config_version=config_version,
            time_origin_ms=time_origin_ms,
            turn_step_ms=turn_step_ms,
            default_state_generation=default_generation,
            confidence_weight=confidence_weight,
            turn_weight=turn_weight,
            score_precision=precision,
            clarification_threshold=threshold,
            clarification_pattern=pattern,
            slot_key_pattern=slot_key_pattern,
            merge_strategy=strategy,
            source_path=source_path,
        )

    @classmethod
    def from_file(cls, path: str | Path) -> "Rules":
        source = Path(path)
        try:
            data = read_json(source)
        except FileNotFoundError:
            fail(E_CONFIG_INVALID, "configuration file does not exist", field=str(source))
        except (OSError, ValueError) as exc:
            fail(
                E_FORMAT_CORRUPT,
                f"configuration file cannot be parsed: {exc}",
                field=str(source),
            )
        return cls.from_dict(data, source_path=str(source))

