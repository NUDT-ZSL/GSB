from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path
from typing import Any

from .canonical import atomic_write_text, canonical_json
from .config import load_config
from .demo import run_demo
from .diagnostics import collect_error_scenarios
from .errors import ErrorCode
from .pipeline import CascadePipeline, CrashController
from .samples import DEFAULT_SEED, generate_samples
from .state import build_state


def _project_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _default_paths() -> dict[str, Path]:
    root = _project_root()
    run_root = root / "runs" / "current"
    return {
        "root": root,
        "data": root / "data",
        "input": root / "data" / "lines.jsonl",
        "dict": root / "data" / "dict_v1.json",
        "labels": root / "data" / "labels_v1.json",
        "config": root / "data" / "config_default.json",
        "store": run_root / "snapshots.sqlite3",
        "out": run_root / "out",
    }


def _safe_reset(directory: Path) -> None:
    root = _project_root().resolve()
    resolved = directory.resolve()
    if root not in resolved.parents and resolved != root:
        raise SystemExit(f"拒绝清理项目目录之外的路径: {resolved}")
    if resolved.exists():
        shutil.rmtree(resolved)


def _add_run_arguments(parser: argparse.ArgumentParser) -> None:
    defaults = _default_paths()
    parser.add_argument("--input", default=str(defaults["input"]))
    parser.add_argument("--dict", default=str(defaults["dict"]))
    parser.add_argument("--labels", default=str(defaults["labels"]))
    parser.add_argument("--config", default=str(defaults["config"]))
    parser.add_argument("--store", default=str(defaults["store"]))
    parser.add_argument("--out", default=str(defaults["out"]))
    parser.add_argument("--fresh", action="store_true", help="清理当前 store 与输出目录")
    parser.add_argument(
        "--crash-after",
        choices=["validate", "segment", "tag", "summarize"],
        help="在指定阶段快照提交后以 os._exit(137) 硬终止",
    )
    parser.add_argument(
        "--crash-mid",
        choices=["validate", "segment", "tag", "summarize"],
        help="在指定阶段处理若干行后硬终止，不提交该阶段",
    )
    parser.add_argument("--crash-docs", type=int, default=1)
    parser.add_argument(
        "--inject-fault",
        choices=["validate", "segment", "tag", "summarize"],
        help="在指定阶段注入未归因异常，用于验证 E_UNATTRIBUTED",
    )


def _run_command(args: argparse.Namespace) -> int:
    if args.fresh:
        _safe_reset(Path(args.store).parent)
    crash = CrashController(
        after_stage=args.crash_after,
        mid_stage=args.crash_mid,
        after_docs=max(1, args.crash_docs),
        hard=True,
    )
    pipeline = CascadePipeline(
        input_path=args.input,
        dict_path=args.dict,
        labels_path=args.labels,
        config=args.config,
        store_path=args.store,
        output_dir=args.out,
        crash=crash,
        fault_injection=args.inject_fault,
    )
    outcome = pipeline.run()
    print(json.dumps(outcome.to_dict(), ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if outcome.ok else 2


def _init_command(args: argparse.Namespace) -> int:
    manifest = generate_samples(args.data, args.seed)
    print(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def _upgrade_command(args: argparse.Namespace) -> int:
    defaults = _default_paths()
    return _run_command(
        argparse.Namespace(
            input=args.input or str(defaults["input"]),
            dict=args.dict or str(defaults["data"] / "dict_v2.json"),
            labels=args.labels or str(defaults["data"] / "labels_v2.json"),
            config=args.config,
            store=args.store,
            out=args.out,
            fresh=False,
            crash_after=None,
            crash_mid=None,
            crash_docs=1,
            inject_fault=None,
        )
    )


def _state_command(args: argparse.Namespace) -> int:
    defaults = _default_paths()
    state = build_state(
        store_path=args.store or defaults["store"],
        output_dir=args.out or defaults["out"],
        data_dir=args.data or defaults["data"],
    )
    print(json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if state.get("ok") else 2


def _errors_command(args: argparse.Namespace) -> int:
    report = collect_error_scenarios(args.data)
    output = Path(args.out)
    output.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(output, canonical_json(report) + "\n")
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report["ok"] else 2


def _demo_command(args: argparse.Namespace) -> int:
    report = run_demo(args.root)
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report["ok"] else 1


def _serve_command(args: argparse.Namespace) -> int:
    from .web import serve

    serve(
        host=args.host,
        port=args.port,
        project_root=Path(args.root),
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    defaults = _default_paths()
    parser = argparse.ArgumentParser(
        prog="poscascade",
        description="确定性中文分词与词性标注级联应用",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init", help="生成固定种子本地样本")
    init_parser.add_argument("--data", default=str(defaults["data"]))
    init_parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    init_parser.set_defaults(handler=_init_command)

    run_parser = subparsers.add_parser("run", help="运行或从最后已提交阶段恢复")
    _add_run_arguments(run_parser)
    run_parser.set_defaults(handler=_run_command)

    resume_parser = subparsers.add_parser("resume", help="run 的恢复别名")
    _add_run_arguments(resume_parser)
    resume_parser.set_defaults(handler=_run_command)

    upgrade_parser = subparsers.add_parser("upgrade", help="升级到 v2 词典与收缩标签集")
    upgrade_parser.add_argument("--data", default=str(defaults["data"]))
    upgrade_parser.add_argument("--input", default=None)
    upgrade_parser.add_argument("--dict", default=None)
    upgrade_parser.add_argument("--labels", default=None)
    upgrade_parser.add_argument("--config", default=str(defaults["config"]))
    upgrade_parser.add_argument("--store", default=str(defaults["store"]))
    upgrade_parser.add_argument("--out", default=str(defaults["out"]))
    upgrade_parser.set_defaults(handler=_upgrade_command)

    state_parser = subparsers.add_parser("state", help="输出当前阶段、快照与自检状态")
    state_parser.add_argument("--data", default=str(defaults["data"]))
    state_parser.add_argument("--store", default=str(defaults["store"]))
    state_parser.add_argument("--out", default=str(defaults["out"]))
    state_parser.set_defaults(handler=_state_command)

    errors_parser = subparsers.add_parser("errors", help="执行异常链路归因演示")
    errors_parser.add_argument("--data", default=str(defaults["data"]))
    errors_parser.add_argument(
        "--out",
        default=str(defaults["root"] / "runs" / "errors" / "report.json"),
    )
    errors_parser.set_defaults(handler=_errors_command)

    demo_parser = subparsers.add_parser("demo", help="执行完整恢复/升级/降级对比演示")
    demo_parser.add_argument("--root", default=str(defaults["root"]))
    demo_parser.set_defaults(handler=_demo_command)

    serve_parser = subparsers.add_parser("serve", help="启动本地交互应用")
    serve_parser.add_argument("--host", default="127.0.0.1")
    serve_parser.add_argument("--port", type=int, default=8765)
    serve_parser.add_argument("--root", default=str(defaults["root"]))
    serve_parser.set_defaults(handler=_serve_command)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.handler(args))


if __name__ == "__main__":
    raise SystemExit(main())
