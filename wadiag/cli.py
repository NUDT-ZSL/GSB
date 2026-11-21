from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any, Sequence

from .engine import DiagnosticEngine
from .errors import EngineError
from .serialization import canonical_json_text


SAMPLE_DIR = Path(__file__).resolve().parent.parent / "samples"


def _workspace_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--workspace",
        default=argparse.SUPPRESS,
        help="状态目录（SQLite、段文件与清单）",
    )


def _output_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--pretty", action="store_true", default=argparse.SUPPRESS)
    parser.add_argument("--crash-point", default=argparse.SUPPRESS)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="wadiag")
    parser.add_argument("--workspace", help="状态目录（也可放在子命令之后）")
    parser.add_argument("--pretty", action="store_true", help="输出缩进 JSON")
    parser.add_argument("--crash-point", help="测试用强制终止注入点")
    subparsers = parser.add_subparsers(dest="command", required=True)

    load_parser = subparsers.add_parser("load", help="校验并载入固定样本")
    _workspace_argument(load_parser)
    _output_arguments(load_parser)
    load_parser.add_argument(
        "--trajectory",
        default=str(SAMPLE_DIR / "write_trajectory.json"),
    )
    load_parser.add_argument(
        "--storage",
        default=str(SAMPLE_DIR / "storage_config.json"),
    )
    load_parser.add_argument(
        "--diagnostic",
        default=str(SAMPLE_DIR / "diagnostic_config.json"),
    )
    load_parser.add_argument(
        "--clock-table",
        default=str(SAMPLE_DIR / "clock_steps.json"),
    )

    flush_parser = subparsers.add_parser("flush", help="按内存表规则刷盘")
    _workspace_argument(flush_parser)
    _output_arguments(flush_parser)
    flush_parser.add_argument("--from-segment", type=int)
    flush_parser.add_argument("--to-segment", type=int)
    flush_parser.add_argument("--segment-start", dest="from_segment", type=int)
    flush_parser.add_argument("--segment-end", dest="to_segment", type=int)

    diagnose_parser = subparsers.add_parser("diagnose", help="输出分层写入放大诊断")
    _workspace_argument(diagnose_parser)
    _output_arguments(diagnose_parser)
    diagnose_parser.add_argument("--query-key", "--key", dest="query_key")

    for name, help_text in (
        ("compact", "执行一轮预算约束合并裁决"),
        ("resume", "恢复未完成刷盘或合并"),
        ("status", "输出当前持久化视图"),
        ("verify", "校验段文件、层视图与诊断输出"),
    ):
        command_parser = subparsers.add_parser(name, help=help_text)
        _workspace_argument(command_parser)
        _output_arguments(command_parser)
    return parser


def _resolve_workspace(args: argparse.Namespace) -> str:
    workspace = getattr(args, "workspace", None) or os.environ.get("WADIAG_WORKSPACE")
    if not workspace:
        raise EngineError(
            code="E_BAD_USAGE",
            message="必须提供 --workspace 或 WADIAG_WORKSPACE",
        )
    return workspace


def _dispatch(args: argparse.Namespace) -> dict[str, Any]:
    engine = DiagnosticEngine(_resolve_workspace(args))
    if args.command == "load":
        return engine.load(
            args.trajectory,
            args.storage,
            args.diagnostic,
            args.clock_table,
        )
    if args.command == "flush":
        return engine.flush(
            from_segment=args.from_segment,
            to_segment=args.to_segment,
        )
    if args.command == "diagnose":
        return engine.diagnose(query_key=args.query_key)
    if args.command == "compact":
        return engine.compact()
    if args.command == "resume":
        return engine.resume()
    if args.command == "status":
        return engine.status()
    if args.command == "verify":
        return engine.verify()
    raise EngineError(code="E_BAD_USAGE", message="未知子命令", command=args.command)


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.crash_point:
        os.environ["WADIAG_CRASH_POINT"] = args.crash_point
    try:
        payload = _dispatch(args)
    except EngineError as exc:
        print(
            canonical_json_text(
                {"ok": False, "error": exc.to_dict()},
                getattr(args, "pretty", False),
            ),
            end="",
        )
        return 2
    print(
        canonical_json_text(payload, getattr(args, "pretty", False)),
        end="",
    )
    return 0 if payload.get("ok", True) else 1


if __name__ == "__main__":
    sys.exit(main())
