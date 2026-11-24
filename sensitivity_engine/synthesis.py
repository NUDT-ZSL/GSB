"""Pure effective-level synthesis."""

from __future__ import annotations

from typing import Any

from .codes import Code
from .domain import LEVEL_PRIORITY
from .hashing import marker, stable_digest
from .hierarchy import GroupGraph, object_inheritance_chain
from .models import GroupEdge, ObjectDecl, SourceGrant


def source_is_active(
    grant: SourceGrant, as_of_clock: int, revoked_keys: set[str]
) -> bool:
    if grant.source_key in revoked_keys:
        return False
    if as_of_clock < grant.valid_from:
        return False
    if grant.valid_to is not None and as_of_clock > grant.valid_to:
        return False
    return True


def build_graph(edges: list[GroupEdge] | set[tuple[str, str]] | list[tuple[str, str]]) -> GroupGraph:
    return GroupGraph(edges)


def descendant_objects(
    source_object_key: str, objects_by_key: dict[str, ObjectDecl]
) -> set[str]:
    children: dict[str, list[ObjectDecl]] = {}
    for obj in objects_by_key.values():
        if obj.parent_key is not None:
            children.setdefault(obj.parent_key, []).append(obj)

    result = {source_object_key}
    stack = [source_object_key]
    while stack:
        parent_key = stack.pop()
        for child in sorted(children.get(parent_key, []), key=lambda item: item.object_key):
            if child.inheritance_mode != "ALLOW":
                continue
            if child.object_key in result:
                continue
            result.add(child.object_key)
            stack.append(child.object_key)
    return result


def _source_evidence(
    grant: SourceGrant,
    *,
    target_object_key: str,
    member_trustee: str,
    expansion: dict[str, object],
) -> dict[str, Any]:
    path = list(expansion.get("path") or [grant.trustee_id, member_trustee])  # type: ignore[arg-type]
    tainted = bool(expansion.get("tainted"))
    path_marker = marker("gpm", ">".join(path))
    return {
        "source_key": grant.source_key,
        "grant_marker": grant.grant_marker,
        "level": grant.level,
        "priority": LEVEL_PRIORITY[grant.level],
        "origin_object_key": grant.object_key,
        "target_object_key": target_object_key,
        "inherited": grant.object_key != target_object_key,
        "granted_to": grant.trustee_id,
        "member_trustee": member_trustee,
        "trustee_path": path,
        "path_marker": path_marker,
        "valid_from": grant.valid_from,
        "valid_to": grant.valid_to,
        "cycle_tainted": tainted,
    }


