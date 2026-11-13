"""Command-line entry point for the Markdown hidden-text scanner.

Usage::

    python scan_md.py --dir docs --config rules.json --output report.json

Exit codes
----------
0  scan finished and the report was written (findings do not change this)
1  unexpected internal error
2  bad arguments, missing config, invalid JSON or invalid rules
3  --dir does not exist or is not a directory
4  the report could not be written
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Sequence

from . import __version__
from .config import ConfigError, load_config
from .detector import FileResult, scan_directory, summarise

EXIT_OK = 0
EXIT_INTERNAL = 1
EXIT_CONFIG = 2
EXIT_DIRECTORY = 3
EXIT_OUTPUT = 4

PROG = "scan_md.py"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=PROG,
        description="Scan a directory of Markdown files for hidden / injected text.",
    )
    parser.add_argument("--dir", required=True, metavar="DIRECTORY", dest="directory",
                        help="directory to scan recursively (only *.md files)")
    parser.add_argument("--config", required=True, metavar="JSON",
                        help="JSON rule configuration file")
    parser.add_argument("--output", required=True, metavar="FILE",
                        help="path of the JSON report to write")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser


def render_report(results: Sequence[FileResult]) -> str:
    """Serialise the report as a JSON array of per-file objects.

    Each object has exactly ``file``, ``suspicious``, ``matched_rules`` and
    ``error``; the array is already ordered by path (code-point order).
    ``ensure_ascii=False`` keeps Chinese file names readable.
    """
    return json.dumps([row.to_dict() for row in results], ensure_ascii=False, indent=2) + "\n"


def write_report(output_path: Path, text: str) -> None:
    """Write through a temp file in the same directory, then atomically replace.

    A crash or a full disk never leaves a truncated report at the target path,
    so a report that exists is always a complete one.
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", newline="\n", dir=output_path.parent,
        prefix=f".{output_path.name}.", suffix=".tmp", delete=False,
    )
    temp_name = handle.name
    try:
        with handle:
            handle.write(text)
        os.replace(temp_name, output_path)
    except BaseException:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        rules = load_config(args.config)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_CONFIG

    root = Path(args.directory)
    if not root.exists():
        print(f"error: target directory does not exist: {root}", file=sys.stderr)
        return EXIT_DIRECTORY
    if not root.is_dir():
        print(f"error: target path is not a directory: {root}", file=sys.stderr)
        return EXIT_DIRECTORY

    results = scan_directory(root, rules)
    summary = summarise(results)

    output_path = Path(args.output)
    try:
        write_report(output_path, render_report(results))
    except OSError as exc:
        print(f"error: cannot write report {output_path}: {exc}", file=sys.stderr)
        return EXIT_OUTPUT
    except Exception as exc:  # defensive: never leave the user with a bare traceback
        print(f"error: unexpected failure while writing report: {exc}", file=sys.stderr)
        return EXIT_INTERNAL

    print(
        f"scanned {summary['total_files']} markdown file(s) in {root}: "
        f"{summary['suspicious_files']} suspicious, {summary['clean_files']} clean"
        + (f", {summary['files_with_errors']} unreadable" if summary["files_with_errors"] else "")
    )
    print(f"report written to {output_path}")
    return EXIT_OK


def entrypoint() -> int:
    """Wrap :func:`main` so an unexpected defect is a clear exit 1, not a traceback."""
    try:
        return main()
    except KeyboardInterrupt:
        print("error: interrupted", file=sys.stderr)
        return EXIT_INTERNAL


if __name__ == "__main__":  # pragma: no cover
    sys.exit(entrypoint())
