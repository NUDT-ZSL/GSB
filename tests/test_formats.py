import json

import numpy as np
import pytest

from pano_workbench.errors import (
    E_FORMAT_CORRUPT,
    E_VERSION_UNSUPPORTED,
    PanoError,
)
from pano_workbench.formats import (
    HEADER_SIZE,
    encode_rle_json,
    encode_v1,
    encode_v2,
    read_mask,
    rle_encode,
)
from pano_workbench.generator import generate_sample


def test_v1_roundtrip(cfg):
    sample = generate_sample(cfg)
    parsed = read_mask(sample.pred_v1, cfg)
    assert parsed.version == 1
    assert np.array_equal(parsed.class_map, sample.pred_class)
    assert parsed.instance_map is None
    assert np.array_equal(parsed.ignore_mask, sample.pred_ignore)


def test_v2_roundtrip(cfg):
    sample = generate_sample(cfg)
    parsed = read_mask(sample.pred_v2, cfg)
    assert parsed.version == 2
    assert np.array_equal(parsed.class_map, sample.pred_class)
    assert np.array_equal(parsed.instance_map, sample.pred_instance)
    assert np.allclose(parsed.conf_map, sample.pred_conf, atol=1e-6)


def test_rle_roundtrip(cfg):
    sample = generate_sample(cfg)
    parsed = read_mask(sample.pred_rle, cfg)
    assert parsed.version == 3
    assert np.array_equal(parsed.class_map, sample.pred_class)
    assert np.array_equal(parsed.instance_map, sample.pred_instance)
    assert np.allclose(parsed.conf_map, sample.pred_conf, atol=1e-6)
    assert np.array_equal(parsed.ignore_mask, sample.pred_ignore)


def test_parse_is_byte_reproducible(cfg):
    sample = generate_sample(cfg)
    for blob in (sample.pred_v1, sample.pred_v2, sample.pred_rle):
        a = read_mask(blob, cfg)
        b = read_mask(blob, cfg)
        assert a.canonical_bytes() == b.canonical_bytes()
        assert a.checksum() == b.checksum()


def test_unknown_binary_version(cfg):
    sample = generate_sample(cfg)
    bad = bytearray(sample.pred_v1)
    bad[4] = 99
    with pytest.raises(PanoError) as exc:
        read_mask(bytes(bad), cfg)
    assert exc.value.code == E_VERSION_UNSUPPORTED
    assert exc.value.details["version"] == 99


def test_unknown_magic(cfg):
    sample = generate_sample(cfg)
    bad = b"XXXX" + sample.pred_v1[4:]
    with pytest.raises(PanoError) as exc:
        read_mask(bad, cfg)
    assert exc.value.code == E_VERSION_UNSUPPORTED


def test_unknown_rle_version(cfg):
    with pytest.raises(PanoError) as exc:
        read_mask({"version": 42, "width": 4, "height": 4}, cfg)
    assert exc.value.code == E_VERSION_UNSUPPORTED


def test_truncated_header_reports_offset(cfg):
    sample = generate_sample(cfg)
    with pytest.raises(PanoError) as exc:
        read_mask(sample.pred_v1[: HEADER_SIZE - 3], cfg)
    assert exc.value.code == E_FORMAT_CORRUPT
    assert exc.value.details["field_offset"] == HEADER_SIZE - 3


def test_pixel_total_mismatch(cfg):
    sample = generate_sample(cfg)
    with pytest.raises(PanoError) as exc:
        read_mask(sample.pred_v1 + b"\x00", cfg)   # one byte too many
    assert exc.value.code == E_FORMAT_CORRUPT
    assert exc.value.details["extra_bytes"] == 1
    with pytest.raises(PanoError) as exc:
        read_mask(sample.pred_v1[:-1], cfg)        # one byte too few
    assert exc.value.code == E_FORMAT_CORRUPT


def test_rle_run_out_of_bounds_reports_offset(cfg):
    doc = json.loads(generate_sample(cfg).pred_rle)
    doc["segments"][0]["rle"]["counts"][1] += 10 ** 9   # inflate first fg run
    with pytest.raises(PanoError) as exc:
        read_mask(doc, cfg)
    assert exc.value.code == E_FORMAT_CORRUPT
    assert "segments[0].rle.counts[1]" == exc.value.details["field_offset"]


def test_rle_pixel_total_mismatch(cfg):
    doc = json.loads(generate_sample(cfg).pred_rle)
    doc["ignore"]["counts"] = [1, 1]               # sums to 2, not W*H
    with pytest.raises(PanoError) as exc:
        read_mask(doc, cfg)
    assert exc.value.code == E_FORMAT_CORRUPT
    assert exc.value.details["decoded"] == 2


def test_rle_encode_decode_edge_cases():
    assert rle_encode(np.zeros((3, 3), dtype=np.uint8)) == [9]
    assert rle_encode(np.ones((3, 3), dtype=np.uint8)) == [0, 9]
