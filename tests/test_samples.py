"""需求 1：样本解析校验，非法输入一律 E_BAD_SAMPLE 且不落盘。"""
import base64
import hashlib
import json

import pytest

from chunkmeta.errors import E_BAD_SAMPLE, StoreError
from chunkmeta.samples import load_samples, parse_config


def write_samples(tmp_path, config, objects, buckets):
    (tmp_path / "config.json").write_text(json.dumps(config), encoding="utf-8")
    (tmp_path / "objects.json").write_text(json.dumps(objects), encoding="utf-8")
    (tmp_path / "buckets.json").write_text(json.dumps(buckets), encoding="utf-8")


def good_config():
    return {
        "max_chunk_size": 64,
        "cache_max_entries": 2,
        "read_latency_budget_ms": 10.0,
        "fixed_scale": 100,
        "compare_tolerance": 0.1,
        "error_bound_budget": 0.5,
        "clock_step_ms": 5,
    }


def good_object():
    content = b"hello-chunk"
    return {
        "object_key": "docs/a.txt",
        "version": 1,
        "chunks": [{
            "seq": 0,
            "content_b64": base64.b64encode(content).decode(),
            "size": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
        }],
    }


def good_bucket():
    return {"bucket": "media-store", "consistency": "strong",
            "metadata": {"owner": "dev"}, "clock_ms": 100}


def expect_bad(tmp_path, config, objects, buckets, **ctx):
    write_samples(tmp_path, config, objects, buckets)
    with pytest.raises(StoreError) as exc:
        load_samples(tmp_path)
    assert exc.value.code == E_BAD_SAMPLE
    # 不写入任何持久化状态：样本目录只有三个输入文件
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        "buckets.json", "config.json", "objects.json"]
    return exc.value


def test_valid_samples_load(tmp_path):
    write_samples(tmp_path, good_config(), [good_object()], [good_bucket()])
    manifests, buckets, config = load_samples(tmp_path)
    assert manifests[0].object_key == "docs/a.txt"
    assert buckets[0].bucket == "media-store"
    assert config.fixed_scale == 100


def test_bad_object_key(tmp_path):
    obj = good_object()
    obj["object_key"] = "../escape"
    expect_bad(tmp_path, good_config(), [obj], [good_bucket()])


def test_bad_bucket_name(tmp_path):
    bucket = good_bucket()
    bucket["bucket"] = "UPPER_CASE"
    expect_bad(tmp_path, good_config(), [good_object()], [bucket])


def test_non_contiguous_seq(tmp_path):
    obj = good_object()
    obj["chunks"][0]["seq"] = 1
    expect_bad(tmp_path, good_config(), [obj], [good_bucket()])


def test_digest_mismatch_in_sample(tmp_path):
    obj = good_object()
    obj["chunks"][0]["sha256"] = "0" * 64
    expect_bad(tmp_path, good_config(), [obj], [good_bucket()])


def test_chunk_size_exceeds_limit(tmp_path):
    config = good_config()
    config["max_chunk_size"] = 4
    expect_bad(tmp_path, config, [good_object()], [good_bucket()])


def test_chunk_size_not_positive(tmp_path):
    obj = good_object()
    obj["chunks"][0]["size"] = 0
    expect_bad(tmp_path, good_config(), [obj], [good_bucket()])


def test_bad_metadata_value_type(tmp_path):
    bucket = good_bucket()
    bucket["metadata"]["nested"] = {"illegal": "dict"}
    expect_bad(tmp_path, good_config(), [good_object()], [bucket])


def test_non_positive_scale(tmp_path):
    config = good_config()
    config["fixed_scale"] = 0
    expect_bad(tmp_path, config, [good_object()], [good_bucket()])


def test_non_positive_tolerance(tmp_path):
    config = good_config()
    config["compare_tolerance"] = -1
    expect_bad(tmp_path, config, [good_object()], [good_bucket()])


def test_parse_config_missing_field():
    with pytest.raises(StoreError) as exc:
        parse_config({"max_chunk_size": 1})
    assert exc.value.code == E_BAD_SAMPLE
