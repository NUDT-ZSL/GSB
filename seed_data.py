#!/usr/bin/env python3
"""Compatibility entry point; the fixed generator lives in ``seed_samples.py``."""

from seed_samples import generate, main


__all__ = ["generate", "main"]


if __name__ == "__main__":
    raise SystemExit(main())
