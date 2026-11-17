from __future__ import annotations

import json
from pathlib import Path

import pytest

from klp_audit.generator import generate_samples


@pytest.fixture(scope="session")
def generated_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("klp-generated")
    generate_samples(root)
    return root


@pytest.fixture(scope="session")
def manifest(generated_root: Path) -> dict[str, object]:
    return json.loads((generated_root / "truth" / "manifest.json").read_text(encoding="utf-8"))

