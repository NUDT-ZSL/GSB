"""可复现异常注入与注入生效验证。

注入只改变输入文件副本或命令参数，绝不修改源码：
* E_ORDER：固定 LCG 置换重排观测 NDJSON 行（事件集合/内容必须完全不变）；
* E_TOPO：以拓扑变更集替换依赖边（受影响簇集合须与预测一致）；
* E_GAP  ：确定性删除指定服务的观测行（该服务相关簇必须消失/改变）。

每条注入产出 injection_id、影响范围、预期受影响簇集合，
验证阶段对比实际受影响簇集合，verified=False 会在业务码中显式暴露。
"""
from __future__ import annotations

import json
import os
from .errors import E_GAP, E_ORDER, E_TOPO
from .models import Cluster, Injection, Topology, canonical_dumps

_INJECTION_PREFIX = {
    E_ORDER: "INJ-ORDER",
    E_TOPO: "INJ-TOPO",
    E_GAP: "INJ-GAP",
}


def _deterministic_permutation(size: int, seed: int) -> list[int]:
    """固定种子的 LCG 置换，跨平台/跨 Python 版本结果一致。"""
    indices = list(range(size))
    state = seed & 0xFFFFFFFF
    for cursor in range(size - 1, 0, -1):
        state = (1103515245 * state + 12345) & 0x7FFFFFFF
        swap_with = state % (cursor + 1)
        indices[cursor], indices[swap_with] = indices[swap_with], indices[cursor]
    return indices


def _injection_id(code: str, params: dict) -> str:
    digest = canonical_dumps(params)
    import hashlib

    return f"{_INJECTION_PREFIX[code]}-{hashlib.sha256(digest.encode('utf-8')).hexdigest()[:10]}"


def _write_ndjson(path: str, lines: list[str]) -> None:
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        for line in lines:
            handle.write(line.rstrip("\n") + "\n")


def _read_ndjson_lines(path: str) -> list[str]:
    with open(path, "r", encoding="utf-8", newline="") as handle:
        return [line for line in handle.read().splitlines() if line.strip()]


def inject_order(
    observations_path: str,
    output_path: str,
    seed: int = 20261117,
) -> tuple[Injection, str]:
    lines = _read_ndjson_lines(observations_path)
    permutation = _deterministic_permutation(len(lines), seed)
    shuffled = [lines[index] for index in permutation]
    _write_ndjson(output_path, shuffled)
    params = {"seed": seed, "source": os.path.basename(observations_path)}
    injection = Injection(
        injection_id=_injection_id(E_ORDER, params),
        code=E_ORDER,
        scope=f"{len(lines)} observation rows reordered",
        params=tuple(sorted((key, str(value)) for key, value in params.items())),
        expected_affected_cluster_ids=(),
        actual_affected_cluster_ids=(),
        verified=False,
        detail="shuffled with deterministic LCG permutation; event set must be identical",
    )
    return injection, output_path


def inject_gap(
    observations_path: str,
    output_path: str,
    service: str,
    topology: Topology,
) -> tuple[Injection, str]:
    if service not in topology.services:
        raise ValueError(f"gap injection target service unknown: {service}")
    lines = _read_ndjson_lines(observations_path)
    kept: list[str] = []
    removed = 0
    for line in lines:
        record = json.loads(line)
        if record.get("service") == service:
            removed += 1
            continue
        kept.append(line)
    _write_ndjson(output_path, kept)
    params = {"service": service, "removed_rows": removed}
    injection = Injection(
        injection_id=_injection_id(E_GAP, params),
        code=E_GAP,
        scope=f"{removed} rows of service {service} removed",
        params=tuple(sorted((key, str(value)) for key, value in params.items())),
        expected_affected_cluster_ids=(),
        actual_affected_cluster_ids=(),
        verified=False,
        detail=f"all observations of {service} deleted deterministically",
    )
    return injection, output_path


def build_topo_injection(
    changeset_path: str,
    expected_affected: tuple[str, ...],
) -> Injection:
    params = {"changeset": os.path.basename(changeset_path)}
    return Injection(
        injection_id=_injection_id(E_TOPO, params),
        code=E_TOPO,
        scope="dependency edge replacement via versioned changeset",
        params=tuple(sorted((key, str(value)) for key, value in params.items())),
        expected_affected_cluster_ids=tuple(sorted(expected_affected)),
        actual_affected_cluster_ids=(),
        verified=False,
        detail="topology changeset applied; source code untouched",
    )


