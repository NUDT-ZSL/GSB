"""Multi-process concurrency consistency check.

Two scheduler processes open the SAME SQLite database and cache directory but
are driven against different datasets.  SQLite ``IMMEDIATE`` transactions plus
the advisory file lock must guarantee each (dataset, shard, algebra) is
generated and committed exactly once, and batch records stay unique.

Run directly with ``python tests/test_multiprocess.py`` (safe on Windows because
the entry point is module-protected).
"""

from __future__ import annotations

import multiprocessing as mp
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datapipe.loader import load_scenario  # noqa: E402
from datapipe.pipeline import Pipeline  # noqa: E402


def worker(input_dir: str, work_dir: str, allowed_datasets: list, queue: "mp.Queue") -> None:
    try:
        scenario = load_scenario(input_dir)
        # Restrict this process to a subset of datasets by rewriting the job.
        object.__setattr__(
            scenario.job,
            "datasets",
            tuple(name for name in scenario.job.datasets if name in allowed_datasets),
        )
        pipeline = Pipeline(work_dir, scenario)
        try:
            result = pipeline.run(assemble=False)
            queue.put(
                {
                    "ok": True,
                    "datasets": allowed_datasets,
                    "committed": pipeline.status()["committed_shards"],
                    "rounds": [report.counts() for report in result.round_reports],
                }
            )
        finally:
            pipeline.close()
    except Exception as exc:  # pragma: no cover - reported back to parent
        queue.put({"ok": False, "datasets": allowed_datasets, "error": repr(exc)})


def main() -> int:
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    input_dir = os.path.join(root, "samples", "baseline")
    work_dir = os.path.join(root, "run", "multiprocess")
    import shutil

    shutil.rmtree(work_dir, ignore_errors=True)

    # Shared warm-up materialises the cache view once before parallel scheduling.
    warm_scenario = load_scenario(input_dir)
    warm_pipeline = Pipeline(work_dir, warm_scenario)
    try:
        warm_pipeline.warm_cache_from_view()
    finally:
        warm_pipeline.close()

    ctx = mp.get_context("spawn")
    queue: "mp.Queue" = ctx.Queue()
    procs = [
        ctx.Process(target=worker, args=(input_dir, work_dir, ["images"], queue)),
        ctx.Process(target=worker, args=(input_dir, work_dir, ["text"], queue)),
    ]
    for proc in procs:
        proc.start()
    for proc in procs:
        proc.join()

    outcomes = [queue.get() for _ in procs]
    for outcome in outcomes:
        print(outcome)
        if not outcome["ok"]:
            return 1

    import sqlite3

    conn = sqlite3.connect(os.path.join(work_dir, "pipeline.sqlite3"))
    duplicate_shards = conn.execute(
        "SELECT COUNT(*) FROM (SELECT dataset, shard_index, algebra, COUNT(*) n "
        "FROM committed_shards GROUP BY 1,2,3 HAVING n > 1)"
    ).fetchone()[0]
    duplicate_ledger = conn.execute(
        "SELECT COUNT(*) FROM (SELECT round_index, dataset, shard_index, algebra, COUNT(*) n "
        "FROM memory_ledger GROUP BY 1,2,3,4 HAVING n > 1)"
    ).fetchone()[0]
    total_shards = conn.execute("SELECT COUNT(*) FROM committed_shards").fetchone()[0]
    print(
        {
            "duplicate_shards": duplicate_shards,
            "duplicate_ledger": duplicate_ledger,
            "total_shards": total_shards,
        }
    )
    expected_shards = sum(
        spec.shard_count for spec in load_scenario(input_dir).datasets
        if spec.name in ("images", "text")
    )
    return 0 if duplicate_shards == 0 and duplicate_ledger == 0 and total_shards == expected_shards else 1


if __name__ == "__main__":
    raise SystemExit(main())
