"""Command-line entry point for samples, rendering, migration, and diagnosis."""

from __future__ import annotations

import argparse
import json
import sys
import traceback
from pathlib import Path
from typing import Any, Sequence

from .cache import ContentAddressedCache
from .config import AppConfig, load_config
from .diagnostics import run_diagnostics
from .errors import BusinessError
from .formats import parse_file, write_scene
from .injections import InjectionSpec, load_injection_specs
from .migration import migrate_scene
from .outputs import write_render_outputs
from .pipeline import render_text
from .samples import (
    SampleOptions,
    generate_sample,
    load_manifest,
    load_sample_images,
)


def _print_json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False))


def _effective_config(args: argparse.Namespace, samples: str | Path | None = None) -> AppConfig:
    config_path = getattr(args, "config", None)
    if config_path:
        return load_config(config_path)
    if samples is not None:
        candidate = Path(samples) / "config.json"
        if candidate.exists():
            return load_config(candidate)
    root_default = Path("config/default.json")
    if root_default.exists():
        return load_config(root_default)
    return AppConfig()


def _cache_for(args: argparse.Namespace, config: AppConfig) -> ContentAddressedCache:
    cache_dir = getattr(args, "cache_dir", None) or config.cache_dir
    cache = ContentAddressedCache(cache_dir, config.resources.max_cache_entries)
    if getattr(args, "clean_cache", False):
        cache.clear()
    return cache


def _parse_injections(path: str | None) -> list[InjectionSpec]:
    return load_injection_specs(path) if path else []


def cmd_sample(args: argparse.Namespace) -> int:
    config = _effective_config(args)
    gains = tuple(float(item) for item in args.gains.split(",")) if args.gains else SampleOptions().gains
    options = SampleOptions(
        seed=args.seed,
        canvas_width=args.width,
        canvas_height=args.height,
        view_count=args.views,
        focal=args.focal,
        distortion_k1=args.k1,
        distortion_k2=args.k2,
        feather_radius=args.feather,
        camera_spacing=args.camera_spacing,
        gains=gains,
    )
    generated = generate_sample(
        args.out,
        options,
        config=config,
        clean=args.clean,
    )
    _print_json(
        {
            "ok": True,
            "sample_root": str(generated.root),
            "seed": options.seed,
            "view_count": options.view_count,
            "semantic_hash": generated.scene.semantic_hash,
            "manifest": str(generated.root / "manifest.json"),
            "params": {
                "v1": str(generated.root / "params_v1.txt"),
                "v2": str(generated.root / "params_v2.txt"),
                "v3": str(generated.root / "params_v3.json"),
            },
        }
    )
    return 0


def cmd_render(args: argparse.Namespace) -> int:
    config = _effective_config(args, args.samples)
    if args.parallel_tasks is not None:
        config = AppConfig(
            parse_defaults=config.parse_defaults,
            render=type(config.render)(
                interpolation=config.render.interpolation,
                weight_tolerance=config.render.weight_tolerance,
                strict_validation=config.render.strict_validation,
                require_full_coverage=config.render.require_full_coverage,
                requested_parallel_tasks=args.parallel_tasks,
            ),
            resources=config.resources,
            cache_dir=config.cache_dir,
            snapshots_dir=config.snapshots_dir,
            source_path=config.source_path,
        )
    manifest = load_manifest(args.samples)
    images = load_sample_images(args.samples, manifest)
    injections = _parse_injections(args.injections)
    cache = _cache_for(args, config)
    text = Path(args.params).read_text(encoding="utf-8")
    result = render_text(
        text,
        images,
        config=config,
        cache=cache,
        injections=injections,
    )
    artifacts = write_render_outputs(result, args.out)
    _print_json(
        {
            "ok": True,
            "output": str(Path(args.out)),
            "render": result.report(),
            "files": artifacts["files"],
        }
    )
    return 0


