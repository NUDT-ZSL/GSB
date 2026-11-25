#!/usr/bin/env python3
"""Thin wrapper around health_machine.seed.generate_sample."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from health_machine.seed import generate_sample  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate deterministic HSM samples")
    parser.add_argument("--out", default="samples")
    args = parser.parse_args()
    print(json.dumps({"ok": True, **generate_sample(args.out)}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
