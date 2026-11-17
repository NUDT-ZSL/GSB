"""命令行入口：样本生成、首帧预览、全量渲染与信号优雅退出。"""
from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import threading
from typing import List, Optional

from .config import ConfigError, load_config
from .errors import ErrorCode, Failure
from .pipeline import Pipeline, RenderRequest
from .report import build_report
from .samplegen import generate_fault_suite, generate_valid_sample
from .samples import load_clips
from .snapshot import SnapshotStore


def _write_report(output_dir: str, report: dict) -> str:
    path = os.path.join(output_dir, "diagnostic_report.json")
    os.makedirs(output_dir, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2, sort_keys=True)
    os.replace(tmp, path)
    return path


def _build_request(args: argparse.Namespace, config) -> RenderRequest:
    clips, load_failures = load_clips(args.sample_dir, config)
    return RenderRequest(
        sample_dir=args.sample_dir, output_dir=args.output_dir,
        clips=clips, load_failures=load_failures,
        stop_event=threading.Event() if args.graceful_signal else None)


def cmd_generate(args: argparse.Namespace) -> int:
    config_path = generate_valid_sample(
        args.sample_dir, clip_count=args.clips, frames=args.frames,
        denoise_steps=args.steps, seed=args.seed,
        tiers=args.tiers.split(","), embed_dim=args.embed_dim,
        replace_steps=args.replace_steps, concurrency=args.concurrency,
        first_frame_budget_ms=args.preview_budget,
        memory_cap_mb=args.memory_cap)
    fault_dir = os.path.join(os.path.dirname(args.sample_dir), "faults")
    suite = generate_fault_suite(fault_dir, args.sample_dir)
    payload = {"config_path": config_path,
               "sample_dir": args.sample_dir,
               "fault_suite": suite}
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def cmd_preview(args: argparse.Namespace) -> int:
    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print(json.dumps({"code": ErrorCode.E_CONFIG_INVALID,
                          "message": str(exc)}, ensure_ascii=False))
        return 2
    request = _build_request(args, config)
    pipeline = Pipeline(config)
    response = pipeline.preview_first_frame(request)
    print(json.dumps(response, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if response.get("code") != ErrorCode.E_PREVIEW_BUDGET_PENDING else 3


def cmd_run(args: argparse.Namespace) -> int:
    try:
        config = load_config(args.config)
    except ConfigError as exc:
        report = {"code": ErrorCode.E_CONFIG_INVALID, "message": str(exc)}
        os.makedirs(args.output_dir, exist_ok=True)
        _write_report(args.output_dir, report)
        print(json.dumps(report, ensure_ascii=False))
        return 2
    request = _build_request(args, config)
    pipeline = Pipeline(config)

    if args.graceful_signal:
        def _handler(signum, frame):  # pragma: no cover - 信号路径
            request.stop_event.set()
        signal.signal(signal.SIGTERM, _handler)
        signal.signal(signal.SIGINT, _handler)

    outcome = pipeline.run(request)
    report = build_report(
        config, outcome["results"], outcome["failures"],
        outcome["decision"], outcome["manifest_version"],
        extra={"graceful_shutdown": outcome["graceful_shutdown"]})
    report_path = _write_report(args.output_dir, report)
    print(json.dumps({"report": report_path,
                      "snapshot_version": outcome["manifest_version"],
                      "failure_summary": report["failure_summary"],
                      "graceful_shutdown": outcome["graceful_shutdown"]},
                     ensure_ascii=False, indent=2, sort_keys=True))
    if outcome["graceful_shutdown"]:
        return 0
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="vbench",
        description="本地确定性视频预测与运动时序渲染诊断工作台")
    sub = parser.add_subparsers(dest="command", required=True)

    gen = sub.add_parser("generate", help="生成确定性样本与异常注入用例")
    gen.add_argument("--sample-dir", required=True)
    gen.add_argument("--clips", type=int, default=4)
    gen.add_argument("--frames", type=int, default=6)
    gen.add_argument("--steps", type=int, default=8)
    gen.add_argument("--seed", type=int, default=20261117)
    gen.add_argument("--tiers", default="64x64,128x128,256x256")
    gen.add_argument("--embed-dim", type=int, default=32)
    gen.add_argument("--replace-steps", type=int, default=3)
    gen.add_argument("--concurrency", type=int, default=4)
    gen.add_argument("--preview-budget", type=float, default=500.0)
    gen.add_argument("--memory-cap", type=float, default=2048.0)
    gen.set_defaults(func=cmd_generate)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", required=True)
    common.add_argument("--sample-dir", required=True)
    common.add_argument("--output-dir", required=True)
    common.add_argument("--graceful-signal", action="store_true")

    preview = sub.add_parser("preview", parents=[common],
                             help="首帧预算内最低档预览")
    preview.set_defaults(func=cmd_preview)

    run = sub.add_parser("run", parents=[common], help="全量双路径多档位渲染")
    run.set_defaults(func=cmd_run)
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
