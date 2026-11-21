"""latent_diag 命令行入口：generate-samples / run / demo-injection / demo-degradation / workbench。"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from latent_diag.config import WorkbenchConfig, ensure_dirs
from latent_diag.runner import run_workbench
from latent_diag.workbench import render_report, render_from_file

MANIFEST = "samples/tasks.jsonl"
ANOMALIES = "samples/anomalies.json"
CONFIG_PATH = "samples/config.json"


def _load_or_create_config(args) -> WorkbenchConfig:
    cfg_path = Path(getattr(args, "config", None) or CONFIG_PATH)
    if cfg_path.exists():
        return WorkbenchConfig.load(cfg_path)
    cfg = WorkbenchConfig()
    cfg.save(cfg_path)
    return cfg


def cmd_generate_samples(args):
    from scripts.generate_samples import generate
    cfg = _load_or_create_config(args)
    ensure_dirs(cfg, clear_snapshots=args.clear_snapshots)
    manifest, anomalies = generate(cfg.sample_dir)
    print(f"[generate-samples] manifest={manifest} anomalies={anomalies}")


def cmd_run(args):
    cfg = _load_or_create_config(args)
    ensure_dirs(cfg, clear_snapshots=args.clear_snapshots)
    anomalies = ANOMALIES if args.anomalies else None
    report = run_workbench(cfg, MANIFEST, anomalies_path=anomalies)
    print(render_report(report))
    print(f"\n[run] 报告已写入 {Path(cfg.report_dir) / 'report.json'}")


def cmd_demo_injection(args):
    from scripts.generate_samples import generate
    cfg = WorkbenchConfig()
    ensure_dirs(cfg, clear_snapshots=True)
    generate(cfg.sample_dir)
    report = run_workbench(cfg, MANIFEST, anomalies_path=ANOMALIES,
                           report_path=Path(cfg.report_dir) / "demo_injection.json")
    print(render_report(report))
    print("\n[demo-injection] 含注入演示完成（快照已从初始版本重建）")


def cmd_demo_degradation(args):
    from scripts.generate_samples import generate
    cfg = WorkbenchConfig()
    cfg.resource_limits.max_blocks_per_task = 4
    cfg.resource_limits.max_parallel_tasks = 2
    ensure_dirs(cfg, clear_snapshots=True)
    generate(cfg.sample_dir)
    report = run_workbench(cfg, MANIFEST, anomalies_path=None,
                           report_path=Path(cfg.report_dir) / "demo_degradation.json")
    print(render_report(report))
    print("\n[demo-degradation] 含降级演示完成（快照已从初始版本重建）")


def cmd_workbench(args):
    report_path = args.report or str(Path("reports") / "report.json")
    print(render_from_file(report_path))


def main(argv=None):
    parser = argparse.ArgumentParser(prog="latent_diag", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    def add_common(p):
        p.add_argument("--config", default=None, help="配置文件路径")

    p = sub.add_parser("generate-samples")
    add_common(p)
    p.add_argument("--clear-snapshots", action="store_true")
    p.set_defaults(func=cmd_generate_samples)

    p = sub.add_parser("run")
    add_common(p)
    p.add_argument("--anomalies", action="store_true", help="叠加异常参数注入")
    p.add_argument("--clear-snapshots", action="store_true",
                   help="运行前清空快照目录（版本号从初始值开始）")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("demo-injection")
    p.set_defaults(func=cmd_demo_injection)

    p = sub.add_parser("demo-degradation")
    p.set_defaults(func=cmd_demo_degradation)

    p = sub.add_parser("workbench")
    p.add_argument("--report", default=None)
    p.set_defaults(func=cmd_workbench)

    args = parser.parse_args(argv)
    args.func(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
