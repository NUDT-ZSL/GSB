from __future__ import annotations

from pathlib import Path

from app.db import connect
from app.generator import generate_dataset
from tests.conftest import database_fingerprint


def test_fixed_seed_generation_is_reproducible(tmp_path: Path) -> None:
    first = tmp_path / "first.db"
    second = tmp_path / "second.db"
    summary = generate_dataset(first, write_artifacts=False)
    generate_dataset(second, write_artifacts=False)

    assert summary["counts"] == {
        "athletes": 8,
        "training_clips": 192,
        "ranking_requests": 16,
        "request_candidates": 192,
    }
    assert database_fingerprint(first) == database_fingerprint(second)


def test_request_candidate_links_are_complete(generated_db: Path) -> None:
    with connect(generated_db) as connection:
        rows = connection.execute(
            """
            SELECT rr.request_id, COUNT(rc.clip_id) AS candidate_count
            FROM ranking_requests rr
            LEFT JOIN request_candidates rc ON rc.request_id = rr.request_id
            GROUP BY rr.request_id
            """
        ).fetchall()
    assert len(rows) == 16
    assert all(row["candidate_count"] == 12 for row in rows)
