from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Iterable

import pytest


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from trustman.engine import SnapshotContext
from trustman.parser import parse_snapshot_files, resolve_snapshot_candidates
from trustman.samples import generate_samples


def write_json(path: Path, value: Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return path


def make_snapshot_context(
    snapshot_paths: Iterable[str | Path],
) -> SnapshotContext:
    parsed = parse_snapshot_files(snapshot_paths)
    accepted, conflicts, all_candidates = resolve_snapshot_candidates(
        parsed.items
    )
    return SnapshotContext(
        accepted=accepted,
        all_candidates=all_candidates,
        rejections=list(parsed.rejections) + list(conflicts),
    )


@pytest.fixture
def samples(tmp_path: Path) -> Path:
    output = tmp_path / "samples"
    generate_samples(output)
    return output


@pytest.fixture
def snapshot_files(samples: Path) -> list[Path]:
    return [
        samples / "snapshots" / "v1.json",
        samples / "snapshots" / "v2.json",
        samples / "snapshots" / "v3.json",
    ]
