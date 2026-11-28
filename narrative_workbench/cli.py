from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Sequence

from .checkpoint import CheckpointError
from .compare import compare_all, compare_normalized
from .generator import (
    DEFAULT_FRAME_COUNT,
    DEFAULT_HEIGHT,
    DEFAULT_SEED,
    DEFAULT_WIDTH,
    GeneratedProject,
    generate_project,
)
from .independent import verify_frames
from .renderer import (
    load_normalized_versions,
    render_all,
    render_version,
    write_normalized_results,
)
from .storage import read_json, write_json_atomic


def _common_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--output-dir", default="results", help="workbench output directory")
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED, help="deterministic scene seed")
    parser.add_argument(
        "--frames", type=int, default=DEFAULT_FRAME_COUNT, help="camera-track frame count"
    )
    parser.add_argument("--width", type=int, default=DEFAULT_WIDTH, help="frame width")
    parser.add_argument("--height", type=int, default=DEFAULT_HEIGHT, help="frame height")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="narrative-workbench",
        description=(
            "Deterministic transparent 3D Gaussian narrative projection, sorting, "
            "compositing, validation, schema comparison, and resumable rendering."
        ),
    )
    subparsers = parser.add_subparsers(dest="command")

    generate = subparsers.add_parser("generate", help="generate deterministic scene assets")
    _common_arguments(generate)
    generate.add_argument("--force", action="store_true", help="regenerate existing scene")

    normalize = subparsers.add_parser(
        "normalize", help="normalize v1/v2/v3 schemas and write diagnostics"
    )
    _common_arguments(normalize)
    normalize.add_argument("--force", action="store_true", help="regenerate scene first")

    render = subparsers.add_parser(
        "render", help="render/validate committed frames with resume support"
    )
    _common_arguments(render)
    render.add_argument(
        "--versions",
        nargs="+",
        choices=("v1", "v2", "v3"),
        default=("v1", "v2", "v3"),
        help="schema versions to render",
    )
    render.add_argument(
        "--max-frames",
        type=int,
        default=None,
        help="maximum new frames per version this invocation (0 renders none)",
    )
    render.add_argument(
        "--reset-version",
        action="store_true",
        help="discard committed frames for selected versions before rendering",
    )
    render.add_argument("--force", action="store_true", help="regenerate scene first")

    verify = subparsers.add_parser(
        "verify", help="run independent finite-difference projection/composite verification"
    )
    _common_arguments(verify)

    compare = subparsers.add_parser(
        "compare", help="compare normalized models and rendered frames across v1/v2/v3"
    )
    _common_arguments(compare)

    run_all = subparsers.add_parser(
        "run-all", help="generate, normalize, render, verify, and compare"
    )
    _common_arguments(run_all)
    run_all.add_argument("--force", action="store_true", help="regenerate scene first")

    status = subparsers.add_parser("status", help="show persisted checkpoint state")
    _common_arguments(status)
    return parser


def _project_from_args(args: argparse.Namespace, force: bool = False) -> GeneratedProject:
    return generate_project(
        args.output_dir,
        seed=args.seed,
        frame_count=args.frames,
        width=args.width,
        height=args.height,
        force=force,
    )


def _normalize_only(project: GeneratedProject) -> dict[str, Any]:
    _, normalized = load_normalized_versions(project)
    return write_normalized_results(project, normalized)


def _ensure_complete(project: GeneratedProject) -> None:
    incomplete: list[str] = []
    for version in ("v1", "v2", "v3"):
        path = project.root / "frames" / version / "checkpoint.json"
        if not path.exists():
            incomplete.append(f"{version}:not-started")
            continue
        state = read_json(path)
        if int(state["next_frame"]) < int(state["total_frames"]):
            incomplete.append(
                f"{version}:{state['next_frame']}/{state['total_frames']}"
            )
    if incomplete:
        raise RuntimeError(
            "cannot verify/compare before all frames are committed: "
            + ", ".join(incomplete)
        )


