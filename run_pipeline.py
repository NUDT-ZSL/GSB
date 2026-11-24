#!/usr/bin/env python3
"""Root-level convenience wrapper: ``python run_pipeline.py ...``."""

from surface_stitch.cli import main


if __name__ == "__main__":
    raise SystemExit(main(["run", *__import__("sys").argv[1:]]))