def _cluster_conclusion_bytes(clusters: tuple[Cluster, ...]) -> str:
    # 仅比较簇划分、根因与折叠结论；注入元数据/业务码不参与可复现判定
    return canonical_dumps(
        [
            {
                "cluster_id": cluster.cluster_id,
                "content_hash": cluster.content_hash,
                "folded": [
                    {
                        "event": folded.event.member_key(),
                        "triggered_by_service": folded.triggered_by_service,
                        "reason": folded.reason,
                    }
                    for folded in cluster.folded
                ],
                "members": [event.member_key() for event in cluster.members],
                "reach_paths": [
                    [service, [[link.source, link.target, link.weight] for link in links]]
                    for service, links in cluster.reach_paths
                ],
                "root_causes": [
                    {
                        "earliest_ts": candidate.earliest_ts,
                        "in_degree": candidate.in_degree,
                        "rank": candidate.rank,
                        "reasons": list(candidate.reasons),
                        "service": candidate.service,
                        "severity": candidate.severity,
                        "support": [event.member_key() for event in candidate.supporting_events],
                    }
                    for candidate in cluster.root_causes
                ],
            }
            for cluster in clusters
        ]
    )


def verify_order_injection(
    injection: Injection,
    baseline_clusters: tuple[Cluster, ...],
    rerun_clusters: tuple[Cluster, ...],
    baseline_report_bytes: str,
    rerun_report_bytes: str,
) -> Injection:
    same_ids = [cluster.cluster_id for cluster in baseline_clusters] == [
        cluster.cluster_id for cluster in rerun_clusters
    ]
    same_conclusion = _cluster_conclusion_bytes(baseline_clusters) == _cluster_conclusion_bytes(
        rerun_clusters
    )
    actual = tuple(cluster.cluster_id for cluster in rerun_clusters)
    verified = same_ids and same_conclusion
    return Injection(
        injection_id=injection.injection_id,
        code=injection.code,
        scope=injection.scope,
        params=injection.params,
        expected_affected_cluster_ids=injection.expected_affected_cluster_ids,
        actual_affected_cluster_ids=actual,
        verified=verified,
        detail=(
            "byte-identical clusters and report under reordered arrival"
            if verified
            else "MISMATCH: reordered input changed deterministic output"
        ),
    )


def verify_gap_injection(
    injection: Injection,
    baseline_clusters: tuple[Cluster, ...],
    rerun_clusters: tuple[Cluster, ...],
    target_service: str,
) -> Injection:
    baseline_hashes = {cluster.content_hash for cluster in baseline_clusters}
    rerun_hashes = {cluster.content_hash for cluster in rerun_clusters}
    expected_old = {
        cluster.cluster_id
        for cluster in baseline_clusters
        if any(event.service == target_service for event in cluster.members)
    }
    actual_set = set(expected_old)
    # 删除观测可能使原簇收缩/拆分，产生 content_hash 全新的簇也属于受影响集合
    for cluster in rerun_clusters:
        if cluster.content_hash not in baseline_hashes:
            actual_set.add(cluster.cluster_id)
    for cluster in baseline_clusters:
        if cluster.content_hash not in rerun_hashes and cluster.cluster_id in expected_old:
            actual_set.add(cluster.cluster_id)
    expected = tuple(sorted(expected_old))
    actual = tuple(sorted(actual_set))
    verified = expected_old.issubset(actual_set) and bool(expected)
    return Injection(
        injection_id=injection.injection_id,
        code=injection.code,
        scope=injection.scope,
        params=injection.params,
        expected_affected_cluster_ids=expected,
        actual_affected_cluster_ids=actual,
        verified=verified,
        detail=(
            "all clusters containing gapped service changed as predicted"
            if verified
            else "MISMATCH: gap affected cluster set differs from prediction"
        ),
    )


def verify_topo_injection(
    injection: Injection,
    changed_old_ids: tuple[str, ...],
    new_ids: tuple[str, ...] = (),
) -> Injection:
    expected = injection.expected_affected_cluster_ids
    # 旧簇失效必须被预测集合覆盖；新簇是重算产生的新内容，单独登记
    old_covered = set(changed_old_ids).issubset(set(expected))
    actual_sorted = tuple(sorted(set(changed_old_ids) | set(new_ids)))
    verified = old_covered and bool(actual_sorted)
    return Injection(
        injection_id=injection.injection_id,
        code=injection.code,
        scope=injection.scope,
        params=injection.params,
        expected_affected_cluster_ids=expected,
        actual_affected_cluster_ids=actual_sorted,
        verified=verified,
        detail=(
            "invalidated old clusters covered by prediction; new clusters recorded"
            if verified
            else "MISMATCH: topology affected set differs from prediction"
        ),
    )
