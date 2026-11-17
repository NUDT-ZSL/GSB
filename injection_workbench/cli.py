"""命令行入口。

退出码：
0 全部清单处理成功（拒绝记录属于业务结果，不算处理失败）；
1 部分清单失败（E_TIMEOUT/E_BUDGET/E_LOCK/内部错误/JSON 致命错误）；
2 参数错误。
"""
from __future__ import annotations

import argparse
import glob
import sys
from pathlib import Path

from .analyzer import MODE_EXACT, MODE_BATCH
from .checkpoints import CheckpointStore
from .config import Config, Thresholds, Budget
from .parser import load_manifest
from .runner import run_batch
from .samples import generate_all


def _build_config(args: argparse.Namespace) -> Config:
    return Config(
        thresholds=Thresholds(path_count=args.path_threshold, sink_count=args.sink_threshold),
        budget=Budget(
            concurrency=args.concurrency,
            deadline_seconds=args.deadline,
            max_paths_per_manifest=args.max_paths,
        ),
    )


def _expand_inputs(inputs: list[str]) -> list[Path]:
    paths: list[Path] = []
    for item in inputs:
        matched = [Path(p) for p in glob.glob(item)]
        if matched:
            paths.extend(matched)
        else:
            paths.append(Path(item))
    return sorted({p.resolve() for p in paths})


def _check_file_id_uniqueness(paths: list[Path]) -> list[dict]:
    """跨清单校验 file_id 唯一性，重复者返回拒绝记录（不阻断其他清单）。"""
    seen: dict[str, str] = {}
    duplicates: list[dict] = []
    for path in paths:
        parsed, fatal = load_manifest(path)
        file_id = parsed.file_id if parsed else None
        if fatal is not None or file_id is None:
            continue
        if file_id in seen:
            duplicates.append({
                "manifest": str(path),
                "file_id": file_id,
                "error_code": "E_DUP_FILE_ID",
                "message": f"file_id {file_id} 与 {seen[file_id]} 重复",
            })
        else:
            seen[file_id] = str(path)
    return duplicates


def _cmd_generate(args: argparse.Namespace) -> int:
    written = generate_all(args.samples_dir)
    for path in written:
        print(f"generated {path}")
    return 0


def _cmd_run(args: argparse.Namespace) -> int:
    if args.force_mode and args.force_mode not in (MODE_EXACT, MODE_BATCH):
        print(f"参数错误: 未知模式 {args.force_mode}", file=sys.stderr)
        return 2
    paths = _expand_inputs(args.inputs)
    if not paths:
        print("参数错误: 未匹配到任何清单文件", file=sys.stderr)
        return 2
    missing = [str(p) for p in paths if not p.exists()]
    if missing:
        print(f"参数错误: 文件不存在 {missing}", file=sys.stderr)
        return 2

    config = _build_config(args)
    dup_file_ids = _check_file_id_uniqueness(paths)
    store = None if args.no_checkpoint else CheckpointStore(args.checkpoint_dir, config)
    report = run_batch(
        paths, config, checkpoint_store=store,
        report_path=args.report, force_mode=args.force_mode,
    )
    report["file_id_uniqueness_violations"] = dup_file_ids

    if args.report:
        from .resources import atomic_write_text
        import json
        atomic_write_text(args.report, json.dumps(report, ensure_ascii=False, indent=2))

    failed = report["summary"]["failed"]
    lock_error = report["summary"].get("lock_error")
    for item in report["manifests"]:
        tag = item.get("error_code") or item.get("mode", "ok")
        print(f"[{item['status']}] {item.get('file_id') or item['manifest']} {tag}")
    if dup_file_ids:
        for violation in dup_file_ids:
            print(f"[rejected] {violation['file_id']} {violation['error_code']}")
    print(f"报告已写出: {args.report}")
    if failed or lock_error or dup_file_ids:
        return 1
    return 0


def _cmd_web(args: argparse.Namespace) -> int:
    from .webapp import create_app
    app = create_app(args.samples_dir, args.report, args.checkpoint_dir)
    app.run(host="127.0.0.1", port=args.port, debug=False)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="injection-workbench",
                                     description="注入类漏洞静态检测工作台")
    sub = parser.add_subparsers(dest="command", required=True)

    gen = sub.add_parser("generate", help="生成确定性本地样本")
    gen.add_argument("--samples-dir", default="samples")
    gen.set_defaults(func=_cmd_generate)

    run = sub.add_parser("run", help="分析清单并输出 JSON 报告")
    run.add_argument("inputs", nargs="+", help="清单 JSON 路径或 glob")
    run.add_argument("--report", default="reports/report.json")
    run.add_argument("--checkpoint-dir", default="checkpoints")
    run.add_argument("--no-checkpoint", action="store_true")
    run.add_argument("--concurrency", type=int, default=4)
    run.add_argument("--deadline", type=float, default=30.0, help="单清单墙钟上限秒，<=0 不限")
    run.add_argument("--max-paths", type=int, default=5000)
    run.add_argument("--path-threshold", type=int, default=200)
    run.add_argument("--sink-threshold", type=int, default=50)
    run.add_argument("--force-mode", choices=[MODE_EXACT, MODE_BATCH], default=None)
    run.set_defaults(func=_cmd_run)

    web = sub.add_parser("web", help="启动本地诊断工作台")
    web.add_argument("--samples-dir", default="samples")
    web.add_argument("--report", default="reports/report.json")
    web.add_argument("--checkpoint-dir", default="checkpoints")
    web.add_argument("--port", type=int, default=5000)
    web.set_defaults(func=_cmd_web)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit:
        return 2
    try:
        return args.func(args)
    except FileNotFoundError as exc:
        print(f"参数错误: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
