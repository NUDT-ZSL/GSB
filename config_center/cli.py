from __future__ import annotations

import argparse
import json
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any

from .errors import BusinessError, bad_config
from .service import ConfigCenter
from .util import pretty_json, read_json, strict_loads, to_jsonable
from .verify import verify_center


def _read_path(path: str) -> Any:
    if path == "-":
        return strict_loads(sys.stdin.read())
    return read_json(path)


def _emit(value: Any) -> None:
    print(
        json.dumps(
            to_jsonable(value),
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        )
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="config-center",
        description="本地配置中心热更新、定点换算与数值诊断引擎",
    )
    parser.add_argument(
        "--root",
        default=None,
        help="工作区根目录（默认当前目录）",
    )
    parser.add_argument(
        "--data-dir",
        default=None,
        help="样本数据目录（默认 <root>/data）",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    load_parser = subparsers.add_parser("load", help="加载初始 JSON 配置快照")
    load_parser.add_argument("path", help="快照文件路径，- 表示 stdin")

    apply_parser = subparsers.add_parser("apply", help="校验并原子应用补丁")
    apply_parser.add_argument("path", help="补丁文件路径，- 表示 stdin")

    compute_parser = subparsers.add_parser("compute", help="绑定当前版本执行确定性计算")
    compute_parser.add_argument("path", help="计算输入文件路径，- 表示 stdin")
    compute_parser.add_argument("--client-id", "--client", dest="client_id")
    compute_parser.add_argument("--round-key", "--round", dest="round_key")
    compute_parser.add_argument("--namespace")
    compute_parser.add_argument(
        "--delay-ms",
        type=int,
        default=0,
        help="绑定版本后的等待毫秒数，用于热更新并发演示",
    )
    compute_parser.add_argument(
        "--crash-at",
        choices=["after-start", "before-commit", "after-commit"],
        help="在指定阶段强制结束进程（退出码 86）",
    )

    resume_parser = subparsers.add_parser("resume", help="只重放未提交计算作业")
    resume_parser.add_argument("--namespace")
    resume_parser.add_argument("--client-id", "--client", dest="client_id")
    resume_parser.add_argument("--round-key", "--round", dest="round_key")

    status_parser = subparsers.add_parser("status", help="稳定排序 JSON 状态")
    status_parser.add_argument("--namespace")
    status_parser.add_argument("--json", action="store_true", help="JSON 输出（默认行为）")

    verify_parser = subparsers.add_parser("verify", help="复算版本链、账本、结果与错误码")
    verify_parser.add_argument("--namespace")
    verify_parser.add_argument("--tolerance", default="0.001")
    verify_parser.add_argument("--scale-factor", type=int, default=100)
    verify_parser.add_argument("--input", dest="compute_input")
    verify_parser.add_argument("--json", action="store_true", help="JSON 输出（默认行为）")

    digest_parser = subparsers.add_parser("digest", help="计算并写入补丁内容摘要")
    digest_parser.add_argument("path", help="补丁文件路径，- 表示 stdin")
    digest_parser.add_argument(
        "--write",
        action="store_true",
        help="将带摘要的规范化补丁写回文件",
    )

    bootstrap_parser = subparsers.add_parser(
        "bootstrap", help="重置并加载样本快照及补丁序列"
    )
    bootstrap_parser.add_argument(
        "--without-patches",
        action="store_true",
        help="只加载初始快照",
    )

    subparsers.add_parser("reset", help="清空业务状态但保留 SQLite 表结构")

    serve_parser = subparsers.add_parser("serve", help="启动 Flask 交互界面")
    serve_parser.add_argument("--host", default="127.0.0.1")
    serve_parser.add_argument("--port", type=int, default=5000)
    serve_parser.add_argument("--debug", action="store_true")
    return parser


def _center(args: argparse.Namespace) -> ConfigCenter:
    return ConfigCenter(args.root, data_dir=args.data_dir)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        center = _center(args)
        if args.command == "load":
            _emit(center.load_file(args.path) if args.path != "-" else center.load_snapshot_document(_read_path("-")))
        elif args.command == "apply":
            _emit(center.apply_file(args.path) if args.path != "-" else center.apply_patch_document(_read_path("-")))
        elif args.command == "compute":
            if args.path == "-":
                payload = _read_path("-")
                result = center.compute_document(
                    payload,
                    client_id=args.client_id,
                    round_key=args.round_key,
                    namespace=args.namespace,
                    delay_ms=args.delay_ms,
                    crash_at=args.crash_at,
                )
            else:
                result = center.compute_file(
                    args.path,
                    client_id=args.client_id,
                    round_key=args.round_key,
                    namespace=args.namespace,
                    delay_ms=args.delay_ms,
                    crash_at=args.crash_at,
                )
            _emit(result)
        elif args.command == "resume":
            _emit(
                center.resume(
                    namespace=args.namespace,
                    client_id=args.client_id,
                    round_key=args.round_key,
                )
            )
        elif args.command == "status":
            _emit(center.status(args.namespace))
        elif args.command == "verify":
            _emit(
                verify_center(
                    center,
                    namespace=args.namespace,
                    tolerance=Decimal(args.tolerance),
                    scale_factor=args.scale_factor,
                    compute_input_path=args.compute_input,
                )
            )
        elif args.command == "digest":
            payload = _read_path(args.path)
            digest = center.patch_digest(payload)
            _emit(digest)
            if args.write and args.path != "-":
                Path(args.path).write_text(
                    pretty_json(digest["patch"]) + "\n",
                    encoding="utf-8",
                )
        elif args.command == "bootstrap":
            _emit(
                center.bootstrap_demo(
                    include_patches=not args.without_patches
                )
            )
        elif args.command == "reset":
            _emit(center.reset())
        elif args.command == "serve":
            from .web import create_app

            app = create_app(
                root=args.root,
                data_dir=args.data_dir,
            )
            app.run(host=args.host, port=args.port, debug=args.debug)
        else:  # pragma: no cover - argparse enforces choices
            raise bad_config("未知子命令", command=args.command)
    except BusinessError as exc:
        _emit(exc.to_dict())
        return 1
    except Exception as exc:
        _emit(
            BusinessError(
                "E_INTERNAL",
                "未预期的内部错误",
                details={"exception": type(exc).__name__, "message": str(exc)},
                http_status=500,
            ).to_dict()
        )
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

