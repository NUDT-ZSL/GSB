from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, Optional

from .errors import BakerError, BakeInterrupted, SchemaError
from .heightfield import GlobalHeightField
from .pipeline import bake_scene
from .sample_generator import apply_edit_to_directory, generate_samples
from .scheduler import BoundarySignal, install_signal_handlers
from .schema import load_edit_file, load_scene_file


def print_json(value: Dict[str, Any]) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=False))


def _load_validated_scene(scene_path: str) -> tuple[Dict[str, Any], GlobalHeightField]:
    scene = load_scene_file(scene_path)
    heightfield = GlobalHeightField.from_scene(scene)
    return scene, heightfield


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="terrain_baker",
        description="Local deterministic terrain height-field baking engine",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    generate = sub.add_parser("generate-sample", help="generate deterministic local sample")
    generate.add_argument("--output", default="samples")
    generate.add_argument("--seed", type=int, default=355)

    validate = sub.add_parser("validate", help="validate scene and all height-field tiles")
    validate.add_argument("--scene", required=True)

    bake = sub.add_parser("bake", help="bake or incrementally rebake scene chunks")
    bake.add_argument("--scene", required=True)
    bake.add_argument("--output-dir", required=True)
    bake.add_argument("--state-dir", default=None)
    bake.add_argument("--report", default=None)
    bake.add_argument("--edit", default=None)
    bake.add_argument(
        "--mode",
        default="auto",
        choices=[
            "auto",
            "initial",
            "terrain_edit",
            "lighting",
            "camera",
            "ray_step",
            "full_reference",
        ],
    )
    bake.add_argument("--changed-key", action="append", default=None)
    bake.add_argument("--workers", type=int, default=None)
    bake.add_argument("--memory-budget-bytes", type=int, default=None)
    bake.add_argument("--no-reference", action="store_true")
    bake.add_argument("--stop-after-completed", type=int, default=None)
    bake.add_argument("--run-id", default=None)

    apply_edit = sub.add_parser("apply-edit", help="apply generated integer-grid sculpt edit")
    apply_edit.add_argument("--sample-dir", required=True)
    apply_edit.add_argument("--edit", default=None)
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "generate-sample":
        result = generate_samples(args.output, args.seed)
        print_json({"ok": True, **result})
        return 0

    if args.command == "apply-edit":
        sample_dir = Path(args.sample_dir)
        edit = None
        if args.edit:
            edit_path = Path(args.edit)
            edit = json.loads(edit_path.read_text(encoding="utf-8"))
        else:
            default_edit = sample_dir / "edits" / "local_ridge_valley.json"
            if default_edit.exists():
                edit = json.loads(default_edit.read_text(encoding="utf-8"))
        result = apply_edit_to_directory(sample_dir, edit)
        print_json({"ok": True, **result})
        return 0

    if args.command in {"validate", "bake"}:
        try:
            scene, heightfield = _load_validated_scene(args.scene)
            edit = None
            if args.command == "bake" and args.edit:
                edit = load_edit_file(args.edit, scene)
        except SchemaError as exc:
            print_json(exc.to_dict())
            return exc.exit_code

        if args.command == "validate":
            print_json(
                {
                    "ok": True,
                    "scene": str(Path(args.scene).resolve()),
                    "global_rows": heightfield.rows,
                    "global_cols": heightfield.cols,
                    "tiles": len(heightfield.tiles),
                    "heightfield_content_digest": heightfield.content_digest,
                }
            )
            return 0

        token = BoundarySignal()
        installed = install_signal_handlers(token)
        try:
            report = bake_scene(
                scene,
                heightfield,
                output_dir=args.output_dir,
                state_dir=args.state_dir,
                report_path=args.report,
                mode=args.mode,
                edit=edit,
                changed_keys=args.changed_key,
                max_workers_override=args.workers,
                memory_budget_override=args.memory_budget_bytes,
                run_reference=not args.no_reference,
                signal_token=token,
                stop_after_completed=args.stop_after_completed,
                run_id=args.run_id,
            )
            print_json(
                {
                    "ok": True,
                    "status": report["status"],
                    "report": str(Path(args.report or Path(args.output_dir) / scene["output"]["report"]).resolve()),
                    "counts": report["counts"],
                    "pixel_exact": report["seam_validation"].get("pixel_exact"),
                    "installed_signal_handlers": installed,
                }
            )
            return 0
        except BakeInterrupted as exc:
            print_json(exc.to_dict())
            return exc.exit_code
        except BakerError as exc:
            print_json(exc.to_dict())
            return exc.exit_code
        except Exception as exc:  # unexpected failure should still be machine-readable
            print_json(
                {
                    "ok": False,
                    "error": {
                        "code": "E_INTERNAL",
                        "message": f"{type(exc).__name__}: {exc}",
                    },
                }
            )
            return 1

    parser.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
