"""四类验收场景入口：完整运行 / 中断续跑 / 并发度变更 / 注入故障。

用法: python3 scripts/run_scenarios.py [--workspace DIR]
全部产物落在 workspace/run_artifacts 下，结束打印逐场景核对结论。
"""

import argparse
import json
import os
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLI = [sys.executable, "-m", "ranker.cli"]


def run(args):
    proc = subprocess.run(CLI + args, cwd=ROOT, capture_output=True, text=True)
    print(proc.stdout.strip() or proc.stderr.strip())
    if proc.returncode not in (0, 2):
        raise SystemExit(f"command failed rc={proc.returncode}: {proc.stderr}")
    return proc


def strip_runtime(report):
    import copy
    clone = copy.deepcopy(report)
    for req in clone["requests"]:
        req.pop("cache_events", None)
    return clone


def write_worker_config(src, dst, workers):
    cfg = json.load(open(src))
    cfg["max_workers"] = workers
    json.dump(cfg, open(dst, "w"), ensure_ascii=False, indent=2, sort_keys=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", default=os.path.join(ROOT, "run_artifacts"))
    args = parser.parse_args()
    ws = args.workspace
    if os.path.exists(ws):
        shutil.rmtree(ws)
    os.makedirs(ws, exist_ok=True)
    params = os.path.join(ROOT, "params")
    cand = os.path.join(params, "candidates.jsonl")
    cfg = os.path.join(params, "rank_config.json")
    groups = os.path.join(params, "feature_groups.json")
    inject = os.path.join(params, "injection.json")

    def common(snap, report, extra=None):
        base = ["--candidates", cand, "--config", cfg, "--groups", groups,
                "--snapshot-dir", os.path.join(ws, snap),
                "--report", os.path.join(ws, report), "--no-signal-handlers"]
        return base + (extra or [])

    print("== 场景1: 完整运行（确定性，跑两次逐元素比对） ==")
    run(common("s_full_a", "full_a.json"))
    run(common("s_full_b", "full_b.json"))
    a = json.load(open(os.path.join(ws, "full_a.json")))
    b = json.load(open(os.path.join(ws, "full_b.json")))
    deterministic = strip_runtime(a)["requests"] == strip_runtime(b)["requests"] and a["failures"] == b["failures"]
    print(f"  两次运行逐元素一致: {deterministic}")

    print("== 场景2: 中断后续跑（stop-after 5，再续跑，与完整运行比对） ==")
    run(common("s_resume", "part1.json", ["--stop-after", "5"]))
    run(common("s_resume", "part2.json"))
    p2 = json.load(open(os.path.join(ws, "part2.json")))
    resume_ok = strip_runtime(p2)["requests"] == strip_runtime(a)["requests"] and p2["failures"] == a["failures"]
    print(f"  恢复后与不中断运行逐字段一致: {resume_ok}; 缓存命中={p2['cache']['totals']['hits']}")

    print("== 场景3: 并发度变更（1/2/4/8 线程结果比对） ==")
    worker_reports = []
    for workers in (1, 2, 4, 8):
        wcfg = os.path.join(ws, f"cfg_w{workers}.json")
        write_worker_config(cfg, wcfg, workers)
        run(["--candidates", cand, "--config", wcfg, "--groups", groups,
             "--snapshot-dir", os.path.join(ws, f"s_w{workers}"),
             "--report", os.path.join(ws, f"w{workers}.json"),
             "--no-signal-handlers"])
        worker_reports.append(json.load(open(os.path.join(ws, f"w{workers}.json"))))
    concurrency_ok = all(
        strip_runtime(r)["requests"] == strip_runtime(worker_reports[0])["requests"] and
        r["failures"] == worker_reports[0]["failures"]
        for r in worker_reports[1:]
    )
    print(f"  四种并发度结果逐字段一致: {concurrency_ok}")

    print("== 场景4: 注入故障（四类注入，失败隔离，报告留痕） ==")
    run(common("s_inject", "inject.json", ["--injection", inject]))
    inj_report = json.load(open(os.path.join(ws, "inject.json")))
    pairs = {(f["request_id"], f["code"]) for f in inj_report["failures"]
             if f.get("injection_ids")}
    expected = {("req-003", "E_INPUT_INVALID"),
                ("req-005", "E_CONFIG_INVALID"),
                ("req-008", "E_CONFIG_INVALID")}
    injection_ok = expected.issubset(pairs)
    req002 = next(r for r in inj_report["requests"] if r["request_id"] == "req-002")
    threshold_ok = "inj-004" in req002["injection_hits"] and any(
        p["similarity"] == p["threshold"] for p in req002["penalties"]
    )
    print(f"  三类阶段故障隔离并记录业务码: {injection_ok}")
    print(f"  相似度等值门限注入命中: {threshold_ok}")

    print("\n== 汇总 ==")
    summary = {
        "deterministic": deterministic,
        "resume_consistent": resume_ok,
        "concurrency_consistent": concurrency_ok,
        "injection_isolated": injection_ok,
        "threshold_injection_hit": threshold_ok,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if not all(summary.values()):
        raise SystemExit(1)
    print("全部场景验收通过")


if __name__ == "__main__":
    main()
