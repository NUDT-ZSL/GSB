from __future__ import annotations

import argparse
import hashlib
import json
import random
from pathlib import Path
from typing import Any, Mapping, Sequence

from .model import NormalizedProject
from .normalize import normalize_project
from .selector import select_prefix

DEFAULT_SEED = 20261128

COMMUNITIES: tuple[tuple[str, int, int], ...] = (
    ("pref:gold", 1, 300),
    ("pref:silver", 2, 150),
    ("scope:regional", 4, 50),
    ("maintenance", 8, 0),
    ("blocked:test", 16, 0),
    ("cost:low", 32, 25),
)

REGIONS = (
    {"id": "region-north", "name": "华北构建区"},
    {"id": "region-east", "name": "华东构建区"},
    {"id": "region-eu", "name": "欧洲构建区"},
)

BUILD_NODES = (
    {"id": "node-bj-1", "name": "北京构建节点 1", "region_id": "region-north"},
    {"id": "node-sh-1", "name": "上海构建节点 1", "region_id": "region-east"},
    {"id": "node-fra-1", "name": "法兰克福构建节点 1", "region_id": "region-eu"},
)

NEXT_HOPS = (
    {
        "id": "nh-bj-a",
        "name": "北京接入 A",
        "region_id": "region-north",
        "asn": 64501,
        "access_point": "bj-pe1",
    },
    {
        "id": "nh-bj-b",
        "name": "北京接入 B",
        "region_id": "region-north",
        "asn": 64502,
        "access_point": "bj-pe2",
    },
    {
        "id": "nh-sh-a",
        "name": "上海接入 A",
        "region_id": "region-east",
        "asn": 64511,
        "access_point": "sh-pe1",
    },
    {
        "id": "nh-sh-b",
        "name": "上海接入 B",
        "region_id": "region-east",
        "asn": 64512,
        "access_point": "sh-pe2",
    },
    {
        "id": "nh-fra-a",
        "name": "法兰克福接入 A",
        "region_id": "region-eu",
        "asn": 64521,
        "access_point": "fra-pe1",
    },
    {
        "id": "nh-fra-b",
        "name": "法兰克福接入 B",
        "region_id": "region-eu",
        "asn": 64522,
        "access_point": "fra-pe2",
    },
)