def command_status(args: argparse.Namespace) -> dict[str, Any]:
    root = Path(args.output_dir)
    versions: dict[str, Any] = {}
    for version in ("v1", "v2", "v3"):
        path = root / "frames" / version / "checkpoint.json"
        if path.exists():
            state = read_json(path)
            versions[version] = {
                "next_frame": int(state["next_frame"]),
                "total_frames": int(state["total_frames"]),
                "status": state["status"],
                "committed_frame_count": len(state["committed_frames"]),
                "resume_count": int(state.get("resume_count", 0)),
            }
        else:
            versions[version] = {"status": "not_started"}
    result = {
        "schema_version": "workbench-status/1",
        "output_dir": str(root.resolve()),
        "scene_exists": (root / "scene" / "manifest.json").exists(),
        "versions": versions,
        "verification_exists": (root / "verification.json").exists(),
        "comparison_exists": (root / "comparison.json").exists(),
    }
    write_json_atomic(root / "status.json", result)
    return result


def command_run_all(args: argparse.Namespace) -> dict[str, Any]:
    project = _project_from_args(args, force=args.force)
    normalization = _normalize_only(project)
    render_session = render_all(
        args.output_dir,
        seed=args.seed,
        frame_count=args.frames,
        width=args.width,
        height=args.height,
        versions=("v1", "v2", "v3"),
        max_new_frames=None,
        reset_version=args.force,
        force_generate=args.force,
    )
    verification = verify_frames(project)
    comparison = compare_all(project)
    result = {
        "schema_version": "workbench-run/1",
        "scene_fingerprint": project.fingerprint,
        "normalization": normalization,
        "render_session": render_session,
        "verification": {
            "passed": verification["passed"],
            "frame_count": verification["frame_count"],
            "failure_count": verification["failure_count"],
            "failures": verification["failures"],
            "file": str(project.root / "verification.json"),
        },
        "comparison": comparison,
        "outputs": {
            "scene": str(project.scene_dir),
            "normalized": str(project.root / "normalized"),
            "frames": str(project.root / "frames"),
            "reference_ellipses": str(project.reference_file),
            "sorting_evidence": str(project.root / "frames"),
            "comparison": str(project.root / "comparison.json"),
            "normalization_diff": str(
                project.root / "normalized" / "normalization_diff.json"
            ),
        },
    }
    write_json_atomic(project.root / "workbench_summary.json", result)
    return result


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    if not raw_argv or raw_argv[0].startswith("-"):
        raw_argv = ["run-all", *raw_argv]
    args = parser.parse_args(raw_argv)
    try:
        if args.command == "generate":
            project = _project_from_args(args, force=args.force)
            result = {
                "schema_version": "generate-result/1",
                "scene_fingerprint": project.fingerprint,
                "scene_dir": str(project.scene_dir),
                "scene_files": {
                    key: str(value) for key, value in project.scene_files.items()
                },
                "camera_file": str(project.camera_file),
                "reference_file": str(project.reference_file),
                "element_count": len(project.normalized_v1.scene.elements),
            }
        elif args.command == "normalize":
            project = _project_from_args(args, force=args.force)
            summary = _normalize_only(project)
            normalization_diff, _ = compare_normalized(project)
            result = {
                "normalization_summary": summary,
                "normalization_diff": {
                    "all_valid_elements_equivalent": normalization_diff[
                        "all_valid_elements_equivalent"
                    ],
                    "normalized_difference_count": len(
                        normalization_diff["normalized_differences"]
                    ),
                    "schema_audit_issue_summary": normalization_diff[
                        "schema_audit_issue_summary"
                    ],
                    "file": str(
                        project.root / "normalized" / "normalization_diff.json"
                    ),
                },
            }
        elif args.command == "render":
            result = render_all(
                args.output_dir,
                seed=args.seed,
                frame_count=args.frames,
                width=args.width,
                height=args.height,
                versions=tuple(args.versions),
                max_new_frames=args.max_frames,
                reset_version=args.reset_version or args.force,
                force_generate=args.force,
            )
        elif args.command == "verify":
            project = _project_from_args(args)
            _ensure_complete(project)
            result = verify_frames(project)
        elif args.command == "compare":
            project = _project_from_args(args)
            _ensure_complete(project)
            _normalize_only(project)
            result = {
                "normalization": compare_normalized(project)[0],
                "comparison": compare_all(project),
            }
        elif args.command == "status":
            result = command_status(args)
        else:
            result = command_run_all(args)
        print(json.dumps(result, indent=2, ensure_ascii=False, sort_keys=True))
        return 0
    except (CheckpointError, RuntimeError, ValueError, FileNotFoundError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
