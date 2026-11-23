#!/usr/bin/env python3
"""Repeatable local demo: full run + incremental invalidation + replay."""

from __future__ import annotations

import shutil
import sys

from aed.studio import Studio

WS = "workspace_demo"


def main() -> int:
    shutil.rmtree(WS, ignore_errors=True)
    studio = Studio(WS)

    print("== 1. 生成确定性数据集 ==")
    print("  manifest:", studio.generate_dataset())

    print("== 2. 完整检测链路（首次全量重算） ==")
    reports = studio.run_all()
    for did, rep in reports.items():
        print(f"  {did}: {rep['status']}")
        print(f"    events={len(rep['values']['segments'])} "
              f"frame_f1={rep['values']['metrics']['frame_level']['f1']}")

    print("== 3. 二次运行（全部缓存命中） ==")
    reports = studio.run_all()
    for did, rep in reports.items():
        assert all(s == "hit" for s in rep["status"].values())
        print(f"  {did}: {rep['status']}")

    print("== 4. 增量：模板库升级 t1 -> t2 ==")
    res = studio.apply_change(
        {"kind": "template_upgrade", "template_version": "t2"}, "dlg-001")
    print(f"  重算={res['recompute_nodes']} 复用={res['reused_nodes']}")
    print(f"  受影响区间(ms)={res['affected_intervals_ms']}")
    same = Studio._report_bytes(res["report"]) == studio.full_recompute_bytes(
        "dlg-001", dialog=res["dialog"])
    print(f"  与整体重算逐字节一致: {same}")
    assert same

    print("== 5. 增量：修正单条事件真值 ==")
    res = studio.apply_change(
        {"kind": "truth_fix", "event_index": 0, "new_type": "cough"},
        "dlg-001")
    print(f"  重算={res['recompute_nodes']} 复用={res['reused_nodes']}")
    print(f"  受影响区间(ms)={res['affected_intervals_ms']}")
    same = Studio._report_bytes(res["report"]) == studio.full_recompute_bytes(
        "dlg-001", dialog=res["dialog"])
    print(f"  与整体重算逐字节一致: {same}")
    assert same

    print("== 6. 增量：最短事件时长 60 -> 100ms ==")
    res = studio.apply_change(
        {"kind": "min_duration_change", "value": 100}, "dlg-001")
    print(f"  重算={res['recompute_nodes']} 复用={res['reused_nodes']}")
    same = Studio._report_bytes(res["report"]) == studio.full_recompute_bytes(
        "dlg-001", dialog=res["dialog"])
    print(f"  与整体重算逐字节一致: {same}")
    assert same

    print("== 7. 追溯与重放 ==")
    records = studio.trace.load()
    print(f"  结论记录数: {len(records)}")
    replayed = studio.replay_event(len(records) - 1)
    print(f"  末条重放一致: {replayed}")

    print("\n演示完成，全部断言通过。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
