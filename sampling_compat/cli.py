"""Local command-line application and interactive menu."""

from __future__ import annotations

import argparse
from pathlib import Path

from .engine import SamplingApplication
from .errors import BusinessError, ErrorCode
from .render import (
    print_error,
    render_adjudication,
    render_decision,
    render_error_outcomes,
    render_inspection,
    render_migration,
    render_recent_errors,
    render_request_list,
    render_self_check,
    render_snapshots,
)
from .selfcheck import run_self_check


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sampling-compat",
        description="Local LLM sampling configuration compatibility laboratory",
    )
    parser.add_argument(
        "--samples-dir",
        type=Path,
        default=None,
        help="fixture directory (default: <project>/samples)",
    )
    parser.add_argument(
        "--snapshot-path",
        type=Path,
        default=None,
        help="snapshot file (default: <project>/runtime/snapshots.json)",
    )
    subparsers = parser.add_subparsers(dest="command")

    subparsers.add_parser("menu", help="open the interactive local menu")
    subparsers.add_parser("list", help="list requests")
    subparsers.add_parser("snapshots", help="show snapshot versions and checksums")
    subparsers.add_parser("errors", help="run all business-error demonstrations")
    subparsers.add_parser("self-check", help="recompute decisions and verify snapshots")
    subparsers.add_parser("demo", help="run the complete non-interactive demo")

    show = subparsers.add_parser("show", help="show one request/configuration view")
    show.add_argument("request_id")
    show.add_argument("--version", choices=("v1", "v2", "v3"), default=None)

    run = subparsers.add_parser("run", help="sample one request")
    run.add_argument("request_id")
    run.add_argument("--version", choices=("v1", "v2", "v3"), default=None)
    run.add_argument("--persist", action="store_true")

    adjudicate = subparsers.add_parser("adjudicate", help="adjudicate all profile views")
    adjudicate.add_argument("request_id")

    migrate = subparsers.add_parser("migrate", help="show v1->v2 and v2->v1 migration")
    migrate.add_argument("request_id")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    command = args.command or "menu"
    try:
        app = SamplingApplication(
            samples_dir=args.samples_dir,
            snapshot_path=args.snapshot_path,
        )
        if command == "menu":
            return _interactive_menu(app)
        return _dispatch_command(app, command, args)
    except BusinessError as error:
        print_error(error)
        return 2
    except KeyboardInterrupt:
        print("\n已退出。")
        return 130


def _dispatch_command(
    app: SamplingApplication,
    command: str,
    args: argparse.Namespace,
) -> int:
    if command == "list":
        render_request_list(app)
        return 0
    if command == "show":
        render_inspection(app.inspect_profiles(args.request_id, args.version))
        return 0
    if command == "run":
        decision = app.run_request(
            args.request_id,
            args.version,
            persist=args.persist,
        )
        render_decision(decision)
        return 0
    if command == "adjudicate":
        render_adjudication(app.adjudicate(args.request_id))
        return 0
    if command == "migrate":
        render_migration(app.migrate_demo(args.request_id))
        return 0
    if command == "errors":
        render_error_outcomes(app.run_error_demo())
        return 0
    if command == "self-check":
        report = run_self_check(app, run_all=True)
        render_self_check(report)
        return 0
    if command == "snapshots":
        render_snapshots(app.snapshot_summary())
        return 0
    if command == "demo":
        return _run_demo(app)
    raise AssertionError(f"unknown command: {command}")


def _run_demo(app: SamplingApplication) -> int:
    print("Sampling Compatibility Lab | 本地确定性演示")
    print("=" * 78)
    report = app.run_all(persist=True)
    print(
        f"已采样 {report['request_count']} 个请求；"
        f"快照版本 {report['snapshot_versions']}"
    )
    render_request_list(app)

    print("\n逐请求 token 结果")
    print("-" * 78)
    for request_id, decision in report["decisions"].items():
        print(
            f"{request_id}: {decision['token_ids']}  "
            f"sha256={decision['token_sha256']}"
        )

    print("\n跨版本裁决")
    print("-" * 78)
    for request_id in app.request_ids():
        try:
            conclusion = app.adjudicate(request_id)
            print(
                f"{request_id}: CONSISTENT  versions={conclusion['versions']}  "
                f"tokens={conclusion['token_ids']}"
            )
        except BusinessError as error:
            if error.code != ErrorCode.SEMANTIC_GAP:
                raise
            print(
                f"{request_id}: {error.code}  "
                f"fields={error.details['unrestorable_fields']}  "
                f"steps={error.details['affected_steps']}  "
                f"divergent_steps={error.details['divergent_steps']}"
            )

    render_migration(app.migrate_demo("req-0002"))
    render_error_outcomes(app.run_error_demo())
    render_self_check(run_self_check(app, run_all=True))
    render_snapshots(app.snapshot_summary())
    print("\n演示完成。")
    return 0


def _interactive_menu(app: SamplingApplication) -> int:
    selected_request = app.request_ids()[0]
    selected_version = app.preferred_version(app.requests[selected_request])
    print("Sampling Compatibility Lab | 本地交互菜单")
    print("输入菜单编号执行；Ctrl-C 退出。")
    while True:
        last_error = app.last_error
        last_error_code = (
            last_error["error"]["code"] if last_error is not None else "<none>"
        )
        print("\n" + "=" * 78)
        print(
            f"当前请求: {selected_request} | 当前视图: {selected_version} | "
            f"tokenizer: {app.requests[selected_request].tokenizer_version}"
        )
        print(f"最近业务错误码: {last_error_code}")
        print("-" * 78)
        print("1 请求列表/选择    2 切换 v1/v2/v3 视图    3 查看配置与归一化")
        print("4 运行确定性采样   5 跨版本裁决            6 迁移/降级演示")
        print("7 错误码演示       8 一致性自检            9 快照版本")
        print("p 持久化全部       e 最近错误              0 退出")
        try:
            choice = input("> ").strip().lower()
        except EOFError:
            print()
            return 0
        if choice == "0":
            print("已退出。")
            return 0
        try:
            if choice == "1":
                render_request_list(app)
                candidate = input("request_id（留空取消）: ").strip()
                if candidate:
                    if candidate not in app.requests:
                        print(f"未知请求: {candidate}")
                    else:
                        selected_request = candidate
                        selected_version = app.preferred_version(app.requests[candidate])
            elif choice == "2":
                versions = app.requests[selected_request].profile_versions()
                print(f"可用视图: {', '.join(versions)}")
                candidate = input("版本: ").strip()
                if candidate in versions:
                    selected_version = candidate
                elif candidate:
                    print(f"该请求没有视图: {candidate}")
            elif choice == "3":
                render_inspection(
                    app.inspect_profiles(selected_request, selected_version)
                )
            elif choice == "4":
                decision = app.run_request(selected_request, selected_version)
                render_decision(decision)
            elif choice == "5":
                render_adjudication(app.adjudicate(selected_request))
            elif choice == "6":
                render_migration(app.migrate_demo(selected_request))
            elif choice == "7":
                render_error_outcomes(app.run_error_demo())
            elif choice == "8":
                render_self_check(run_self_check(app, run_all=True))
            elif choice == "9":
                render_snapshots(app.snapshot_summary())
            elif choice == "p":
                report = app.run_all(persist=True)
                print(f"已持久化，快照版本: {report['snapshot_versions']}")
            elif choice == "e":
                render_recent_errors(app.errors)
            else:
                print("未知菜单编号。")
        except BusinessError as error:
            print_error(error)
        except ValueError as error:
            print(f"输入错误: {error}")


if __name__ == "__main__":
    raise SystemExit(main())
