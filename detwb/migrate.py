"""三套书写形式的双向迁移器。

v1 -> v2/v3：补全类别名、难度标记与置信度区间默认值，逐项记录补全来源。
v3 -> v1：对 v1 无法表达的字段按显式声明的丢弃规则处理并输出丢弃明细。
迁移不得改变已能表达字段的取值。
"""

from .errors import Err, WorkbenchError
from .formats import V1_FIELDS

V1_INEXPRESSIBLE = ("class_name", "difficulty", "occluded",
                    "conf_lo", "conf_hi", "vocab_version")

FIELD_DEFAULTS = {
    "difficulty": "unknown",
    "occluded": False,
    "conf_lo": 0.0,
    "conf_hi": 1.0,
}

# 显式声明的 v1 丢弃规则：字段 -> 规则标识
DEFAULT_DROP_RULES = {
    "class_name": "DROP_DERIVABLE_FROM_VOCAB",
    "difficulty": "DROP_V1_INEXPRESSIBLE",
    "occluded": "DROP_V1_INEXPRESSIBLE",
    "conf_lo": "DROP_V1_INEXPRESSIBLE",
    "conf_hi": "DROP_V1_INEXPRESSIBLE",
    "vocab_version": "DROP_HEADER_CARRIED",
}


def _key(rec):
    return f"{rec['sample_id']}#{rec['cand_idx']}"


def migrate_v1_to_rich(v1_records, vocab_snapshot):
    """v1 记录升级为全量字段。返回 (rich_records, provenance)。"""
    rich = []
    provenance = []
    for rec in v1_records:
        out = {}
        for f in V1_FIELDS:
            out[f] = rec[f]
            provenance.append({"record": _key(rec), "field": f, "source": "v1"})
        cid = int(rec["class_id"])
        out["class_name"] = vocab_snapshot.name_of(cid)
        provenance.append({"record": _key(rec), "field": "class_name",
                           "source": "vocab"})
        for f, default in FIELD_DEFAULTS.items():
            out[f] = default
            provenance.append({"record": _key(rec), "field": f,
                               "source": "default"})
        out["vocab_version"] = int(rec.get("vocab_version", vocab_snapshot.version))
        provenance.append({"record": _key(rec), "field": "vocab_version",
                           "source": "header"})
        rich.append(out)
    return rich, provenance


def migrate_rich_to_v1(rich_records, drop_rules=None):
    """全量记录降级为 v1。返回 (v1_records, drop_manifest)。"""
    rules = dict(DEFAULT_DROP_RULES)
    if drop_rules:
        unknown = set(drop_rules) - set(DEFAULT_DROP_RULES)
        if unknown:
            raise WorkbenchError(Err.FORMAT_CORRUPT,
                                 f"未知丢弃规则字段: {sorted(unknown)}",
                                 field=sorted(unknown)[0])
        rules.update(drop_rules)
    v1_records = []
    drop_manifest = []
    for rec in rich_records:
        out = {f: rec[f] for f in V1_FIELDS}
        out["vocab_version"] = int(rec["vocab_version"])
        v1_records.append(out)
        for f in V1_INEXPRESSIBLE:
            value = rec.get(f)
            default = FIELD_DEFAULTS.get(f)
            drop_manifest.append({
                "record": _key(rec), "field": f, "value": value,
                "rule": rules[f],
                "restorable": f in ("class_name", "vocab_version")
                or value == default,
            })
    return v1_records, drop_manifest


def expressible_fields_unchanged(before_rich, after_rich):
    """校验迁移前后 v1 可表达字段取值不变。返回首个不一致项或 None。"""
    def key_of(r):
        return (r["sample_id"], r["cand_idx"])
    a = {key_of(r): r for r in before_rich}
    b = {key_of(r): r for r in after_rich}
    if set(a) != set(b):
        return {"field": "record_set", "detail": "记录集合不一致"}
    for k in sorted(a):
        for f in V1_FIELDS:
            if a[k][f] != b[k][f]:
                return {"record": f"{k[0]}#{k[1]}", "field": f,
                        "before": a[k][f], "after": b[k][f]}
    return None
