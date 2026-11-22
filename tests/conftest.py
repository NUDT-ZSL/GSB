from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from panoweave.config import load_config
from panoweave.formats import parse_text
from panoweave.samples import (
    generate_sample,
    load_sample_images,
    parameter_text,
)


@pytest.fixture(scope="session")
def sample(tmp_path_factory: pytest.TempPathFactory):
    root = tmp_path_factory.mktemp("panoweave_sample")
    generated = generate_sample(root, clean=True)
    config = load_config(root / "config.json")
    parsed = {
        version: parse_text(
            parameter_text(root, version), config.parse_defaults
        )
        for version in ("v1", "v2", "v3")
    }
    images = load_sample_images(root, generated.manifest)
    return {
        "root": root,
        "generated": generated,
        "config": config,
        "parsed": parsed,
        "images": images,
    }

