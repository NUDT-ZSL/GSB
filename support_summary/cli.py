"""Command-line and editable application entry points."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from .canonical import atomic_write_json, canonical_bytes
from .cache import CacheStore
from .errors import BusinessError
from .incremental import run_incremental_scenario
from .models import config_from_dict
from .pipeline import load_runner
from .recovery import run_recovery_demo
from .sample_data import (
    add_session_record,
    remove_session_record,
    write_dataset,
)
from .selfcheck import run_self_check
from .validation import (
    load_config,
    load_sessions,
    load_vocabulary,
    validate_config,
)


def _default_paths(root: Path) -> Dict[str, Path]:
    return {
        "sessions": root / "data" / "sessions.jsonl",
        "vocabulary": root / "data" / "vocabulary_v1.json",
        "config": root / "data" / "params_v1.json",
        "cache": root / "artifacts" / "cache",
        "checkpoint": root / "checkpoints" / "demo",
    }


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument("--sessions", type=Path)
    parser.add_argument("--vocabulary", type=Path)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--cache-dir", type=Path)


def _paths(args: argparse.Namespace) -> Dict[str, Path]:
    defaults = _default_paths(args.root.resolve())
    return {
        "root": args.root.resolve(),
        "sessions": args.sessions or defaults["sessions"],
        "vocabulary": args.vocabulary or defaults["vocabulary"],
        "config": args.config or defaults["config"],
        "cache": args.cache_dir or defaults["cache"],
        "checkpoint": defaults["checkpoint"],
    }


def _print(value: Any, *, pretty: bool = True) -> None:
    if pretty:
        text = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)
    else:
        text = canonical_bytes(value).decode("utf-8")
    print(text)


def _write_output(path: Optional[Path], value: Any) -> None:
    if path is not None:
        atomic_write_json(path, value)


def _crash_handler(point: str) -> None:
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(97)


def command_init(args: argparse.Namespace) -> int:
    paths = _paths(args)
    write_dataset(paths["root"], force=args.force)
    _print(
        {
            "ok": True,
            "data": {
                "sessions": str(paths["sessions"]),
                "vocabulary_v1": str(paths["root"] / "data" / "vocabulary_v1.json"),
                "vocabulary_v2": str(paths["root"] / "data" / "vocabulary_v2.json"),
                "params_v1": str(paths["root"] / "data" / "params_v1.json"),
                "params_v2": str(paths["root"] / "data" / "params_v2.json"),
            },
            "checkpoint_demo": str(paths["checkpoint"]),
        }
    )
    return 0


def command_run(args: argparse.Namespace) -> int:
    paths = _paths(args)
    checkpoint = args.checkpoint_dir
    runner = load_runner(
        paths["sessions"],
        paths["vocabulary"],
        paths["config"],
        paths["cache"],
    )
    result = runner.run(
        checkpoint_dir=checkpoint,
        resume=not args.fresh,
        crash_at=args.crash_at,
        crash_handler=_crash_handler if args.crash_at else None,
    )
    report = result.to_report()
    checks = run_self_check(result)
    report["self_check"] = checks
    _write_output(args.output, report)
    if not args.output or not args.quiet:
        _print(report)
    return 0 if checks["ok"] else 1


def command_self_check(args: argparse.Namespace) -> int:
    paths = _paths(args)
    runner = load_runner(
        paths["sessions"],
        paths["vocabulary"],
        paths["config"],
        paths["cache"],
    )
    result = runner.run(
        checkpoint_dir=args.checkpoint_dir,
        resume=not args.fresh,
    )
    checks = run_self_check(result)
    value = {
        "ok": checks["ok"],
        "summary_digest": result.summary_digest,
        "checks": checks["checks"],
        "stages": result.to_report()["stages"],
    }
    _write_output(args.output, value)
    _print(value)
    return 0 if checks["ok"] else 1


def command_incremental(args: argparse.Namespace) -> int:
    paths = _paths(args)
    sessions = load_sessions(paths["sessions"])
    vocabulary = load_vocabulary(paths["vocabulary"])
    config = load_config(paths["config"])
    report = run_incremental_scenario(
        sessions,
        vocabulary,
        config,
        cache_dir=paths["cache"],
        scenario=args.scenario,
        target_session_id=args.session,
        new_strength=args.strength,
        vocabulary_path=args.new_vocabulary,
        seed=args.seed,
    )
    _write_output(args.output, report)
    _print(report)
    return 0 if report["ok"] else 1


def command_cache_query(args: argparse.Namespace) -> int:
    paths = _paths(args)
    matches = CacheStore(paths["cache"]).query_session(args.session)
    _print(
        {
            "ok": True,
            "session_id": args.session,
            "count": len(matches),
            "objects": matches,
        }
    )
    return 0


def command_recovery_demo(args: argparse.Namespace) -> int:
    value = run_recovery_demo(args.root.resolve())
    _write_output(args.output, value)
    _print(value)
    return 0 if value["ok"] else 1


def command_sessions(args: argparse.Namespace) -> int:
    paths = _paths(args)
    if args.session_action == "list":
        sessions = load_sessions(paths["sessions"])
        value = {
            "ok": True,
            "sessions": [
                {
                    "session_id": session.session_id,
                    "content_version": session.content_version,
                    "turn_count": len(session.turns),
                    "unit_count": len(session.units),
                }
                for session in sessions
            ],
        }
    elif args.session_action == "add":
        record = add_session_record(
            paths["sessions"], args.session_id, seed=args.seed
        )
        value = {"ok": True, "added": record["session_id"]}
    else:
        value = {
            "ok": True,
            **remove_session_record(paths["sessions"], args.session_id),
        }
    _print(value)
    return 0


def command_config(args: argparse.Namespace) -> int:
    paths = _paths(args)
    if args.config_action == "show":
        _print({"ok": True, "config": load_config(paths["config"]).to_dict()})
        return 0
    current = load_config(paths["config"]).to_dict()
    field = args.field
    try:
        parsed_value = json.loads(args.value)
    except json.JSONDecodeError:
        parsed_value = args.value
    cursor: Dict[str, Any] = current
    parts = field.split(".")
    for part in parts[:-1]:
        if part not in cursor or not isinstance(cursor[part], dict):
            raise BusinessError(
                "E_CONFIG_FIELD_INVALID",
                f"unknown config field {field!r}",
                field=field,
            )
        cursor = cursor[part]
    if parts[-1] not in cursor:
        raise BusinessError(
            "E_CONFIG_FIELD_INVALID",
            f"unknown config field {field!r}",
            field=field,
        )
    cursor[parts[-1]] = parsed_value
    updated = config_from_dict(current)
    validate_config(updated)
    atomic_write_json(paths["config"], current)
    _print({"ok": True, "config": current})
    return 0


def command_snapshot_status(args: argparse.Namespace) -> int:
    from .snapshots import SnapshotManager
    from .validation import dataset_signature

    paths = _paths(args)
    sessions = load_sessions(paths["sessions"])
    vocabulary = load_vocabulary(paths["vocabulary"])
    config = load_config(paths["config"])
    manager = SnapshotManager(
        args.checkpoint_dir,
        [session.session_id for session in sessions],
        dataset_signature(sessions, vocabulary, config),
    )
    snapshots = manager.load()
    _print(
        {
            "ok": True,
            "snapshots": {
                stage: {
                    "version": envelope["version"],
                    "completed_sessions": envelope["completed_sessions"],
                    "checksum": envelope["checksum"],
                }
                for stage, envelope in snapshots.items()
            },
        }
    )
    return 0


def command_interactive(args: argparse.Namespace) -> int:
    paths = _paths(args)
    while True:
        print(
            "\n客服摘要编辑应用\n"
            "1 查看会话  2 新增会话  3 删除会话\n"
            "4 查看配置  5 修改配置  6 执行全量/续跑\n"
            "7 增量演示  8 中断恢复演示  0 退出"
        )
        choice = input("选择: ").strip()
        if choice == "0":
            return 0
        try:
            if choice == "1":
                command_sessions(
                    argparse.Namespace(
                        **_paths(args),
                        session_action="list",
                        session_id=None,
                        seed=20261124,
                    )
                )
            elif choice == "2":
                sid = input("新会话标识: ").strip()
                add_session_record(paths["sessions"], sid)
                print(f"已新增 {sid}")
            elif choice == "3":
                sid = input("要删除的会话标识: ").strip()
                remove_session_record(paths["sessions"], sid)
                print(f"已删除 {sid}")
            elif choice == "4":
                _print(load_config(paths["config"]).to_dict())
            elif choice == "5":
                field = input("字段(如 rewrite_strength): ").strip()
                value = input("新值: ").strip()
                command_config(
                    argparse.Namespace(
                        **_paths(args),
                        config_action="set",
                        field=field,
                        value=value,
                    )
                )
            elif choice == "6":
                command_run(
                    argparse.Namespace(
                        **_paths(args),
                        checkpoint_dir=paths["checkpoint"],
                        fresh=False,
                        crash_at=None,
                        output=None,
                        quiet=True,
                    )
                )
            elif choice == "7":
                scenario = input("append/strength/vocab: ").strip()
                command_incremental(
                    argparse.Namespace(
                        **_paths(args),
                        scenario=scenario,
                        session=input("目标会话(可空): ").strip() or None,
                        strength=0.65,
                        new_vocabulary=paths["root"] / "data" / "vocabulary_v2.json",
                        seed=20261124,
                        output=None,
                    )
                )
            elif choice == "8":
                _print(run_recovery_demo(paths["root"]))
            else:
                print("未知选项")
        except BusinessError as exc:
            _print({"ok": False, "error": exc.to_dict()})


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="support_summary",
        description="Deterministic customer-support summary/rewrite DAG",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init", help="generate deterministic samples")
    init_parser.add_argument("--root", type=Path, default=Path("."))
    init_parser.add_argument("--force", action="store_true")
    init_parser.set_defaults(func=command_init)

    run_parser = subparsers.add_parser("run", help="run or resume the DAG")
    _add_common(run_parser)
    run_parser.add_argument("--checkpoint-dir", type=Path)
    group = run_parser.add_mutually_exclusive_group()
    group.add_argument("--fresh", action="store_true")
    group.add_argument("--resume", action="store_true")
    run_parser.add_argument("--crash-at", choices=["rewrite:partial"])
    run_parser.add_argument("--output", type=Path)
    run_parser.add_argument("--quiet", action="store_true")
    run_parser.set_defaults(func=command_run)

    incremental_parser = subparsers.add_parser(
        "incremental", help="run one targeted invalidation scenario"
    )
    _add_common(incremental_parser)
    incremental_parser.add_argument(
        "--scenario", choices=["append", "strength", "vocab"], required=True
    )
    incremental_parser.add_argument("--session")
    incremental_parser.add_argument("--strength", type=float)
    incremental_parser.add_argument("--new-vocabulary", type=Path)
    incremental_parser.add_argument("--seed", type=int, default=20261124)
    incremental_parser.add_argument("--output", type=Path)
    incremental_parser.set_defaults(func=command_incremental)

    self_check_parser = subparsers.add_parser("self-check")
    _add_common(self_check_parser)
    self_check_parser.add_argument("--checkpoint-dir", type=Path)
    self_check_parser.add_argument("--fresh", action="store_true")
    self_check_parser.add_argument("--output", type=Path)
    self_check_parser.set_defaults(func=command_self_check)

    cache_parser = subparsers.add_parser("cache-query")
    _add_common(cache_parser)
    cache_parser.add_argument("--session", required=True)
    cache_parser.set_defaults(func=command_cache_query)

    recovery_parser = subparsers.add_parser("recovery-demo")
    recovery_parser.add_argument("--root", type=Path, default=Path("."))
    recovery_parser.add_argument("--output", type=Path)
    recovery_parser.set_defaults(func=command_recovery_demo)

    sessions_parser = subparsers.add_parser("sessions")
    session_sub = sessions_parser.add_subparsers(
        dest="session_action", required=True
    )
    list_parser = session_sub.add_parser("list")
    _add_common(list_parser)
    add_parser = session_sub.add_parser("add")
    _add_common(add_parser)
    add_parser.add_argument("session_id")
    add_parser.add_argument("--seed", type=int, default=20261124)
    remove_parser = session_sub.add_parser("remove")
    _add_common(remove_parser)
    remove_parser.add_argument("session_id")
    sessions_parser.set_defaults(func=command_sessions)

    config_parser = subparsers.add_parser("config")
    config_sub = config_parser.add_subparsers(
        dest="config_action", required=True
    )
    show_parser = config_sub.add_parser("show")
    _add_common(show_parser)
    set_parser = config_sub.add_parser("set")
    _add_common(set_parser)
    set_parser.add_argument("field")
    set_parser.add_argument("value")
    config_parser.set_defaults(func=command_config)

    snapshot_parser = subparsers.add_parser("snapshot-status")
    _add_common(snapshot_parser)
    snapshot_parser.add_argument("--checkpoint-dir", type=Path, required=True)
    snapshot_parser.set_defaults(func=command_snapshot_status)

    interactive_parser = subparsers.add_parser("interactive")
    _add_common(interactive_parser)
    interactive_parser.set_defaults(func=command_interactive)
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except BusinessError as exc:
        _print({"ok": False, "error": exc.to_dict()})
        return 2
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130
