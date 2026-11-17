"""Command-line entry points for the local inference workbench."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Sequence

from .assets import generate_assets, load_assets, save_assets
from .canonical import atomic_write_json
from .config import BatchConfig
from .errors import WorkbenchError
from .session import InferenceSession, human_summary


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DATA_DIR = PROJECT_ROOT / "data"
DEFAULT_SNAPSHOT_DIR = PROJECT_ROOT / "snapshots"
DEFAULT_REPORT_DIR = PROJECT_ROOT / "reports"


def _error_exit_code(code: str) -> int:
    return {
        "E_CONFIG": 2,
        "E_DATA": 3,
        "E_REPLAY": 4,
        "E_SNAPSHOT": 5,
        "E_IO": 6,
    }.get(code, 1)


def _print_issue(exc: WorkbenchError) -> None:
    payload = {"status": "rejected", "issue": exc.to_dict()}
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


def _write_report(session: InferenceSession, report_path: str | Path, report: dict[str, Any]) -> Path:
    destination = Path(report_path)
    atomic_write_json(destination, report)
    return destination


def _load_session(data_dir: str | Path, snapshot_dir: str | Path) -> InferenceSession:
    assets = load_assets(data_dir)
    return InferenceSession(assets, snapshot_dir=snapshot_dir)


def command_generate(args: argparse.Namespace) -> int:
    data_dir = Path(args.data_dir)
    existing = [data_dir / "dataset.json", data_dir / "model.json"]
    if any(path.exists() for path in existing) and not args.force:
        raise WorkbenchError(
            "E_IO",
            "数据文件已存在；使用 --force 才会整体重新生成",
            {"data_dir": data_dir.as_posix()},
        )
    assets = generate_assets(
        seed=args.seed,
        sample_count=args.samples,
        input_dim=args.input_dim,
        hidden_dims=args.hidden_dims,
        output_dim=args.output_dim,
    )
    paths = save_assets(data_dir, assets)
    result = {
        "status": "generated",
        "generation": assets.generation,
        "data_digest": assets.data_digest,
        "parameter_digest": assets.parameter_digest,
        "files": [path.as_posix() for path in paths],
        "sample_ids": [sample.sample_id for sample in assets.samples],
        "layer_names": list(assets.layer_names),
    }
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def command_run(args: argparse.Namespace) -> int:
    config = BatchConfig.load(args.config)
    session = _load_session(args.data_dir, args.snapshot_dir)
    report = session.run(
        config,
        stop_after=args.stop_after,
        snapshot_path=args.snapshot,
    )
    report_path = _write_report(session, args.report, report)
    print(human_summary(report), end="")
    print(f"report={report_path}")
    if args.snapshot:
        print(f"snapshot={Path(args.snapshot)}")
    return 0


def command_restore(args: argparse.Namespace) -> int:
    config = BatchConfig.load(args.config) if args.config else None
    session = _load_session(args.data_dir, args.snapshot_dir)
    restore = session.restore_snapshot(args.snapshot)
    if restore.get("code"):
        report = session.build_report()
        report_path = _write_report(session, args.report, report)
        print(human_summary(report), end="")
        print(f"report={report_path}")
        return _error_exit_code(str(restore["code"]))

    if args.replay:
        replay = session.replay_snapshot(args.snapshot, expected_config=config)
        if replay.get("code"):
            report = session.build_report()
            report_path = _write_report(session, args.report, report)
            print(human_summary(report), end="")
            print(f"report={report_path}")
            return _error_exit_code(str(replay["code"]))

    if args.continue_run:
        session.continue_remaining(
            stop_after=args.stop_after,
            snapshot_path=args.snapshot,
        )
    report = session.build_report()
    report_path = _write_report(session, args.report, report)
    print(human_summary(report), end="")
    print(f"report={report_path}")
    return 0


def command_replay(args: argparse.Namespace) -> int:
    expected = BatchConfig.load(args.expected_config) if args.expected_config else None
    session = _load_session(args.data_dir, args.snapshot_dir)
    replay = session.replay_snapshot(args.snapshot, expected_config=expected)
    result = {
        "status": replay["status"],
        "code": replay.get("code"),
        "replay": replay,
        "business_codes": [replay["code"]] if replay.get("code") else [],
    }
    report_path = _write_report(session, args.report, result)
    print(json.dumps(replay, ensure_ascii=False, indent=2, sort_keys=True))
    print(f"report={report_path}")
    return _error_exit_code(str(replay["code"])) if replay.get("code") else 0


def command_demo(args: argparse.Namespace) -> int:
    from .demo import run_demo

    result = run_demo(
        project_root=Path(args.project_root),
        out_dir=Path(args.out_dir),
        clean=not args.keep,
    )
    print(result["summary"], end="")
    print(f"demo_manifest={result['manifest_path']}")
    return 0


def command_ui(args: argparse.Namespace) -> int:
    try:
        from .ui import launch_ui
    except ImportError as exc:
        raise WorkbenchError(
            "E_IO",
            "当前 Python 未安装 tkinter；Windows 官方 Python 安装包默认包含该组件",
            {"cause": str(exc)},
        ) from exc
    launch_ui(
        data_dir=Path(args.data_dir),
        snapshot_dir=Path(args.snapshot_dir),
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="infer-workbench",
        description="本地确定性推理批处理与数值精度诊断工作台",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    generate = subparsers.add_parser("generate", help="生成确定性样本与模型参数")
    generate.add_argument("--seed", type=int, default=20261117)
    generate.add_argument("--samples", type=int, default=12)
    generate.add_argument("--input-dim", type=int, default=4)
    generate.add_argument(
        "--hidden-dims",
        "--hidden-dim",
        dest="hidden_dims",
        type=int,
        nargs="+",
        default=[6, 5],
    )
    generate.add_argument("--output-dim", type=int, default=3)
    generate.add_argument("--data-dir", default=str(DEFAULT_DATA_DIR))
    generate.add_argument("--force", action="store_true")
    generate.set_defaults(handler=command_generate)

    run = subparsers.add_parser("run", help="按 JSON 配置执行一次整体推理")
    run.add_argument("--config", required=True)
    run.add_argument("--data-dir", default=str(DEFAULT_DATA_DIR))
    run.add_argument("--snapshot-dir", default=str(DEFAULT_SNAPSHOT_DIR))
    run.add_argument("--snapshot", default=None, help="每完成一批后原子写入该快照")
    run.add_argument("--stop-after", type=int, default=None, help="本次最多执行多少批")
    run.add_argument("--report", default=str(DEFAULT_REPORT_DIR / "latest_report.json"))
    run.set_defaults(handler=command_run)

    restore = subparsers.add_parser("restore", help="恢复快照并可选重放/继续")
    restore.add_argument("--snapshot", required=True)
    restore.add_argument("--config", default=None, help="重放时用于比较的当前配置")
    restore.add_argument("--data-dir", default=str(DEFAULT_DATA_DIR))
    restore.add_argument("--snapshot-dir", default=str(DEFAULT_SNAPSHOT_DIR))
    restore.add_argument("--report", default=str(DEFAULT_REPORT_DIR / "restore_report.json"))
    restore.add_argument("--replay", action="store_true")
    restore.add_argument("--continue-run", action="store_true")
    restore.add_argument("--stop-after", type=int, default=None)
    restore.set_defaults(handler=command_restore)

    replay = subparsers.add_parser("replay", help="对快照中的已完成批次做逐位重放校验")
    replay.add_argument("--snapshot", required=True)
    replay.add_argument("--expected-config", default=None)
    replay.add_argument("--data-dir", default=str(DEFAULT_DATA_DIR))
    replay.add_argument("--snapshot-dir", default=str(DEFAULT_SNAPSHOT_DIR))
    replay.add_argument("--report", default=str(DEFAULT_REPORT_DIR / "replay_report.json"))
    replay.set_defaults(handler=command_replay)

    demo = subparsers.add_parser("demo", help="运行三阶段可重复演示")
    demo.add_argument("--project-root", default=str(PROJECT_ROOT))
    demo.add_argument("--out-dir", default=str(PROJECT_ROOT / "demo_out"))
    demo.add_argument("--keep", action="store_true", help="保留旧 demo_out 而非先清理")
    demo.set_defaults(handler=command_demo)

    ui = subparsers.add_parser("ui", help="启动 Tkinter 诊断界面")
    ui.add_argument("--data-dir", default=str(DEFAULT_DATA_DIR))
    ui.add_argument("--snapshot-dir", default=str(DEFAULT_SNAPSHOT_DIR))
    ui.set_defaults(handler=command_ui)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.handler(args))
    except WorkbenchError as exc:
        _print_issue(exc)
        return _error_exit_code(exc.code)
    except OSError as exc:
        issue = WorkbenchError("E_IO", str(exc), {"errno": exc.errno})
        _print_issue(issue)
        return _error_exit_code(issue.code)


if __name__ == "__main__":
    raise SystemExit(main())
