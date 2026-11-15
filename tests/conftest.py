from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from medvol import Camera, Volume  # noqa: E402


@pytest.fixture
def small_volume() -> Volume:
    size = 32
    z, y, x = np.mgrid[0:size, 0:size, 0:size]
    distance = np.sqrt((x - 15.5) ** 2 + (y - 15.5) ** 2 + (z - 15.5) ** 2)
    data = np.zeros((size, size, size), dtype=np.uint16)
    data[distance < 11.0] = 2200
    data[(np.abs(x - 22) < 3) & (np.abs(y - 16) < 3)] = 3600
    return Volume(data)


@pytest.fixture
def small_camera(small_volume: Volume) -> Camera:
    return Camera.look_at(
        small_volume.center + np.asarray((40.0, 28.0, 40.0)),
        small_volume.center,
        fov_y_deg=38.0,
    )