PREFIX_SPECS: tuple[dict[str, Any], ...] = (
    {
        "id": "pfx-bj-maven",
        "cidr": "203.0.113.0/24",
        "build_node_id": "node-bj-1",
        "repository": "maven-central",
        "candidates": (
            ("cand-bj-maven-1", "nh-bj-a", 2, 400, ("pref:silver", "scope:regional")),
            ("cand-bj-maven-2", "nh-bj-b", 2, 400, ("pref:gold", "scope:regional")),
            ("cand-bj-maven-3", "nh-sh-a", 3, 500, ("pref:gold",)),
        ),
    },
    {
        "id": "pfx-bj-pypi",
        "cidr": "203.0.114.0/24",
        "build_node_id": "node-bj-1",
        "repository": "pypi",
        "candidates": (
            ("cand-bj-pypi-1", "nh-bj-a", 2, 300, ("pref:silver", "scope:regional")),
            ("cand-bj-pypi-2", "nh-bj-b", 3, 500, ("pref:gold", "scope:regional")),
        ),
    },
    {
        "id": "pfx-bj-npm",
        "cidr": "203.0.115.0/24",
        "build_node_id": "node-bj-1",
        "repository": "npm",
        "candidates": (
            ("cand-bj-npm-1", "nh-bj-b", 2, 350, ("cost:low", "scope:regional")),
        ),
    },
    {
        "id": "pfx-sh-maven",
        "cidr": "198.51.100.0/24",
        "build_node_id": "node-sh-1",
        "repository": "maven-central",
        "candidates": (
            ("cand-sh-maven-1", "nh-sh-a", 2, 500, ("pref:gold", "scope:regional")),
            ("cand-sh-maven-2", "nh-sh-b", 2, 350, ("pref:silver",)),
            ("cand-sh-maven-3", "nh-bj-b", 3, 450, ("pref:gold",)),
        ),
    },
    {
        "id": "pfx-sh-pypi",
        "cidr": "198.51.101.0/24",
        "build_node_id": "node-sh-1",
        "repository": "pypi",
        "candidates": (
            ("cand-sh-pypi-1", "nh-sh-a", 2, 450, ("pref:silver", "scope:regional")),
            ("cand-sh-pypi-2", "nh-sh-b", 2, 430, ("cost:low",)),
        ),
    },
    {
        "id": "pfx-sh-docker",
        "cidr": "198.51.102.0/24",
        "build_node_id": "node-sh-1",
        "repository": "docker-hub",
        "candidates": (
            ("cand-sh-docker-1", "nh-sh-b", 3, 400, ("pref:silver",)),
        ),
    },
    {
        "id": "pfx-fra-maven",
        "cidr": "192.0.2.0/24",
        "build_node_id": "node-fra-1",
        "repository": "maven-central",
        "candidates": (
            ("cand-fra-maven-1", "nh-fra-a", 4, 600, ("pref:silver", "scope:regional")),
            ("cand-fra-maven-2", "nh-fra-b", 2, 350, ("cost:low",)),
        ),
    },
    {
        "id": "pfx-fra-pypi",
        "cidr": "192.0.3.0/24",
        "build_node_id": "node-fra-1",
        "repository": "pypi",
        "candidates": (
            ("cand-fra-pypi-1", "nh-fra-a", 3, 550, ("pref:gold",)),
            ("cand-fra-pypi-2", "nh-fra-b", 2, 420, ("pref:silver", "scope:regional")),
            ("cand-fra-pypi-3", "nh-sh-b", 2, 420, ("pref:silver",)),
        ),
    },
    {
        "id": "pfx-fra-npm",
        "cidr": "192.0.4.0/24",
        "build_node_id": "node-fra-1",
        "repository": "npm",
        "candidates": (
            ("cand-fra-npm-1", "nh-fra-a", 2, 380, ("pref:gold", "blocked:test")),
            ("cand-fra-npm-2", "nh-fra-b", 2, 380, ("pref:silver",)),
        ),
    },
)


INITIAL_FILTERS = (
    {
        "id": "deny-maintenance",
        "match": "any",
        "communities": ("maintenance",),
        "action": "deny",
    },
)

COMMUNITY_DIFF = {
    "prefix_id": "pfx-bj-maven",
    "candidate_id": "cand-bj-maven-2",
    "community": "pref:gold",
}

LOCAL_PREF_DIFF = {
    "prefix_id": "pfx-sh-pypi",
    "candidate_id": "cand-sh-pypi-1",
    "v2_local_pref_pct": 40,
}


def _make_as_path(asn: int, length: int, rng: random.Random) -> list[int]:
    if length <= 0:
        return []
    if length == 1:
        return [asn]
    tail = [rng.randrange(65000, 65535) for _ in range(length - 1)]
    return [asn, *tail]


