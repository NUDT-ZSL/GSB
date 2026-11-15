#!/usr/bin/env python3
"""Generate the deterministic 128^3 uint16 sphere/cylinder phantom."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from medvol.synthetic import make_synthetic_ct_volume  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--size", type=int, default=128, help="Edge length (default: 128)")
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "artifacts" / "demo" / "volume_128.npy",
    )
    args = parser.parse_args()

    volume = make_synthetic_ct_volume(args.size)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    np.save(args.output, volume.data, allow_pickle=False)
    metadata_path = args.output.with_suffix(".json")
    metadata_path.write_text(
        json.dumps(volume.metadata(), indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    vmin, vmax = volume.value_range
    print(f"wrote {args.output}")
    print(f"shape={volume.shape_zyx} dtype={volume.data.dtype} range=[{vmin}, {vmax}]")
    print(f"sha256={volume.sha256}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

