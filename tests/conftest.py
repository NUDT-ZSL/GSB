from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from relief_workbench.config import Octave, WorkbenchConfig


@pytest.fixture
def small_config() -> WorkbenchConfig:
    return WorkbenchConfig(
        width=128,
        height=96,
        grid_rows=3,
        grid_cols=4,
        seed=7319,
        octaves=(
            Octave(1.0, 0.9),
            Octave(2.25, 0.45),
            Octave(5.5, 0.2),
        ),
        light_direction=(0.36, -0.48, 0.8),
        relief_strength=2.2,
        normalization_tolerance=1e-6,
        concurrency=4,
        memory_budget_bytes=4 * 1024 * 1024,
        preview_tile=(1, 2),
    )
