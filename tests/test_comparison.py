from __future__ import annotations

import copy

from georope.compare import cross_version_comparison
from georope.normalize import normalize_request


def test_coordinate_unit_drift_is_classified_with_source_version(
    sample_payload: dict,
) -> None:
    payload = copy.deepcopy(sample_payload)
    target = next(
        block for block in payload["blocks"] if block["block_id"] == "v2-tile-r0c0"
    )
    target["geo_origin"] = [500000.0, 3300000.0]
    request = normalize_request(payload)
    report = cross_version_comparison(request)
    assert report["coordinate_unit_drift"]
    first_drift = report["coordinate_unit_drift"][0]
    assert first_drift["classification"] == "coordinate_unit_conversion"
    assert "v2" in first_drift["source_versions"]


def test_channel_group_expression_drift_is_classified(sample_payload: dict) -> None:
    payload = copy.deepcopy(sample_payload)
    target = next(
        block for block in payload["blocks"] if block["block_id"] == "v2-tile-r0c0"
    )
    target["channel_groups"][0]["indices"] = [0, 1, 8, 9]
    target["channel_groups"][2]["indices"] = [2, 3, 10, 11]
    request = normalize_request(payload)
    report = cross_version_comparison(request)
    assert report["channel_group_drift"]
    first_drift = report["channel_group_drift"][0]
    classification = first_drift["classification"]
    if isinstance(classification, list):
        assert "channel_group_expression" in classification
    else:
        assert classification == "channel_group_expression"
    assert set(first_drift["source_versions"]) == {"v1", "v2"}
