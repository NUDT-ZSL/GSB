"""命令行入口：运行管线并处理 SIGINT/SIGTERM 可处理退出信号。"""

import argparse
import signal
import sys

from .config import load_config
from .errors import ConfigMismatch, SnapshotCorrupt
from .runner import ExitController, PipelineRunner


def _install_signal_handlers(controller):
    def handler(signum, _frame):
        controller.request_exit(f"signal:{signum}")

    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        signum = getattr(signal, name, None)
        if signum is not None:
            try:
                signal.signal(signum, handler)
            except (ValueError, OSError):
                # 非主线程或平台不支持时跳过
                pass


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="本地确定性中英跨语言相关性检索管线"
    )
    parser.add_argument("config", help="管线配置 JSON 路径")
    args = parser.parse_args(argv)

    controller = ExitController()
    _install_signal_handlers(controller)
    try:
        config = load_config(args.config)
        runner = PipelineRunner(config, controller)
        outcome = runner.run()
    except ConfigMismatch as exc:
        print(f"{exc.code}: {exc}", file=sys.stderr)
        return 2
    except SnapshotCorrupt as exc:
        print(f"{exc.code}: {exc}", file=sys.stderr)
        return 3

    if outcome["status"] == "interrupted":
        print(
            f"graceful exit after snapshot commit; reason={outcome['reason']}",
            file=sys.stderr,
        )
        return 0
    print(f"report written: {outcome['report_path']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
