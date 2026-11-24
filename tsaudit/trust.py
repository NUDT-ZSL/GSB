"""Unified TSA trust view, temporal queries, conflicts and cycles."""

from __future__ import annotations

from typing import Any

from .canon import canonical_issuer_id, sha256_obj
from .codes import Code


SOURCE_RANK = {"v1": 1, "v2": 2, "update": 3}


def find_cycles(edges: dict[str, set[str]]) -> list[dict[str, Any]]:
    """Return stable cycle descriptions for a directed node graph."""

    cycles: dict[tuple[str, ...], list[str]] = {}
    color: dict[str, int] = {}
    stack: list[str] = []
    on_stack: set[str] = set()

    def canonical_rotation(nodes: list[str]) -> tuple[str, ...]:
        if not nodes:
            return ()
        rotations = [tuple(nodes[i:] + nodes[:i]) for i in range(len(nodes))]
        return min(rotations)

    def dfs(node: str) -> None:
        color[node] = 1
        stack.append(node)
        on_stack.add(node)
        for nxt in sorted(edges.get(node, set())):
            if nxt not in color:
                dfs(nxt)
            elif color.get(nxt) == 1 and nxt in on_stack:
                start = stack.index(nxt)
                path = stack[start:] + [nxt]
                key = tuple(sorted(set(path[:-1])))
                cycles[key] = list(canonical_rotation(path[:-1])) + [
                    canonical_rotation(path[:-1])[0]
                ]
        stack.pop()
        on_stack.remove(node)
        color[node] = 2

    for node in sorted(set(edges) | {n for targets in edges.values() for n in targets}):
        if node not in color:
            dfs(node)

    result = []
    for index, (nodes_key, path) in enumerate(sorted(cycles.items()), start=1):
        result.append({
            "cycle_id": f"cycle-{index:02d}-{sha256_obj(list(nodes_key))[:10]}",
            "code": Code.E_CYCLE_REFERENCE,
            "nodes": list(nodes_key),
            "cycle_path": path,
        })
    return result


def build_reference_graph(records: list[dict[str, Any]]) -> dict[str, set[str]]:
    edges: dict[str, set[str]] = {}
    for record in records:
        subject = f"issuer:{record['issuer_canonical_id']}"
        edges.setdefault(subject, set())
        for ref in record.get("references", []):
            edges[subject].add(ref)
            edges.setdefault(ref, set())
    return {key: set(values) for key, values in edges.items()}


def _authority_key(record: dict[str, Any]) -> tuple[Any, ...]:
    return (
        int(record["tsa_generation"]),
        int(record["registered_at_ms"]),
        int(record["effective_at_ms"]),
        1 if record["tombstone"] else 0,
        SOURCE_RANK.get(record["source_format"], 0),
        int(record.get("snapshot_basis_generation", 0)),
        record["record_key"],
    )


def _generation_bindings(record: dict[str, Any]) -> set[int]:
    bindings = set(record.get("authorized_token_generations") or [])
    if record.get("token_generation") is not None:
        bindings.add(int(record["token_generation"]))
    if not bindings:
        bindings.add(int(record["tsa_generation"]))
    return bindings


