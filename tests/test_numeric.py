from __future__ import annotations

import math

import pytest

from colagg.canonical import sha256_json
from colagg.errors import E_FIXED_OVERFLOW, EngineError
from colagg.numeric import convert_fixed
from colagg.validation import parse_batch, parse_config
from conftest import default_config


def test_fixed_conversion_records_scale_direction_and_raw_value() -> None:
    converted = convert_fixed(1.2345, 1000, -(2**63), 2**63 - 1)
    assert converted.scale == 1000
    assert converted.scaled == 1234
    assert converted.direction == "down"
    assert converted.raw_repr == (1.2345).hex()

    exact = convert_fixed(1.25, 1000, -(2**63), 2**63 - 1)
    assert exact.scaled == 1250
    assert exact.direction == "none"

    rounded_up = convert_fixed(1.2346, 1000, -(2**63), 2**63 - 1)
    assert rounded_up.scaled == 1235
    assert rounded_up.direction == "up"


def test_fixed_conversion_handles_nonfinite_without_silent_drop() -> None:
    nan = convert_fixed(math.nan, 100, -(2**63), 2**63 - 1)
    pos_inf = convert_fixed(math.inf, 100, -(2**63), 2**63 - 1)
    neg_inf = convert_fixed(-math.inf, 100, -(2**63), 2**63 - 1)
    assert nan.nonfinite == "nan" and nan.scaled is None
    assert pos_inf.nonfinite == "positive_infinity" and pos_inf.scaled is None
    assert neg_inf.nonfinite == "negative_infinity" and neg_inf.scaled is None


def test_fixed_conversion_overflow_is_explicit() -> None:
    with pytest.raises(EngineError) as caught:
        convert_fixed(2.0, 100, -128, 127)
    assert caught.value.code == E_FIXED_OVERFLOW
    assert caught.value.details["raw"]


def test_literal_nonfinite_json_tokens_normalize_for_digest() -> None:
    digest = sha256_json(
        {
            "batch_seq": 1,
            "row_count": 1,
            "columns": [
                {"name": "site", "type": "string", "values": ["A"]},
                {
                    "name": "value",
                    "type": "float",
                    "values": ["NaN"],
                },
            ],
        }
    )
    raw = {
        "batch_seq": 1,
        "row_count": 1,
        "columns": [
            {"name": "site", "type": "string", "values": ["A"]},
            {"name": "value", "type": "float", "values": [math.nan]},
        ],
        "batch_digest": digest,
    }
    batch = parse_batch(raw, parse_config(default_config(max_batch_rows=10)))
    assert math.isnan(batch.columns[1].values[0])
