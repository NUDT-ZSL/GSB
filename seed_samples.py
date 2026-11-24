#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path

from formbench.generator import generate_dataset


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate deterministic local form samples")
    parser.add_argument("--root", type=Path, default=Path.cwd(), help="project root")
    args = parser.parse_args()
    task = generate_dataset(args.root)
    print(f"generated {len(task['images'])} images under {args.root / 'samples'}")
    print(f"task: {args.root / 'samples' / 'task.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
