#!/usr/bin/env python3
"""Delivered fixed-seed generator entry point.

Run with:  python scripts/seed_data.py [workspace_root]
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kfdiff.seed import generate_seed_files  # noqa: E402


if __name__ == "__main__":
    root = sys.argv[1] if len(sys.argv) > 1 else "."
    info = generate_seed_files(root)
    print(json.dumps({"ok": True, **info}, indent=2, ensure_ascii=False))
