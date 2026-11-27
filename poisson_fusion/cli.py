"""Command-line entry points for generation, fusion, replacement and demo."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .errors import FusionError
from .io_utils import save_json
from .logging_utils import log_record
from .pipeline import FusionParameters, fuse_image_files, replace_foreground_files
from .sample_gen import generate_sample


def _parse_color(value: str) -> tuple[float, float, float]:
    parts = [part.strip() for part in value.split(",")]
    if len(parts) != 3:
        raise argparse.ArgumentTypeError("颜色格式应为 R,G,B")
    try:
        return tuple(float(part) for part in parts)  # type: ignore[return-value]
    except ValueError as exc:
        raise argparse.ArgumentTypeError("颜色分量必须为数值") from exc


def _params(args: argparse.Namespace) -> FusionParameters:
    return FusionParameters(
        tolerance=args.tolerance,
        max_iterations=args.max_iterations,
        band_radius=args.band_radius,
        foreground_mode=args.foreground_mode,
        background_color=args.background_color,
        alpha_threshold=args.alpha_threshold,
        solid_threshold=args.solid_threshold,
    )


def _add_common_fusion_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--tolerance", type=float, default=1e-8)
    parser.add_argument("--max-iterations", type=int, default=8000)
    parser.add_argument("--band-radius", type=int, default=3)
    parser.add_argument(
        "--foreground-mode",
        choices=("auto", "alpha", "solid"),
        default="auto",
        help="alpha uses RGBA alpha; solid removes a uniform background colour",
    )
    parser.add_argument("--background-color", type=_parse_color, default=None)
    parser.add_argument("--alpha-threshold", type=float, default=1.0)
    parser.add_argument("--solid-threshold", type=float, default=8.0)
    parser.add_argument("--log-path", default="runs/run_log.jsonl")
    parser.add_argument("--seed-hint", type=int, default=None)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m poisson_fusion",
        description="Deterministic Poisson assembly-image fusion with incremental replacement.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    generate = subparsers.add_parser("generate", help="create a fixed-seed sample")
    generate.add_argument("--seed", type=int, required=True)
    generate.add_argument("--out", required=True)
    generate.add_argument("--height", type=int, default=192)
    generate.add_argument("--width", type=int, default=256)
    generate.add_argument("--band-radius", type=int, default=3)

    fuse = subparsers.add_parser("fuse", help="run full Poisson fusion")
    fuse.add_argument("--base", required=True)
    fuse.add_argument("--foreground", required=True)
    fuse.add_argument("--mask", required=True)
    fuse.add_argument("--output-dir", required=True)
    _add_common_fusion_args(fuse)

    replace = subparsers.add_parser("replace", help="replace one foreground incrementally")
    replace.add_argument("--state", required=True)
    replace.add_argument("--foreground", required=True)
    replace.add_argument("--mask", default=None, help="new mask; defaults to the previous mask")
    replace.add_argument("--output-dir", required=True)
    _add_common_fusion_args(replace)

    demo = subparsers.add_parser("demo", help="run generation, fusion and two replacements")
    demo.add_argument("--seed", type=int, default=20261128)
    demo.add_argument("--workdir", default="demo_output")
    demo.add_argument("--height", type=int, default=192)
    demo.add_argument("--width", type=int, default=256)
    demo.add_argument("--band-radius", type=int, default=3)
    demo.add_argument("--tolerance", type=float, default=1e-8)
    demo.add_argument("--max-iterations", type=int, default=8000)
    demo.add_argument("--log-path", default="runs/run_log.jsonl")
    return parser


def _print(data: Any) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True))


def _run_demo(args: argparse.Namespace) -> dict[str, Any]:
    root = Path(args.workdir).resolve()
    samples = root / "samples"
    output_dir = root / "outputs"
    sample = generate_sample(
        args.seed,
        samples,
        height=args.height,
        width=args.width,
        band_radius=args.band_radius,
    )
    params = FusionParameters(
        tolerance=args.tolerance,
        max_iterations=args.max_iterations,
        band_radius=args.band_radius,
    )
    initial = fuse_image_files(
        samples / "base.npy",
        samples / "foreground_rgba.npy",
        samples / "target_mask.npy",
        output_dir / "initial",
        params=params,
        log_path=args.log_path,
        seed_hint=args.seed,
    )
    same_mask = replace_foreground_files(
        output_dir / "initial" / "state.json",
        samples / "foreground_v2_rgba.npy",
        output_dir / "replace_same_mask",
        params_override=params,
        log_path=args.log_path,
        seed_hint=args.seed,
    )
    changed_mask = replace_foreground_files(
        output_dir / "initial" / "state.json",
        samples / "foreground_v2_rgba.npy",
        output_dir / "replace_changed_mask",
        new_mask_path=samples / "target_mask_v2.npy",
        params_override=params,
        log_path=args.log_path,
        seed_hint=args.seed,
    )
    summary = {
        "ok": True,
        "seed": args.seed,
        "workdir": str(root),
        "sample": {
            "base_sha256": sample["base_sha256"],
            "mask_sha256": sample["mask_sha256"],
            "mask_pixels": sample["mask_pixels"],
            "band_pixels": sample["band_pixels"],
        },
        "initial": {
            "output_dir": initial["output_dir"],
            "boundary_max_error": initial["metrics"]["boundary_match"]["max_error"],
            "seam_max": initial["metrics"]["seam_error"]["max"],
            "gradient_p95": initial["metrics"]["gradient_error"]["p95"],
            "solver_iterations_max": initial["solver"]["iterations_max"],
        },
        "replace_same_mask": {
            "output_dir": same_mask["output_dir"],
            "verification": same_mask["incremental_verification"],
            "mask_change_detected": same_mask["mask_change"]["detected"],
        },
        "replace_changed_mask": {
            "output_dir": changed_mask["output_dir"],
            "verification": changed_mask["incremental_verification"],
            "mask_change_detected": changed_mask["mask_change"]["detected"],
            "old_only_pixels": changed_mask["mask_change"]["old_only"]["pixels"],
            "new_only_pixels": changed_mask["mask_change"]["new_only"]["pixels"],
            "xor_pixels": changed_mask["mask_change"]["xor_pixels"],
        },
        "run_log": str(Path(args.log_path).resolve()),
    }
    save_json(root / "demo_summary.json", summary)
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "generate":
            result = generate_sample(
                args.seed,
                args.out,
                height=args.height,
                width=args.width,
                band_radius=args.band_radius,
            )
            _print({"ok": True, "output_dir": str(Path(args.out).resolve()), "sample": result})
            return 0
        if args.command == "fuse":
            result = fuse_image_files(
                args.base,
                args.foreground,
                args.mask,
                args.output_dir,
                params=_params(args),
                log_path=args.log_path,
                seed_hint=args.seed_hint,
            )
            _print(result)
            return 0
        if args.command == "replace":
            result = replace_foreground_files(
                args.state,
                args.foreground,
                args.output_dir,
                new_mask_path=args.mask,
                params_override=_params(args),
                log_path=args.log_path,
                seed_hint=args.seed_hint,
            )
            _print(result)
            return 0
        if args.command == "demo":
            result = _run_demo(args)
            _print(result)
            return 0
        parser.error(f"unsupported command: {args.command}")
    except FusionError as exc:
        payload = exc.to_dict()
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True), file=sys.stderr)
        return exc.exit_code
    except (OSError, ValueError) as exc:
        error = FusionError("E_IMAGE_IO", str(exc), {"type": type(exc).__name__})
        print(json.dumps(error.to_dict(), ensure_ascii=False, sort_keys=True), file=sys.stderr)
        return error.exit_code
    return 0
