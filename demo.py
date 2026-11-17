"""本地确定性演示入口：加载 -> 风险 -> 升级增量 -> 执行 -> 崩溃恢复 -> 重放 -> 异常注入。

用法：
    python demo.py                 # 顺序演示全流程
    python demo.py --inject corrupt-ticket|cycle-deps|version-conflict
"""

from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from rollback_ops.app import OpsApp
from rollback_ops.errors import OpsError
from rollback_ops.graph import remove_step, topo_order
from rollback_ops.ledger import (DeterministicClock, open_ledger, recover,
                                 verify_ledger)
from rollback_ops.loader import load_plan, load_ticket
from rollback_ops.risk import compare_scores, compute_risk

LEDGER_PATH = "artifacts/demo_ledger.db"


def show(title, payload):
    print(f"\n=== {title} ===")
    if isinstance(payload, str):
        print(payload)
    else:
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2))


def expect_error(title, fn):
    try:
        fn()
    except OpsError as exc:
        show(title, exc.to_dict())
        return exc.code
    show(title, "<未触发预期错误>")
    return None


def main(argv=None):
    parser = argparse.ArgumentParser(prog="demo")
    parser.add_argument("--inject",
                        choices=["corrupt-ticket", "cycle-deps", "version-conflict"])
    parser.add_argument("--ledger", default=LEDGER_PATH)
    args = parser.parse_args(argv)

    if os.path.exists(args.ledger):
        os.remove(args.ledger)

    if args.inject:
        return run_injection(args)

    # 1) 加载 v1 并归一
    app = OpsApp(open_ledger(args.ledger))
    view = app.load("samples/ticket_v1.json", "samples/rollback_plan.json")
    show("1. v1 变更单归一（带版本来源标记）", view.ticket.to_dict())
    show("1b. v1 风险（定点，含误差界）", view.report.to_dict())

    # 2) 演进 v1 -> v2 -> v3：增量重算 + 整体替换
    for path in ("samples/ticket_v2.json", "samples/ticket_v3.json"):
        view, replacement, incr = app.evolve(path)
        show(f"2. 升级到 {path}：整体替换报告", {
            "old": replacement.old_label, "new": replacement.new_label,
            "changed_items": list(replacement.changed_items),
            "affected_steps": list(replacement.affected_steps),
            "cache_entries_cleared": replacement.cache_entries_cleared,
            "recomputed_keys": list(incr.recomputed_keys),
            "reused_keys": list(incr.reused_keys),
        })
    show("2b. 只读历史版本列表", app.store.version_labels())

    # 3) 风险比较：误差界主导的决胜
    report_v3 = app.store.current.report
    ticket_rev4 = load_ticket("samples/ticket_v3_rev4.json")
    report_rev4 = compute_risk(ticket_rev4)
    show("3. 风险分数比较（误差界决胜规则）",
         compare_scores(report_v3, report_rev4).to_dict())

    # 4) 执行回滚计划（确定性时钟）
    entries = app.execute(clock=DeterministicClock())
    show("4. 回滚执行账本", [e.to_dict() for e in entries])
    app.verify_ledger()
    show("4b. 账本与计划逐项对应校验", "OK")

    # 5) 幂等重放：重复执行不重复写入
    again = app.execute(clock=DeterministicClock())
    show("5. 幂等重放（idempotent_replay 标记）",
         [{"step_id": e.step_id, "idempotent_replay": e.idempotent_replay} for e in again])

    # 6) 版本演进后整体替换 + 新计划执行
    view, replacement, incr = app.evolve("samples/ticket_v3_rev4.json")
    show("6. 演进 rev4：变化项/受影响步骤/清空缓存", {
        "changed_items": list(replacement.changed_items),
        "affected_steps": list(replacement.affected_steps),
        "cache_entries_cleared": replacement.cache_entries_cleared,
    })
    entries = app.execute(clock=DeterministicClock())
    show("6b. rev4 执行账本", [e.to_dict() for e in entries])

    # 7) 模拟强制终止后恢复 + 一致性重放
    crash_ledger = "artifacts/demo_ledger_crash.db"
    if os.path.exists(crash_ledger):
        os.remove(crash_ledger)
    app2 = OpsApp(open_ledger(crash_ledger))
    app2.load("samples/ticket_v3.json", "samples/rollback_plan.json")

    class SimulatedKill(Exception):
        pass

    def crash_hook(index):
        if index == 2:
            raise SimulatedKill("process killed before writing step 3")

    try:
        app2.execute(clock=DeterministicClock(), crash_hook=crash_hook)
    except SimulatedKill:
        pass
    report = app2.recover()
    show("7. 崩溃恢复（未完成步骤=unknown，需重跑；不生成退出报告）", report.to_dict())

    # 8) 异常业务码演示
    expect_error("8a. E_FORMAT：损坏变更单",
                 lambda: load_ticket("samples/broken/ticket_corrupt.json"))
    expect_error("8b. E_FORMAT：非法类别（带字段路径）",
                 lambda: load_ticket("samples/broken/ticket_bad_category.json"))
    expect_error("8c. E_DEPEND：依赖成环（输出环序列）",
                 lambda: topo_order(load_plan("samples/broken/rollback_plan_cycle.json")))
    plan = load_plan("samples/rollback_plan.json")
    expect_error("8d. E_DEPEND：删除仍被依赖的步骤",
                 lambda: remove_step(plan, "rb-01"))

    def ledger_divergence():
        verify_ledger(app2.conn, plan, ticket_revision=3)  # app2 只执行了 2 步
    expect_error("8e. E_LEDGER：缺失步骤", ledger_divergence)

    def version_conflict():
        stale = load_ticket("samples/ticket_v2.json")
        app.store.replace_ticket(stale)  # revision 2 <= 当前 4
    expect_error("8f. E_VERSION：版本号回退", version_conflict)

    print("\n演示完成：全部输出由确定性样本产生，可复现。")
    return 0


def run_injection(args) -> int:
    """异常注入：仅改变输入文件或参数，分别报告 E_FORMAT / E_DEPEND / E_VERSION。"""
    app = OpsApp(open_ledger(args.ledger))
    try:
        if args.inject == "corrupt-ticket":
            app.load("samples/broken/ticket_corrupt.json", "samples/rollback_plan.json")
        elif args.inject == "cycle-deps":
            app.load("samples/ticket_v3.json", "samples/broken/rollback_plan_cycle.json")
        elif args.inject == "version-conflict":
            app.load("samples/ticket_v3.json", "samples/rollback_plan.json")
            app.execute(clock=DeterministicClock())
            stale = load_ticket("samples/ticket_v2.json")
            app.store.replace_ticket(stale)
    except OpsError as exc:
        print(exc.render())
        return 1
    print("注入未触发错误", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
