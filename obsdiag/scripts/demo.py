"""确定性演示脚本：顺序执行三个场景并输出可逐次比对的诊断总结。

场景 1：首次全量关联（分别以并发度 1/4 运行，校验逐字节一致）
场景 2：注入 E_ORDER 乱序后重算（结论必须逐字节一致），附带 E_GAP 缺失验证
场景 3：拓扑演进 v1->v2（E_TOPO），增量失效 + 整体替换视图
附加：强制终止恢复演示（UNKNOWN + 要求重跑，不生成退出报告）

用法（在项目根目录）：
    python scripts/demo.py
输出全部落在 output/demo/，包含 .summary.txt 与 .canonical.json。
"""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from obsdiag.injection import inject_gap, inject_order  # noqa: E402
from obsdiag.persistence import WorkbenchStore  # noqa: E402
from obsdiag.pipeline import (  # noqa: E402
    report_canonical_bytes,
    run_pipeline,
    run_topology_evolution,
    rerun_gap_scenario,
    rerun_order_scenario,
)
from obsdiag.recovery import recover_session, render_recovery_report  # noqa: E402
from obsdiag.reporting import render_summary  # noqa: E402

TOPO = os.path.join(ROOT, "samples", "v1", "topology.json")
RULES = os.path.join(ROOT, "samples", "v1", "rules.json")
OBS = os.path.join(ROOT, "samples", "v1", "observations.ndjson")
CHANGESET = os.path.join(ROOT, "samples", "v2", "topology_changeset.json")
WORKDIR = os.path.join(ROOT, "output", "demo")
WINDOW_MS = 5000


def _write(name: str, content: str) -> str:
    os.makedirs(WORKDIR, exist_ok=True)
    path = os.path.join(WORKDIR, name)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(content if content.endswith("\n") else content + "\n")
    return path


def main() -> int:
    os.makedirs(WORKDIR, exist_ok=True)
    db_path = os.path.join(WORKDIR, "workbench.sqlite3")
    for suffix in ("", "-wal", "-shm"):
        stale = db_path + suffix
        if os.path.exists(stale):
            os.remove(stale)
    store = WorkbenchStore(db_path)
    failures: list[str] = []

    print("########## 场景 1：首次全量关联（并发度 1 与 4 字节比对） ##########")
    baseline_c1, topology, _, _ = run_pipeline(
        TOPO, RULES, OBS, WINDOW_MS, concurrency=1, tag="01-baseline", store=store
    )
    baseline_c4, _, _, _ = run_pipeline(
        TOPO, RULES, OBS, WINDOW_MS, concurrency=4, tag="01-baseline", store=store
    )
    _write("01-baseline.summary.txt", render_summary(baseline_c1))
    _write("01-baseline.canonical.json", report_canonical_bytes(baseline_c1))
    if report_canonical_bytes(baseline_c1) != report_canonical_bytes(baseline_c4):
        failures.append("scenario1: concurrency 1 vs 4 reports differ")
    print(render_summary(baseline_c1))
    print(f"[check] 并发度 1 vs 4 逐字节一致: {report_canonical_bytes(baseline_c1) == report_canonical_bytes(baseline_c4)}")

    print("\n########## 场景 2a：E_ORDER 乱序注入后重算 ##########")
    shuffled = os.path.join(WORKDIR, "observations.e_order.ndjson")
    order_injection, _ = inject_order(OBS, shuffled)
    order_report, order_verified = rerun_order_scenario(
        baseline_c1, TOPO, RULES, shuffled, WINDOW_MS, 4, order_injection, store=store
    )
    _write("02a-e_order.summary.txt", render_summary(order_report))
    _write("02a-e_order.canonical.json", report_canonical_bytes(order_report))
    print(render_summary(order_report))
    print(f"[check] E_ORDER verified={order_verified.verified}")
    if not order_verified.verified:
        failures.append("scenario2a: E_ORDER verification failed")

    print("\n########## 场景 2b：E_GAP 观测缺失注入后重算 ##########")
    gapped = os.path.join(WORKDIR, "observations.e_gap.ndjson")
    gap_injection, _ = inject_gap(OBS, gapped, "payment-db", topology)
    gap_report, gap_verified = rerun_gap_scenario(
        baseline_c1, topology, TOPO, RULES, gapped, WINDOW_MS, 2,
        gap_injection, "payment-db", store=store,
    )
    _write("02b-e_gap.summary.txt", render_summary(gap_report))
    print(f"[check] E_GAP verified={gap_verified.verified} expected={list(gap_verified.expected_affected_cluster_ids)}")
    if not gap_verified.verified:
        failures.append("scenario2b: E_GAP verification failed")

    print("\n########## 场景 3：E_TOPO 拓扑演进 v1 -> v2（整体替换视图） ##########")
    evolved, new_topology, topo_verified, diff = run_topology_evolution(
        baseline_c1, topology, TOPO, RULES, OBS, CHANGESET, WINDOW_MS, 2, store=store
    )
    _write("03-topo-v2.summary.txt", render_summary(evolved))
    _write("03-topo-v2.canonical.json", report_canonical_bytes(evolved))
    print(render_summary(evolved))
    print(
        f"[check] E_TOPO verified={topo_verified.verified} "
        f"invalidated={list(diff.invalidated_cluster_ids)} retained={list(diff.retained_cluster_ids)} "
        f"new={list(diff.new_cluster_ids)}"
    )
    if not topo_verified:
        failures.append("scenario3: E_TOPO verification failed")

    print("\n########## 附加：强制终止恢复（UNKNOWN，不生成退出报告） ##########")
    crash_session = "SESS-CRASH-DEMO"
    store.begin_session(crash_session, topology.version, 1, 25, WINDOW_MS, "crash-demo")
    store.save_checkpoint(crash_session, 12, 0, "simulated force-kill boundary")
    store.close()
    reopen = WorkbenchStore(db_path)
    recovery = recover_session(reopen, crash_session)
    _write("04-recovery.txt", render_recovery_report(recovery))
    print(render_recovery_report(recovery))
    if not recovery.rerun_required or recovery.status != "UNKNOWN":
        failures.append("recovery: crash session not marked UNKNOWN/rerun-required")
    reopen.close()

    print("\n########## 演示结论 ##########")
    if failures:
        for failure in failures:
            print("FAIL:", failure)
        return 3
    print("ALL CHECKS PASSED: 三场景可复现、注入生效验证通过、快照恢复符合预期")
    print(f"artifacts directory: {WORKDIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
