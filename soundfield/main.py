"""命令行入口。

示例::

    python -m soundfield.main --scene data/scene.json --state-dir state --output-dir out --clean

可处理终止信号（SIGINT/SIGTERM；Windows 上 SIGTERM 映射为 CTRL_BREAK/CTRL_C
语义，仅 SIGINT 可移植）只在分块边界生效，写中断标记后以退出码 3 退出；
强杀（SIGKILL / 任务管理器结束进程）没有清理机会，未完成分块因
“临时文件 + 原子替换 + 完成元数据双写”而不会被误认为已完成。
"""

from __future__ import annotations

import argparse
import json
import signal
import sys
from pathlib import Path

from . import errors as E
from .config import EXIT_BUSINESS_ERROR, EXIT_INTERRUPTED, EXIT_RUNTIME_ERROR
from .pipeline import run_pipeline
from .scheduler import InterruptFlag


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="离线多通道镜像声源声场合成链路")
    parser.add_argument("--scene", required=True, help="录制棚场景 JSON 路径")
    parser.add_argument("--state-dir", required=True, help="版本化状态目录")
    parser.add_argument("--output-dir", required=True, help="输出目录")
    parser.add_argument("--clean", action="store_true", help="运行前清空历史状态")
    parser.add_argument(
        "--fail-after-blocks",
        type=int,
        default=None,
        help=argparse.SUPPRESS,
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    interrupt = InterruptFlag()
    state_dir = Path(args.state_dir)
    output_dir = Path(args.output_dir)

    def _handle(signum, _frame) -> None:  # pragma: no cover - 信号路径
        interrupt.set(reason=f"signal_{signum}")

    signal.signal(signal.SIGINT, _handle)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _handle)

    try:
        artifacts = run_pipeline(
            args.scene,
            state_dir,
            output_dir,
            clean=args.clean,
            interrupt=interrupt,
        )
    except E.BusinessError as exc:
        if exc.code == E.E_INTERRUPTED:
            print(json.dumps(exc.to_dict(), ensure_ascii=False), file=sys.stderr)
            return EXIT_INTERRUPTED
        print(json.dumps(exc.to_dict(), ensure_ascii=False), file=sys.stderr)
        return EXIT_BUSINESS_ERROR
    except Exception as exc:  # noqa: BLE001
        payload = E.BusinessError(
            E.E_INTERNAL,
            "未预期运行时错误",
            {"type": type(exc).__name__, "message": str(exc)},
        ).to_dict()
        print(json.dumps(payload, ensure_ascii=False), file=sys.stderr)
        return EXIT_RUNTIME_ERROR

    summary = {
        "ok": True,
        "business_code": E.E_OK,
        "version_id": artifacts.version_id,
        "output_wav": str(artifacts.output_wav),
        "report": str(artifacts.report_path),
        "image_count": artifacts.report["image_count"],
        "block_count": artifacts.report["block_count"],
    }
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
