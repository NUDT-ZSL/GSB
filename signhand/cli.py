"""Command-line entry points for local demo and version retrieval."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .pipeline import run_pipeline
from .store import VersionedArtifactStore, read_json


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="signhand", description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    run = subparsers.add_parser("run", help="render and verify one sign-language project")
    run.add_argument("--project", default="examples/generated/sign_actions.json")
    run.add_argument("--key-moments", default="examples/generated/key_moments.json")
    run.add_argument("--output-dir", default="output/demo")
    run.add_argument("--version", default=None)
    run.add_argument("--no-repeat-check", action="store_true")
    run.add_argument("--quiet", action="store_true")

    versions = subparsers.add_parser("versions", help="list persisted project versions")
    versions.add_argument("--output-dir", default="output/demo")

    inspect = subparsers.add_parser("inspect", help="print a persisted version manifest")
    inspect.add_argument("--output-dir", default="output/demo")
    inspect.add_argument("--version", default=None)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.command == "run":
        progress = None if args.quiet else (lambda message: print(message, file=sys.stderr))
        result = run_pipeline(
            project_path=args.project,
            key_moments_path=args.key_moments,
            output_dir=args.output_dir,
            version=args.version,
            repeat_check=not args.no_repeat_check,
            progress=progress,
        )
        print(
            json.dumps(
                {
                    **result.report,
                    "version_directory": str(result.version_dir),
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        )
        return 0 if result.report["run_status"] == "COMPLETE" else 2
    if args.command == "versions":
        store = VersionedArtifactStore(args.output_dir)
        print(
            json.dumps(
                {"versions": store.list_versions()},
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    if args.command == "inspect":
        store = VersionedArtifactStore(args.output_dir)
        path = store.resolve(args.version)
        print(
            json.dumps(
                read_json(path / "artifact_manifest.json"),
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    parser.error("unknown command")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

