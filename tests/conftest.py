from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.generator import generate_dataset


def database_fingerprint(path: Path) -> str:
    from app.db import connect

    with connect(path) as connection:
        payload = {}
        for table in (
            "athletes",
            "training_clips",
            "ranking_requests",
            "request_candidates",
        ):
            rows = connection.execute(f"SELECT * FROM {table} ORDER BY 1, 2").fetchall()
            payload[table] = [dict(row) for row in rows]
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


@pytest.fixture(scope="session")
def generated_db(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("motion-db") / "motion_ranking.db"
    generate_dataset(path, write_artifacts=False)
    return path
