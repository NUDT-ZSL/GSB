"""策略补丁应用、依赖边影响闭包与全量快照替换校验。"""
from __future__ import annotations

import copy
import re
from typing import Any, Dict, List, Optional, Set, Tuple

from .canon import digest
from .errors import Code, GsbError


def node_core(node: Dict[str, Any]) -> Dict[str, Any]:
    """参与内容比较的策略节点内核（忽略合并裁决元数据）。"""
    return {
        "instance_id": node.get("instance_id"),
        "retention_deadline_ms": node.get("retention_deadline_ms"),
        "policy_clock_ms": node.get("policy_clock_ms"),
        "generation": node.get("generation"),
        "tombstone": bool(node.get("tombstone", False)),
        "segment_refs": sorted(node.get("segment_refs", [])),
        "directives": sorted(
            ({"segment_id": d.get("segment_id"), "action": d.get("action")}
             for d in node.get("directives", [])),
            key=lambda d: (d["segment_id"], d["action"]),
        ),
        "checkpoint_refs": sorted(node.get("checkpoint_refs", [])),
        "signature": node.get("signature"),
    }


def node_digest(node: Dict[str, Any]) -> str:
    return digest(node_core(node))


def parse_version(version: str) -> Tuple[int, ...]:
    if not isinstance(version, str):
        raise GsbError(Code.E_BAD_POLICY, "snapshot version must be a string", {"version": version})
    nums = re.findall(r"\d+", version)
    if not nums:
        raise GsbError(Code.E_BAD_POLICY, f"unparseable version: {version}")
    return tuple(int(x) for x in nums[:3])


def snapshot_node_to_merged(node: Dict[str, Any], origin: str = "snapshot") -> Dict[str, Any]:
    iid = node.get("instance_id")
    until = node.get("retention_until_ms", node.get("retention_deadline_ms"))
    if not isinstance(iid, str) or not iid:
        raise GsbError(Code.E_BAD_POLICY, "snapshot node missing instance_id")
    if not isinstance(until, int) or isinstance(until, bool):
        raise GsbError(Code.E_BAD_POLICY, f"snapshot node {iid} retention timestamp must be integer")
    return {
        "instance_id": iid,
        "winner_source": origin,
        "decision": {"basis": origin, "winner_format": origin,
                     "rule": f"delivered via {origin}"},
        "policy_clock_ms": node.get("policy_clock_ms"),
        "generation": node.get("generation"),
        "version": node.get("version"),
        "signature": node.get("signature"),
        "retention_deadline_ms": until,
        "time_representation": {"kind": "absolute_ms", "absolute_retention_until_ms": until},
        "tombstone": bool(node.get("tombstone", False)),
        "segment_refs": sorted(int(x) for x in node.get("segment_refs", [])),
        "directives": list(node.get("directives", [])),
        "checkpoint_refs": list(node.get("checkpoint_refs", [])),
        "sources": [{"format": origin, "retention_deadline_ms": until}],
    }


def apply_patch(merged: Dict[str, Any], patch_doc: Dict[str, Any]) -> Tuple[Dict[str, Any], List[str]]:
    """把补丁应用到当前合并结果，返回新合并结果与被改动实例集合。"""
    if not isinstance(patch_doc, dict) or not isinstance(patch_doc.get("changes"), list):
        raise GsbError(Code.E_BAD_POLICY, "patch must contain a changes list")
    new_merged = copy.deepcopy(merged)
    nodes = new_merged.setdefault("nodes", [])
    by_id = {n["instance_id"]: n for n in nodes}
    changed: List[str] = []

    for ch in patch_doc["changes"]:
        op = ch.get("op")
        if op == "upsert_node":
            incoming = ch.get("node")
            if not isinstance(incoming, dict):
                raise GsbError(Code.E_BAD_POLICY, "upsert_node change requires node object")
            merged_node = snapshot_node_to_merged(incoming, origin="patch")
            iid = merged_node["instance_id"]
            old = by_id.get(iid)
            if old is not None:
                merged_node["sources"] = old.get("sources", []) + merged_node["sources"]
                nodes.remove(old)
            nodes.append(merged_node)
            by_id[iid] = merged_node
            changed.append(iid)
        elif op == "tombstone":
            iid = ch.get("instance_id")
            node = by_id.get(iid)
            if node is None:
                node = {"instance_id": iid, "segment_refs": [], "directives": [],
                        "checkpoint_refs": [], "sources": [], "retention_deadline_ms": None}
                nodes.append(node)
                by_id[iid] = node
            node["tombstone"] = True
            node.setdefault("decision", {"basis": "patch_tombstone"})
            changed.append(iid)
        else:
            raise GsbError(Code.E_BAD_POLICY, f"unsupported patch op: {op}")

    nodes.sort(key=lambda n: n["instance_id"])
    new_merged["content_digest"] = digest({"nodes": nodes, "now": new_merged.get("injected_now_ms")})
    return new_merged, sorted(set(changed))