def _snapshot_summary(
    snapshot: dict[str, Any],
    records_by_key: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    records = [records_by_key[key] for key in snapshot.get("record_keys", []) if key in records_by_key]
    issuer_generation: dict[str, int] = {}
    revocation_set: set[str] = set()
    issuer_set: set[str] = set()
    for record in records:
        issuer = record["issuer_canonical_id"]
        issuer_set.add(issuer)
        issuer_generation[issuer] = max(issuer_generation.get(issuer, 0), record["tsa_generation"])
        if record["revoked"]:
            revocation_set.add(issuer)
    content = {
        "issuer_set": sorted(issuer_set),
        "tsa_generations": dict(sorted(issuer_generation.items())),
        "revocation_set": sorted(revocation_set),
    }
    return {
        "snapshot_id": snapshot["snapshot_id"],
        "source_format": snapshot["source_format"],
        "basis_generation": snapshot["basis_generation"],
        "effective_at_ms": snapshot["effective_at_ms"],
        "source_file": snapshot.get("source_file"),
        "content_digest": sha256_obj(content),
        **content,
    }


def build_trust_view(
    snapshots: list[dict[str, Any]],
    records: list[dict[str, Any]],
    *,
    observation_clock_ms: int | None = None,
) -> dict[str, Any]:
    records_by_key = {record["record_key"]: record for record in records}
    ordered_snapshots = sorted(
        snapshots,
        key=lambda s: (int(s["effective_at_ms"]), int(s["basis_generation"]), s["snapshot_id"]),
    )
    snapshot_summaries = [_snapshot_summary(s, records_by_key) for s in ordered_snapshots]

    effective_snapshots: list[dict[str, Any]] = []
    previous: dict[str, Any] | None = None
    for summary in snapshot_summaries:
        changes: dict[str, Any] = {}
        replacement = previous is not None and previous["content_digest"] != summary["content_digest"]
        if previous is not None:
            old_issuers = set(previous["issuer_set"])
            new_issuers = set(summary["issuer_set"])
            old_rev = set(previous["revocation_set"])
            new_rev = set(summary["revocation_set"])
            gen_changes = {
                issuer: {"from_generation": previous["tsa_generations"].get(issuer),
                          "to_generation": summary["tsa_generations"].get(issuer)}
                for issuer in sorted(old_issuers | new_issuers)
                if previous["tsa_generations"].get(issuer) != summary["tsa_generations"].get(issuer)
            }
            changes = {
                "issuers_added": sorted(new_issuers - old_issuers),
                "issuers_removed": sorted(old_issuers - new_issuers),
                "tsa_generation_changes": gen_changes,
                "revocations_added": sorted(new_rev - old_rev),
                "revocations_removed": sorted(old_rev - new_rev),
            }
            replacement = replacement and bool(
                changes["issuers_added"] or changes["issuers_removed"]
                or changes["tsa_generation_changes"]
                or changes["revocations_added"] or changes["revocations_removed"]
            )
        entry = {
            **summary,
            "replacement": bool(replacement or previous is None),
            "basis_for": summary["basis_generation"],
            "changes": changes,
        }
        effective_snapshots.append(entry)
        previous = summary

    grouped: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        grouped.setdefault(record["issuer_canonical_id"], []).append(record)

    issuers: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []
    tombstone_overrides: list[dict[str, Any]] = []
    generation_issuers: dict[str, set[str]] = {}

    for issuer_id in sorted(grouped):
        issuer_records = sorted(grouped[issuer_id], key=_authority_key)
        current = issuer_records[-1]
        for generation in _generation_bindings(current):
            generation_issuers.setdefault(str(generation), set()).add(issuer_id)

        family_records: dict[str, dict[str, Any]] = {}
        for family in ("v1", "v2", "update"):
            candidates = [r for r in issuer_records if r["source_format"] == family]
            if candidates:
                family_records[family] = max(candidates, key=_authority_key)
        comparison_partner = family_records.get("v2") or family_records.get("update")
        v1_record = family_records.get("v1")
        issuer_conflicts: list[str] = []
        if v1_record and comparison_partner:
            for field_name in ("trusted", "revoked"):
                left = bool(v1_record.get(field_name))
                right = bool(comparison_partner.get(field_name))
                if left == right:
                    continue
                same_time_authority = (
                    v1_record["tsa_generation"] == comparison_partner["tsa_generation"]
                    and v1_record["registered_at_ms"] == comparison_partner["registered_at_ms"]
                )
                tombstone_resolves = (
                    same_time_authority
                    and v1_record["tombstone"] != comparison_partner["tombstone"]
                )
                if tombstone_resolves:
                    status = "resolved_tombstone_override"
                    resolution_code = Code.W_TOMBSTONE_OVERRIDE
                elif _authority_key(comparison_partner)[:2] != _authority_key(v1_record)[:2]:
                    status = "resolved_authoritative_generation_time"
                    resolution_code = Code.OK
                else:
                    status = "unresolved"
                    resolution_code = Code.E_TRUST_CONFLICT
                conflict = {
                    "conflict_id": "conflict-" + sha256_obj(
                        [issuer_id, field_name, v1_record["record_key"],
                         comparison_partner["record_key"]])[:12],
                    "issuer_id": issuer_id,
                    "field": field_name,
                    "v1_value": left,
                    "v2_value": right,
                    "v1_record_key": v1_record["record_key"],
                    "v2_record_key": comparison_partner["record_key"],
                    "tsa_generation": comparison_partner["tsa_generation"],
                    "effective_at_ms": comparison_partner["effective_at_ms"],
                    "status": status,
                    "code": resolution_code,
                }
                conflicts.append(conflict)
                issuer_conflicts.append(conflict["conflict_id"])

        if current["tombstone"]:
            for old in issuer_records[:-1]:
                if not old["tombstone"]:
                    override = {
                        "code": Code.W_TOMBSTONE_OVERRIDE,
                        "issuer_id": issuer_id,
                        "tombstone_record_key": current["record_key"],
                        "covered_record_key": old["record_key"],
                        "tsa_generation": current["tsa_generation"],
                        "effective_at_ms": current["effective_at_ms"],
                    }
                    tombstone_overrides.append(override)

        issuers.append({
            "issuer_id": issuer_id,
            "issuer_display_id": current["issuer_id"],
            "trusted": current["trusted"],
            "revoked": current["revoked"],
            "tombstone": current["tombstone"],
            "tsa_generation": current["tsa_generation"],
            "token_generation": current["token_generation"],
            "authorized_token_generations": sorted(_generation_bindings(current)),
            "registered_at_ms": current["registered_at_ms"],
            "effective_at_ms": current["effective_at_ms"],
            "revoked_at_ms": current["revoked_at_ms"],
            "revocation_reason_code": current["revocation_reason_code"],
            "basis_generation": current["snapshot_basis_generation"],
            "authoritative_record_key": current["record_key"],
            "conflict_ids": issuer_conflicts,
            "has_unresolved_conflict": any(
                c["issuer_id"] == issuer_id and c["status"] == "unresolved" for c in conflicts
            ),
        })

    graph = build_reference_graph(records)
    for snapshot in snapshots:
        for edge in snapshot.get("reference_edges", []) or []:
            source = edge.get("from") if isinstance(edge, dict) else None
            target = edge.get("to") if isinstance(edge, dict) else None
            if source and target:
                graph.setdefault(source, set()).add(target)
                graph.setdefault(target, set())
    cycles = find_cycles(graph)
    cycle_nodes = {node for cycle in cycles for node in cycle["nodes"]}

    current_revoked = sorted(item["issuer_id"] for item in issuers if item["revoked"])
    current_tombstones = []
    for item in issuers:
        if item["tombstone"]:
            for generation in item["authorized_token_generations"]:
                current_tombstones.append({
                    "issuer_id": item["issuer_id"],
                    "token_generation": generation,
                    "tsa_generation": item["tsa_generation"],
                })

    view = {
        "format": "tsaudit.trust-view",
        "version": 1,
        "merge_policy_version": 1,
        "observation_clock_ms": observation_clock_ms,
        "snapshot_count": len(snapshots),
        "effective_snapshots": effective_snapshots,
        "issuers": sorted(issuers, key=lambda x: x["issuer_id"]),
        "conflicts": sorted(conflicts, key=lambda x: x["conflict_id"]),
        "tombstone_overrides": sorted(tombstone_overrides, key=lambda x: x["covered_record_key"]),
        "cycles": cycles,
        "reference_graph": {key: sorted(values) for key, values in sorted(graph.items())},
        "generation_issuers": {key: sorted(values) for key, values in sorted(generation_issuers.items())},
        "current_revoked_issuers": current_revoked,
        "tombstoned_generations": sorted(
            current_tombstones,
            key=lambda x: (x["issuer_id"], x["token_generation"]),
        ),
        "cycle_nodes": sorted(cycle_nodes),
        "records": sorted(records, key=lambda r: r["record_key"]),
    }
    view["view_digest"] = sha256_obj({
        "issuers": [{k: v for k, v in item.items()} for item in view["issuers"]],
        "conflicts": view["conflicts"],
        "cycles": view["cycles"],
        "snapshots": effective_snapshots,
    })
    return view


def evaluate_token(token: dict[str, Any], view: dict[str, Any]) -> dict[str, Any]:
    issued_at = int(token["issued_at_ms"])
    issuer = canonical_issuer_id(token["issuer_id"])
    generation = int(token["token_generation"])
    codes: list[str] = []
    issuance_codes: list[str] = []
    current_codes: list[str] = []

    effective = [s for s in view["effective_snapshots"] if int(s["effective_at_ms"]) <= issued_at]
    effective_snapshot = max(
        effective,
        key=lambda s: (int(s["effective_at_ms"]), int(s["basis_generation"]), s["snapshot_id"]),
        default=None,
    )
    basis_generation = effective_snapshot["basis_generation"] if effective_snapshot else None

    issuer_records = [
        record for record in view["records"]
        if record["issuer_canonical_id"] == issuer
    ]
    eligible = [
        r for r in issuer_records
        if int(r["registered_at_ms"]) <= issued_at
        and int(r["effective_at_ms"]) <= issued_at
    ]
    selected = max(eligible, key=_authority_key, default=None)
    issuer_view = next((item for item in view["issuers"] if item["issuer_id"] == issuer), None)

    node_issuer = f"issuer:{issuer}"
    node_generation = f"token_generation:{generation}"
    in_cycle = node_issuer in view.get("cycle_nodes", []) or node_generation in view.get("cycle_nodes", [])
    if in_cycle:
        codes.append(Code.E_CYCLE_REFERENCE)
        issuance_codes.append(Code.E_CYCLE_REFERENCE)

    if not eligible or selected is None:
        codes.append(Code.E_ISSUER_NOT_REGISTERED)
        issuance_codes.append(Code.E_ISSUER_NOT_REGISTERED)
    else:
        owners = set(view.get("generation_issuers", {}).get(str(generation), []))
        eligible_generations: set[int] = set()
        for record in eligible:
            eligible_generations.update(_generation_bindings(record))
        if (owners and issuer not in owners) or generation not in eligible_generations:
            codes.append(Code.E_GENERATION_MISMATCH)
            issuance_codes.append(Code.E_GENERATION_MISMATCH)

        if selected["tombstone"]:
            codes.append(Code.E_ISSUER_TOMBSTONED)
            issuance_codes.append(Code.E_ISSUER_TOMBSTONED)
        elif not selected["trusted"]:
            codes.append(Code.E_ISSUER_UNTRUSTED)
            issuance_codes.append(Code.E_ISSUER_UNTRUSTED)

        conflict = next((
            c for c in view["conflicts"]
            if c["issuer_id"] == issuer and c["status"] == "unresolved"
            and int(c.get("effective_at_ms", c.get("registered_at_ms", 0))) <= issued_at
        ), None)
        if conflict or (issuer_view and issuer_view["has_unresolved_conflict"]):
            codes.append(Code.E_TRUST_CONFLICT)
            issuance_codes.append(Code.E_TRUST_CONFLICT)

        early_revocation = any(
            r.get("revoked_at_ms") is not None and int(r["revoked_at_ms"]) <= issued_at
            for r in eligible if r.get("revoked")
        )
        later_revocation = any(
            r.get("revoked_at_ms") is not None and int(r["revoked_at_ms"]) > issued_at
            for r in issuer_records if r.get("revoked")
        )
        if early_revocation:
            codes.append(Code.E_REVOCATION_BEFORE_ISSUANCE)
            issuance_codes.append(Code.E_REVOCATION_BEFORE_ISSUANCE)
        elif later_revocation or (issuer_view and issuer_view["revoked"]):
            codes.append(Code.W_REVOKED_AFTER_ISSUANCE)
            issuance_codes.append(Code.W_REVOKED_AFTER_ISSUANCE)

    if issued_at < int(token["valid_from_ms"]):
        codes.append(Code.E_TOKEN_NOT_YET_VALID)
        issuance_codes.append(Code.E_TOKEN_NOT_YET_VALID)
    if issued_at > int(token["valid_to_ms"]):
        codes.append(Code.E_TOKEN_EXPIRED)
        issuance_codes.append(Code.E_TOKEN_EXPIRED)

    trusted_at_issuance = not any(code.startswith("E_") for code in issuance_codes)

    if issuer_view:
        if issuer_view["has_unresolved_conflict"] and Code.E_TRUST_CONFLICT not in codes:
            codes.append(Code.E_TRUST_CONFLICT)
            current_codes.append(Code.E_TRUST_CONFLICT)
        if issuer_view["tombstone"]:
            current_codes.append(Code.E_ISSUER_TOMBSTONED)
        elif issuer_view["revoked"]:
            current_codes.append(Code.E_ISSUER_REVOKED)
        elif not issuer_view["trusted"]:
            current_codes.append(Code.E_ISSUER_UNTRUSTED)

    tombstoned_generation = any(
        item["token_generation"] == generation
        and (item["issuer_id"] == issuer or item["issuer_id"] in {"*", ""})
        for item in view.get("tombstoned_generations", [])
    )
    if tombstoned_generation:
        current_codes.append(Code.E_ISSUER_TOMBSTONED)

    for current_code in current_codes:
        if current_code not in codes:
            codes.append(current_code)

    anchor_eligible = trusted_at_issuance and not current_codes
    return {
        "token_id": token["token_id"],
        "issuer_id": token["issuer_id"],
        "issuer_canonical_id": issuer,
        "signature_digest": token["signature_digest"],
        "issued_at_ms": issued_at,
        "token_generation": generation,
        "valid_from_ms": token["valid_from_ms"],
        "valid_to_ms": token["valid_to_ms"],
        "sync_marker": token.get("sync_marker"),
        "status": "trusted" if trusted_at_issuance else "untrusted",
        "trust_at_issuance": "trusted" if trusted_at_issuance else "untrusted",
        "current_status": "anchor_eligible" if anchor_eligible else "blocked_by_current_trust",
        "anchor_eligible": anchor_eligible,
        "business_codes": sorted(set(codes)),
        "issuance_codes": sorted(set(issuance_codes)),
        "current_codes": sorted(set(current_codes)),
        "basis_generation": basis_generation,
        "tsa_generation": selected["tsa_generation"] if selected else None,
        "effective_snapshot_id": effective_snapshot["snapshot_id"] if effective_snapshot else None,
        "authoritative_record_key": selected["record_key"] if selected else None,
    }
