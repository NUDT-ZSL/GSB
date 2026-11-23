"""完整示例：三格式互操作、增量失效、快照整体替换与一致性自检。

用法：python examples/run_demo.py --workdir <本地临时目录>
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from memcomp import MemoryCompressor, WindowSpec
from memcomp.errors import MemError
from memcomp.model import canonical_bytes
from memcomp.samplegen import (
    append_turn,
    generate_entry_samples,
    generate_sessions,
    revise_turn,
    write_sessions_jsonl,
)
from memcomp.snapshot import StageLock


def capture(fn):
    """执行 fn，把 MemError 转成 {code, message} 记录。"""
    try:
        return {"ok": True, "result": fn()}
    except MemError as exc:
        return {"ok": False, "error": exc.to_dict()}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--workdir", required=True, help="本地临时目录（缓存/快照/报告）")
    parser.add_argument("--seed", type=int, default=20240101)
    args = parser.parse_args()
    workdir = Path(args.workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    report = {"workdir": str(workdir), "seed": args.seed, "steps": {}}

    # 1. 确定性样本：会话清单 + 三格式记忆条目
    spec = WindowSpec()
    sessions = generate_sessions(seed=args.seed)
    sessions_path = workdir / "sessions.jsonl"
    write_sessions_jsonl(sessions, sessions_path)
    sample_paths, sample_entries = generate_entry_samples(sessions, spec, workdir)
    report["steps"]["samples"] = {
        "sessions_jsonl": str(sessions_path),
        "entry_files": sample_paths,
        "entry_count": len(sample_entries),
    }

    comp = MemoryCompressor(str(workdir), spec)

    # 2. 三格式读取 + 双向迁移
    v1_ver, v1_raw = comp.load(sample_paths[1])
    v2_ver, v2_raw = comp.load(sample_paths[2])
    v3_ver, v3_raw = comp.load(sample_paths[3])
    up12, rec12 = comp.migrate(v1_raw, 1, 2)
    up23, rec23 = comp.migrate(up12, 2, 3)
    up23_direct, rec23_direct = comp.migrate(v2_raw, 2, 3)
    down31, rec31 = comp.migrate(v3_raw, 3, 1)
    deterministic = all(
        canonical_bytes(a) == canonical_bytes(b)
        for a, b in zip(comp.migrate(v1_raw, 1, 3)[0], up23)
    )
    report["steps"]["migration"] = {
        "versions_detected": [v1_ver, v2_ver, v3_ver],
        "completion_records_v1_to_v2": len(rec12),
        "completion_records_v2_to_v3": len(rec23),
        "discard_records_v3_to_v1": len(rec31),
        "discard_rules": sorted({r["rule"].split(":")[0] for r in rec31}),
        "migration_deterministic_byte_identical": deterministic,
    }

    # 3. 记忆压缩流水线（首次，全部重算）
    first = comp.merge_entries(sessions)
    report["steps"]["pipeline_first_run"] = {
        "windows": {sid: len(ws) for sid, ws in first.windows.items()},
        "entries": len(first.entries),
        "chain_refs": {k: list(v) for k, v in first.chain_refs.items() if v},
        "cache": first.cache_report,
        "pruning": first.pruning,
    }

    # 4. 追加轮次 + 修订轮次后的定向失效
    new_sessions = {sid: {"session_id": s["session_id"], "turns": [dict(t) for t in s["turns"]]}
                    for sid, s in sessions.items()}
    s02 = new_sessions["s02"]
    new_sessions["s02"] = append_turn(s02, ["w005", "w006", "w007", "w005"], 1_700_000_900_000)
    new_sessions["s03"] = revise_turn(new_sessions["s03"], 2, ["w010", "w011"] * 4)
    inv = comp.invalidation_report(sessions, new_sessions)
    second = comp.merge_entries(new_sessions)
    all_wids = [w.window_id for ws in second.windows.values() for w in ws]
    all_eids = [e.entry_id for e in second.entries]
    comp.self_check_invalidation(inv, all_wids, all_eids)
    report["steps"]["invalidation"] = {
        "changes": inv["changes"],
        "recompute_windows": inv["recompute_windows"],
        "reuse_windows": inv["reuse_windows"],
        "recompute_entries": inv["recompute_entries"],
        "reuse_entries": inv["reuse_entries"],
        "cache_after_change": second.cache_report,
        "self_check": "passed",
    }

    # 5. 调整窗口上限 -> 全量失效
    spec2 = WindowSpec(window_token_limit=18)
    inv2 = comp.invalidation_report(sessions, sessions, new_spec=spec2)
    report["steps"]["window_param_change"] = {
        "recompute_windows": len(inv2["recompute_windows"]),
        "reuse_windows": len(inv2["reuse_windows"]),
    }

    # 6. 快照整体替换与并发提交控制（版本号基于当前值递增，示例可重复运行）
    base_entries = comp.snapshots.current_version("entries")
    payload_v1 = json.dumps([canonical_bytes(e).decode() for e in up23])
    comp.snapshots.commit("entries", payload_v1, base_entries + 1)
    payload_v2 = json.dumps([canonical_bytes(e).decode() for e in up23_direct])
    comp.snapshots.commit("entries", payload_v2, base_entries + 2)
    rollback = capture(
        lambda: comp.snapshots.commit("entries", payload_v1, base_entries + 1)
    )
    corrupt_path = workdir / "snapshots" / f"entries_v{base_entries + 2}.json"
    corrupt_path.write_text(corrupt_path.read_text().replace('"checksum"', '"checksum_x"', 1))
    corrupt = capture(lambda: comp.snapshots.load("entries", base_entries + 2))
    lock = StageLock(str(workdir)).acquire()
    busy = capture(lambda: StageLock(str(workdir)).acquire())
    lock.release()
    base_cfg = comp.snapshots.current_version("migration_config")
    mig_cfg = json.dumps({"default_confidence": [0.5, 1.0], "discard_rules_version": 1})
    comp.snapshots.commit("migration_config", mig_cfg, base_cfg + 1)
    comp.snapshots.commit("migration_config", json.dumps(
        {"default_confidence": [0.5, 1.0], "discard_rules_version": 2}), base_cfg + 2)
    report["steps"]["snapshot"] = {
        "entries_version": comp.snapshots.current_version("entries"),
        "migration_config_version": comp.snapshots.current_version("migration_config"),
        "rollback_attempt": rollback,
        "corrupt_load": corrupt,
        "stage_busy": busy,
    }

    # 7. 跨模块一致性自检
    comp.self_check_views(up23, up23_direct, v3_raw)
    comp.self_check_replay(v1_raw, rec12, up12)
    report["steps"]["self_check"] = {
        "three_format_views": "passed",
        "migration_replay": "passed",
        "invalidation_sets": "passed",
    }

    report_path = workdir / "report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"报告已写入 {report_path}")
    print(json.dumps({k: ("ok" if v else v) for k, v in
                      {s: True for s in report["steps"]}.items()}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
