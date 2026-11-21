from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Sequence

from . import __version__
from .engine import Reconciler
from .errors import (
    EXIT_BUSINESS,
    EXIT_INTERNAL,
    EXIT_OK,
    EXIT_USAGE,
    EXIT_VERIFY,
    BusinessError,
    diagnostic,
)
from .store import LedgerStore
from .validation import parse_clock_file, parse_registry, parse_shard


def _json_print(payload: dict[str, Any]) -> None:
    sys.stdout.write(
        json.dumps(
            payload,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )


def _add_storage_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--workspace",
        default="workspace",
        help="账本工作目录（默认 workspace）",
    )
    parser.add_argument("--db", default=None, help="SQLite 路径")
    parser.add_argument("--ledger", default=None, help="规范化账本快照路径")
    parser.add_argument("--diagnostics", default=None, help="差异报告路径")


def _store_from_args(args: argparse.Namespace) -> LedgerStore:
    return LedgerStore(
        args.workspace,
        db_path=args.db,
        ledger_path=args.ledger,
        diagnostics_path=args.diagnostics,
    )


def _discover_shards(
    explicit: list[str],
    directories: list[str],
) -> list[str]:
    files: list[str] = []
    for value in explicit:
        files.append(value)
    for directory in directories:
        path = Path(directory)
        if not path.exists():
            raise BusinessError(
                "E_FILE_READ",
                "分片目录不存在",
                {"directory": str(path)},
                EXIT_USAGE,
            )
        files.extend(
            str(item)
            for item in sorted(path.glob("*.json"))
            if item.is_file()
        )
    deduped: list[str] = []
    seen: set[str] = set()
    for value in files:
        key = str(Path(value).resolve())
        if key in seen:
            continue
        seen.add(key)
        deduped.append(value)
    return deduped


def _expected_shard_digests(
    files: list[str],
    specs: dict[str, Any],
    clocks: dict[tuple[str, int], int] | None,
    errors: list[dict[str, Any]],
) -> dict[str, str]:
    result: dict[str, str] = {}
    for path in files:
        try:
            shard = parse_shard(path, specs, clocks=clocks)
            result[shard.shard_id] = shard.content_digest
        except BusinessError as exc:
            errors.append(
                {
                    **exc.to_dict(),
                    "file": str(path),
                }
            )
    return result


def command_load(args: argparse.Namespace) -> int:
    positional = list(args.paths)
    registry_path = args.registry
    if registry_path is None and positional:
        registry_path = positional.pop(0)
    if registry_path is None:
        raise BusinessError(
            "E_USAGE",
            "load 需要 --registry 或第一个位置参数作为注册清单",
            {},
            EXIT_USAGE,
        )
    registry = parse_registry(registry_path)
    clocks = parse_clock_file(args.clocks) if args.clocks else {}
    shard_files = _discover_shards(positional + list(args.shard), list(args.shard_dir))

    store = _store_from_args(args)
    init = store.initialize_registry(registry)
    store.open()
    assert store.conn is not None

    loaded: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for path in shard_files:
        try:
            shard = parse_shard(
                path,
                registry.instances,
                clocks=clocks,
                arrival_start=store.next_arrival_order(),
            )
            result = store.load_shard(shard)
            loaded.append(
                {
                    "file": str(path),
                    "shard_id": shard.shard_id,
                    "content_digest": shard.content_digest,
                    **{
                        key: value
                        for key, value in result.items()
                        if key not in {"snapshot", "batch_id", "state_digest"}
                    },
                }
            )
        except BusinessError as exc:
            rejected.append(
                {
                    "file": str(path),
                    **exc.to_dict(),
                }
            )

    conflicts = [
        conflict
        for item in loaded
        for conflict in item.get("conflicts", [])
    ]
    materialized = store.materialize()
    payload = {
        "ok": not rejected,
        "command": "load",
        "registry": str(Path(registry_path)),
        "manifest_digest": registry.digest,
        "loaded": loaded,
        "rejected": rejected,
        "conflicts": conflicts,
        "pointer_version": materialized["pointer_version"],
    }
    _json_print(payload)
    if rejected:
        return EXIT_USAGE
    if conflicts:
        return EXIT_BUSINESS
    return EXIT_OK


