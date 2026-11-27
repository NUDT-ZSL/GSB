from __future__ import annotations

import copy

import pytest

from georope.errors import BusinessError
from georope.normalize import normalize_request


def test_three_versions_normalize_to_same_geography(sample_payload: dict) -> None:
    request = normalize_request(sample_payload)
    first_by_version = {
        version: request.blocks_for_version(version)[0]
        for version in request.schema_versions
    }
    assert request.schema_versions == ["v1", "v2", "v3"]
    assert {
        block.origin_m for block in first_by_version.values()
    } == {(500000.0, 3300000.0)}
    assert {
        block.resolution_m for block in first_by_version.values()
    } == {(10.0, 10.0)}
    assert first_by_version["v2"].origin_unit == "pixels"
    assert first_by_version["v1"].origin_unit == "meters"
    assert first_by_version["v3"].origin_unit == "meters"
    for block in first_by_version.values():
        assert [band.channels for band in block.bands] == [
            (0, 1, 2, 3),
            (4, 5, 6, 7),
            (8, 9, 10, 11),
        ]
        assert [
            (band.angle_base, band.frequency_scale, band.axis)
            for band in block.bands
        ] == [
            (10000.0, 0.006, (1.0, 0.0)),
            (6000.0, 0.009, (0.0, 1.0)),
            (2000.0, 0.004, (0.7071067811865476, 0.7071067811865476)),
        ]


def test_normalization_summary_records_source_forms(sample_payload: dict) -> None:
    summary = normalize_request(sample_payload).summary()
    v1 = summary["schema_versions"]["v1"]
    v2 = summary["schema_versions"]["v2"]
    assert v1["origin_units"] == ["meters"]
    assert v2["origin_units"] == ["pixels"]
    assert v1["blocks"][0]["source"]["group_container_field"] == "channel_ranges"
    assert v2["blocks"][0]["source"]["group_container_field"] == "channel_groups"
    assert v1["blocks"][0]["origin_was_converted"] is False
    assert v2["blocks"][0]["origin_was_converted"] is True
    assert summary["coordinate_model"]["geographic_unit"] == "meters"


def test_unit_ambiguity_is_business_error(sample_payload: dict) -> None:
    payload = {
        **sample_payload,
        "blocks": [dict(block) for block in sample_payload["blocks"]],
    }
    payload["blocks"][0].pop("origin_unit")
    with pytest.raises(BusinessError) as error:
        normalize_request(payload)
    assert error.value.code == "COORD_UNIT_AMBIGUOUS"
    assert error.value.locations[0]["block_id"] == "v1-tile-r0c0"


def test_channel_group_overlap_is_business_error(sample_payload: dict) -> None:
    payload = {
        **sample_payload,
        "blocks": [dict(block) for block in sample_payload["blocks"]],
    }
    payload["blocks"][4] = {
        **payload["blocks"][4],
        "channel_groups": [
            {
                **payload["blocks"][4]["channel_groups"][0],
                "indices": [0, 1, 2, 3, 4],
            },
            *payload["blocks"][4]["channel_groups"][1:],
        ],
    }
    with pytest.raises(BusinessError) as error:
        normalize_request(payload)
    assert error.value.code == "CHANNEL_GROUP_OVERLAP"
    assert error.value.locations[0]["channel"] == 4


def test_block_extent_conflict_is_business_error(sample_payload: dict) -> None:
    payload = {
        **sample_payload,
        "blocks": [dict(block) for block in sample_payload["blocks"]],
    }
    first = next(
        block for block in payload["blocks"] if block["block_id"] == "v1-tile-r0c0"
    )
    second = next(
        block for block in payload["blocks"] if block["block_id"] == "v1-tile-r0c1"
    )
    second["origin_x"] = first["origin_x"]
    second["origin_y"] = first["origin_y"]
    with pytest.raises(BusinessError) as error:
        normalize_request(payload)
    assert error.value.code == "BLOCK_EXTENT_CONFLICT"
    assert error.value.locations[0]["blocks"] == [
        "v1-tile-r0c0",
        "v1-tile-r0c1",
    ]


def test_block_level_frequency_parameters_are_supported(sample_payload: dict) -> None:
    payload = copy.deepcopy(sample_payload)
    target = payload["blocks"][0]
    target["angle_base"] = 777.0
    target["frequency_scale"] = 0.011
    target["frequency_axis"] = [0.0, 1.0]
    for band in target["frequency_bands"]:
        band.pop("angle_base")
        band.pop("frequency_scale")
        band.pop("frequency_axis")
    request = normalize_request(payload)
    first_band = request.blocks[0].bands[0]
    assert first_band.angle_base == 777.0
    assert first_band.frequency_scale == 0.011
    assert first_band.axis == (0.0, 1.0)
