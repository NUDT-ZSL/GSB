"""诊断流水线编排：加载 -> 判定 -> 关联 -> 根因 -> 报告。

run_id 由输入文件内容指纹 + 窗口 + 并发度推导，同输入重复运行得到同一
run_id 与逐字节一致的报告；报告中不包含墙钟时间、进程 id 等非确定字段。
"""
from __future__ import annotations

import hashlib
import os

from .correlation import correlate
from .graph import TopologyGraph
from .injection import (
    build_topo_injection,
    verify_gap_injection,
    verify_order_injection,
    verify_topo_injection,
)
from .loader import load_observations, load_rules, load_topology
from .models import (
    DiagnosticReport,
    Injection,
    Observation,
    RuleSet,
    Topology,
    canonical_dumps,
    dataclass_to_jsonable,
)
from .persistence import WorkbenchStore
from .rules import evaluate_events
from .versioning import (
    affected_by_edge_change,
    apply_topology_changeset,
    build_version_diff,
)


def input_fingerprint(topology_path: str, rules_path: str, observations_path: str) -> str:
    """逻辑输入指纹：观测行先排序再哈希，故与到达顺序无关。"""
    digest = hashlib.sha256()
    for label, path in (("topology", topology_path), ("rules", rules_path)):
        with open(path, "rb") as handle:
            digest.update(label.encode("utf-8"))
            digest.update(handle.read())
            digest.update(b"\0")
    with open(observations_path, "r", encoding="utf-8", newline="") as handle:
        ordered_lines = sorted(line.strip() for line in handle if line.strip())
    digest.update("observations".encode("utf-8"))
    digest.update("\n".join(ordered_lines).encode("utf-8"))
    return digest.hexdigest()


def make_run_id(
    topology_path: str,
    rules_path: str,
    observations_path: str,
    window_ms: int,
    concurrency: int,
    tag: str,
) -> str:
    # run_id 只依赖逻辑输入与窗口；并发度/标签是执行参数，不影响报告字节
    raw = canonical_dumps(
        {
            "fingerprint": input_fingerprint(topology_path, rules_path, observations_path),
            "window_ms": window_ms,
        }
    )
    return "RUN-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]


def make_session_id(run_id: str) -> str:
    return "SESS-" + run_id[4:]


