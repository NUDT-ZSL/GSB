#!/usr/bin/env python3
"""Convenience wrapper for ``python -m exchange_storage``."""

from exchange_storage.cli import main


if __name__ == "__main__":
    raise SystemExit(main())
