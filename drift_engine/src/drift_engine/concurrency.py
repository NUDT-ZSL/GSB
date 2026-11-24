"""Optional concurrent snapshot parsing with deterministic result ordering."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from .parsers import SnapshotResult, parse_v1, parse_v2
from .spec import BaselineSpec


def parse_snapshots(v1_paths: list[str], v2_targets: list[tuple[str, str]],
                    spec: BaselineSpec, workers: int = 1) -> list[SnapshotResult]:
    """Parse every snapshot, returning results in canonical input order.

    With ``workers > 1`` parsing runs in a thread pool, but results are always
    reordered by the sorted input descriptor, so conclusions are identical to
    serial execution.
    """
    jobs: list[tuple] = []
    for path in sorted(v1_paths):
        jobs.append(("v1", path, None, None))
    for shard_dir, sqlite_path in sorted(v2_targets):
        jobs.append(("v2", None, shard_dir, sqlite_path))

    def run(job: tuple) -> SnapshotResult:
        kind, path, shard_dir, sqlite_path = job
        if kind == "v1":
            return parse_v1(path, spec)
        return parse_v2(shard_dir, sqlite_path, spec)

    if workers <= 1:
        return [run(job) for job in jobs]
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(run, jobs))
