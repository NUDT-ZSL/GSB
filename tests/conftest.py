from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sensitive_flow.samplegen import (  # noqa: E402
    build_cycle_snapshots_payload,
    build_edits_payload,
    build_invalid_snapshots_payload,
    build_snapshots_payload,
    build_truth_payload,
)


@pytest.fixture(scope="session")
def root() -> Path:
    return ROOT


@pytest.fixture(scope="session")
def samples() -> dict[str, dict]:
    return {
        "snapshots": build_snapshots_payload(),
        "edits": build_edits_payload(),
        "truth": build_truth_payload(),
        "invalid_snapshots": build_invalid_snapshots_payload(),
        "cycle_snapshots": build_cycle_snapshots_payload(),
    }


@pytest.fixture
def workspace(tmp_path: Path):
    from sensitive_flow.storage import Workspace

    return Workspace(tmp_path / "workspace").ensure()


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
