"""Command-line interface."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .errors import EngineError, error
from .interrupts import CancellationToken, SignalGuard
from .pipeline import run_render
from .util import version_id_for
from .validation import load_scene


def _print_json(payload: Any, *, pretty: bool) -> None:
    print(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            indent=2 if pretty else None,
            separators=None if pretty else (",", ":"),
        )
    )


def _validate_payload(scene) -> dict[str, Any]:
    return {
        "ok": True,
        "scene": scene.name,
        "source": str(scene.source_path),
        "seed": scene.seed,
        "version_id": version_id_for(scene),
        "bounds": {"min": list(scene.bounds_min), "max": list(scene.bounds_max)},
        "media": [
            {
                "id": medium.id,
                "kind": medium.kind,
                "minimum": list(medium.minimum),
                "maximum": list(medium.maximum),
                "extinction": list(medium.extinction),
                "albedo": list(medium.albedo),
                "ior": medium.ior,
            }
            for medium in scene.media
        ],
        "lights": [
            {"id": light.id, "position": list(light.position), "intensity": light.intensity}
            for light in scene.lights
        ],
        "render": {
            "width": scene.render.width,
            "height": scene.render.height,
            "samples_per_pixel": scene.render.samples_per_pixel,
            "step_size": scene.render.step_size,
            "max_steps": scene.render.max_steps,
            "transmittance_cutoff": scene.render.transmittance_cutoff,
            "fixed_scale": scene.render.fixed_scale,
            "transmittance_tolerance": scene.render.transmittance_tolerance,
            "tile": {
                "width": scene.render.tile_width,
                "height": scene.render.tile_height,
            },
            "max_workers": scene.render.max_workers,
            "inflight_memory_limit_bytes": scene.render.inflight_memory_limit_bytes,
            "state_dir": str(scene.render.state_dir),
            "output_dir": str(scene.render.output_dir),
        },
        "jitter": {
            "sample_offsets": [list(pair) for pair in scene.jitter.sample_offsets],
            "step_scales": list(scene.jitter.step_scales),
            "cutoff_scales": list(scene.jitter.cutoff_scales),
        },
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m volumetric_engine",
        description="Deterministic local volumetric offscreen renderer",
    )
    parser.add_argument("--pretty", action="store_true", help="pretty-print JSON output")
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate_parser = subparsers.add_parser("validate", help="validate a scene without touching output")
    validate_parser.add_argument("--scene", required=True, type=Path)
    validate_parser.add_argument("--pretty", action="store_true", default=argparse.SUPPRESS)

    render_parser = subparsers.add_parser("render", help="render a scene")
    render_parser.add_argument("--scene", required=True, type=Path)
    render_parser.add_argument("--pretty", action="store_true", default=argparse.SUPPRESS)
    render_parser.add_argument(
        "--no-stability",
        action="store_true",
        help="skip step/cutoff stability re-renders",
    )
    render_parser.add_argument(
        "--no-trace",
        action="store_true",
        help="skip per-sample JSONL trace (not recommended; report still has first-exceed details)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        scene = load_scene(args.scene)
        if args.command == "validate":
            _print_json(_validate_payload(scene), pretty=args.pretty)
            return 0
        token = CancellationToken()
        with SignalGuard(token):
            result = run_render(
                scene,
                token=token,
                run_stability=not args.no_stability,
                collect_trace=not args.no_trace,
            )
        _print_json(result.payload, pretty=args.pretty)
        return result.exit_code
    except EngineError as exc:
        _print_json(exc.as_dict(), pretty=args.pretty)
        return exc.exit_code
    except KeyboardInterrupt:
        _print_json(
            {"ok": False, "code": "E_INTERRUPTED", "message": "keyboard interrupt"},
            pretty=args.pretty,
        )
        return 130
    except (OSError, RuntimeError, ValueError) as exc:
        failure = error("E_INTERNAL", f"{type(exc).__name__}: {exc}")
        _print_json(failure.as_dict(), pretty=args.pretty)
        return failure.exit_code
    except Exception as exc:  # keep the CLI contract even for unexpected bugs
        failure = error("E_INTERNAL", f"{type(exc).__name__}: {exc}")
        _print_json(failure.as_dict(), pretty=args.pretty)
        return failure.exit_code


if __name__ == "__main__":
    sys.exit(main())

