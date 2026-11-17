#!/usr/bin/env python3
from __future__ import annotations

import argparse
import shutil
from pathlib import Path


def _remove(path: Path, workspace: Path) -> None:
    resolved = path.resolve(strict=False)
    resolved.relative_to(workspace)
    if resolved.exists():
        shutil.rmtree(resolved)
        print(f"removed {resolved}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Reset generated sample state and output")
    parser.add_argument("--workspace", default=".")
    parser.add_argument("--state-dir", default="state/sample")
    parser.add_argument("--output-dir", default="out")
    args = parser.parse_args()
    workspace = Path(args.workspace).resolve()
    _remove(workspace / args.state_dir, workspace)
    _remove(workspace / args.output_dir, workspace)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
