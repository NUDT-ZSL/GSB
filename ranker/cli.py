"""命令行入口。"""

import argparse
import sys

from .atomicio import atomic_write_json
from .config import load_config, load_groups
from .errors import ConfigInvalid, ConfigMismatch
from .runner import RunOptions, run


def build_parser():
    parser = argparse.ArgumentParser(description="定点搜索精排与重排组件")
    parser.add_argument("--candidates", required=True)
    parser.add_argument("--config", required=True)
    parser.add_argument("--groups", required=True)
    parser.add_argument("--snapshot-dir", required=True)
    parser.add_argument("--report", required=True)
    parser.add_argument("--injection", default="")
    parser.add_argument("--incremental", action="store_true",
                        help="配置指纹变化时按缓存键增量失效重算，而非拒绝续跑")
    parser.add_argument("--stop-after", type=int, default=None,
                        help="确定性中断：处理完 N 个请求后在阶段提交边界退出")
    parser.add_argument("--no-signal-handlers", action="store_true")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        config = load_config(args.config)
        groups_table = load_groups(args.groups)
    except ConfigInvalid as exc:
        payload = {"fatal": True, "code": exc.code, "stage": exc.stage,
                   "message": str(exc)}
        atomic_write_json(args.report, payload)
        print(f"[ranker] {exc.code}: {exc}", file=sys.stderr)
        return 2

    options = RunOptions(
        snapshot_dir=args.snapshot_dir,
        injection_path=args.injection,
        incremental=args.incremental,
        stop_after_requests=args.stop_after,
        install_handlers=not args.no_signal_handlers,
    )
    try:
        report, _manifest, _signalled = run(
            args.candidates, config, groups_table, options
        )
    except ConfigMismatch as exc:
        payload = {"fatal": True, "code": exc.code, "stage": exc.stage,
                   "message": str(exc)}
        atomic_write_json(args.report, payload)
        print(f"[ranker] {exc.code}: {exc}", file=sys.stderr)
        return 2
    atomic_write_json(args.report, report)
    print(
        f"[ranker] exit={report['exit_code']} reason={report['exit_reason']} "
        f"requests={len(report['requests'])} failures={len(report['failures'])} "
        f"cache={report['cache']['totals']}"
    )
    return report["exit_code"]


if __name__ == "__main__":
    raise SystemExit(main())