def _run_reconcile(args: argparse.Namespace, command: str) -> int:
    store = _store_from_args(args)
    store.open()
    reconciler = Reconciler(
        store,
        fault_after_transitions=args.fault_after_transitions,
        fault_before_commit=args.fault_point == "before-commit",
    )
    result = reconciler.reconcile(seal=bool(args.seal))
    payload = {
        "command": command,
        **result,
    }
    _json_print(payload)
    return EXIT_OK if result["ok"] else EXIT_BUSINESS


def command_reconcile(args: argparse.Namespace) -> int:
    return _run_reconcile(args, "reconcile")


def command_resume(args: argparse.Namespace) -> int:
    return _run_reconcile(args, "resume")


def command_status(args: argparse.Namespace) -> int:
    store = _store_from_args(args)
    store.open()
    result = store.status(args.instance)
    _json_print({"ok": True, "command": "status", **result})
    return EXIT_OK


def command_verify(args: argparse.Namespace) -> int:
    store = _store_from_args(args)
    store.open()
    errors: list[dict[str, Any]] = []
    registry = parse_registry(args.registry) if args.registry else None
    clocks = parse_clock_file(args.clocks) if args.clocks else None
    shard_files = _discover_shards(list(args.shard), list(args.shard_dir))
    expected_shards: dict[str, str] = {}
    if shard_files:
        if registry is None:
            raise BusinessError(
                "E_USAGE",
                "核验分片时必须同时提供 --registry 以解析实例",
                {},
                EXIT_USAGE,
            )
        expected_shards = _expected_shard_digests(
            shard_files,
            registry.instances,
            clocks,
            errors,
        )
    result = store.verify(
        expected_manifest_digest=None if registry is None else registry.digest,
        expected_shards=expected_shards,
    )
    result["errors"].extend(errors)
    result["ok"] = not result["errors"]
    result["command"] = "verify"
    _json_print(result)
    return EXIT_OK if result["ok"] else EXIT_VERIFY


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="probe-ledger",
        description="本地探测观测差分解算与确定性状态账本",
    )
    parser.add_argument("--version", action="version", version=__version__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    load_parser = subparsers.add_parser("load", help="加载注册清单与观测分片")
    _add_storage_args(load_parser)
    load_parser.add_argument("--registry", default=None)
    load_parser.add_argument("--clocks", default=None)
    load_parser.add_argument("--shard", action="append", default=[])
    load_parser.add_argument("--shard-dir", action="append", default=[])
    load_parser.add_argument("paths", nargs="*")
    load_parser.set_defaults(func=command_load)

    for name, function, help_text in (
        ("reconcile", command_reconcile, "差分推进待裁决观测"),
        ("resume", command_resume, "从已提交水位恢复并继续推进"),
    ):
        sub = subparsers.add_parser(name, help=help_text)
        _add_storage_args(sub)
        sub.add_argument("--seal", action="store_true", help="强制封口并补洞")
        sub.add_argument(
            "--fault-after-transitions",
            type=int,
            default=None,
            help=argparse.SUPPRESS,
        )
        sub.add_argument(
            "--fault-point",
            choices=("after-commit", "before-commit"),
            default="after-commit",
            help=argparse.SUPPRESS,
        )
        sub.set_defaults(func=function)

    status_parser = subparsers.add_parser("status", help="输出稳定排序的状态 JSON")
    _add_storage_args(status_parser)
    status_parser.add_argument("--instance", default=None)
    status_parser.set_defaults(func=command_status)

    verify_parser = subparsers.add_parser("verify", help="校验账本自洽性")
    _add_storage_args(verify_parser)
    verify_parser.add_argument("--registry", default=None)
    verify_parser.add_argument("--clocks", default=None)
    verify_parser.add_argument("--shard", action="append", default=[])
    verify_parser.add_argument("--shard-dir", action="append", default=[])
    verify_parser.set_defaults(func=command_verify)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except BusinessError as exc:
        _json_print(
            {
                "ok": False,
                "command": args.command,
                "error": exc.to_dict(),
            }
        )
        return exc.exit_code
    except KeyboardInterrupt:
        _json_print(
            {
                "ok": False,
                "command": args.command,
                "error": diagnostic("E_INTERRUPTED", "操作被中断"),
            }
        )
        return 130
    except Exception as exc:  # pragma: no cover - final safety net
        _json_print(
            {
                "ok": False,
                "command": args.command,
                "error": diagnostic(
                    "E_INTERNAL",
                    "未预期的内部错误",
                    error=f"{type(exc).__name__}: {exc}",
                ),
            }
        )
        return EXIT_INTERNAL


if __name__ == "__main__":
    raise SystemExit(main())
