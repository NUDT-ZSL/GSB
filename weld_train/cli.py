"""Command-line entry points for generation, validation, and serving."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .generator import DEFAULT_SEED, generate_project
from .service import create_app


def _default_data_dir() -> Path:
    return Path(__file__).resolve().parent.parent / "data"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    generate_parser = subparsers.add_parser(
        "generate", help="generate the deterministic welding project"
    )
    generate_parser.add_argument(
        "--data-dir", type=Path, default=_default_data_dir()
    )
    generate_parser.add_argument("--seed", type=int, default=DEFAULT_SEED)

    serve_parser = subparsers.add_parser("serve", help="serve the local Flask API")
    serve_parser.add_argument(
        "--data-dir", type=Path, default=_default_data_dir()
    )
    serve_parser.add_argument("--host", default="127.0.0.1")
    serve_parser.add_argument("--port", type=int, default=8010)
    serve_parser.add_argument("--debug", action="store_true")

    validate_parser = subparsers.add_parser(
        "validate", help="print the generated validation record"
    )
    validate_parser.add_argument(
        "--data-dir", type=Path, default=_default_data_dir()
    )

    args = parser.parse_args()
    if args.command == "generate":
        result = generate_project(args.data_dir, seed=args.seed)
        print(
            json.dumps(
                {
                    "ok": True,
                    "data_dir": str(args.data_dir),
                    "segments": [
                        {
                            "segment_id": item["segment_id"],
                            "snapshot_version": item["snapshot_version"],
                        }
                        for item in result["outputs"]
                    ],
                    "validation_pass": result["validation"]["pass"],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0 if result["validation"]["pass"] else 2
    if args.command == "validate":
        path = args.data_dir / "validation" / "records.json"
        if not path.exists():
            print(f"validation record missing: {path}")
            return 2
        record = json.loads(path.read_text(encoding="utf-8"))
        print(json.dumps(record, ensure_ascii=False, indent=2))
        return 0 if record.get("pass") else 2
    if args.command == "serve":
        app = create_app(args.data_dir)
        app.run(
            host=args.host,
            port=args.port,
            debug=args.debug,
            use_reloader=args.debug,
        )
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
