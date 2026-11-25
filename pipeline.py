#!/usr/bin/env python3
"""Root-level compatibility wrapper for the pipeline CLI."""

from risk_governance.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
