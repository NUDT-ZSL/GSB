#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from stream_reopt.errors import EngineError
from stream_reopt.generator import DeterministicGenerator
from stream_reopt.sample import load_sample


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate deterministic local streaming batches")
    parser.add_argument("--sample", required=True)
    parser.add_argument("--out", default=None)
    parser.add_argument("--summary-only", action="store_true")
    parser.add_argument(
        "--arrays-dir",
        default=None,
        help="optionally write each batch as one .npy file per column",
    )
    args = parser.parse_args()
    try:
        sample = load_sample(args.sample)
        generator = DeterministicGenerator(sample)
        manifest = generator.manifest()
        if args.arrays_dir:
            arrays_dir = Path(args.arrays_dir)
            arrays_dir.mkdir(parents=True, exist_ok=True)
            for spec in generator.batch_specs:
                batch = generator.batch(spec)
                for column_name in sorted(batch.columns):
                    output_path = arrays_dir / (
                        f"arrival_{batch.arrival:06d}.{batch.table}.{column_name}.npy"
                    )
                    array = batch.columns[column_name]
                    if array.dtype.kind == "O":
                        array = array.astype(str)
                    np.save(output_path, array, allow_pickle=False)
        if args.summary_only:
            manifest = {
                "sample_id": manifest["sample_id"],
                "global_seed": manifest["global_seed"],
                "manifest_sha256": manifest["manifest_sha256"],
                "batches": [
                    {
                        "arrival": batch["arrival"],
                        "table": batch["table"],
                        "row_count": batch["row_count"],
                        "digest": batch["digest"],
                    }
                    for batch in manifest["batches"]
                ],
            }
        encoded = json.dumps(manifest, sort_keys=True, ensure_ascii=False, indent=2) + "\n"
        if args.out:
            output = Path(args.out)
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(encoded, encoding="utf-8")
        print(encoded, end="")
        return 0
    except EngineError as error:
        print(json.dumps(error.to_dict(), sort_keys=True, ensure_ascii=False, indent=2))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
