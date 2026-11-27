"""Deterministic build_task database generator used by tests and demos."""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import sqlite3
from pathlib import Path
from typing import Any, Sequence

DEFAULT_SEED = 20261128
DEFAULT_ROWS = 1000
EPOCH_MS = 1_700_000_000_000
_BURST_SIZE = 7
_BURSTS_PER_SEGMENT = 3


def sample_rows(rows: int, seed: int = DEFAULT_SEED) -> list[tuple[Any, ...]]:
    if rows < 0:
        raise ValueError("rows must be non-negative")
    rng = random.Random(seed)
    statuses = [
        "SUCCESS",
        "SUCCESS",
        "SUCCESS",
        "SUCCESS",
        None,
        "FAILED",
        "TIMEOUT",
        "QUEUED",
    ]
    result: list[tuple[Any, ...]] = []
    rows_per_segment = _BURST_SIZE * _BURSTS_PER_SEGMENT
    for input_seq in range(rows):
        segment = input_seq // rows_per_segment
        within_segment = input_seq % rows_per_segment
        burst = within_segment // _BURST_SIZE
        segment_start = (
            (segment - 2) * 10_000 if segment % 3 == 2 else segment * 10_000
        )
        base_ts = EPOCH_MS + segment_start + burst * 3
        artifact_key = (
            f"制品库/{input_seq:06d}-构{chr(0x4E00 + input_seq % 73)}"
            f"-{('构建', '部署', '测试')[input_seq % 3]}-🔩"
        )
        status = statuses[rng.randrange(len(statuses))]
        result.append((input_seq + 1, base_ts, artifact_key, status))
    return result


def _reference_bytes(rows: Sequence[tuple[Any, ...]]) -> bytes:
    documents = []
    for row in rows:
        documents.append(
            json.dumps(
                list(row),
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        )
    return b"\n".join(documents)


def generate_database(
    db_path: Path,
    *,
    rows: int = DEFAULT_ROWS,
    seed: int = DEFAULT_SEED,
) -> dict[str, Any]:
    db_path = Path(db_path).expanduser().resolve()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if db_path.exists():
        db_path.unlink()
    generated_rows = sample_rows(rows, seed)
    connection = sqlite3.connect(db_path)
    try:
        connection.execute(
            """
            CREATE TABLE build_task (
                task_id INTEGER PRIMARY KEY,
                trigger_ts INTEGER NOT NULL,
                artifact_key TEXT NOT NULL,
                status TEXT
            )
            """
        )
        connection.executemany(
            """
            INSERT INTO build_task
                (task_id, trigger_ts, artifact_key, status)
            VALUES (?, ?, ?, ?)
            """,
            generated_rows,
        )
        connection.commit()
        reference_sorted = connection.execute(
            """
            SELECT task_id, trigger_ts, artifact_key, status
            FROM build_task
            ORDER BY trigger_ts ASC, task_id ASC
            """
        ).fetchall()
    finally:
        connection.close()

    reference_bytes = _reference_bytes(reference_sorted)
    return {
        "db_path": str(db_path),
        "seed": seed,
        "rows": rows,
        "reference_sha256": hashlib.sha256(reference_bytes).hexdigest(),
        "duplicate_timestamp_groups": _count_duplicate_groups(generated_rows),
        "rollback_pairs": _count_rollback_pairs(generated_rows),
    }


def _count_duplicate_groups(rows: Sequence[tuple[Any, ...]]) -> int:
    groups = 0
    previous_ts = None
    in_group = False
    for row in rows:
        ts = row[1]
        if ts == previous_ts:
            if not in_group:
                groups += 1
                in_group = True
        else:
            in_group = False
        previous_ts = ts
    return groups


def _count_rollback_pairs(rows: Sequence[tuple[Any, ...]]) -> int:
    count = 0
    for previous, current in zip(rows, rows[1:]):
        if current[1] < previous[1]:
            count += 1
    return count


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate a deterministic build_task DB")
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--rows", type=int, default=DEFAULT_ROWS)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    args = parser.parse_args(argv)
    summary = generate_database(args.db, rows=args.rows, seed=args.seed)
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
