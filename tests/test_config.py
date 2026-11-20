from __future__ import annotations

import pytest

from rope_attention.config import parse_config
from rope_attention.data import lossy_v3_config, sample_configs
from rope_attention.errors import E_FORMAT_CORRUPT, E_VERSION_UNSUPPORTED, RopeError
from rope_attention.util import sha256_json


def test_three_versions_parse_to_same_core_semantics() -> None:
    configs = {version: parse_config(text) for version, text in sample_configs().items()}
    assert configs["v1"].base_theta == configs["v2"].base_theta == configs["v3"].base_theta
    assert configs["v1"].pos_scale == configs["v2"].pos_scale == configs["v3"].pos_scale
    assert configs["v1"].block_size == configs["v2"].block_size == configs["v3"].block_size
    assert configs["v1"].variant is None
    assert configs["v2"].variant == "rope"
    assert configs["v3"].variant == "rope"
    assert configs["v3"].per_head_scale == (1.0, 1.0, 1.0, 1.0)
    assert configs["v1"].source_line == 2
    assert configs["v2"].field_locations["base_theta"] == "line:2,field:1"
    assert configs["v3"].field_locations["variant"].startswith("line:2,json:")


def test_repeated_parse_is_byte_identical_and_records_source_line() -> None:
    text = sample_configs()["v2"]
    first = parse_config(text)
    second = parse_config(text)
    assert first.canonical_bytes() == second.canonical_bytes()
    assert first.content_checksum() == second.content_checksum()
    assert first.source_span == "line:1-line:2"
    assert sha256_json(first.to_dict()) == sha256_json(second.to_dict())


@pytest.mark.parametrize(
    ("text", "expected_code", "location_fragment"),
    [
        ("v9\n10000|1.0|6\n", E_VERSION_UNSUPPORTED, "source_line"),
        ("v1\n10000|1.0\n", E_FORMAT_CORRUPT, "field_count"),
        (
            "v1\nnot-a-number|1.0|6\n",
            E_FORMAT_CORRUPT,
            "line:2,field:1",
        ),
        (
            "v2\nbase_theta=10000|pos_scale=1.0|variant=rope\n",
            E_FORMAT_CORRUPT,
            "block_size",
        ),
        (
            "v2\nbase_theta=10000|pos_scale=x|block_size=6\n",
            E_FORMAT_CORRUPT,
            "line:2,field:2",
        ),
        (
            'v3\n{"base_theta":10000,"pos_scale":1.0,"block_size":6,'
            '"frequency_adjustment":{"method":"identity"},'
            '"per_head_scale":[1.0]}\n',
            E_FORMAT_CORRUPT,
            "variant",
        ),
    ],
)
def test_parse_errors_are_typed_and_located(
    text: str,
    expected_code: str,
    location_fragment: str,
) -> None:
    with pytest.raises(RopeError) as captured:
        parse_config(text)
    error = captured.value
    assert error.code == expected_code
    assert location_fragment in str(error.details)


def test_v3_json_field_set_can_dispatch_without_header() -> None:
    text = sample_configs()["v3"].split("\n", 1)[1]
    parsed = parse_config(text)
    assert parsed.source_version == "v3"
    assert parsed.variant == "rope"


def test_lossy_v3_sample_is_structurally_valid() -> None:
    parsed = parse_config(lossy_v3_config())
    assert parsed.variant == "block_freq"
    assert parsed.frequency_adjustment is not None
    assert parsed.frequency_adjustment.method == "blockwise"
    assert parsed.per_head_scale == (0.5, 1.0, 1.5, 2.0)

