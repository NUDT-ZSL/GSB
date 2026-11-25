#!/usr/bin/env python3
"""Root wrapper for ``python cli.py <command>``."""

from proxy_diag.cli import main


if __name__ == "__main__":
    raise SystemExit(main())
