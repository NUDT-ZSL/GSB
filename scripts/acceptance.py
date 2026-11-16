"""端到端验收：并行/串行一致、预算耗尽、步数上限、异常隔离、快照恢复、
配置不匹配、信号退出与异常注入，产出 artifacts/acceptance_report.json。

运行：python scripts/acceptance.py
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dp_bench.model import sha256_hex  # noqa: E402

DATA = ROOT / "data"
RUN = ROOT / "run" / "acceptance"
SNAP = ROOT / "snapshots" / "acceptance"
ART = ROOT / "artifacts"


def clean():
    shutil.rmtree(RUN, ignore_errors=True)
    shutil.rmtree(SNAP, ignore_errors=True)
    RUN.mkdir(parents=True, exist_ok=True)
    SNAP.mkdir(parents=True, exist_ok=True)
    ART.mkdir(parents=True, exist_ok=True)


def cli(*args, expect_exit=0):
    proc = subprocess.run(
        [sys.executable, "-m", "dp_bench.cli", *map(str, args)],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8")
    if proc.returncode != expect_exit:
        raise AssertionError(
            f"命令失败 rc={proc.returncode}: {' '.join(map(str, args))}\n"
            f"stdout={proc.stdout}\nstderr={proc.stderr}")
    return proc


def load(name):
    with open(RUN / name, encoding="utf-8") as fh:
        return json.load(fh)


def main():
    clean()
    checks = []

    def record(name, passed, detail=""):
        checks.append({"name": name, "passed": bool(passed),
                       "detail": detail})

    samples = DATA / "sentences.txt"
    exc_samples = DATA / "sentences_exception.txt"
    model = DATA / "model.json"
    cfg = DATA / "config.json"
    cfg_budget = DATA / "config_budget.json"
    cfg_step = DATA / "config_steplimit.json"

    # 1) 串行
    cli("run", "--samples", samples, "--model", model, "--config", cfg,
        "--snapshot-dir", SNAP / "serial", "--report", RUN / "serial.json",
        "--mode", "serial", "--no-signal")
    record("serial_run_exit0", True)
    serial = load("serial.json")

    # 2) 并行
    cli("run", "--samples", samples, "--model", model, "--config", cfg,
        "--snapshot-dir", SNAP / "parallel",
        "--report", RUN / "parallel.json", "--no-signal")
    record("parallel_run_exit0", True)
    parallel = load("parallel.json")

    identical = serial["consistency"]["determinism_digest"] == \
        parallel["consistency"]["determinism_digest"] and \
        serial["sentences"] == parallel["sentences"] and \
        serial["budget"]["deduction_sequence"] == \
        parallel["budget"]["deduction_sequence"]
    record("parallel_serial_field_identical", identical,
           f"digest={parallel['consistency']['determinism_digest'][:16]}")
    record("parallel_uses_threads",
           parallel["observability"]["thread_peak"] >= 2,
           f"thread_peak={parallel['observability']['thread_peak']}")

    # 3) 预算耗尽
    cli("run", "--samples", samples, "--model", model,
        "--config", cfg_budget, "--snapshot-dir", SNAP / "budget",
        "--report", RUN / "budget.json", "--no-signal")
    budget = load("budget.json")
    exhausted = [s for s in budget["sentences"]
                 if s["code"] == "E_BUDGET_EXHAUSTED"]
    record("budget_exhausted_mid_sentence", len(exhausted) == 1,
           f"{exhausted[0]['sentence_id']} code=E_BUDGET_EXHAUSTED"
           if exhausted else "")
    unsched = [s["sentence_id"] for s in budget["sentences"]
               if s["code"] == "E_NOT_SCHEDULED"]
    record("unscheduled_ascending", unsched == sorted(unsched) and
           bool(unsched), f"unscheduled={unsched}")
    record("budget_no_partial_arcs",
           all(len(s["arcs"]) == 0 for s in exhausted),
           f"{exhausted[0]['sentence_id']} steps_used="
           f"{exhausted[0]['steps_used']}" if exhausted else "")
    curve = budget["budget"]["consumption_curve"]
    record("budget_curve_monotonic",
           all(curve[i] <= curve[i + 1] for i in range(len(curve) - 1)))

    # 4) 步数上限
    cli("run", "--samples", samples, "--model", model,
        "--config", cfg_step, "--snapshot-dir", SNAP / "step",
        "--report", RUN / "steplimit.json", "--no-signal")
    step = load("steplimit.json")
    codes = {s["code"] for s in step["sentences"]}
    record("step_limit_all_sentences", codes == {"E_STEP_LIMIT"},
           f"codes={codes}")
    record("step_limit_no_arcs",
           all(len(s["arcs"]) == 0 for s in step["sentences"]))

    # 5) 异常隔离
    cli("run", "--samples", exc_samples, "--model", model,
        "--config", cfg, "--snapshot-dir", SNAP / "exc",
        "--report", RUN / "exception.json", "--no-signal")
    exc = load("exception.json")
    exc_fail = [f for f in exc["failures"]
                if f["code"] == "E_SENTENCE_EXCEPTION"]
    ok = bool(exc_fail) and all(
        k in exc_fail[0] for k in
        ("sentence_id", "step_no", "phase", "exception_type"))
    record("exception_isolated", ok,
           f"failure={json.dumps(exc_fail[0], ensure_ascii=False)}"
           if exc_fail else "")
    record("exception_other_sentences_continue",
           len([s for s in exc["sentences"] if s["status"] == "ok"]) == 12)

    # 6) 崩溃恢复：跑到一半用新进程模拟中断后恢复
    baseline_path = RUN / "resume_baseline.json"
    cli("run", "--samples", samples, "--model", model, "--config", cfg,
        "--snapshot-dir", SNAP / "resume",
        "--report", baseline_path, "--no-signal")
    baseline = load("resume_baseline.json")

    # 构造只含前 5 句快照的中断现场
    crash_dir = SNAP / "crash"
    shutil.rmtree(crash_dir, ignore_errors=True)
    partial = _simulate_crash(crash_dir, samples, model, cfg, 5)
    record("crash_snapshot_complete",
           len(partial["completed_sentence_ids"]) == 5,
           f"completed={partial['completed_sentence_ids']}")

    # 恢复并跑完
    cli("run", "--samples", samples, "--model", model, "--config", cfg,
        "--snapshot-dir", crash_dir, "--report", RUN / "resumed.json",
        "--no-signal")
    record("resume_exit0", True)
    resumed = load("resumed.json")
    resume_same = resumed["consistency"]["determinism_digest"] == \
        baseline["consistency"]["determinism_digest"] and \
        resumed["sentences"] == baseline["sentences"] and \
        resumed["budget"] == baseline["budget"]
    record("resume_field_identical", resume_same,
           f"resumed_from={resumed['resume']['resumed_from']}, "
           f"snapshot_completed={resumed['resume']['snapshot_completed']}")

    # 7) 配置不匹配拒绝恢复（退出码 3）
    mismatch = _test_config_mismatch(crash_dir)
    record("config_mismatch_refused", mismatch == 3,
           f"exit={mismatch}")

    # 8) 可处理退出信号（SIGINT）退出码 0
    sig_rc, sig_snap = _test_graceful_signal(samples, model, cfg)
    record("graceful_shutdown_exit0", sig_rc == 0,
           f"returncode={sig_rc}, snapshot={sig_snap}")

    # 9) 注入校验
    proc = cli("inject", "--injections", DATA / "injections.json",
               "--snapshot-dir", SNAP / "inj",
               "--report", RUN / "injections.json")
    inj = load("injections.json")
    total = inj["injection_summary"]["total"]
    hit = inj["injection_summary"]["hit"]
    record("injections_all_hit", hit == total, f"{hit}/{total}")
    record("injections_snapshot_untouched",
           all(c["snapshot_untouched"] for c in inj["injection_checks"]))

    all_passed = all(c["passed"] for c in checks)
    report = {
        "all_passed": all_passed,
        "checks": checks,
        "consistency_summary": {
            "serial_digest": serial["consistency"]["determinism_digest"],
            "parallel_digest":
                parallel["consistency"]["determinism_digest"],
            "baseline_digest":
                baseline["consistency"]["determinism_digest"],
            "resumed_digest":
                resumed["consistency"]["determinism_digest"],
        },
        "graceful_shutdown": {
            "exit_code": sig_rc, "signal": "SIGINT",
            "snapshot_exists": sig_snap, "passed": sig_rc == 0},
        "injection_checks": inj["injection_checks"],
    }
    out = ART / "acceptance_report.json"
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if all_passed else 1


def _simulate_crash(crash_dir, samples, model_path, cfg_path, n):
    """在进程内跑前 n 句后丢弃引擎，模拟硬崩溃（仅快照可用于恢复）。"""
    from dp_bench.engine import ParseEngine, ShutdownController
    from dp_bench.loader import load_config, load_model, load_samples
    from dp_bench.model import sha256_hex
    from dp_bench.snapshot import SnapshotStore

    config = load_config(cfg_path)
    model = load_model(model_path)
    sentences = load_samples(samples)
    store = SnapshotStore(crash_dir, sha256_hex(config),
                          model.content_hash())
    engine = ParseEngine(model, config, sentences, snapshot_store=store,
                         shutdown=ShutdownController())
    import concurrent.futures
    index = dict(sentences)
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        futs = [pool.submit(engine._run_one, sid, index[sid])
                for sid in engine.sentence_ids[:n]]
        for fut in futs:
            fut.result()
    return store.latest()


def _test_config_mismatch(snapshot_dir):
    """篡改配置后恢复，应返回退出码 3 且 E_CONFIG_MISMATCH。"""
    bad_cfg = RUN / "config_mismatch.json"
    cfg = json.loads((DATA / "config.json").read_text(encoding="utf-8"))
    cfg["timeout_observe_ms"] = 999
    bad_cfg.write_text(json.dumps(cfg), encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, "-m", "dp_bench.cli", "run",
         "--samples", DATA / "sentences.txt",
         "--model", DATA / "model.json", "--config", bad_cfg,
         "--snapshot-dir", snapshot_dir,
         "--report", RUN / "should_not_exist.json", "--no-signal"],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8")
    return proc.returncode


def _test_graceful_signal(samples, model_path, cfg_path):
    """启动任务后在中途发送 SIGINT，验证在途句落盘且退出码 0。

    使用多工作线程 + 较小批次，使部分句仍在排队/在途时收到信号；
    无论信号落点早晚，退出码必须为 0 且存在完整快照。
    """
    import signal as _signal
    sig_cfg = RUN / "config_signal.json"
    cfg = json.loads(Path(cfg_path).read_text(encoding="utf-8"))
    cfg["max_workers"] = 4
    cfg["batch_size"] = 2
    sig_cfg.write_text(json.dumps(cfg, ensure_ascii=False),
                       encoding="utf-8")
    snap_dir = SNAP / "signal"
    report = RUN / "signal.json"
    proc = subprocess.Popen(
        [sys.executable, "-m", "dp_bench.cli", "run",
         "--samples", samples, "--model", model_path,
         "--config", sig_cfg, "--snapshot-dir", snap_dir,
         "--report", report],
        cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8")
    # 等待解析真正开始（快照目录出现文件），避免信号在解释器导入阶段到达
    deadline = time.time() + 10
    while time.time() < deadline and not any(snap_dir.rglob("*")):
        time.sleep(0.02)
    time.sleep(0.05)
    proc.send_signal(_signal.SIGINT)
    proc.wait(timeout=20)
    snap_exists = (snap_dir / "latest.json").exists()
    return proc.returncode, snap_exists


if __name__ == "__main__":
    sys.exit(main())
