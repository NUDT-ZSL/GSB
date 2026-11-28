#!/usr/bin/env python3
"""Generate the deterministic archive-audio assessment corpus."""

from __future__ import annotations

import argparse
import json

from audio_qa.generator import generate_dataset


def main() -> int:
    parser = argparse.ArgumentParser(description="生成固定种子的单声道 WAV、受损区间与噪声底真值清单")
    parser.add_argument("--out", default="data", help="输出目录（manifest.json 与 WAV 同目录）")
    parser.add_argument("--seed", type=int, default=20261128, help="固定随机种子")
    args = parser.parse_args()
    manifest = generate_dataset(args.out, seed=args.seed)
    print(
        json.dumps(
            {
                "out": args.out,
                "seed": manifest["seed"],
                "recordings": len(manifest["recordings"]),
                "segments": sum(len(rec["segments"]) for rec in manifest["recordings"]),
                "duration_seconds": manifest["total_duration_seconds"],
                "truth_frame_length": manifest["truth_frame_length"],
                "truth_hop": manifest["truth_hop"],
                "manifest": f"{args.out}/manifest.json",
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
