#!/usr/bin/env python3
"""Executable wrapper for the local CI/CD artifact cache."""

from pipeline_cache.cli import main


if __name__ == "__main__":
    raise SystemExit(main())