def cmd_migrate(args: argparse.Namespace) -> int:
    config = _effective_config(args)
    parsed = parse_file(args.input, config.parse_defaults)
    result = migrate_scene(parsed, args.to, config.parse_defaults)
    output = Path(args.out)
    write_scene(output, result.scene, args.to)
    report_path = output.with_suffix(output.suffix + ".report.json")
    report_path.write_text(
        json.dumps(result.report_dict(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    _print_json(
        {
            "ok": True,
            "input_version": parsed.format_version,
            "target_version": args.to,
            "output": str(output),
            "report": str(report_path),
            "migration": result.report_dict(),
        }
    )
    return 0


def cmd_inject(args: argparse.Namespace) -> int:
    config = _effective_config(args, args.samples)
    manifest = load_manifest(args.samples)
    images = load_sample_images(args.samples, manifest)
    spec = InjectionSpec(args.stage, args.kind, args.view)
    cache = _cache_for(args, config)
    text = Path(args.params).read_text(encoding="utf-8")
    result = render_text(
        text,
        images,
        config=config,
        cache=cache,
        injections=[spec],
    )
    artifacts = write_render_outputs(result, args.out)
    _print_json(
        {
            "ok": True,
            "injection": spec.to_dict(),
            "ledger": result.ledger.report(),
            "render": result.report(),
            "files": artifacts["files"],
        }
    )
    return 0


def cmd_diagnose(args: argparse.Namespace) -> int:
    config = _effective_config(args, args.samples)
    report = run_diagnostics(
        args.samples,
        args.out,
        config,
        clean_cache=args.clean_cache,
        raise_on_inconsistent=not args.allow_inconsistent,
    )
    _print_json(
        {
            "ok": report["consistent"],
            "consistent": report["consistent"],
            "checks": report["checks"],
            "differences": report["differences"],
            "latest_business_error_code": report["latest_business_error_code"],
            "report": str(Path(args.out) / "diagnostic_report.json"),
            "html": str(Path(args.out) / "diagnostic.html"),
        }
    )
    return 0 if report["consistent"] else 1


def cmd_demo(args: argparse.Namespace) -> int:
    config = _effective_config(args, args.samples)
    options = SampleOptions(seed=args.seed)
    generated = generate_sample(
        args.samples,
        options,
        config=config,
        clean=args.clean,
    )
    sample_config = load_config(generated.root / "config.json")
    report = run_diagnostics(
        generated.root,
        args.out,
        sample_config,
        clean_cache=True,
        raise_on_inconsistent=not args.allow_inconsistent,
    )
    validation_path = Path(args.out) / "validation_report.json"
    validation_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    _print_json(
        {
            "ok": report["consistent"],
            "consistent": report["consistent"],
            "samples": str(generated.root),
            "checks": report["checks"],
            "differences": report["differences"],
            "latest_business_error_code": report["latest_business_error_code"],
            "report": str(validation_path),
            "html": str(Path(args.out) / "diagnostic.html"),
        }
    )
    return 0 if report["consistent"] else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="panoweave",
        description="Deterministic multi-view panorama stitching and parameter interoperability",
    )
    parser.add_argument("--version", action="version", version="panoweave 1.0.0")
    subparsers = parser.add_subparsers(dest="command", required=True)

    sample = subparsers.add_parser("sample", help="generate a deterministic local sample")
    sample.add_argument("--out", default="samples")
    sample.add_argument("--config", default=None)
    sample.add_argument("--seed", type=int, default=20261123)
    sample.add_argument("--width", type=int, default=320)
    sample.add_argument("--height", type=int, default=180)
    sample.add_argument("--views", type=int, default=3)
    sample.add_argument("--focal", type=float, default=300.0)
    sample.add_argument("--k1", type=float, default=0.015)
    sample.add_argument("--k2", type=float, default=-0.003)
    sample.add_argument("--feather", type=float, default=8.0)
    sample.add_argument("--camera-spacing", type=float, default=0.28)
    sample.add_argument("--gains", default="1.06,0.94,1.0")
    sample.add_argument("--clean", action="store_true")
    sample.set_defaults(func=cmd_sample)

    render = subparsers.add_parser("render", help="render one parameter file")
    render.add_argument("--params", required=True)
    render.add_argument("--samples", required=True)
    render.add_argument("--out", required=True)
    render.add_argument("--config", default=None)
    render.add_argument("--cache-dir", default=None)
    render.add_argument("--clean-cache", action="store_true")
    render.add_argument("--parallel-tasks", type=int, default=None)
    render.add_argument("--injections", default=None)
    render.set_defaults(func=cmd_render)

    migrate = subparsers.add_parser("migrate", help="migrate one parameter file")
    migrate.add_argument("--input", required=True)
    migrate.add_argument("--to", choices=["v1", "v2", "v3"], required=True)
    migrate.add_argument("--out", required=True)
    migrate.add_argument("--config", default=None)
    migrate.set_defaults(func=cmd_migrate)

    inject = subparsers.add_parser("inject", help="run one explicit fault injection")
    inject.add_argument("--params", required=True)
    inject.add_argument("--samples", required=True)
    inject.add_argument("--out", required=True)
    inject.add_argument("--config", default=None)
    inject.add_argument("--cache-dir", default=None)
    inject.add_argument("--clean-cache", action="store_true")
    inject.add_argument("--stage", choices=["parse", "geometry", "gain", "blend"], required=True)
    inject.add_argument("--kind", required=True)
    inject.add_argument("--view", type=int, default=None)
    inject.set_defaults(func=cmd_inject)

    diagnose = subparsers.add_parser("diagnose", help="run full acceptance diagnostics")
    diagnose.add_argument("--samples", required=True)
    diagnose.add_argument("--out", default="artifacts")
    diagnose.add_argument("--config", default=None)
    diagnose.add_argument("--clean-cache", action="store_true")
    diagnose.add_argument("--allow-inconsistent", action="store_true")
    diagnose.set_defaults(func=cmd_diagnose)

    demo = subparsers.add_parser("demo", help="generate samples and run all diagnostics")
    demo.add_argument("--samples", default="samples")
    demo.add_argument("--out", default="artifacts")
    demo.add_argument("--config", default=None)
    demo.add_argument("--seed", type=int, default=20261123)
    demo.add_argument("--clean", action="store_true")
    demo.add_argument("--allow-inconsistent", action="store_true")
    demo.set_defaults(func=cmd_demo)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except BusinessError as error:
        _print_json({"ok": False, "error": error.to_dict()})
        return 2 if error.code != "E_INCONSISTENT" else 1
    except Exception as error:
        _print_json(
            {
                "ok": False,
                "error": {
                    "code": "E_RUNTIME",
                    "type": type(error).__name__,
                    "message": str(error),
                    "traceback": traceback.format_exc(limit=8),
                },
            }
        )
        return 3


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())

