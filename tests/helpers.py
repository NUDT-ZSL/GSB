from __future__ import annotations

import copy
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from grayroll.samples import (
    sample_defaults,
    sample_events,
    sample_v1,
    sample_v2_shards,
    write_package_snapshot,
)
from grayroll.sources import load_package
from grayroll.store import Store

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SAMPLES = PROJECT_ROOT / "samples"


def copy_samples(destination: Path) -> Path:
    target = destination / "samples"
    shutil.copytree(SAMPLES, target)
    return target


def variant_package(
    root: Path,
    *,
    package_version: int = 1,
    generation_floor: int = 2,
    shards: dict[str, dict[str, Any]] | None = None,
    v1: dict[str, Any] | None = None,
) -> Path:
    sample_root = root / "samples"
    if not sample_root.exists():
        sample_root.mkdir(parents=True)
    defaults = sample_defaults()
    events = sample_events()
    from grayroll.util import canonical_line, write_json_file

    write_json_file(sample_root / "defaults.json", defaults)
    with (sample_root / "events.jsonl").open("wb") as handle:
        for event in events:
            handle.write(canonical_line(event))
    write_package_snapshot(
        sample_root,
        package_version=package_version,
        generation_floor=generation_floor,
        v1=copy.deepcopy(v1 or sample_v1()),
        shards=copy.deepcopy(shards or sample_v2_shards()),
    )
    return sample_root


def prepare_state(sample_root: Path, state: Path) -> str:
    package = load_package(sample_root / "manifest.json", sample_root / "defaults.json")
    store = Store(state)
    try:
        result = store.load_package(package)
        store.merge_package(result["package_id"])
        return result["package_id"]
    finally:
        store.close()


def run_cli(arguments: list[str], state: Path) -> subprocess.CompletedProcess[bytes]:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(PROJECT_ROOT)
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "grayroll",
            "--state",
            str(state),
            *arguments,
        ],
        cwd=PROJECT_ROOT,
        env=env,
        capture_output=True,
        check=False,
    )


def status_json(store: Store) -> dict[str, Any]:
    return store.status()


def load_events_file(sample_root: Path) -> list[dict[str, Any]]:
    from grayroll.events import load_events

    return load_events(sample_root / "events.jsonl")


def add_extra_batch(shards: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    result = copy.deepcopy(shards)
    extra = copy.deepcopy(result["v2/shards/batch-4.json"])
    extra.update(
        {
            "absolute_rollout_ms": 1700000009000,
            "batch_id": "5",
            "depends_on": ["2"],
            "observation_markers": [0.2, 0.6],
            "rollback_window": {
                "end_ms": 1700000025000,
                "start_ms": 1700000009000,
            },
            "tiers": [0.2, 0.6, 1.0],
        }
    )
    result["v2/shards/batch-5.json"] = extra
    return result
