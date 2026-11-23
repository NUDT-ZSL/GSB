"""Interactive local professional AED application (terminal UI)."""

from __future__ import annotations

import json
import os
import sys

from .errors import AEDError
from .pipeline import NODES, UPSTREAM
from .studio import Studio

BANNER = "AED Studio - deterministic local audio event detection"


def _fmt_status(status: dict) -> str:
    return "  ".join(f"{n}:{'HIT' if status[n] == 'hit' else 'RECOMP'}"
                     for n in NODES)


def show_topology() -> None:
    print("\n[计算图拓扑]")
    for node in NODES:
        ups = ", ".join(UPSTREAM[node]) or "(source)"
        print(f"  {node:<10} <- {ups}")


def show_reports(studio: Studio) -> None:
    if not studio.last_report:
        print("尚未运行检测链路。")
        return
    print("\n[节点状态 / 事件明细 / 指标]")
    for did, rep in studio.last_report.items():
        print(f"\n  dialog={did}")
        print(f"    {_fmt_status(rep['status'])}")
        for seg in rep["values"]["segments"]:
            print(f"    evt#{seg['event_seq']:<2} {seg['event_type']:<9}"
                  f" {seg['start_ms']:>6}ms - {seg['end_ms']:>6}ms"
                  f"  conf={seg['confidence']:.4f}")
        mets = rep["values"]["metrics"]
        print(f"    frame-level: {json.dumps(mets['frame_level'], sort_keys=True)}")
        print(f"    event-level: {json.dumps(mets['event_level'], sort_keys=True)}")


def show_cache_keys(studio: Studio) -> None:
    print("\n[缓存键组成与产物哈希]")
    for node in NODES:
        for meta in studio.describe_cache(node):
            comps = json.dumps(meta["components"], sort_keys=True)
            print(f"  {node:<10} key={meta['key'][:16]}… "
                  f"artifact={meta['artifact_hash'][:16]}…")
            print(f"    components={comps[:160]}")


def incremental_demo(studio: Studio) -> None:
    print("\n[增量演示] 选择变更类型:")
    print("  1) 模板库升级 t1 -> t2")
    print("  2) 修正单条事件真值")
    print("  3) 调整帧移 hop 160 -> 80")
    print("  4) 调整最短事件时长 60 -> 100ms")
    choice = input("  > ").strip()
    dialog_id = "dlg-001"
    if choice == "1":
        change = {"kind": "template_upgrade", "template_version": "t2"}
    elif choice == "2":
        change = {"kind": "truth_fix", "event_index": 0, "new_type": "cough"}
    elif choice == "3":
        change = {"kind": "hop_change", "hop": 80}
    elif choice == "4":
        change = {"kind": "min_duration_change", "value": 100}
    else:
        print("无效选择。")
        return
    result = studio.apply_change(change, dialog_id)
    print(f"\n  重算节点: {result['recompute_nodes']}")
    print(f"  复用节点: {result['reused_nodes']}")
    print(f"  受影响区间(ms): {result['affected_intervals_ms']}")
    print(f"  受影响事件: {result['affected_events']}")
    studio.last_report = {dialog_id: result["report"]}
    ref = studio.full_recompute_bytes(dialog_id, dialog=result["dialog"])
    inc = Studio._report_bytes(result["report"])
    print(f"  与整体重算逐字节一致: {inc == ref}")


def trace_menu(studio: Studio) -> None:
    records = studio.trace.load()
    print(f"\n[追溯] 共 {len(records)} 条检测结论记录")
    for i, rec in enumerate(records[:20]):
        print(f"  [{i}] {rec['dialog_id']} evt#{rec['event_seq']} "
              f"{rec['event_type']} {rec['start_ms']}-{rec['end_ms']}ms "
              f"tpl={rec['template_version']} key={rec['cache_key'][:12]}…")
    raw = input("  输入记录编号重放 (回车取消): ").strip()
    if not raw:
        return
    try:
        replayed = studio.replay_event(int(raw))
        print(f"  重放一致: {replayed}")
    except AEDError as err:
        print(f"  拒绝: {json.dumps(err.to_dict(), ensure_ascii=False)}")


def main() -> None:
    workspace = sys.argv[1] if len(sys.argv) > 1 else "workspace"
    studio = Studio(workspace)
    print(BANNER)
    print(f"workspace: {os.path.abspath(workspace)}")
    menu = ("\n1) 生成确定性数据集  2) 运行完整检测链路  3) 计算图拓扑\n"
            "4) 事件明细与指标    5) 缓存键查询        6) 增量演示\n"
            "7) 追溯与重放        0) 退出")
    while True:
        print(menu)
        choice = input("> ").strip()
        try:
            if choice == "1":
                path = studio.generate_dataset()
                print(f"数据集已生成: {path}")
            elif choice == "2":
                studio.run_all()
                show_reports(studio)
            elif choice == "3":
                show_topology()
            elif choice == "4":
                show_reports(studio)
            elif choice == "5":
                show_cache_keys(studio)
            elif choice == "6":
                incremental_demo(studio)
            elif choice == "7":
                trace_menu(studio)
            elif choice == "0":
                return
        except AEDError as err:
            print(json.dumps(err.to_dict(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
