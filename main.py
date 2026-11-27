#!/usr/bin/env python3
"""Convenience entry point: ``python3 main.py demo`` or ``python3 main.py run``."""

from __future__ import annotations

import sys

from flight_compile_cache.cli import main


if __name__ == "__main__":
    sys.exit(main())
