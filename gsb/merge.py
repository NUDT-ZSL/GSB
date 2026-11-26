"""v1 单文件策略与 v2 分片/SQLite 策略的跨版本合并。

同一回放实例同时存在于两代策略时，裁决顺序（全序，保证确定性）：

1. 策略声明的逻辑时钟 ``policy_clock_ms`` 更大者胜 —— ``basis=clock``
2. 时钟相同，保留代数 ``generation`` 更大者胜 —— ``basis=generation``
3. 时钟与代数都相同，按来源格式优先级 v2 > v1 —— ``basis=format_priority``

每个合并节点保留全部候选来源、原始时间表示、v1 相对秒数在注入时钟下的
绝对截止换算（含标度与舍入方向）以及明确的裁决依据。
"""
from __future__ import annotations

from typing import Any, Dict, List

from .canon import digest
from .config import EngineConfig
from .fixedpoint import convert_relative_seconds
from .model import Bundle

FORMAT_PRIORITY = {"v1": 1, "v2": 2}


def _v1_candidate(iid: str, node: Dict[str, Any], doc: Dict[str, Any],
                  cfg: EngineConfig) -> Dict[str, Any]:
    rel = node.get("relative_retention_seconds", doc.get("relative_retention_seconds"))
    clock = node.get("policy_clock_ms", doc.get("policy_clock_ms"))
    gen = node.get("generation", doc.get("generation"))
    version = node.get("version", doc.get("version"))
    conversion = convert_relative_seconds(cfg.now_ms, int(rel), cfg.deadline_scale)
    return {
        "format": "v1",
        "format_priority": FORMAT_PRIORITY["v1"],
        "snapshot_id": doc.get("snapshot_id"),
        "version": version,
        "policy_clock_ms": clock,
        "generation": gen,
        "signature": node.get("signature"),
        "retention_deadline_ms": conversion["absolute_deadline_ms"],
        "time_representation": conversion,
        "tombstone": False,
        "segment_refs": list(node.get("segment_refs", [])),
        "directives": list(node.get("directives", [])),
        "checkpoint_refs": list(node.get("checkpoint_refs", [])),
    }


def _v2_candidate(node: Any, meta: Dict[str, str]) -> Dict[str, Any]:
    return {
        "format": "v2",
        "format_priority": FORMAT_PRIORITY["v2"],
        "snapshot_id": meta.get("snapshot_id"),
        "version": meta.get("version"),
        "policy_clock_ms": node.policy_clock_ms,
        "generation": node.generation,
        "signature": node.signature,
        "retention_deadline_ms": node.retention_until_ms,
        "time_representation": {
            "kind": "absolute_ms",
            "absolute_retention_until_ms": node.retention_until_ms,
        },
        "tombstone": node.tombstone,
        "segment_refs": list(node.segment_refs),
        "directives": [dict(d) for d in node.directives],
        "checkpoint_refs": list(node.checkpoint_refs),
        "source_shard": node.source_shard,
    }


def _decide(cands: List[Dict[str, Any]]) -> Dict[str, Any]:
    """返回 (winner, decision)。cands 非空。"""
    if len(cands) == 1:
        w = cands[0]
        return w, {"basis": "single_source", "winner_format": w["format"],
                   "rule": "only one policy generation defines this instance"}

    by_clock = max(cands, key=lambda c: c["policy_clock_ms"])
    clocks = {c["policy_clock_ms"] for c in cands}
    if len(clocks) > 1:
        return by_clock, {
            "basis": "clock",
            "winner_format": by_clock["format"],
            "rule": "higher policy_clock_ms wins",
            "compared": [{"format": c["format"], "policy_clock_ms": c["policy_clock_ms"],
                          "generation": c["generation"]} for c in cands],
        }

    by_gen = max(cands, key=lambda c: c["generation"])
    gens = {c["generation"] for c in cands}
    if len(gens) > 1:
        return by_gen, {
            "basis": "generation",
            "winner_format": by_gen["format"],
            "rule": "clocks equal; higher generation wins",
            "compared": [{"format": c["format"], "policy_clock_ms": c["policy_clock_ms"],
                          "generation": c["generation"]} for c in cands],
        }

    winner = max(cands, key=lambda c: c["format_priority"])
    return winner, {
        "basis": "format_priority",
        "winner_format": winner["format"],
        "rule": "clock and generation equal; source format priority v2>v1 wins",
        "format_priority": FORMAT_PRIORITY,
        "compared": [{"format": c["format"], "policy_clock_ms": c["policy_clock_ms"],
                      "generation": c["generation"]} for c in cands],
    }


def merge_policies(bundle: Bundle, cfg: EngineConfig) -> Dict[str, Any]:
    v1_doc = bundle.v1_raw
    v1_instances: Dict[str, Any] = v1_doc.get("instances", {})
    v2_by_id = {n.instance_id: n for n in bundle.v2_nodes}

    all_ids = sorted(set(v1_instances) | set(v2_by_id))
    nodes: List[Dict[str, Any]] = []
    basis_count: Dict[str, int] = {}
    for iid in all_ids:
        cands: List[Dict[str, Any]] = []
        if iid in v1_instances:
            cands.append(_v1_candidate(iid, v1_instances[iid], v1_doc, cfg))
        if iid in v2_by_id:
            cands.append(_v2_candidate(v2_by_id[iid], bundle.v2_meta_raw))
        winner, decision = _decide(cands)
        basis_count[decision["basis"]] = basis_count.get(decision["basis"], 0) + 1
        node = {
            "instance_id": iid,
            "winner_source": winner["format"],
            "decision": decision,
            "policy_clock_ms": winner["policy_clock_ms"],
            "generation": winner["generation"],
            "version": winner.get("version") or bundle.v2_meta_raw.get("version")
            if winner["format"] == "v2" else winner.get("version"),
            "signature": winner.get("signature"),
            "retention_deadline_ms": winner["retention_deadline_ms"],
            "time_representation": winner["time_representation"],
            "tombstone": bool(winner["tombstone"]),
            "segment_refs": sorted(int(x) for x in winner["segment_refs"]),
            "directives": winner["directives"],
            "checkpoint_refs": winner["checkpoint_refs"],
            "sources": cands,
        }
        nodes.append(node)

    merged = {
        "merge_version": 1,
        "injected_now_ms": cfg.now_ms,
        "format_priority": FORMAT_PRIORITY,
        "basis_count": basis_count,
        "nodes": nodes,
    }
    merged["content_digest"] = digest({"nodes": nodes, "now": cfg.now_ms})
    return merged


def active_nodes(merged: Dict[str, Any]) -> List[Dict[str, Any]]:
    """未作废（非墓碑）的合并策略节点。"""
    return [n for n in merged["nodes"] if not n.get("tombstone")]


def node_map(merged: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    return {n["instance_id"]: n for n in merged["nodes"]}
