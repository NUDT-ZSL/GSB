"""命令行入口。

  python -m qu_service.cli run --config data/configs/serial.json [--resume]
  python -m qu_service.cli verify

退出码：0 正常/可处理信号优雅退出；2 配置哈希不符拒绝恢复；9 模拟强制终止；1 其他错误。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .errors import ConfigMismatch, ForcedTermination, GracefulExit
from .pipeline import Pipeline
from .report import strip_volatile

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _cmd_run(config: Path, resume: bool) -> int:
    pipe = Pipeline(config)
    try:
        report = pipe.run(resume=resume)
    except GracefulExit as exc:
        print(f"[graceful-exit] {exc}; snapshots persisted, exit 0")
        return 0
    except ForcedTermination as exc:
        print(f"[forced-termination] {exc}; only persisted snapshots remain")
        return 9
    except ConfigMismatch as exc:
        print(f"[E_CONFIG_MISMATCH] {exc}; resume refused")
        return 2
    print(f"[run-ok] run={report['run_name']} total={report['total_queries']} "
          f"processed={report['processed']} failed={report['failed']} "
          f"fingerprint={report['report_fingerprint'][:16]}")
    print(f"[report] {pipe.report_path}")
    return 0


def _compare(name_a: str, rep_a: dict, name_b: str, rep_b: dict) -> bool:
    """逐字段比较两份报告的可复现视图。"""
    view_a, view_b = strip_volatile(rep_a), strip_volatile(rep_b)
    # run_name/config_hash 允许不同（不同配置文件），比较业务结果字段
    for key in ("run_name", "config_hash"):
        view_a.pop(key, None)
        view_b.pop(key, None)
    same = view_a == view_b
    status = "PASS" if same else "FAIL"
    print(f"  [{status}] {name_a} vs {name_b}: "
          f"intent/slots/rewrite/failures field-identical")
    if not same:
        for key in sorted(set(view_a) | set(view_b)):
            if view_a.get(key) != view_b.get(key):
                print(f"    diff field: {key}")
    return same


def _load_report(config_path: Path) -> dict:
    pipe = Pipeline(config_path)
    return json.loads(pipe.report_path.read_text(encoding="utf-8"))


def _cmd_verify() -> int:
    configs = PROJECT_ROOT / "data" / "configs"
    ok = True

    print("== 1. 串行(1 worker) vs 并发(8 workers) 一致性 ==")
    serial_cfg, parallel_cfg = configs / "serial.json", configs / "parallel.json"
    Pipeline(serial_cfg).run()
    Pipeline(parallel_cfg).run()
    ok &= _compare("serial", _load_report(serial_cfg),
                   "parallel", _load_report(parallel_cfg))

    print("== 2. 快照恢复：模拟强制终止后恢复，报告与不中断运行一致 ==")
    crash_cfg = configs / "crash_demo.json"
    try:
        Pipeline(crash_cfg).run()  # stop_after_stage=intent -> 模拟崩溃
        print("  [FAIL] crash demo did not terminate")
        ok = False
    except ForcedTermination as exc:
        print(f"  simulated crash: {exc}")
    Pipeline(crash_cfg).run(resume=True)
    ok &= _compare("resumed", _load_report(crash_cfg),
                   "serial", _load_report(serial_cfg))

    print("== 3. 异常注入运行（畸形查询/词典缺失/规则冲突/评分越界） ==")
    inj_cfg = configs / "injected.json"
    report = Pipeline(inj_cfg).run()
    print(f"  injection_hash={report['injection_hash'][:16]}")
    print(f"  failures={len(report['failures'])} codes={report['codes_summary']}")
    print(f"  injection_hit_matrix={json.dumps(report['injection_hit_matrix'], ensure_ascii=False)}")

    print("== 4. 注入可复现性：同配置重跑，失败清单与业务码序列一致 ==")
    report2 = Pipeline(inj_cfg).run()
    same = (strip_volatile(report) == strip_volatile(report2))
    print(f"  [{'PASS' if same else 'FAIL'}] injected rerun reproducible")
    ok &= same

    print(f"VERIFY {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="qu_service")
    sub = parser.add_subparsers(dest="command", required=True)
    p_run = sub.add_parser("run", help="运行一次实验")
    p_run.add_argument("--config", required=True, type=Path)
    p_run.add_argument("--resume", action="store_true", help="从持久化快照恢复")
    sub.add_parser("verify", help="运行验收实验矩阵")
    args = parser.parse_args(argv)

    if args.command == "run":
        return _cmd_run(args.config, args.resume)
    return _cmd_verify()


if __name__ == "__main__":
    sys.exit(main())
