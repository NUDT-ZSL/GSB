#!/usr/bin/env python3
"""Generate the deterministic local WAV/v1/v2 sample corpus."""

from __future__ import annotations

import argparse
import json

from asr_editor.samples import generate_samples


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="samples")
    args = parser.parse_args()
    manifest = generate_samples(args.out)
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
