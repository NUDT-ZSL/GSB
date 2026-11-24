import pytest

from drift_engine import codes, values
from drift_engine.parsers import parse_v1, parse_v2


def test_v1_rejections_keep_processing(sample_workspace):
    from drift_engine.spec import BaselineSpec

    spec = BaselineSpec.load_file(str(sample_workspace / "spec.json"))
    result = parse_v1(str(sample_workspace / "v1" / "snapshot-v1.json"), spec)
    rejected_codes = {entry["code"] for entry in result.rejected}
    assert codes.E_BAD_TS in rejected_codes
    assert codes.E_BAD_HOST in rejected_codes
    assert codes.E_RECORD_DUP in rejected_codes
    # valid records still accepted
    assert len(result.accepted) >= 6
    for entry in result.rejected:
        assert entry["generation"] == 1
        assert entry["record_index"] >= 1


def test_v2_monotonic_and_digest_rejections(sample_workspace):
    from drift_engine.spec import BaselineSpec

    spec = BaselineSpec.load_file(str(sample_workspace / "spec.json"))
    result = parse_v2(
        str(sample_workspace / "v2" / "shards"),
        str(sample_workspace / "v2" / "metadata.db"),
        spec,
    )
    rejected_codes = {entry["code"] for entry in result.rejected}
    assert codes.E_BAD_GENERATION in rejected_codes
    assert codes.E_INTERVAL_REVERSED in rejected_codes
    assert any(evidence.tombstone for evidence in result.accepted)


def test_value_relations_order_independent():
    small = {"type": "interval", "low": 0, "high": 10}
    big = {"type": "interval", "low": 0, "high": 20}
    forward = values.compare_values(small, big)
    backward = values.compare_values(big, small)
    assert forward["relation"] == codes.R_CONTAINS
    assert backward["relation"] == codes.R_CONTAINS
    assert forward["container_side"] == "right"
    assert backward["container_side"] == "left"

    mask_a = {"type": "mask", "labels": ["A", "B"]}
    mask_b = {"type": "mask", "labels": ["B", "C"]}
    rel = values.compare_values(mask_a, mask_b)
    assert rel["relation"] == codes.R_EXCLUSIVE
    assert rel["partial_overlap"] is True


def test_type_mismatch_and_interval_reversed():
    with pytest.raises(values.ValueError) as exc:
        values.normalize_v2({"type": "int", "value": 1}, "bool")
    assert exc.value.code == codes.E_TYPE_MISMATCH
    with pytest.raises(values.ValueError) as exc:
        values.normalize_v2({"type": "interval", "low": 9, "high": 1}, "interval")
    assert exc.value.code == codes.E_INTERVAL_REVERSED


def test_v1_string_parsing_aligns_with_v2():
    parsed = values.parse_v1_string("SSH|HTTP", "mask", ["SSH", "HTTP", "ADMIN"])
    assert parsed == {"type": "mask", "labels": ["HTTP", "SSH"]}
    assert values.value_key(parsed) == values.value_key(
        values.normalize_v2({"type": "mask", "labels": ["HTTP", "SSH"]}, "mask")
    )
