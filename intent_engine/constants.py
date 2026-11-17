"""Shared constants and precision helpers."""

from __future__ import annotations

SCHEMA_VERSION = 1

STAGES = ("normalize", "intent", "slots", "sequence", "merge")
STAGE_ORDER = {name: index for index, name in enumerate(STAGES)}

LABELS = ("B", "I", "E", "S", "O")

ENTITY_TYPES = ("品牌", "地点", "数量", "时间")
ENTITY_TYPE_ALIASES = {
    "brand": "品牌",
    "location": "地点",
    "place": "地点",
    "quantity": "数量",
    "qty": "数量",
    "number": "数量",
    "time": "时间",
    "date": "时间",
    "品牌": "品牌",
    "地点": "地点",
    "数量": "数量",
    "时间": "时间",
}

BUSINESS_CODES = (
    "E_EMPTY_SENTENCE",
    "E_BAD_RECORD",
    "E_UNKNOWN_INTENT",
    "E_SPAN_OUT_OF_RANGE",
    "E_CONFIG_MISMATCH",
    "E_NONFINITE_WEIGHT",
    "E_INTERNAL",
)

PRECISION_TIERS = {
    "low": 2,
    "medium": 4,
    "high": 6,
    "exact": 10,
}


def precision_digits(value: object) -> int:
    """Return a safe number of decimal places for the configured tier."""

    if isinstance(value, str):
        key = value.strip().lower()
        if key in PRECISION_TIERS:
            return PRECISION_TIERS[key]
        if key.isdigit():
            value = int(key)
        else:
            raise ValueError(f"unsupported precision tier: {value!r}")
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("precision must be an integer digit count or a named tier")
    if value < 0 or value > 12:
        raise ValueError("precision digit count must be between 0 and 12")
    return value

