"""Small service-level integration example."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from multipart_store import MultipartStore, load_manifest


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", default=".example-store")
    parser.add_argument(
        "--manifest",
        default=str(Path(__file__).resolve().parents[1] / "samples" / "primary.json"),
    )
    args = parser.parse_args()

    store = MultipartStore(args.data_dir)
    loaded = store.load(load_manifest(args.manifest))
    round_result = store.upload_round()
    completion = store.complete("sess-main")
    verification = store.verify("sess-main")
    print(
        json.dumps(
            {
                "loaded": loaded["loaded"],
                "round": {
                    "succeeded": round_result["succeeded_count"],
                    "failed": round_result["failed_count"],
                },
                "completion": {
                    "status": completion["status"],
                    "visible_version": completion["visible_version"],
                    "final_digest": completion["final_digest"],
                },
                "verification": {
                    "valid": verification["valid"],
                    "issues": verification["issues"],
                },
            },
            sort_keys=True,
            indent=2,
        )
    )
    return 0 if verification["valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
