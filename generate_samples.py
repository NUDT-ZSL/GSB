#!/usr/bin/env python3
"""Convenience wrapper for ``python -m coref_pipeline.generator``."""

from coref_pipeline.generator import main


if __name__ == "__main__":
    raise SystemExit(main())
