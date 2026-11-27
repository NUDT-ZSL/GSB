from __future__ import annotations

import json
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Mapping

from .model import (
    REFERENCE_ERROR,
    BuildNode,
    FilterRule,
    NextHop,
    NormalizedProject,
    Policy,
    Prefix,
    ReferenceIssue,
    Region,
    RouteCandidate,
)


class ReferenceValidationError(ValueError):
    def __init__(self, report: Mapping[str, Any]):
        self.report = dict(report)
        super().__init__(
            f"policy project contains {self.report.get('issue_count', 0)} "
            "dangling reference(s)"
        )


def _project_meta(raw: Mapping[str, Any]) -> tuple[str, str, int]:
    project = raw.get("project") or {}
    return (
        str(project.get("id") or raw.get("project_id") or "unknown-project"),
        str(project.get("name") or raw.get("name") or "Build Egress Policy"),
        int(project.get("seed") or raw.get("seed") or 0),
    )


def _schema_version(raw: Mapping[str, Any]) -> int:
    value = raw.get("schema_version", raw.get("schemaVersion", 1))
    if isinstance(value, str):
        cleaned = value.lower().replace("policy.", "").replace("v", "")
        return int(cleaned)
    return int(value)


def _round_half_up_ratio(value: int | float, numerator: int, denominator: int) -> int:
    scaled = (
        Decimal(str(value))
        * Decimal(numerator)
        / Decimal(denominator)
    )
    return int(scaled.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _decode_bitmap(
    bitmap: int,
    bit_to_label: Mapping[int, str],
    issue_entity: str,
    issue_id: str,
    source_schema: str,
) -> tuple[tuple[str, ...], list[ReferenceIssue]]:
    labels = sorted(
        label
        for bit, label in bit_to_label.items()
        if bitmap & bit
    )
    known_mask = 0
    for bit in bit_to_label:
        known_mask |= bit
    unknown = int(bitmap) & ~known_mask
    issues: list[ReferenceIssue] = []
    if unknown:
        issues.append(
            ReferenceIssue(
                code=REFERENCE_ERROR,
                entity=issue_entity,
                entity_id=issue_id,
                field="community_mask",
                reference=str(unknown),
                message=f"community bitmap contains uncatalogued bit mask {unknown}",
                source_schema=source_schema,
            )
        )
    return tuple(labels), issues


def _make_filter_rule_v1(raw: Mapping[str, Any]) -> FilterRule:
    return FilterRule(
        id=str(raw["id"]),
        match=str(raw.get("match", "any")),
        communities=tuple(
            sorted(str(item) for item in raw.get("communities", ()))
        ),
        action=str(raw.get("action", "deny")),
    )


def _normalize_v1(raw: Mapping[str, Any]) -> tuple[
    tuple[Region, ...],
    tuple[BuildNode, ...],
    tuple[NextHop, ...],
    tuple[Prefix, ...],
    Policy,
    Mapping[str, Any],
    list[ReferenceIssue],
]:
    regions = tuple(
        Region(id=str(item["id"]), name=str(item.get("name", item["id"])))
        for item in raw.get("regions", ())
    )
    build_nodes = tuple(
        BuildNode(
            id=str(item["id"]),
            name=str(item.get("name", item["id"])),
            region_id=str(item["region_id"]),
        )
        for item in raw.get("build_nodes", ())
    )
    next_hops = tuple(
        NextHop(
            id=str(item["id"]),
            name=str(item.get("name", item["id"])),
            region_id=str(item["region_id"]),
            asn=int(item.get("asn", 0)),
            access_point=str(item.get("access_point", "")),
        )
        for item in raw.get("next_hops", ())
    )
    policy_raw = raw.get("policy") or {}
    weights = {
        str(key): int(value)
        for key, value in (policy_raw.get("community_weights") or {}).items()
    }
    filters = tuple(
        _make_filter_rule_v1(item) for item in policy_raw.get("filters", ())
    )
    policy = Policy(community_weights=weights, filters=filters)
    prefixes = tuple(
        Prefix(
            id=str(item["id"]),
            cidr=str(item["cidr"]),
            build_node_id=str(item["build_node_id"]),
            repository=str(item.get("repository", "")),
            candidates=tuple(
                RouteCandidate(
                    id=str(candidate["id"]),
                    next_hop_id=str(candidate["next_hop_id"]),
                    as_path=tuple(int(value) for value in candidate.get("as_path", ())),
                    local_pref=int(candidate["local_pref"]),
                    communities=tuple(
                        sorted(
                            str(value)
                            for value in candidate.get("communities", ())
                        )
                    ),
                )
                for candidate in item.get("candidates", ())
            ),
        )
        for item in raw.get("prefixes", ())
    )
    scale = dict(raw.get("local_pref_scale") or {"kind": "absolute", "maximum": 1000})
    return regions, build_nodes, next_hops, prefixes, policy, scale, []


def _normalize_v2(raw: Mapping[str, Any]) -> tuple[
    tuple[Region, ...],
    tuple[BuildNode, ...],
    tuple[NextHop, ...],
    tuple[Prefix, ...],
    Policy,
    Mapping[str, Any],
    list[ReferenceIssue],
]:
    source_schema = str(raw.get("schema_version", "policy.v2"))
    catalog = raw.get("community_catalog", ())
    label_to_bit: dict[str, int] = {}
    bit_to_label: dict[int, str] = {}
    issues: list[ReferenceIssue] = []
    for item in catalog:
        label = str(item["label"])
        bit = int(item["bit"])
        if label in label_to_bit or bit in bit_to_label:
            issues.append(
                ReferenceIssue(
                    code=REFERENCE_ERROR,
                    entity="community_catalog",
                    entity_id=label,
                    field="community_catalog",
                    reference=str(bit),
                    message="duplicate community label or bitmap position",
                    source_schema=source_schema,
                )
            )
        label_to_bit[label] = bit
        bit_to_label[bit] = label

    raw_scale = dict(
        raw.get("local_pref_scale") or {"kind": "percent", "canonical_max": 1000}
    )
    canonical_max = int(raw_scale.get("canonical_max", 1000))

    regions = tuple(
        Region(id=str(item["id"]), name=str(item.get("name", item["id"])))
        for item in raw.get("regions", ())
    )
    build_nodes = tuple(
        BuildNode(
            id=str(item["id"]),
            name=str(item.get("name", item["id"])),
            region_id=str(item["region_id"]),
        )
        for item in raw.get("build_nodes", ())
    )
    next_hops = tuple(
        NextHop(
            id=str(item["id"]),
            name=str(item.get("name", item["id"])),
            region_id=str(item["region_id"]),
            asn=int(item.get("asn", 0)),
            access_point=str(item.get("access_point", "")),
        )
        for item in raw.get("next_hops", ())
    )

    policy_raw = raw.get("policy") or {}
    weights: dict[str, int] = {}
    for item in policy_raw.get("community_weights", ()):
        bit = int(item["bit"])
        label = bit_to_label.get(bit)
        if label is None:
            issues.append(
                ReferenceIssue(
                    code=REFERENCE_ERROR,
                    entity="policy",
                    entity_id=str(item.get("id", "community_weights")),
                    field="community_weights.bit",
                    reference=str(bit),
                    message="community preference references an uncatalogued bitmap",
                    source_schema=source_schema,
                )
            )
            continue
        weights[label] = int(item["weight"])

    filter_rules: list[FilterRule] = []
    for item in policy_raw.get("filters", ()):
        mask = int(item.get("community_mask", 0))
        communities, decode_issues = _decode_bitmap(
            mask,
            bit_to_label,
            "policy",
            str(item.get("id", "filter")),
            source_schema,
        )
        issues.extend(decode_issues)
        filter_rules.append(
            FilterRule(
                id=str(item["id"]),
                match=str(item.get("match", "any")),
                communities=communities,
                action=str(item.get("action", "deny")),
            )
        )

    prefixes: list[Prefix] = []
    for item in raw.get("prefixes", ()):
        candidates: list[RouteCandidate] = []
        for candidate in item.get("candidates", ()):
            mask = int(candidate.get("community_mask", 0))
            communities, decode_issues = _decode_bitmap(
                mask,
                bit_to_label,
                "candidate",
                str(candidate["id"]),
                source_schema,
            )
            issues.extend(decode_issues)
            local_pref = _round_half_up_ratio(
                candidate["local_pref_pct"],
                canonical_max,
                100,
            )
            candidates.append(
                RouteCandidate(
                    id=str(candidate["id"]),
                    next_hop_id=str(candidate["next_hop_id"]),
                    as_path=tuple(int(value) for value in candidate.get("as_path", ())),
                    local_pref=local_pref,
                    communities=communities,
                )
            )
        prefixes.append(
            Prefix(
                id=str(item["id"]),
                cidr=str(item["cidr"]),
                build_node_id=str(item["build_node_id"]),
                repository=str(item.get("repository", "")),
                candidates=tuple(candidates),
            )
        )

    return (
        regions,
        build_nodes,
        next_hops,
        tuple(prefixes),
        Policy(community_weights=weights, filters=tuple(filter_rules)),
        raw_scale,
        issues,
    )


def _validate_references(project: NormalizedProject) -> tuple[ReferenceIssue, ...]:
    issues: list[ReferenceIssue] = list(project.reference_issues)
    source = project.source_schema
    region_ids = {item.id for item in project.regions}
    node_ids = {item.id for item in project.build_nodes}
    next_hop_ids = {item.id for item in project.next_hops}

    seen_regions: set[str] = set()
    for region in project.regions:
        if region.id in seen_regions:
            issues.append(
                ReferenceIssue(
                    REFERENCE_ERROR,
                    "region",
                    region.id,
                    "id",
                    region.id,
                    "duplicate region id",
                    source,
                )
            )
        seen_regions.add(region.id)

    seen_nodes: set[str] = set()
    for node in project.build_nodes:
        if node.id in seen_nodes:
            issues.append(
                ReferenceIssue(
                    REFERENCE_ERROR,
                    "build_node",
                    node.id,
                    "id",
                    node.id,
                    "duplicate build node id",
                    source,
                )
            )
        seen_nodes.add(node.id)
        if node.region_id not in region_ids:
            issues.append(
                ReferenceIssue(
                    REFERENCE_ERROR,
                    "build_node",
                    node.id,
                    "region_id",
                    node.region_id,
                    f"build node references missing region {node.region_id}",
                    source,
                )
            )

    seen_hops: set[str] = set()
    for hop in project.next_hops:
        if hop.id in seen_hops:
            issues.append(
                ReferenceIssue(
                    REFERENCE_ERROR,
                    "next_hop",
                    hop.id,
                    "id",
                    hop.id,
                    "duplicate next hop id",
                    source,
                )
            )
        seen_hops.add(hop.id)
        if hop.region_id not in region_ids:
            issues.append(
                ReferenceIssue(
                    REFERENCE_ERROR,
                    "next_hop",
                    hop.id,
                    "region_id",
                    hop.region_id,
                    f"next hop references missing region {hop.region_id}",
                    source,
                )
            )

    seen_prefixes: set[str] = set()
    seen_candidates: set[str] = set()
    for prefix in project.prefixes:
        if prefix.id in seen_prefixes:
            issues.append(
                ReferenceIssue(
                    REFERENCE_ERROR,
                    "prefix",
                    prefix.id,
                    "id",
                    prefix.id,
                    "duplicate prefix id",
                    source,
                )
            )
        seen_prefixes.add(prefix.id)
        if prefix.build_node_id not in node_ids:
            issues.append(
                ReferenceIssue(
                    REFERENCE_ERROR,
                    "prefix",
                    prefix.id,
                    "build_node_id",
                    prefix.build_node_id,
                    f"prefix references missing build node {prefix.build_node_id}",
                    source,
                )
            )
        for candidate in prefix.candidates:
            if candidate.id in seen_candidates:
                issues.append(
                    ReferenceIssue(
                        REFERENCE_ERROR,
                        "candidate",
                        f"{prefix.id}:{candidate.id}",
                        "id",
                        candidate.id,
                        "duplicate candidate id",
                        source,
                    )
                )
            seen_candidates.add(candidate.id)
            if candidate.next_hop_id not in next_hop_ids:
                issues.append(
                    ReferenceIssue(
                        REFERENCE_ERROR,
                        "candidate",
                        f"{prefix.id}:{candidate.id}",
                        "next_hop_id",
                        candidate.next_hop_id,
                        f"candidate references missing next hop {candidate.next_hop_id}",
                        source,
                    )
                )

    return tuple(issues)


def normalize_project(raw: Mapping[str, Any], *, strict: bool = False) -> NormalizedProject:
    project_id, name, seed = _project_meta(raw)
    version = _schema_version(raw)
    source_schema = str(raw.get("schema_version", f"policy.v{version}"))
    if version == 1:
        parts = _normalize_v1(raw)
    elif version == 2:
        parts = _normalize_v2(raw)
    else:
        raise ValueError(f"unsupported schema version: {version}")

    regions, nodes, hops, prefixes, policy, scale, issues = parts
    project = NormalizedProject(
        project_id=project_id,
        name=name,
        schema_version=version,
        source_schema=source_schema,
        seed=seed,
        regions=regions,
        build_nodes=nodes,
        next_hops=hops,
        prefixes=prefixes,
        policy=policy,
        local_pref_scale=scale,
        reference_issues=tuple(issues),
        metadata={
            key: value
            for key, value in raw.get("metadata", {}).items()
        },
    )
    project = NormalizedProject(
        project_id=project.project_id,
        name=project.name,
        schema_version=project.schema_version,
        source_schema=project.source_schema,
        seed=project.seed,
        regions=project.regions,
        build_nodes=project.build_nodes,
        next_hops=project.next_hops,
        prefixes=project.prefixes,
        policy=project.policy,
        local_pref_scale=project.local_pref_scale,
        reference_issues=_validate_references(project),
        metadata=project.metadata,
    )
    if strict and project.reference_issues:
        raise ReferenceValidationError(reference_report(project))
    return project


def load_project(path: str | Path, *, strict: bool = False) -> NormalizedProject:
    with Path(path).open("r", encoding="utf-8") as handle:
        raw = json.load(handle)
    return normalize_project(raw, strict=strict)


def project_from_storage(raw: Mapping[str, Any]) -> NormalizedProject:
    """Load the canonical ``NormalizedProject.to_dict`` representation.

    This is deliberately separate from schema normalization so a database can
    retain an exact normalized project without having to remember which delivery
    schema originally produced it.
    """

    project_meta = raw.get("project") or {}

    def issue(item: Mapping[str, Any]) -> ReferenceIssue:
        return ReferenceIssue(
            code=str(item.get("code", REFERENCE_ERROR)),
            entity=str(item["entity"]),
            entity_id=str(item["entity_id"]),
            field=str(item["field"]),
            reference=str(item.get("reference", "")),
            message=str(item.get("message", "")),
            source_schema=str(item.get("source_schema", "")),
        )

    return NormalizedProject(
        project_id=str(project_meta.get("id", "unknown-project")),
        name=str(project_meta.get("name", "Build Egress Policy")),
        schema_version=int(raw.get("schema_version", 1)),
        source_schema=str(raw.get("source_schema", "canonical")),
        seed=int(project_meta.get("seed", 0)),
        regions=tuple(
            Region(id=str(item["id"]), name=str(item.get("name", item["id"])))
            for item in raw.get("regions", ())
        ),
        build_nodes=tuple(
            BuildNode(
                id=str(item["id"]),
                name=str(item.get("name", item["id"])),
                region_id=str(item["region_id"]),
            )
            for item in raw.get("build_nodes", ())
        ),
        next_hops=tuple(
            NextHop(
                id=str(item["id"]),
                name=str(item.get("name", item["id"])),
                region_id=str(item["region_id"]),
                asn=int(item.get("asn", 0)),
                access_point=str(item.get("access_point", "")),
            )
            for item in raw.get("next_hops", ())
        ),
        prefixes=tuple(
            Prefix(
                id=str(item["id"]),
                cidr=str(item["cidr"]),
                build_node_id=str(item["build_node_id"]),
                repository=str(item.get("repository", "")),
                candidates=tuple(
                    RouteCandidate(
                        id=str(candidate["id"]),
                        next_hop_id=str(candidate["next_hop_id"]),
                        as_path=tuple(
                            int(value) for value in candidate.get("as_path", ())
                        ),
                        local_pref=int(candidate["local_pref"]),
                        communities=tuple(
                            str(value)
                            for value in candidate.get("communities", ())
                        ),
                    )
                    for candidate in item.get("candidates", ())
                ),
            )
            for item in raw.get("prefixes", ())
        ),
        policy=Policy.from_dict(raw.get("policy") or {}),
        local_pref_scale=dict(raw.get("local_pref_scale") or {}),
        reference_issues=tuple(
            issue(item) for item in raw.get("reference_issues", ())
        ),
        metadata=dict(raw.get("metadata", {})),
    )


def reference_report(project: NormalizedProject) -> dict[str, Any]:
    issues = [issue.to_dict() for issue in project.reference_issues]
    return {
        "code": REFERENCE_ERROR,
        "status": "invalid" if issues else "ok",
        "project_id": project.project_id,
        "source_schema": project.source_schema,
        "issue_count": len(issues),
        "issues": issues,
    }
