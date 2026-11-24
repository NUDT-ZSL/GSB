#!/usr/bin/env python3
"""Repository-root wrapper for ``python main.py ...``."""

from offline_authz.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
