"""恢复一致性验证脚本（题目要求的独立验证入口）。

覆盖三类路径：
1. 不中断基线运行；
2. 故障注入（可处理退出）后续跑，逐字段比对基线；
3. 配置哈希不一致，确认拒绝续跑并返回 E_CONFIG_MISMATCH。

另在支持 SIGTERM 的平台上验证真实操作系统信号路径。
退出码 0 表示全部断言通过。
"""

import copy
import json
import os
import shutil
import signal
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from xling_pipeline.config import load_config  # noqa: E402
from xling_pipeline.io_utils import canonical_dumps  # noqa: E402

BASE_CONFIG = os.path.join(ROOT, "configs", "pipeline_config.json")
EXIT_CONFIG = os.path.join(ROOT, "configs", "fault_exit_config.json")
MISMATCH_CONFIG = os.path.join(ROOT, "configs", "fault_mismatch_config.json")
VERIFY_DIR = os.path.join(ROOT, "data", "output", "_verify_work")
VOLATILE_KEYS = (
    "latest_snapshot_version", "reused_batches", "reused_items",
    "processed_items_this_run", "status",
)


def _clone_config(name, source=BASE_CONFIG):
    config = copy.deepcopy(load_config(source))
    config["paths"]["snapshot_dir"] = os.path.join(VERIFY_DIR, name, "snapshots")
    config["paths"]["report_path"] = os.path.join(VERIFY_DIR, name, "report.json")
    path = os.path.join(VERIFY_DIR, name, "config.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(config, handle, ensure_ascii=False, indent=2)
    return path


def _run(config_path):
    env = dict(os.environ, PYTHONPATH=os.path.join(ROOT, "src"))
    return subprocess.run(
        [sys.executable, "-m", "xling_pipeline.cli", config_path],
        env=env, capture_output=True, text=True, cwd=ROOT,
    )


def _load_report(config_path):
    config = load_config(config_path)
    with open(config["paths"]["report_path"], encoding="utf-8") as handle:
        return json.load(handle)


def _canonical_without_volatile(report):
    clone = copy.deepcopy(report)
    for key in VOLATILE_KEYS:
        clone.pop(key, None)
    return canonical_dumps(clone)


def main():
    shutil.rmtree(VERIFY_DIR, ignore_errors=True)
    os.makedirs(VERIFY_DIR, exist_ok=True)
    results = []

    def check(name, condition, detail=""):
        results.append((name, bool(condition), detail))
        print(f"[{'PASS' if condition else 'FAIL'}] {name} {detail}")

    # 1) 基线
    baseline_cfg = _clone_config("baseline")
    proc = _run(baseline_cfg)
    check("baseline_exit_zero", proc.returncode == 0, proc.stderr.strip())
    baseline = _load_report(baseline_cfg)

    # 2) 故障注入：中断 -> 续跑 -> 完成
    fault_cfg = _clone_config("fault", EXIT_CONFIG)
    first = _run(fault_cfg)
    check("fault_inject_graceful_exit_code_0", first.returncode == 0, first.stderr.strip())
    fault_config = load_config(fault_cfg)
    check(
        "no_report_during_interrupt",
        not os.path.exists(fault_config["paths"]["report_path"]),
    )
    second = _run(fault_cfg)
    check("resume_completes", second.returncode == 0, second.stderr.strip())
    resumed = _load_report(fault_cfg)
    check(
        "resumed_byte_identical_excluding_volatile",
        _canonical_without_volatile(baseline) == _canonical_without_volatile(resumed),
    )
    check("resumed_reused_batches_positive", resumed["reused_batches"] > 0,
          f"reused={resumed['reused_batches']}")
    check("invalid_items_preserved", resumed["invalid_items"] == baseline["invalid_items"])
    check("batch_sequences_identical",
          resumed["stage_batches"] == baseline["stage_batches"])

    # 3) 配置哈希不一致
    mismatch_cfg = _clone_config("mismatch", MISMATCH_CONFIG)
    # 复用 fault 的快照目录以制造同快照、不同配置
    mismatch_config = load_config(mismatch_cfg)
    mismatch_config["paths"]["snapshot_dir"] = fault_config["paths"]["snapshot_dir"]
    with open(mismatch_cfg, "w", encoding="utf-8") as handle:
        json.dump(mismatch_config, handle, ensure_ascii=False, indent=2)
    proc = _run(mismatch_cfg)
    check("mismatch_rejected_code_2", proc.returncode == 2, proc.stderr.strip()[:80])
    check("mismatch_emits_business_code", "E_CONFIG_MISMATCH" in proc.stderr)

    # 4) 真实 SIGTERM（平台支持时）
    if hasattr(signal, "SIGTERM"):
        signal_cfg = _clone_config("signal")
        cfg = load_config(signal_cfg)
        cfg["test_hooks"]["query_sleep_seconds"] = 0.3
        cfg.pop("fault_injection", None)
        with open(signal_cfg, "w", encoding="utf-8") as handle:
            json.dump(cfg, handle, ensure_ascii=False, indent=2)
        env = dict(os.environ, PYTHONPATH=os.path.join(ROOT, "src"))
        proc_popen = subprocess.Popen(
            [sys.executable, "-m", "xling_pipeline.cli", signal_cfg],
            env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, cwd=ROOT,
        )
        time.sleep(0.35)
        proc_popen.send_signal(signal.SIGTERM)
        _, stderr = proc_popen.communicate(timeout=30)
        check("sigterm_exit_code_0", proc_popen.returncode == 0, stderr.strip())
        resume = _run(signal_cfg)
        check("sigterm_resume_completes", resume.returncode == 0, resume.stderr.strip())
        signal_report = _load_report(signal_cfg)
        check(
            "sigterm_resume_identical",
            _canonical_without_volatile(signal_report)
            == _canonical_without_volatile(baseline),
        )

    failed = [name for name, ok, _ in results if not ok]
    print("\n==== SUMMARY ====")
    print(f"total={len(results)} passed={len(results) - len(failed)} failed={len(failed)}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
