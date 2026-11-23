"""配置包整体替换识别。

比较清单版本号、恢复代数下限与分片哈希，判定：
- full_replace：完整替换（暂存新版本 + 原子切换计划指针）；
- incremental：增量追加（按补丁应用）；
- noop：无需操作；
- 版本回退：拒绝（E_VERSION_ROLLBACK）。
"""

import hashlib
import json
import os

from .errors import BizError, ErrCode


def _sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_manifest(package_dir):
    with open(os.path.join(package_dir, "manifest.json"), "r", encoding="utf-8") as fh:
        manifest = json.load(fh)
    for field in ("version", "type", "generation_floor", "shards"):
        if field not in manifest:
            raise BizError(ErrCode.E_BAD_CONFIG, "配置包清单缺少字段", field=field)
    return manifest


def classify_package(store, manifest):
    """判定配置包类别：full_replace | incremental | noop；版本回抛错。"""
    current = store.current_plan()
    if current is None:
        return "full_replace" if manifest["type"] == "full" else "incremental"
    current_manifest = json.loads(current["manifest_json"])
    current_version = current_manifest["version"]
    if manifest["version"] < current_version:
        raise BizError(ErrCode.E_VERSION_ROLLBACK, "配置包版本回退，拒绝应用",
                       current=current_version, got=manifest["version"])
    if (manifest["version"] == current_version
            and manifest.get("shards") == current_manifest.get("shards")
            and manifest.get("generation_floor") == current_manifest.get("generation_floor")):
        return "noop"
    if manifest["generation_floor"] < current_manifest.get("generation_floor", 0):
        raise BizError(ErrCode.E_GENERATION_CONFLICT, "配置包代数下限回退",
                       current=current_manifest.get("generation_floor"),
                       got=manifest["generation_floor"])
    return "full_replace" if manifest["type"] == "full" else "incremental"


def apply_full_package(store, package_dir, plan_json):
    """完整替换：校验分片哈希后暂存新版本并原子切换计划指针。"""
    manifest = load_manifest(package_dir)
    for shard in manifest["shards"]:
        path = os.path.join(package_dir, shard["id"] + ".json")
        actual = _sha256_file(path)
        if actual != shard["sha256"]:
            raise BizError(ErrCode.E_HASH_MISMATCH, "配置包分片哈希不匹配",
                           shard=shard["id"], expected=shard["sha256"], actual=actual)
    store.stage_and_switch_plan(
        manifest["version"],
        json.dumps(manifest, sort_keys=True, ensure_ascii=False),
        plan_json,
    )
    return manifest["version"]
