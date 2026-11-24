"""Command line entry point with JSON error documents and exit codes."""
from __future__ import annotations

import argparse
import json
import sys

from . import EXIT_DIGEST, EXIT_INTERRUPT, EXIT_OK, EXIT_SCHEMA, EXIT_SEAM
from .errors import DungeonError
from .pipeline import run_pipeline
from .validation import load_level_and_task


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Offline deterministic dungeon tile renderer and validator"
    )
    parser.add_argument("level_json", help="path to the level description JSON")
    parser.add_argument("task_json", help="path to the render task JSON")
    args = parser.parse_args(argv)

    try:
        level, task = load_level_and_task(args.level_json, args.task_json)
        result = run_pipeline(level, task)
    except DungeonError as exc:
        print(json.dumps(exc.to_dict(), ensure_ascii=False, indent=2), file=sys.stderr)
        return {
            "E_SCHEMA": EXIT_SCHEMA,
            "E_DIGEST": EXIT_DIGEST,
            "E_SEAM": EXIT_SEAM,
        }[exc.code]

    print(json.dumps(result, ensure_ascii=False, indent=2))
    if result.get("interrupted"):
        return EXIT_INTERRUPT
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
