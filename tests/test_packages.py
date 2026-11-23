import hashlib
import json
import os

import pytest

from recovery_engine.errors import BizError, ErrCode
from recovery_engine.packages import (apply_full_package, classify_package,
                                      load_manifest)


def make_package(tmp_path, version, generation_floor=2, pkg_type="full",
                 shards=None, name="pkg"):
    pkg_dir = tmp_path / name
    pkg_dir.mkdir()
    shard_entries = []
    for shard_id, services in (shards or {"shard_001": []}).items():
        content = json.dumps({"shard_id": shard_id, "services": services},
                             sort_keys=True, indent=2) + "\n"
        (pkg_dir / (shard_id + ".json")).write_text(content, encoding="utf-8")
        shard_entries.append({
            "id": shard_id,
            "sha256": hashlib.sha256(content.encode()).hexdigest()})
    manifest = {"version": version, "type": pkg_type,
                "generation_floor": generation_floor, "shards": shard_entries}
    (pkg_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return str(pkg_dir)


def test_full_replace_switches_pointer(store, tmp_path):
    pkg = make_package(tmp_path, version=1)
    manifest = load_manifest(pkg)
    assert classify_package(store, manifest) == "full_replace"
    apply_full_package(store, pkg, plan_json='{"services":{}}')
    assert store.get_plan_pointer() == 1
    # 第二版整体替换：暂存新版本 + 原子切换指针
    pkg2 = make_package(tmp_path, version=2, name="pkg2")
    apply_full_package(store, pkg2, plan_json='{"services":{"a":{}}}')
    assert store.get_plan_pointer() == 2
    assert json.loads(store.current_plan()["plan_json"]) == {"services": {"a": {}}}
    # 旧版本仍完整保留（非逐条覆盖）
    assert store.get_plan_version(1) is not None


def test_noop_when_same_version_and_hashes(store, tmp_path):
    pkg = make_package(tmp_path, version=1)
    apply_full_package(store, pkg, plan_json="{}")
    assert classify_package(store, load_manifest(pkg)) == "noop"


def test_incremental_classified(store, tmp_path):
    apply_full_package(store, make_package(tmp_path, version=1), plan_json="{}")
    pkg2 = make_package(tmp_path, version=2, pkg_type="incremental", name="pkg2")
    assert classify_package(store, load_manifest(pkg2)) == "incremental"


def test_version_rollback_rejected(store, tmp_path):
    apply_full_package(store, make_package(tmp_path, version=2), plan_json="{}")
    old = load_manifest(make_package(tmp_path, version=1, name="old"))
    with pytest.raises(BizError) as exc:
        classify_package(store, old)
    assert exc.value.code == ErrCode.E_VERSION_ROLLBACK
    assert exc.value.details["current"] == 2
    assert store.get_plan_pointer() == 2  # 状态不变


def test_generation_floor_regression_rejected(store, tmp_path):
    apply_full_package(store, make_package(tmp_path, version=1, generation_floor=3),
                       plan_json="{}")
    pkg2 = make_package(tmp_path, version=2, generation_floor=2, name="pkg2")
    with pytest.raises(BizError) as exc:
        classify_package(store, load_manifest(pkg2))
    assert exc.value.code == ErrCode.E_GENERATION_CONFLICT


def test_package_shard_hash_mismatch(store, tmp_path):
    pkg_dir = make_package(tmp_path, version=1)
    shard_path = os.path.join(pkg_dir, "shard_001.json")
    with open(shard_path, "a", encoding="utf-8") as fh:
        fh.write("tampered")
    with pytest.raises(BizError) as exc:
        apply_full_package(store, pkg_dir, plan_json="{}")
    assert exc.value.code == ErrCode.E_HASH_MISMATCH
    assert store.get_plan_pointer() is None  # 未写入任何计划版本
