import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from castdiag.generator import generate  # noqa: E402


@pytest.fixture()
def sample_dir(tmp_path):
    out = tmp_path / "data"
    generate(out)
    return out


@pytest.fixture()
def clean_dir(tmp_path):
    return tmp_path


@pytest.fixture(autouse=True)
def _no_leftover(tmp_path):
    yield
    shutil.rmtree(tmp_path, ignore_errors=True)
