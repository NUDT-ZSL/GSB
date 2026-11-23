"""v2 分片 JSON + SQLite 元数据库的解析与校验。

校验规则：
- 每个分片文件 sha256 与元数据库记录一致（E_HASH_MISMATCH）；
- 元数据中恢复代数相对代数下限单调不减（E_GENERATION_CONFLICT）；
- 绝对生效时刻为整数；
- 依赖上游服务存在、无自环（E_BAD_CONFIG）、依赖图无环（E_CYCLE）。
非法输入抛错，且不写入任何持久化状态（只读打开元数据库）。
"""

import hashlib
import json
import os
import sqlite3

from .errors import BizError, ErrCode
from .models import Action, Service


def _sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_v2(shard_dir, meta_path):
    """加载并校验 v2 配置，返回 dict: name -> Service（source_version='v2'）。"""
    # 只读模式打开，保证校验失败也不会写入任何持久化状态
    uri = "file:%s?mode=ro" % os.path.abspath(meta_path)
    conn = sqlite3.connect(uri, uri=True)
    try:
        shard_rows = conn.execute(
            "SELECT shard_id, sha256 FROM shards ORDER BY shard_id").fetchall()
        floor_rows = conn.execute(
            "SELECT scope, generation_floor FROM generation_floor").fetchall()
    finally:
        conn.close()
    floors = {scope: floor for scope, floor in floor_rows}

    services = {}
    for shard_id, expected_hash in shard_rows:
        shard_path = os.path.join(shard_dir, shard_id + ".json")
        if not os.path.exists(shard_path):
            raise BizError(ErrCode.E_HASH_MISMATCH, "分片文件缺失",
                           shard=shard_id, path=shard_path)
        actual = _sha256_file(shard_path)
        if actual != expected_hash:
            raise BizError(ErrCode.E_HASH_MISMATCH, "分片哈希不匹配",
                           shard=shard_id, expected=expected_hash, actual=actual)
        with open(shard_path, "r", encoding="utf-8") as fh:
            payload = json.load(fh)
        if payload.get("shard_id") != shard_id:
            raise BizError(ErrCode.E_BAD_CONFIG, "分片内容与 shard_id 不符",
                           shard=shard_id, got=payload.get("shard_id"))
        for entry in payload.get("services", []):
            name = entry.get("name")
            if not isinstance(name, str) or not name:
                raise BizError(ErrCode.E_BAD_CONFIG, "v2 服务规范名缺失", entry=entry)
            if name in services:
                raise BizError(ErrCode.E_BAD_CONFIG, "v2 服务规范名重复", service=name)
            eff = entry.get("effective_ms")
            if not isinstance(eff, int) or isinstance(eff, bool):
                raise BizError(ErrCode.E_BAD_CONFIG, "v2 绝对生效时刻必须为整数毫秒",
                               service=name, got=eff)
            gen = entry.get("generation")
            if not isinstance(gen, int) or isinstance(gen, bool):
                raise BizError(ErrCode.E_BAD_CONFIG, "v2 恢复代数必须为整数",
                               service=name, got=gen)
            floor = floors.get("global", 0)
            if gen < floor:
                raise BizError(ErrCode.E_GENERATION_CONFLICT,
                               "v2 恢复代数低于代数下限（非单调）",
                               service=name, generation=gen, floor=floor)
            actions = []
            prev_tick = None
            for idx, a in enumerate(entry.get("actions", [])):
                tick = a.get("at_ms")
                if not isinstance(tick, int) or isinstance(tick, bool):
                    raise BizError(ErrCode.E_BAD_CONFIG, "v2 动作 at_ms 必须为整数",
                                   service=name, action=a.get("id"))
                if prev_tick is not None and tick < prev_tick:
                    raise BizError(ErrCode.E_BAD_CONFIG, "v2 动作步骤必须非降序",
                                   service=name, action=a.get("id"))
                prev_tick = tick
                actions.append(Action(
                    action_id=a["id"], kind=a.get("kind", "switch"), at_tick=tick,
                    script=a.get("script", ""), source_version="v2",
                    priority=a.get("priority", ""), seq=idx,
                ))
            services[name] = Service(
                name=name,
                failure_domain=entry.get("failure_domain", ""),
                actions=actions,
                upstreams=list(entry.get("upstreams", [])),
                tombstoned=bool(entry.get("tombstoned", False)),
                source_version="v2",
                effective_ms=eff,
                generation=gen,
                ladder=list(entry.get("ladder", [10, 25, 50, 100])),
            )

    # 依赖上游存在性、自环与环校验
    for name, svc in services.items():
        for up in svc.upstreams:
            if up == name:
                raise BizError(ErrCode.E_BAD_CONFIG, "依赖图存在自环", service=name)
            if up not in services:
                raise BizError(ErrCode.E_BAD_CONFIG, "依赖上游服务不存在",
                               service=name, upstream=up)
    # 拓扑排序检测环
    from .models import Plan
    Plan(services=services).topo_order()

    return {"services": services}
