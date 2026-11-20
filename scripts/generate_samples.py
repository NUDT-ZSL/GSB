#!/usr/bin/env python3
"""Generate the complete deterministic sample corpus."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from voice_emotion import build_demo_dataset  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default=str(ROOT / "samples"), help="output directory")
    parser.add_argument("--seed", type=int, default=20261121)
    parser.add_argument("--dialogs", type=int, default=3)
    parser.add_argument("--duration-ms", type=int, default=2_400)
    parser.add_argument("--frame-ms", type=float, default=20.0)
    parser.add_argument("--frame-shift-ms", type=float, default=20.0)
    parser.add_argument("--enrollment-ms", type=int, default=900)
    args = parser.parse_args()
    dataset = build_demo_dataset(
        output_dir=args.out,
        seed=args.seed,
        dialog_count=args.dialogs,
        duration_ms=args.duration_ms,
        frame_ms=args.frame_ms,
        frame_shift_ms=args.frame_shift_ms,
        enrollment_duration_ms=args.enrollment_ms,
    )
    summary = {
        "output": str(Path(args.out).resolve()),
        "manifest": str(dataset.manifest_path),
        "enrollment_manifest": str(dataset.enrollment_path),
        "dialog_count": len(dataset.dialogs),
        "enrollment_count": len(dataset.enrollments),
        "enrollment_version": dataset.enrollment_version,
        "seed": args.seed,
        "frame_ms": args.frame_ms,
        "frame_shift_ms": args.frame_shift_ms,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
