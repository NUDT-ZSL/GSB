from __future__ import annotations

import json
from pathlib import Path

import pytest

from osl_forensics.generator import generate_all


@pytest.fixture(scope="session")
def generated_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("osl-samples")
    generate_all(root)
    return root


def load_marker(root: Path, case_id: str) -> dict:
    marker_path = root / "injections" / case_id / "sync_marker.json"
    return json.loads(marker_path.read_text(encoding="utf-8"))


def sample_bytes(root: Path, case_id: str) -> bytes:
    marker = load_marker(root, case_id)
    return (root / "injections" / case_id / marker["sample_file"]).read_bytes()
