"""内部统一记忆条目表示与逐字节稳定的规范化序列化。"""

import hashlib
import json
from dataclasses import dataclass, field, replace


def fmt_float(value):
    """浮点数以定点格式参与规范化表示，保证逐字节稳定。"""
    return f"{float(value):.6f}"


@dataclass(frozen=True)
class MemoryEntry:
    entry_id: int
    session_id: str
    turn_start: int
    turn_end: int
    source_turns: tuple = ()
    summary_tokens: tuple = ()
    conf_lo: float = 0.0
    conf_hi: float = 0.0
    importance: float = 0.0
    chain_refs: tuple = ()
    merge_sources: tuple = ()
    revision: int = 0

    def key(self):
        return f"{self.session_id}|{self.turn_start}-{self.turn_end}"

    def with_fields(self, **kwargs):
        return replace(self, **kwargs)


def canonical_dict(entry):
    return {
        "entry_id": entry.entry_id,
        "session_id": entry.session_id,
        "turn_range": [entry.turn_start, entry.turn_end],
        "source_turns": list(entry.source_turns),
        "summary_tokens": list(entry.summary_tokens),
        "confidence": [fmt_float(entry.conf_lo), fmt_float(entry.conf_hi)],
        "importance": fmt_float(entry.importance),
        "chain_refs": list(entry.chain_refs),
        "merge_sources": list(entry.merge_sources),
        "revision": entry.revision,
    }


def canonical_bytes(entry):
    return json.dumps(
        canonical_dict(entry), sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")


def entry_hash(entry):
    return hashlib.sha256(canonical_bytes(entry)).hexdigest()


def v1_projection(entry):
    """v1 可表达字段的投影，用于跨格式视图一致性比较。"""
    return {
        "session_id": entry.session_id,
        "turn_range": [entry.turn_start, entry.turn_end],
        "summary_tokens": list(entry.summary_tokens),
        "importance": fmt_float(entry.importance),
    }


def v2_projection(entry):
    proj = v1_projection(entry)
    proj.update(
        {
            "entry_id": entry.entry_id,
            "source_turns": list(entry.source_turns),
            "confidence": [fmt_float(entry.conf_lo), fmt_float(entry.conf_hi)],
        }
    )
    return proj


def projection_bytes(entries, projection):
    docs = [projection(e) for e in entries]
    return json.dumps(docs, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode(
        "utf-8"
    )
