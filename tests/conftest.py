import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pano_workbench.config import WorkbenchConfig  # noqa: E402


@pytest.fixture()
def cfg(tmp_path):
    return WorkbenchConfig(
        snapshot_dir=str(tmp_path / "snapshots"),
        cache_dir=str(tmp_path / "cache"),
    )
