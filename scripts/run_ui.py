#!/usr/bin/env python3
"""Launch the Tkinter diagnostic workbench (headless summary without a display)."""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ocrbench.ui import launch


def main() -> int:
    parser = argparse.ArgumentParser(description="OCR diagnostic workbench UI")
    parser.add_argument(
        "--workspace",
        default=os.path.join(os.getcwd(), "workspace_ui"),
        help="Workspace directory for data, cache and snapshots",
    )
    args = parser.parse_args()
    result = launch(args.workspace)
    if isinstance(result, str):
        print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
