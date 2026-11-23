"""Verify Windows-compatible spawn start method for the local process pool.

On Linux/macOS this forces the same "spawn" semantics Windows uses. The
if __name__ == '__main__' guard below is mandatory for spawn to re-import the
driver without recursively starting child processes.
"""

from __future__ import annotations

import multiprocessing as mp
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main() -> int:
    from recall_fusion.sample_data import load_dataset
    from recall_fusion.config import FusionConfig
    from recall_fusion.worker import process_batch

    docs, queries = load_dataset(
        Path(__file__).resolve().parents[1] / "data"
    )
    config = FusionConfig.default().to_dict()
    assignments = [
        {
            "query": query.__dict__.copy(),
            "caps": {name: 10 for name in
                     ("inverted", "prefix", "category", "vector")},
        }
        for query in sorted(queries, key=lambda item: item.query_id)[:4]
    ]
    ctx = mp.get_context("spawn")
    with ctx.Pool(2) as pool:
        payload = pool.apply(process_batch, (docs, config, 0, assignments))
    print(
        "spawn pool ok:",
        f"results={len(payload['results'])}",
        f"errors={len(payload['errors'])}",
        f"first={payload['results'][0]['query_id']}",
    )
    return 0 if payload["results"] and not payload["errors"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
