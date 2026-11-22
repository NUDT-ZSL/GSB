#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sft_pipeline.generator import generate_dataset_files
from sft_pipeline.workspace import Workspace


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate deterministic local SFT data")
    parser.add_argument("--root", default=".", help="workspace root")
    parser.add_argument("--force", action="store_true", help="overwrite existing inputs")
    args = parser.parse_args()
    workspace = Workspace(Path(args.root))
    result = generate_dataset_files(workspace, force=args.force)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