def evaluate_with_cursor(
    observations: list[Observation],
    ruleset: RuleSet,
    store: WorkbenchStore | None,
    session_id: str,
    concurrency: int,
    chunk_size: int = 64,
) -> tuple:
    """分块评估并逐块持久化游标，模拟长流程的可恢复检查点。"""
    if store is None:
        return evaluate_events(observations, ruleset, concurrency=concurrency)
    all_events = []
    for start in range(0, len(observations), chunk_size):
        chunk = observations[start : start + chunk_size]
        all_events.extend(evaluate_events(chunk, ruleset, concurrency=concurrency))
        cursor = start + len(chunk)
        store.save_checkpoint(session_id, cursor, start // chunk_size, "rule evaluation chunk")
    all_events.sort(key=lambda event: event.stable_key())
    return tuple(all_events)


def run_pipeline(
    topology_path: str,
    rules_path: str,
    observations_path: str,
    window_ms: int,
    concurrency: int = 1,
    tag: str = "baseline",
    store: WorkbenchStore | None = None,
    topology_override: Topology | None = None,
    injections: tuple[Injection, ...] = (),
    version_diffs: tuple = (),
    business_codes: tuple[str, ...] = (),
    status: str = "COMPLETE",
) -> tuple[DiagnosticReport, Topology, RuleSet, list[Observation]]:
    topology = topology_override or load_topology(topology_path)
    ruleset = load_rules(rules_path, topology)
    observations = load_observations(observations_path, topology, ruleset)

    run_id = make_run_id(
        topology_path, rules_path, observations_path, window_ms, concurrency, tag
    )
    session_id = make_session_id(run_id)
    if store is not None:
        store.begin_session(
            session_id,
            topology.version,
            ruleset.version,
            len(observations),
            window_ms,
            input_fingerprint(topology_path, rules_path, observations_path),
        )

    events = evaluate_with_cursor(
        observations, ruleset, store, session_id, concurrency
    )
    clusters = correlate(events, topology, ruleset, window_ms)
    for injection in injections:
        if store is not None:
            store.save_injection(session_id, injection)
    for diff in version_diffs:
        if store is not None:
            store.save_version_diff(session_id, diff)

    report = DiagnosticReport(
        session_id=session_id,
        run_id=run_id,
        topology_version=topology.version,
        rule_version=ruleset.version,
        window_ms=window_ms,
        observation_cursor=len(observations),
        observation_total=len(observations),
        event_count=len(events),
        clusters=clusters,
        injections=tuple(sorted(injections, key=lambda item: item.injection_id)),
        version_diffs=version_diffs,
        business_codes=tuple(sorted(set(business_codes))),
        status=status,
        input_fingerprint=input_fingerprint(topology_path, rules_path, observations_path),
    )
    if store is not None and status == "COMPLETE":
        store.complete_session(report)
    return report, topology, ruleset, observations


def report_canonical_bytes(report: DiagnosticReport) -> str:
    return canonical_dumps(dataclass_to_jsonable(report))


def rerun_order_scenario(
    baseline_report: DiagnosticReport,
    topology_path: str,
    rules_path: str,
    shuffled_path: str,
    window_ms: int,
    concurrency: int,
    injection: Injection,
    store: WorkbenchStore | None = None,
) -> tuple[DiagnosticReport, Injection]:
    rerun, _, _, _ = run_pipeline(
        topology_path,
        rules_path,
        shuffled_path,
        window_ms,
        concurrency=concurrency,
        tag="e-order",
        store=store,
        injections=(injection,),
        business_codes=(injection.code,),
    )
    verified = verify_order_injection(
        injection,
        baseline_report.clusters,
        rerun.clusters,
        report_canonical_bytes(baseline_report),
        report_canonical_bytes(rerun),
    )
    rerun_injections = tuple(
        verified if item.injection_id == injection.injection_id else item
        for item in rerun.injections
    )
    rerun = DiagnosticReport(
        **{**dataclass_to_jsonable(rerun), "injections": rerun_injections}
    ) if False else _replace_injections(rerun, rerun_injections)
    if store is not None:
        store.save_injection(rerun.session_id, verified)
    return rerun, verified


def _replace_injections(report: DiagnosticReport, injections: tuple) -> DiagnosticReport:
    return DiagnosticReport(
        session_id=report.session_id,
        run_id=report.run_id,
        topology_version=report.topology_version,
        rule_version=report.rule_version,
        window_ms=report.window_ms,
        observation_cursor=report.observation_cursor,
        observation_total=report.observation_total,
        event_count=report.event_count,
        clusters=report.clusters,
        injections=injections,
        version_diffs=report.version_diffs,
        business_codes=report.business_codes,
        status=report.status,
        input_fingerprint=report.input_fingerprint,
    )


def rerun_gap_scenario(
    baseline_report: DiagnosticReport,
    topology: Topology,
    topology_path: str,
    rules_path: str,
    gapped_path: str,
    window_ms: int,
    concurrency: int,
    injection: Injection,
    target_service: str,
    store: WorkbenchStore | None = None,
) -> tuple[DiagnosticReport, Injection]:
    rerun, _, _, _ = run_pipeline(
        topology_path,
        rules_path,
        gapped_path,
        window_ms,
        concurrency=concurrency,
        tag="e-gap",
        store=store,
        topology_override=topology,
        business_codes=(injection.code,),
    )
    verified = verify_gap_injection(
        injection, baseline_report.clusters, rerun.clusters, target_service
    )
    rerun = _replace_injections(rerun, (verified,))
    if store is not None:
        store.save_injection(rerun.session_id, verified)
    return rerun, verified


def run_topology_evolution(
    baseline_report: DiagnosticReport,
    topology: Topology,
    topology_path: str,
    rules_path: str,
    observations_path: str,
    changeset_path: str,
    window_ms: int,
    concurrency: int,
    store: WorkbenchStore | None = None,
) -> tuple[DiagnosticReport, Topology, Injection, object]:
    old_graph = TopologyGraph(topology)
    new_topology, removed_edges, added_edges = apply_topology_changeset(
        topology, changeset_path
    )
    predicted_affected = affected_by_edge_change(
        baseline_report.clusters, old_graph, removed_edges, added_edges
    )
    injection = build_topo_injection(changeset_path, predicted_affected)

    rerun, _, _, _ = run_pipeline(
        topology_path,
        rules_path,
        observations_path,
        window_ms,
        concurrency=concurrency,
        tag="e-topo",
        store=store,
        topology_override=new_topology,
        injections=(injection,),
        business_codes=(injection.code,),
    )
    old_hashes = {cluster.content_hash for cluster in baseline_report.clusters}
    new_hashes = {cluster.content_hash for cluster in rerun.clusters}
    changed_old_ids = {
        cluster.cluster_id
        for cluster in baseline_report.clusters
        if cluster.content_hash not in new_hashes
    }
    changed_new_ids = {
        cluster.cluster_id
        for cluster in rerun.clusters
        if cluster.content_hash not in old_hashes
    }
    actual_affected = tuple(sorted(changed_old_ids | changed_new_ids))
    diff = build_version_diff(
        baseline_report.clusters,
        rerun.clusters,
        topology.version,
        new_topology.version,
        baseline_report.rule_version,
        baseline_report.rule_version,
        removed_edges=removed_edges,
        added_edges=added_edges,
        predicted_invalid=predicted_affected,
    )
    changed_old_ids_tuple = tuple(sorted(changed_old_ids))
    new_ids_tuple = tuple(sorted(changed_new_ids))
    verified = verify_topo_injection(
        injection, changed_old_ids_tuple, new_ids_tuple
    )
    rerun = _replace_injections(rerun, (verified,))
    rerun = DiagnosticReport(
        session_id=rerun.session_id,
        run_id=rerun.run_id,
        topology_version=rerun.topology_version,
        rule_version=rerun.rule_version,
        window_ms=rerun.window_ms,
        observation_cursor=rerun.observation_cursor,
        observation_total=rerun.observation_total,
        event_count=rerun.event_count,
        clusters=rerun.clusters,
        injections=(verified,),
        version_diffs=(diff,),
        business_codes=rerun.business_codes,
        status=rerun.status,
        input_fingerprint=rerun.input_fingerprint,
    )
    if store is not None:
        store.save_injection(rerun.session_id, verified)
        store.save_version_diff(rerun.session_id, diff)
        store.complete_session(rerun)
    return rerun, new_topology, verified, diff
