from __future__ import annotations

import json

import pytest

from ckptlab.errors import BusinessError
from ckptlab.recovery import reconstruct_manifest

from conftest import manager_for, plan_v1, shard_policy


def _save(tmp_path):
    manager = manager_for(tmp_path)
    result = manager.save(plan_v1(), shard_policy())
    manifest = json.loads(
        (tmp_path / "store" / "manifests" / f"{result['manifest_digest']}.json").read_text(
            encoding="utf-8"
        )
    )
    return manager, result, manifest


def test_restore_has_layer_digest_chain_and_arrays(tmp_path):
    manager, result, _ = _save(tmp_path)
    output = tmp_path / "restored"
    restored = manager.restore(output_dir=output)
    assert restored["restored"] is True
    assert restored["layer_count"] == 8
    assert restored["shard_count"] == 4
    assert restored["reconstruction"]["agreement"] is True
    assert len({item["layer_digest"] for item in restored["layers"]}) == 8
    assert len(list(output.glob("*.npy"))) == 18
    assert (output / "restore.json").is_file()
    assert manager.verify()["chain_digest"] == restored["chain_digest"]


def test_missing_shard_has_distinct_code(tmp_path):
    manager, result, manifest = _save(tmp_path)
    shard = manifest["shards"][0]
    manager.store.object_path(shard["digest"]).unlink()
    with pytest.raises(BusinessError) as caught:
        manager.verify()
    assert caught.value.code == "SHARD_MISSING"
    assert caught.value.details["expected_digest"] == shard["digest"]


def test_digest_mismatch_has_distinct_code(tmp_path):
    manager, result, manifest = _save(tmp_path)
    shard = manifest["shards"][0]
    path = manager.store.object_path(shard["digest"])
    data = bytearray(path.read_bytes())
    data[-1] ^= 0xFF
    path.write_bytes(bytes(data))
    with pytest.raises(BusinessError) as caught:
        manager.verify()
    assert caught.value.code == "SHARD_DIGEST_MISMATCH"


def test_concat_order_mismatch_has_distinct_code(tmp_path):
    manager, result, manifest = _save(tmp_path)
    altered = json.loads(json.dumps(manifest))
    altered["shards"] = list(reversed(altered["shards"]))
    with pytest.raises(BusinessError) as caught:
        reconstruct_manifest(altered, manager.store)
    assert caught.value.code == "SHARD_ORDER_MISMATCH"


def test_manifest_parameter_digest_mismatch_has_distinct_code(tmp_path):
    manager, result, manifest = _save(tmp_path)
    altered = json.loads(json.dumps(manifest))
    altered["layers"][0]["param_digest"] = "0" * 64
    with pytest.raises(BusinessError) as caught:
        reconstruct_manifest(altered, manager.store)
    assert caught.value.code == "PARAMETER_DIGEST_MISMATCH"
