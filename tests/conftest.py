from __future__ import annotations

from pathlib import Path

import pytest

from voice_emotion import build_demo_dataset


@pytest.fixture()
def demo_dataset(tmp_path: Path):
    return build_demo_dataset(
        tmp_path / "samples",
        seed=20261121,
        dialog_count=2,
        duration_ms=2_000,
        frame_ms=20.0,
        frame_shift_ms=20.0,
    )
