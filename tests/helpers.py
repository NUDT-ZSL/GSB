"""Shared test helpers: isolated seeded workspaces."""
from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any, Dict, Optional

from kfdiff.engine import Studio
from kfdiff.seed import generate_seed_files


def make_studio(config_overrides: Optional[Dict[str, Any]] = None) -> tuple[Studio, Path]:
    root = Path(tempfile.mkdtemp(prefix="kfdiff_test_"))
    generate_seed_files(root)
    studio = Studio(root, config_overrides=config_overrides)
    return studio, root


def generate_all(studio: Studio) -> None:
    for clip_id in sorted(studio.tasks):
        studio.generate(clip_id, parallel=False)