def _v1_project(seed: int) -> dict[str, Any]:
    rng = random.Random(seed)
    prefixes: list[dict[str, Any]] = []
    for spec in PREFIX_SPECS:
        candidates: list[dict[str, Any]] = []
        for candidate_id, next_hop_id, path_length, local_pref, communities in spec[
            "candidates"
        ]:
            hop = next(item for item in NEXT_HOPS if item["id"] == next_hop_id)
            candidates.append(
                {
                    "id": candidate_id,
                    "next_hop_id": next_hop_id,
                    "as_path": _make_as_path(hop["asn"], path_length, rng),
                    "local_pref": local_pref,
                    "communities": list(communities),
                }
            )
        prefixes.append(
            {
                "id": spec["id"],
                "cidr": spec["cidr"],
                "build_node_id": spec["build_node_id"],
                "repository": spec["repository"],
                "candidates": candidates,
            }
        )
    return {
        "schema_version": 1,
        "schema_uri": "https://example.invalid/build-routing/policy-v1.json",
        "project": {
            "id": "build-egress-demo",
            "name": "多区域构建节点出口策略样本",
            "seed": seed,
        },
        "local_pref_scale": {
            "kind": "absolute",
            "minimum": 0,
            "maximum": 1000,
            "canonical_max": 1000,
        },
        "policy": {
            "community_weights": {
                label: weight for label, _bit, weight in COMMUNITIES
            },
            "filters": [dict(item) for item in INITIAL_FILTERS],
        },
        "regions": [dict(item) for item in REGIONS],
        "build_nodes": [dict(item) for item in BUILD_NODES],
        "next_hops": [dict(item) for item in NEXT_HOPS],
        "prefixes": prefixes,
        "metadata": {
            "generator": "build_routing.generator",
            "generator_version": 1,
            "delivery_schema": "v1-labels-absolute",
            "generated_at_logical": 0,
        },
    }


def _mask_for(communities: Sequence[str], label_to_bit: Mapping[str, int]) -> int:
    mask = 0
    for community in communities:
        mask |= label_to_bit[community]
    return mask


def _v2_project(v1_project: Mapping[str, Any]) -> dict[str, Any]:
    canonical = normalize_project(v1_project)
    label_to_bit = {label: bit for label, bit, _weight in COMMUNITIES}
    bit_to_label = {bit: label for label, bit, _weight in COMMUNITIES}
    prefixes: list[dict[str, Any]] = []
    for prefix in canonical.prefixes:
        candidates: list[dict[str, Any]] = []
        for candidate in prefix.candidates:
            communities = list(candidate.communities)
            if (
                prefix.id == COMMUNITY_DIFF["prefix_id"]
                and candidate.id == COMMUNITY_DIFF["candidate_id"]
            ):
                communities = [
                    item for item in communities if item != COMMUNITY_DIFF["community"]
                ]
            pct = candidate.local_pref // 10
            if (
                prefix.id == LOCAL_PREF_DIFF["prefix_id"]
                and candidate.id == LOCAL_PREF_DIFF["candidate_id"]
            ):
                pct = int(LOCAL_PREF_DIFF["v2_local_pref_pct"])
            candidates.append(
                {
                    "id": candidate.id,
                    "next_hop_id": candidate.next_hop_id,
                    "as_path": list(candidate.as_path),
                    "local_pref_pct": pct,
                    "community_mask": _mask_for(communities, label_to_bit),
                }
            )
        prefixes.append(
            {
                "id": prefix.id,
                "cidr": prefix.cidr,
                "build_node_id": prefix.build_node_id,
                "repository": prefix.repository,
                "candidates": candidates,
            }
        )

    filters: list[dict[str, Any]] = []
    for rule in canonical.policy.filters:
        filters.append(
            {
                "id": rule.id,
                "match": rule.match,
                "community_mask": _mask_for(rule.communities, label_to_bit),
                "action": rule.action,
            }
        )
    weights = [
        {"bit": bit, "label": label, "weight": canonical.policy.community_weights[label]}
        for label, bit, _weight in COMMUNITIES
    ]
    return {
        "schema_version": 2,
        "schema_uri": "https://example.invalid/build-routing/policy-v2.json",
        "project": {
            "id": canonical.project_id,
            "name": canonical.name,
            "seed": canonical.seed,
        },
        "community_catalog": [
            {"label": label, "bit": bit}
            for label, bit, _weight in COMMUNITIES
        ],
        "local_pref_scale": {
            "kind": "percent",
            "minimum": 0,
            "maximum": 100,
            "canonical_max": 1000,
        },
        "policy": {
            "community_weights": weights,
            "filters": filters,
        },
        "regions": [dict(item) for item in REGIONS],
        "build_nodes": [dict(item) for item in BUILD_NODES],
        "next_hops": [dict(item) for item in NEXT_HOPS],
        "prefixes": prefixes,
        "metadata": {
            "generator": "build_routing.generator",
            "generator_version": 1,
            "delivery_schema": "v2-bitmap-percent",
            "generated_at_logical": 0,
            "bitmap_bits": {
                label: bit for label, bit, _weight in COMMUNITIES
            },
            "percent_to_absolute": "round_half_up(pct * canonical_max / 100)",
        },
    }


