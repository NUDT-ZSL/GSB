#!/usr/bin/env python3
"""命令行入口：python3 main.py <command> ..."""
import sys

from scheduler.cli import main

if __name__ == "__main__":
    sys.exit(main())
