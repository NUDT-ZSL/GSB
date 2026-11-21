from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from certlab.clock import LogicalClock
from certlab.snapshot import SnapshotLoader, merge_snapshots
from certlab.store import StateStore
from tools.generate_samples import BASE_MS, generate

ROOT = Path(__file__).resolve().parents[1]
CLOCK = LogicalClock(BASE_MS)


@pytest.fixture(scope="session")
def sample_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("corpus") / "sample"
    generate(path)
    return path


@pytest.fixture()
def snapshots(sample_dir: Path):
    return SnapshotLoader.load(
        v1_path=sample_dir / "trust-v1.json",
        v2_path=sample_dir / "trust-v2",
        clock=CLOCK,
    )


@pytest.fixture()
def merged_view(snapshots):
    return merge_snapshots(snapshots, CLOCK)


@pytest.fixture()
def store(tmp_path: Path, merged_view, sample_dir: Path) -> StateStore:
    instance = StateStore(tmp_path / "state.sqlite")
    instance.initialize(merged_view, sample_dir=sample_dir, clock=CLOCK)
    return instance


def leaf_requests(sample_dir: Path) -> list[dict[str, str]]:
    payload = json.loads((sample_dir / "leaves" / "index.json").read_text())
    return [
        {
            "leaf_id": entry["id"],
            "hostname": entry["hostname"],
            "purpose": entry["purpose"],
        }
        for entry in payload["leaves"]
    ]


def run_cli(
    arguments: list[str],
    *,
    env: dict[str, str] | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    merged_env = dict(os.environ)
    merged_env["PYTHONPATH"] = str(ROOT)
    if env:
        merged_env.update(env)
    completed = subprocess.run(
        [sys.executable, "-m", "certlab", *arguments],
        cwd=ROOT,
        env=merged_env,
        text=True,
        capture_output=True,
        check=False,
    )
    if check and completed.returncode != 0:
        raise AssertionError(
            f"CLI failed ({completed.returncode}): {completed.stdout} {completed.stderr}"
        )
    return completed

