"""语料去重模块。

- 主键 = 用例内容字节流的 SHA-256（来源字段不参与主键）；
- 冲突合并：按 (轮次, 用例ID) 升序确定规范记录，来源列表合并排序；
- 合并结果确定性，重复执行幂等。
"""
from __future__ import annotations

import hashlib


def content_key(case: dict) -> str:
    """内容主键：仅由用例字节内容决定，与 source 等元数据无关。"""
    return hashlib.sha256(bytes.fromhex(case["data_hex"])).hexdigest()


def dedup(cases: list[dict]) -> list[dict]:
    """按内容主键去重合并。返回按用例ID升序的规范用例列表。"""
    merged: dict[str, dict] = {}
    # 冲突合并顺序确定：先按 (round, id) 升序，先到者为规范记录
    for c in sorted(cases, key=lambda x: (x.get("round", 0), x["id"])):
        k = content_key(c)
        if k in merged:
            m = merged[k]
            if c["source"] not in m["sources"]:
                m["sources"].append(c["source"])
                m["sources"].sort()
        else:
            m = dict(c)
            m["sources"] = [c["source"]]
            m["content_key"] = k
            merged[k] = m
    return sorted(merged.values(), key=lambda x: x["id"])