def synthesize_all(
    *,
    objects: list[ObjectDecl],
    grants: list[SourceGrant],
    edges: list[GroupEdge] | list[tuple[str, str]],
    as_of_clock: int,
    revoked_keys: set[str] | None = None,
) -> dict[str, dict[str, Any]]:
    revoked = revoked_keys or set()
    objects_by_key = {obj.object_key: obj for obj in objects}
    graph = build_graph(edges)
    cycles = graph.detect_cycles()
    cyclic_nodes = graph.cyclic_nodes

    result: dict[str, dict[str, Any]] = {}
    for object_key in sorted(objects_by_key):
        obj = objects_by_key[object_key]
        chain = object_inheritance_chain(object_key, objects_by_key)
        chain_keys = {item.object_key for item in chain.objects}
        active_sources: list[SourceGrant] = []
        excluded_sources: list[dict[str, Any]] = []

        for grant in sorted(grants, key=lambda item: item.source_key):
            if grant.object_key not in chain_keys:
                continue
            if grant.source_key in revoked:
                excluded_sources.append(
                    {"source_key": grant.source_key, "reason": "REVOKED", "grant_marker": grant.grant_marker}
                )
                continue
            if as_of_clock < grant.valid_from:
                excluded_sources.append(
                    {
                        "source_key": grant.source_key,
                        "reason": Code.OUTSIDE_WINDOW,
                        "grant_marker": grant.grant_marker,
                    }
                )
                continue
            if grant.valid_to is not None and as_of_clock > grant.valid_to:
                excluded_sources.append(
                    {
                        "source_key": grant.source_key,
                        "reason": Code.OUTSIDE_WINDOW,
                        "grant_marker": grant.grant_marker,
                    }
                )
                continue
            active_sources.append(grant)

        trustee_basis: dict[str, list[dict[str, Any]]] = {}
        unexpanded_sources: list[dict[str, Any]] = []
        for grant in active_sources:
            expansion_map = graph.expansion_for_grant(grant.trustee_id, cyclic_nodes)
            if not expansion_map:
                unexpanded_sources.append(
                    {
                        "source_key": grant.source_key,
                        "grant_marker": grant.grant_marker,
                        "level": grant.level,
                        "granted_to": grant.trustee_id,
                        "origin_object_key": grant.object_key,
                        "reason": "NO_REACHABLE_MEMBER",
                    }
                )
            for member_trustee in sorted(expansion_map):
                expansion = expansion_map[member_trustee]
                evidence = _source_evidence(
                    grant,
                    target_object_key=object_key,
                    member_trustee=member_trustee,
                    expansion=expansion,
                )
                trustee_basis.setdefault(member_trustee, []).append(evidence)

        trustee_rows: list[dict[str, Any]] = []
        for trustee_id in sorted(trustee_basis):
            sources = sorted(trustee_basis[trustee_id], key=lambda item: item["source_key"])
            clean_sources = [item for item in sources if not item["cycle_tainted"]]
            tainted = any(item["cycle_tainted"] for item in sources)
            source_digest = stable_digest(
                [
                    (item["source_key"], item["level"], item["origin_object_key"])
                    for item in sources
                ]
            )
            if not clean_sources and tainted:
                row = {
                    "object_key": object_key,
                    "trustee_id": trustee_id,
                    "effective_level": None,
                    "decision_status": "UNDECIDABLE",
                    "codes": [Code.GROUP_CYCLE],
                    "basis_digest": source_digest,
                    "basis": {"sources": sources},
                    "watermark_clock": 0,
                    "watermark_batch_id": "BASE",
                    "watermark_op_seq": 0,
                }
            else:
                highest = max(item["priority"] for item in clean_sources)
                level = next(
                    item["level"]
                    for item in sorted(clean_sources, key=lambda item: (-item["priority"], item["source_key"]))
                    if item["priority"] == highest
                )
                codes: list[str] = []
                if tainted:
                    codes.append(Code.GROUP_CYCLE)
                row = {
                    "object_key": object_key,
                    "trustee_id": trustee_id,
                    "effective_level": level,
                    "decision_status": "LEVEL",
                    "codes": codes,
                    "basis_digest": source_digest,
                    "basis": {"sources": sources},
                    "watermark_clock": 0,
                    "watermark_batch_id": "BASE",
                    "watermark_op_seq": 0,
                }
            trustee_rows.append(row)

        level_rows = [row for row in trustee_rows if row["decision_status"] == "LEVEL"]
        undecidable_rows = [row for row in trustee_rows if row["decision_status"] == Code.UNDECIDABLE]
        object_codes: list[str] = []
        if chain.cycle:
            object_codes.append(Code.OBJECT_CYCLE)

        if not trustee_rows:
            status = "NO_BASIS"
            effective_level = None
            object_codes.append(Code.NO_BASIS)
        elif level_rows:
            effective_level = max(
                (row["effective_level"] for row in level_rows),
                key=lambda value: LEVEL_PRIORITY[value],
            )
            status = "LEVEL"
            if undecidable_rows:
                object_codes.append(Code.GROUP_CYCLE)
        else:
            effective_level = None
            status = "UNDECIDABLE"
            object_codes.append(Code.GROUP_CYCLE)

        object_digest = stable_digest(
            [
                (row["trustee_id"], row["basis_digest"], row["decision_status"])
                for row in trustee_rows
            ]
        )
        result[object_key] = {
            "object_key": object_key,
            "effective_level": effective_level,
            "decision_status": status,
            "codes": sorted(set(object_codes)),
            "basis_digest": object_digest,
            "basis": {"trustees": trustee_rows},
            "excluded_sources": sorted(excluded_sources, key=lambda item: item["source_key"]),
            "unexpanded_sources": sorted(unexpanded_sources, key=lambda item: item["source_key"]),
            "inheritance_chain": chain.objects and [item.object_key for item in chain.objects],
            "object_cycle": chain.cycle,
            "group_cycles": cycles,
            "watermark_clock": 0,
            "watermark_batch_id": "BASE",
            "watermark_op_seq": 0,
            "reentry_flag": False,
            "changed_flag": False,
            "reentry_reasons": [],
        }

    return result


def flatten_basis_source_keys(ledger_entry: dict[str, Any]) -> list[dict[str, Any]]:
    sources: dict[str, dict[str, Any]] = {}
    for trustee in ledger_entry.get("basis", {}).get("trustees", []):
        for source in trustee.get("basis", {}).get("sources", []):
            sources[source["source_key"]] = source
    return [sources[key] for key in sorted(sources)]
