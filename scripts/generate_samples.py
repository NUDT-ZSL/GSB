#!/usr/bin/env python3
"""Generate deterministic local dialog PCM, truth variants, and manifests."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dialog_emotion.errors import BusinessError  # noqa: E402
from dialog_emotion.samples import generate_bundle, write_bundle  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out",
        default=str(ROOT / "data" / "samples"),
        help="output sample directory",
    )
    args = parser.parse_args()
    try:
        bundle = generate_bundle()
        summary = write_bundle(bundle, args.out)
    except BusinessError as exc:
        print(json.dumps({"status": "error", **exc.to_dict()}, ensure_ascii=False))
        return 2
    print(
        json.dumps(
            {
                "status": "ok",
                "output_dir": str(Path(args.out).resolve()),
                "python": sys.version.split()[0],
                **summary,
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
