#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCENE = "samples/scene.json"


def _run(arguments: list[str], *, cwd: Path = PROJECT_ROOT) -> int:
    completed = subprocess.run(
        [sys.executable, *arguments],
        cwd=cwd,
        env={**os.environ, "PYTHONPATH": str(PROJECT_ROOT)},
    )
    return completed.returncode


def _required(arguments: list[str], *, cwd: Path = PROJECT_ROOT) -> None:
    code = _run(arguments, cwd=cwd)
    if code != 0:
        raise SystemExit(code)


def _baseline() -> None:
    _required(["tools/reset_sample_outputs.py"])
    _required(["tools/generate_samples.py"])
    _required(
        ["-m", "glyph_raster", "render", "--scene", SCENE, "--base"]
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Local sample workflows for the glyph rasterizer"
    )
    parser.add_argument(
        "command",
        choices=[
            "sample",
            "reset",
            "baseline",
            "incremental",
            "resume",
            "resume-base",
            "verify",
            "verify-base",
            "test",
            "demo",
        ],
    )
    args = parser.parse_args()

    if args.command == "sample":
        return _run(["tools/generate_samples.py"])
    if args.command == "reset":
        return _run(["tools/reset_sample_outputs.py"])
    if args.command == "baseline":
        _baseline()
        return 0
    if args.command == "incremental":
        _baseline()
        return _run(["-m", "glyph_raster", "render", "--scene", SCENE])
    if args.command == "resume":
        return _run(
            ["-m", "glyph_raster", "resume", "--scene", SCENE, "--apply-edits"]
        )
    if args.command == "resume-base":
        return _run(["-m", "glyph_raster", "resume", "--scene", SCENE, "--base"])
    if args.command == "verify":
        return _run(
            ["-m", "glyph_raster", "verify", "--scene", SCENE, "--full-check"]
        )
    if args.command == "verify-base":
        return _run(
            [
                "-m",
                "glyph_raster",
                "verify",
                "--scene",
                SCENE,
                "--base",
                "--full-check",
            ]
        )
    if args.command == "test":
        return _run(["-m", "unittest", "discover", "-s", "tests", "-v"])
    if args.command == "demo":
        _baseline()
        code = _run(["-m", "glyph_raster", "render", "--scene", SCENE])
        if code != 0:
            return code
        return _run(
            ["-m", "glyph_raster", "verify", "--scene", SCENE, "--full-check"]
        )
    raise AssertionError(args.command)


if __name__ == "__main__":
    raise SystemExit(main())