def affected_closure(
    *,
    changed_instances: List[str],
    new_merged: Dict[str, Any],
    old_merged: Optional[Dict[str, Any]],
    segment_ids: List[int],
    old_precision: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """沿 节点->段 治理边、段邻接边与既有精度批次边计算影响闭包。"""
    seg_set = set(segment_ids)
    new_by_id = {n["instance_id"]: n for n in new_merged.get("nodes", [])}
    old_by_id = {n["instance_id"]: n for n in (old_merged or {}).get("nodes", [])}

    dirty: Set[int] = set()
    edges: List[Dict[str, Any]] = []
    for iid in changed_instances:
        node = new_by_id.get(iid) or old_by_id.get(iid)
        if node is None:
            continue
        for sid in node.get("segment_refs", []):
            if sid in seg_set:
                dirty.add(int(sid))
                edges.append({"from": iid, "to": f"seg:{sid}", "type": "governs"})
        for cid in node.get("checkpoint_refs", []):
            edges.append({"from": iid, "to": f"cp:{cid}", "type": "checkpoint_dependency"})

    seed_segments = set(dirty)
    # 段邻接边：相邻段可能形成新的精度敏感对
    for sid in list(seed_segments):
        for nb in (sid - 1, sid + 1):
            if nb in seg_set:
                dirty.add(nb)
                edges.append({"from": f"seg:{sid}", "to": f"seg:{nb}", "type": "segment_adjacent"})

    # 既有精度批次边：批次结论必须成对重估
    for rec in old_precision or []:
        if not rec.get("coarse", {}).get("linked_as_batch"):
            continue
        a, b = rec.get("segments", [None, None])
        if a in seed_segments or b in seed_segments:
            dirty.add(a)
            dirty.add(b)
            edges.append({"from": f"seg:{a}", "to": f"seg:{b}", "type": "precision_batch"})

    reusable = sorted(seg_set - dirty)
    return {
        "dirty_segments": sorted(dirty),
        "reusable_segments": reusable,
        "edges": edges,
    }


def diff_snapshot_nodes(
    old_merged: Optional[Dict[str, Any]], incoming_nodes: List[Dict[str, Any]]
) -> Tuple[List[str], Set[int]]:
    """比较节点内核摘要，返回变更实例与涉及段（新旧引用并集）。"""
    old_by_id = {n["instance_id"]: n for n in (old_merged or {}).get("nodes", [])}
    new_norm = [snapshot_node_to_merged(n) for n in incoming_nodes]
    changed: List[str] = []
    touched_segments: Set[int] = set()
    new_ids = set()
    for node in new_norm:
        iid = node["instance_id"]
        new_ids.add(iid)
        old = old_by_id.get(iid)
        if old is None or node_digest(old) != node_digest(node):
            changed.append(iid)
            touched_segments.update(node.get("segment_refs", []))
            if old is not None:
                touched_segments.update(old.get("segment_refs", []))
    for iid, old in old_by_id.items():
        if iid not in new_ids:
            changed.append(iid)
            touched_segments.update(old.get("segment_refs", []))
    return sorted(changed), touched_segments


def validate_snapshot_doc(doc: Any) -> Dict[str, Any]:
    if not isinstance(doc, dict):
        raise GsbError(Code.E_BAD_POLICY, "snapshot must be an object")
    for key in ("version", "generation", "signatures", "content_digest", "nodes", "idempotency_key"):
        if key not in doc:
            raise GsbError(Code.E_BAD_POLICY, f"snapshot missing field: {key}", {"field": key})
    if not isinstance(doc["version"], str):
        raise GsbError(Code.E_BAD_POLICY, "version must be string")
    if not isinstance(doc["generation"], int) or isinstance(doc["generation"], bool) or doc["generation"] < 0:
        raise GsbError(Code.E_BAD_POLICY, "generation must be non-negative integer")
    sigs = doc["signatures"]
    if not isinstance(sigs, list) or not sigs or not all(isinstance(s, str) and s for s in sigs):
        raise GsbError(Code.E_BAD_POLICY, "signatures must be non-empty list of strings")
    if not isinstance(doc["nodes"], list) or not doc["nodes"]:
        raise GsbError(Code.E_BAD_POLICY, "nodes must be a non-empty list")
    expected = digest({k: v for k, v in doc.items() if k != "content_digest"})
    if doc["content_digest"] != expected:
        raise GsbError(
            Code.E_SNAPSHOT_CONFLICT,
            "snapshot content_digest verification failed",
            {"expected": expected, "provided": doc["content_digest"]},
        )
    parse_version(doc["version"])
    return doc