def events_for_seed(seed: int) -> list[dict[str, Any]]:
    _ = seed
    deny_regional = [
        dict(INITIAL_FILTERS[0]),
        {
            "id": "deny-regional",
            "match": "any",
            "communities": ["scope:regional"],
            "action": "deny",
        },
    ]
    deny_blocked = [
        dict(INITIAL_FILTERS[0]),
        {
            "id": "deny-blocked-test",
            "match": "any",
            "communities": ["blocked:test"],
            "action": "deny",
        },
    ]
    return [
        {
            "seq": 1,
            "at": 100,
            "type": "link_down",
            "next_hop_id": "nh-bj-a",
            "description": "北京接入 A 线路中断",
        },
        {
            "seq": 2,
            "at": 200,
            "type": "policy_change",
            "description": "临时拒绝 scope:regional 社区",
            "policy_patch": {"filters": deny_regional},
        },
        {
            "seq": 3,
            "at": 300,
            "type": "link_up",
            "next_hop_id": "nh-bj-a",
            "description": "北京接入 A 线路恢复",
        },
        {
            "seq": 4,
            "at": 400,
            "type": "policy_change",
            "description": "撤销区域社区拒绝",
            "policy_patch": {"filters": [dict(INITIAL_FILTERS[0])]},
        },
        {
            "seq": 5,
            "at": 500,
            "type": "link_down",
            "next_hop_id": "nh-sh-b",
            "description": "上海接入 B 线路中断",
        },
        {
            "seq": 6,
            "at": 600,
            "type": "link_up",
            "next_hop_id": "nh-sh-b",
            "description": "上海接入 B 线路恢复",
        },
        {
            "seq": 7,
            "at": 700,
            "type": "policy_change",
            "description": "拒绝维护和测试阻断社区",
            "policy_patch": {"filters": deny_blocked},
        },
        {
            "seq": 8,
            "at": 800,
            "type": "policy_change",
            "description": "临时下调 gold 社区偏好",
            "policy_patch": {"community_weights": {"pref:gold": 100}},
        },
        {
            "seq": 9,
            "at": 900,
            "type": "policy_change",
            "description": "撤销测试阻断社区过滤",
            "policy_patch": {"filters": [dict(INITIAL_FILTERS[0])]},
        },
        {
            "seq": 10,
            "at": 1000,
            "type": "policy_change",
            "description": "恢复 gold 社区偏好",
            "policy_patch": {"community_weights": {"pref:gold": 300}},
        },
        {
            "seq": 11,
            "at": 1100,
            "type": "link_down",
            "next_hop_id": "nh-fra-a",
            "description": "法兰克福接入 A 线路中断",
        },
        {
            "seq": 12,
            "at": 1200,
            "type": "link_up",
            "next_hop_id": "nh-fra-a",
            "description": "法兰克福接入 A 线路恢复",
        },
    ]


def _initial_selection_summary(project: NormalizedProject) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    for prefix in project.prefixes:
        result = select_prefix(prefix, project)
        summary[prefix.id] = {
            "best_next_hop_id": result.best.next_hop_id if result.best else None,
            "best_candidate_id": result.best.candidate_id if result.best else None,
            "backup_next_hop_id": (
                result.backup.next_hop_id if result.backup else None
            ),
            "backup_candidate_id": (
                result.backup.candidate_id if result.backup else None
            ),
            "status": result.status,
        }
    return summary


