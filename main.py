#!/usr/bin/env python3
"""Convenience entry point; equivalent to ``python -m ckptmgr``."""

import sys

from ckptmgr.cli import main


if __name__ == "__main__":
    sys.exit(main())
