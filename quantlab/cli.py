from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from .config import WorkbenchConfig
from .demo import PROJECT_ROOT, run_demo
from .errors import BusinessError
from .pipeline import ForcedTermination, Workbench
from .server import serve


def _path(value: str) -> Path:
    return Path(value)


def _config_from_args(args: argparse.Namespace) -> WorkbenchConfig:
    return WorkbenchConfig(
        state_dir=_path(args.state_dir),
        cache_dir=_path(args.cache_dir),
        temp_dir=_path(args.temp_dir),
        artifacts_dir=_path(args.artifacts_dir),
        tolerance_abs=args.tolerance_abs,
        tolerance_rel=args.tolerance_rel,
        max_layers_per_stage=args.max_layers,
        max_resident_weight_bytes=args.max_weight_bytes,
        max_calibration_comparisons=args.max_calibration_comparisons,
        max_block_elements=args.max_block_elements,
    )


def _add_runtime_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--state-dir", default="runtime/state")
    parser.add_argument("--cache-dir", default="runtime/cache")
    parser.add_argument("--temp-dir", default="runtime/tmp")
    parser.add_argument("--artifacts-dir", default="artifacts")
    parser.add_argument("--tolerance-abs", type=float, default=0.25)
    parser.add_argument("--tolerance-rel", type=float, default=0.50)
    parser.add_argument("--max-layers", type=int, default=64)
    parser.add_argument("--max-weight-bytes", type=int, default=32 * 1024 * 1024)
    parser.add_argument("--max-calibration-comparisons", type=int, default=2_000_000)
    parser.add_argument("--max-block-elements", type=int, default=4096)
    parser.add_argument("--result-file", default=None)
    parser.add_argument("--json", action="store_true")


def _print_result(result: dict[str, Any], *, json_mode: bool) -> None:
    if json_mode:
        summary = {
            "ok": True,
            "mode": result["recovery"]["mode"],
            "dominant_source": result["diagnostics"]["dominant_source"],
            "prediction": result["inference"]["final_prediction"],
            "prediction_checksum": result["inference"][
                "final_prediction_checksum"
            ],
            "consistency": result["consistency"]["ok"],
            "latest_version": result["progress"]["latest"]["version"]
            if result["progress"].get("latest")
            else None,
            "reused_inference_layers": result["execution"][
                "reused_layers_by_stage"
            ]["inference"],
            "recomputed_inference_layers": result["execution"][
                "recomputed_layers_by_stage"
            ]["inference"],
        }
        print(json.dumps(summary, ensure_ascii=False))
    else:
        print(f"mode: {result['recovery']['mode']}")
        print(f"dominant_source: {result['diagnostics']['dominant_source']}")
        print(f"prediction: {result['inference']['final_prediction']}")
        print(
            "prediction_checksum: "
            f"{result['inference']['final_prediction_checksum']}"
        )
        print(f"consistency: {result['consistency']['ok']}")
        print(
            "progress_version: "
            f"{result['progress']['latest']['version'] if result['progress'].get('latest') else None}"
        )


def _persist_result(result: dict[str, Any], result_file: str | None) -> None:
    if not result_file:
        return
    path = Path(result_file)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


def cmd_run(args: argparse.Namespace) -> int:
    result = Workbench(_config_from_args(args)).run(clean=args.clean)
    _persist_result(result, args.result_file)
    _print_result(result, json_mode=args.json)
    return 0


def cmd_resume(args: argparse.Namespace) -> int:
    result = Workbench(_config_from_args(args)).run(resume=True)
    _persist_result(result, args.result_file)
    _print_result(result, json_mode=args.json)
    return 0


def cmd_crash_run(args: argparse.Namespace) -> int:
    def terminate(info: dict[str, Any]) -> None:
        print(
            json.dumps(
                {
                    "forced_termination": True,
                    **info,
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        os._exit(23)

    Workbench(_config_from_args(args)).run(
        clean=args.clean,
        interrupt_after_inference_layer=args.crash_after_layer,
        termination_hook=terminate,
    )
    return 0


def cmd_revision(args: argparse.Namespace) -> int:
    cfg = WorkbenchConfig(
        state_dir=_path(args.state_dir),
        cache_dir=_path(args.cache_dir),
        temp_dir=_path(args.temp_dir),
        artifacts_dir=_path(args.artifacts_dir),
        tolerance_abs=args.tolerance_abs,
        tolerance_rel=args.tolerance_rel,
        revision_overrides={args.layer_index: args.revision},
    )
    result = Workbench(cfg).run(reuse_previous=True)
    _persist_result(result, args.result_file)
    _print_result(result, json_mode=args.json)
    return 0


def cmd_demo(args: argparse.Namespace) -> int:
    summary = run_demo(
        crash_after_layer=args.crash_after_layer,
        artifacts_dir=_path(args.artifacts_dir),
    )
    print(json.dumps(summary, ensure_ascii=False))
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    serve(host=args.host, port=args.port, artifacts_dir=_path(args.artifacts_dir))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="quantlab",
        description="QuantTrace Lab 本地量化推理诊断工作台",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="执行完整流水线")
    _add_runtime_args(run)
    run.add_argument("--clean", action="store_true")
    run.set_defaults(func=cmd_run)

    resume = sub.add_parser("resume", help="从最近已提交快照恢复")
    _add_runtime_args(resume)
    resume.set_defaults(func=cmd_resume)

    crash = sub.add_parser("crash-run", help="在指定推理层提交后强制退出")
    _add_runtime_args(crash)
    crash.add_argument("--clean", action="store_true")
    crash.add_argument("--crash-after-layer", type=int, default=1)
    crash.set_defaults(func=cmd_crash_run)

    revision = sub.add_parser("revision-demo", help="演示权重版本失效重算")
    _add_runtime_args(revision)
    revision.add_argument("--layer-index", type=int, required=True)
    revision.add_argument("--revision", type=int, required=True)
    revision.set_defaults(func=cmd_revision)

    demo = sub.add_parser("demo", help="完整运行、强杀、恢复并比对")
    demo.add_argument("--crash-after-layer", type=int, default=1)
    demo.add_argument("--artifacts-dir", default="artifacts")
    demo.set_defaults(func=cmd_demo)

    server = sub.add_parser("serve", help="启动本地可视化工作台")
    server.add_argument("--host", default="127.0.0.1")
    server.add_argument("--port", type=int, default=8765)
    server.add_argument("--artifacts-dir", default="artifacts")
    server.set_defaults(func=cmd_serve)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except BusinessError as exc:
        print(json.dumps(exc.to_dict(), ensure_ascii=False), file=sys.stderr)
        return 2
    except ForcedTermination as exc:
        print(json.dumps({"forced_termination": True, **exc.info}, ensure_ascii=False))
        return 23


if __name__ == "__main__":
    raise SystemExit(main())
