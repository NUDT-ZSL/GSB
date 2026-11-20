import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from opchain.config import Config
from opchain.samplegen import VALID_CASES
from opchain.task import parse_spec


@pytest.fixture
def specs():
    return [parse_spec(record) for record in VALID_CASES]


@pytest.fixture
def config():
    return Config()
