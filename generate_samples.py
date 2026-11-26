#!/usr/bin/env python3
"""Generate the deterministic PCM, truth and annotation sample bundle."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from emotion_diag.config import EngineConfig
from emotion_diag.samplegen import generate_sample_bundle


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="samples", help="sample output directory")
    parser.add_argument("--config", default=None, help="optional JSON config override")
    parser.add_argument("--seed", type=int, default=None, help="override fixed seed")
    args = parser.parse_args()

    config = EngineConfig.load(args.config)
    if args.seed is not None:
        config.values["seed"] = args.seed
    manifest = generate_sample_bundle(Path(args.out), config)
    print(json.dumps({"ok": True, "manifest": manifest}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
