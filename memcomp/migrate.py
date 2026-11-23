"""v1->v2->v3 升级迁移与 v3->v1 降级迁移。

升级迁移以“补全记录”驱动：迁移器先生成记录，再通过 apply_completion_records
应用到条目上，因此重放同一组记录必然得到逐字节一致的内部表示。
降级迁移按显式声明的丢弃规则输出丢弃记录。
"""

from .errors import E_VERSION_UNSUPPORTED, MemError
from .model import MemoryEntry

DEFAULT_CONFIDENCE = (0.5, 1.0)
DEFAULT_REVISION = 1

# 显式声明的降级丢弃规则
DISCARD_RULES = {
    "chain_refs": "DROP_CHAIN_REFS:v1 无法表达链引用，整体丢弃",
    "merge_sources": "DROP_MERGE_SOURCES:v1 无法表达合并来源，整体丢弃",
    "revision": "DROP_REVISION:v1 无法表达逐条修订号，整体丢弃",
    "confidence": "DROP_CONFIDENCE:v1 无法表达置信区间，整体丢弃",
    "source_turns": "DROP_SOURCE_TURNS:v1 无法表达来源轮次集合，整体丢弃",
    "entry_id": "DROP_ENTRY_ID:v1 无法表达条目序号，整体丢弃",
}


def _record(entry_key, field, value, source):
    return {"entry": entry_key, "field": field, "value": value, "source": source}


def migrate_v1_to_v2(entries):
    """补全条目序号、来源轮次集合与摘要置信区间，返回 (entries, records)。"""
    records = []
    by_session = {}
    for entry in entries:
        by_session.setdefault(entry.session_id, []).append(entry)
    output = []
    for session_id in sorted(by_session):
        group = sorted(by_session[session_id], key=lambda e: (e.turn_start, e.turn_end))
        for seq, entry in enumerate(group, start=1):
            key = entry.key()
            source_turns = tuple(range(entry.turn_start, entry.turn_end + 1))
            records.append(_record(key, "entry_id", seq, f"assigned:seq:{session_id}"))
            records.append(
                _record(key, "source_turns", list(source_turns), "derived:turn_range")
            )
            records.append(
                _record(
                    key,
                    "confidence",
                    list(DEFAULT_CONFIDENCE),
                    "default:summary_confidence",
                )
            )
            output.append(
                entry.with_fields(
                    entry_id=seq,
                    source_turns=source_turns,
                    conf_lo=DEFAULT_CONFIDENCE[0],
                    conf_hi=DEFAULT_CONFIDENCE[1],
                )
            )
    return output, records


def migrate_v2_to_v3(entries):
    """补全链引用、合并来源与逐条修订号，返回 (entries, records)。"""
    records = []
    output = []
    for entry in entries:
        key = entry.key()
        records.append(_record(key, "chain_refs", [], "default:empty"))
        records.append(_record(key, "merge_sources", [], "default:empty"))
        records.append(_record(key, "revision", DEFAULT_REVISION, "default:initial"))
        output.append(
            entry.with_fields(chain_refs=(), merge_sources=(), revision=DEFAULT_REVISION)
        )
    return output, records


def migrate_v3_to_v1(entries):
    """按 DISCARD_RULES 丢弃 v1 无法表达的字段，返回 (entries, discard_records)。"""
    records = []
    output = []
    for entry in entries:
        key = entry.key()
        dropped = {
            "chain_refs": list(entry.chain_refs),
            "merge_sources": list(entry.merge_sources),
            "revision": entry.revision,
            "confidence": [entry.conf_lo, entry.conf_hi],
            "source_turns": list(entry.source_turns),
            "entry_id": entry.entry_id,
        }
        for field, value in dropped.items():
            records.append(
                {
                    "entry": key,
                    "field": field,
                    "dropped_value": value,
                    "rule": DISCARD_RULES[field],
                }
            )
        output.append(
            MemoryEntry(
                entry_id=0,
                session_id=entry.session_id,
                turn_start=entry.turn_start,
                turn_end=entry.turn_end,
                summary_tokens=entry.summary_tokens,
                importance=entry.importance,
            )
        )
    return output, records


def apply_completion_records(v1_entries, records):
    """把补全记录重放到 v1 读取结果上，产出迁移后的内部表示。"""
    by_key = {}
    for entry in v1_entries:
        by_key.setdefault(entry.key(), entry)
    updated = {}
    for rec in records:
        key = rec["entry"]
        entry = updated.get(key, by_key[key])
        field, value = rec["field"], rec["value"]
        if field == "entry_id":
            entry = entry.with_fields(entry_id=int(value))
        elif field == "source_turns":
            entry = entry.with_fields(source_turns=tuple(int(x) for x in value))
        elif field == "confidence":
            entry = entry.with_fields(conf_lo=float(value[0]), conf_hi=float(value[1]))
        elif field == "chain_refs":
            entry = entry.with_fields(chain_refs=tuple(int(x) for x in value))
        elif field == "merge_sources":
            entry = entry.with_fields(merge_sources=tuple(str(x) for x in value))
        elif field == "revision":
            entry = entry.with_fields(revision=int(value))
        else:
            raise MemError(E_VERSION_UNSUPPORTED, f"未知补全字段: {field}")
        updated[key] = entry
    order = {e.key(): i for i, e in enumerate(v1_entries)}
    return [updated[k] for k in sorted(updated, key=lambda k: order[k])]


def migrate(entries, from_version, to_version):
    """双向迁移入口，返回 (entries, records)。"""
    path = (from_version, to_version)
    if path == (1, 2):
        return migrate_v1_to_v2(entries)
    if path == (2, 3):
        return migrate_v2_to_v3(entries)
    if path == (3, 1):
        return migrate_v3_to_v1(entries)
    if path == (1, 3):
        mid, rec1 = migrate_v1_to_v2(entries)
        out, rec2 = migrate_v2_to_v3(mid)
        return out, rec1 + rec2
    if from_version == to_version:
        return list(entries), []
    raise MemError(
        E_VERSION_UNSUPPORTED, f"不支持的迁移路径: v{from_version} -> v{to_version}"
    )
