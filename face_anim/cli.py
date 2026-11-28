"""argparse command-line interface."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

from .errors import PipelineError
from .generator import generate_samples
from .io_utils import write_json
from .parser import version_dirs_from_samples
from .pipeline import run_pipeline


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="museum-lipsync",
        description=(
            "Normalize three museum narration project versions, synthesize facial "
            "channels, validate synchronization and export player keyframes."
        ),
    )
    subparsers = parser.add_subparsers(dest="command")

    generate = subparsers.add_parser(
        "generate", help="generate deterministic three-version samples"
    )
    generate.add_argument(
        "--output",
        "-o",
        "--out",
        "--out-dir",
        dest="output",
        default="samples",
        help="sample output directory",
    )
    generate.add_argument("--seed", type=int, default=20261128, help="deterministic seed")

    run = subparsers.add_parser(
        "pipeline",
        aliases=["run"],
        help="run normalization, synthesis, validation and export",
    )
    run.add_argument("version_dirs", nargs="*", help="three project version directories")
    run.add_argument(
        "--versions",
        "--version-dirs",
        dest="versions",
        nargs="+",
        default=None,
        help="three project version directories",
    )
    run.add_argument(
        "--version-dir",
        action="append",
        default=None,
        help="project version directory; repeat three times",
    )
    run.add_argument(
        "--samples-dir",
        default=None,
        help="generated sample root (reads manifest.json for version directories)",
    )
    run.add_argument(
        "--fps",
        "--frame-rate",
        "--target-fps",
        dest="fps",
        type=float,
        default=30.0,
        help="target frame rate",
    )
    run.add_argument(
        "--threshold",
        "--max-deviation",
        dest="threshold",
        type=float,
        default=0.05,
        help="maximum permitted per-frame channel/sync deviation",
    )
    run.add_argument(
        "--output",
        "-o",
        "--out",
        "--out-dir",
        dest="output",
        default="outputs/face_animation",
        help="output directory",
    )
    run.add_argument(
        "--ground-truth",
        default=None,
        help="optional independent ground-truth JSON path",
    )

    demo = subparsers.add_parser(
        "demo", help="generate fixtures and immediately run the pipeline"
    )
    demo.add_argument("--samples-dir", default="samples", help="fixture output directory")
    demo.add_argument("--seed", type=int, default=20261128, help="deterministic seed")
    demo.add_argument("--fps", type=float, default=30.0, help="target frame rate")
    demo.add_argument("--threshold", type=float, default=0.05, help="deviation threshold")
    demo.add_argument(
        "--output",
        "-o",
        default="outputs/face_animation",
        help="animation output directory",
    )
    return parser


def _resolve_run_dirs(args: argparse.Namespace) -> list[str]:
    candidates: list[str] = []
    if args.versions:
        candidates.extend(args.versions)
    if args.version_dir:
        candidates.extend(args.version_dir)
    if args.version_dirs:
        candidates.extend(args.version_dirs)
    if args.samples_dir:
        if candidates:
            raise PipelineError(
                "E_ARGUMENT_CONFLICT",
                "use either --samples-dir or explicit version directories, not both",
            )
        candidates = [str(path) for path in version_dirs_from_samples(args.samples_dir)]
    seen: set[str] = set()
    unique: list[str] = []
    for item in candidates:
        resolved = str(Path(item).expanduser().resolve())
        if resolved not in seen:
            unique.append(resolved)
            seen.add(resolved)
    return unique


def _print_summary(summary: dict) -> None:
    print(
        json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True),
        flush=True,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    raw_args = list(sys.argv[1:] if argv is None else argv)
    if raw_args and raw_args[0] not in {
        "generate",
        "pipeline",
        "run",
        "demo",
        "-h",
        "--help",
    }:
        raw_args.insert(0, "pipeline")
    if not raw_args:
        parser.print_help()
        return 0
    args = parser.parse_args(raw_args)
    if args.command is None:
        parser.print_help()
        return 0
    try:
        if args.command == "generate":
            manifest = generate_samples(args.output, seed=args.seed)
            _print_summary(manifest)
            return 0
        if args.command == "demo":
            manifest = generate_samples(args.samples_dir, seed=args.seed)
            summary = run_pipeline(
                list(manifest["version_paths"].values()),
                fps=args.fps,
                threshold=args.threshold,
                output_dir=args.output,
                ground_truth_path=Path(args.samples_dir) / "ground_truth.json",
            )
            _print_summary(summary)
            return 0
        version_dirs = _resolve_run_dirs(args)
        if not version_dirs:
            parser.error(
                "provide three version directories, --version-dir three times, "
                "or --samples-dir"
            )
        summary = run_pipeline(
            version_dirs,
            fps=args.fps,
            threshold=args.threshold,
            output_dir=args.output,
            ground_truth_path=args.ground_truth,
        )
        _print_summary(summary)
        return 0
    except PipelineError as exc:
        output = getattr(args, "output", None)
        if output:
            try:
                write_json(Path(output).expanduser().resolve() / "error.json", exc.to_dict())
            except Exception:
                pass
        print(json.dumps(exc.to_dict(), ensure_ascii=False, sort_keys=True), file=sys.stderr)
        return 2
    except Exception as exc:  # pragma: no cover - defensive final boundary
        error = PipelineError(
            code="E_INTERNAL",
            message=str(exc),
            details={"exception_type": type(exc).__name__},
        )
        output = getattr(args, "output", None)
        if output:
            try:
                write_json(Path(output).expanduser().resolve() / "error.json", error.to_dict())
            except Exception:
                pass
        print(json.dumps(error.to_dict(), ensure_ascii=False, sort_keys=True), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