def _known_differences(
    v1_project: NormalizedProject, v2_project: NormalizedProject, seed: int
) -> dict[str, Any]:
    v1 = _initial_selection_summary(v1_project)
    v2 = _initial_selection_summary(v2_project)
    items = [
        {
            "prefix_id": COMMUNITY_DIFF["prefix_id"],
            "reason": "community_representation",
            "reason_label": "社区标记表达差异",
            "evidence": {
                "candidate_id": COMMUNITY_DIFF["candidate_id"],
                "community": COMMUNITY_DIFF["community"],
                "v1_form": "string community label is present",
                "v2_form": "corresponding bitmap bit is intentionally absent",
            },
            "expected": {
                "v1": v1[COMMUNITY_DIFF["prefix_id"]],
                "v2": v2[COMMUNITY_DIFF["prefix_id"]],
            },
        },
        {
            "prefix_id": LOCAL_PREF_DIFF["prefix_id"],
            "reason": "local_pref_scale",
            "reason_label": "本地优先级刻度表达差异",
            "evidence": {
                "candidate_id": LOCAL_PREF_DIFF["candidate_id"],
                "v1_form": "absolute local preference 450",
                "v2_form": (
                    f"relative percentage {LOCAL_PREF_DIFF['v2_local_pref_pct']}% "
                    "normalizes to 400"
                ),
            },
            "expected": {
                "v1": v1[LOCAL_PREF_DIFF["prefix_id"]],
                "v2": v2[LOCAL_PREF_DIFF["prefix_id"]],
            },
        },
    ]
    return {
        "project_id": v1_project.project_id,
        "seed": seed,
        "items": items,
        "expected_diff_prefixes": sorted(item["prefix_id"] for item in items),
        "expected_reasons_by_prefix": {
            item["prefix_id"]: [item["reason"]] for item in items
        },
    }


def _dump_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def generate_artifacts(
    out_dir: str | Path = "artifacts",
    *,
    seed: int = DEFAULT_SEED,
) -> dict[str, Any]:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    v1_raw = _v1_project(seed)
    v2_raw = _v2_project(v1_raw)
    events = events_for_seed(seed)

    paths = {
        "project_v1": out / "project_v1.json",
        "project_v2": out / "project_v2.json",
        "events": out / "events.json",
        "known_differences": out / "known_differences.json",
        "manifest": out / "manifest.json",
    }
    _dump_json(paths["project_v1"], v1_raw)
    _dump_json(paths["project_v2"], v2_raw)
    _dump_json(paths["events"], events)
    v1_project = normalize_project(v1_raw)
    v2_project = normalize_project(v2_raw)
    known = _known_differences(v1_project, v2_project, seed)
    _dump_json(paths["known_differences"], known)

    manifest = {
        "seed": seed,
        "project_id": v1_project.project_id,
        "schema_versions": {"v1": 1, "v2": 2},
        "event_count": len(events),
        "files": {
            "project_v1": "project_v1.json",
            "project_v2": "project_v2.json",
            "events": "events.json",
            "known_differences": "known_differences.json",
        },
        "sha256": {
            "project_v1": _sha256(paths["project_v1"]),
            "project_v2": _sha256(paths["project_v2"]),
            "events": _sha256(paths["events"]),
            "known_differences": _sha256(paths["known_differences"]),
        },
        "known_diff_prefixes": known["expected_diff_prefixes"],
    }
    _dump_json(paths["manifest"], manifest)
    return {
        "paths": {key: str(value) for key, value in paths.items()},
        "manifest": manifest,
        "known_differences": known,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate deterministic route-policy sample artifacts")
    parser.add_argument("--out", default="artifacts", help="output directory")
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    args = parser.parse_args(argv)
    result = generate_artifacts(args.out, seed=args.seed)
    print(json.dumps(result["manifest"], ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
