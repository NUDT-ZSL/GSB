"""Command line interface for generation, demo, cache and dashboard."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .app import serve
from .cache import ArtifactCache
from .config import PROJECT_ROOT
from .demo import print_demo_summary, run_demo
from .errors import BusinessError
from .formats import parse_description_file
from .generator import write_samples


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="flightfusion",
        description="Deterministic multirotor attitude fusion demo component",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    generate = subparsers.add_parser("generate-data", help="generate fixed-seed samples")
    generate.add_argument("--root", default=str(PROJECT_ROOT))
    generate.add_argument("--force", action="store_true")

    demo = subparsers.add_parser("demo", help="run the full deterministic demo")
    demo.add_argument("--data-dir")
    demo.add_argument("--cache-dir")
    demo.add_argument(
        "--output",
        default=str(PROJECT_ROOT / "artifacts" / "demo_result.json"),
    )
    demo.add_argument(
        "--inject",
        action="append",
        default=[],
        help="extra injection, e.g. parse.axis_missing@mag",
    )
    demo.add_argument("--keep-cache", action="store_true")

    dashboard = subparsers.add_parser("serve", help="start the local dashboard")
    dashboard.add_argument("--host", default="127.0.0.1")
    dashboard.add_argument("--port", type=int, default=8765)
    dashboard.add_argument("--cache-dir")
    dashboard.add_argument("--artifact")

    cache = subparsers.add_parser("cache-get", help="query a cache entry by key")
    cache.add_argument("key")
    cache.add_argument(
        "--cache-dir",
        default=str(PROJECT_ROOT / ".cache" / "flightfusion-demo"),
    )

    validate = subparsers.add_parser("validate-format", help="parse one description file")
    validate.add_argument("path")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "generate-data":
            files = write_samples(Path(args.root), force=args.force)
            for path in files:
                print(path)
            return 0
        if args.command == "demo":
            report = run_demo(
                data_dir=args.data_dir,
                cache_dir=args.cache_dir,
                artifact_path=args.output,
                extra_injections=args.inject or None,
                fresh_cache=not args.keep_cache,
            )
            print_demo_summary(report)
            print(f"report={args.output}")
            return 0
        if args.command == "serve":
            serve(
                host=args.host,
                port=args.port,
                cache_dir=args.cache_dir,
                artifact_path=args.artifact,
            )
            return 0
        if args.command == "cache-get":
            cache = ArtifactCache(args.cache_dir)
            entry = cache.query(args.key)
            if entry is None:
                print(json.dumps({"error": "E_CACHE_MISS", "key": args.key}))
                return 3
            print(json.dumps(entry, ensure_ascii=False, indent=2))
            return 0
        if args.command == "validate-format":
            version, descriptions = parse_description_file(args.path)
            print(
                json.dumps(
                    {
                        "format_version": version,
                        "sensor_count": len(descriptions),
                        "sensors": {
                            key: item.as_dict()
                            for key, item in sorted(descriptions.items())
                        },
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0
    except BusinessError as exc:
        print(json.dumps({"error": exc.to_dict()}, ensure_ascii=False, indent=2))
        return 2
    except ValueError as exc:
        print(
            json.dumps(
                {
                    "error": {
                        "code": "E_FORMAT_CORRUPT",
                        "message": str(exc),
                        "stage": "output",
                    }
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 2
    return 1


if __name__ == "__main__":
    sys.exit(main())

