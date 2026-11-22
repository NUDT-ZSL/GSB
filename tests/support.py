from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable

from hotgray.engine import Engine

ROOT = Path(__file__).resolve().parents[1]
SAMPLES = ROOT / "samples"


def copy_samples(destination: Path) -> Path:
    target = destination / "samples"
    shutil.copytree(SAMPLES, target)
    return target


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def mutate_json(path: Path, mutate: Callable[[Any], Any]) -> None:
    write_json(path, mutate(load_json(path)))


def new_engine(workspace: Path, samples: Path | None = None) -> Engine:
    return Engine.initialize(
        workspace=workspace,
        samples_dir=samples or SAMPLES,
        db_name="state.db",
    )


def warmed_engine(workspace: Path, samples: Path | None = None) -> Engine:
    engine = new_engine(workspace, samples)
    engine.apply_patch(at_ms=1000)
    engine.start_warmup(at_ms=1000)
    engine.tick(at_ms=1100)
    return engine


def request_bucket(request_id: str) -> float:
    return int(hashlib.sha256(request_id.encode("utf-8")).hexdigest()[:16], 16) / float(16**16)


def request_id_under(prefix: str, threshold: float) -> str:
    for index in range(10000):
        candidate = f"{prefix}-{index:04d}"
        if request_bucket(candidate) < threshold:
            return candidate
    raise AssertionError("no request id found")


def request_id_at_least(prefix: str, threshold: float) -> str:
    for index in range(10000):
        candidate = f"{prefix}-{index:04d}"
        if request_bucket(candidate) >= threshold:
            return candidate
    raise AssertionError("no request id found")


def sqlite_rows(db_path: Path, query: str, parameters: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(query, parameters).fetchall()
        return [{key: row[key] for key in row.keys()} for row in rows]
    finally:
        connection.close()


def run_cli(arguments: list[str], *, timeout: float = 30.0) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(ROOT) + os.pathsep + environment.get("PYTHONPATH", "")
    return subprocess.run(
        [sys.executable, "-m", "hotgray", *arguments],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


class TempWorkspace:
    def __init__(self) -> None:
        self._temp = tempfile.TemporaryDirectory(prefix="hotgray-test-")
        self.path = Path(self._temp.name)

    def close(self) -> None:
        self._temp.cleanup()

    def __enter__(self) -> "TempWorkspace":
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.close()
