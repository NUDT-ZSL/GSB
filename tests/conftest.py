from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


@pytest.fixture
def samples_dir() -> Path:
    return ROOT / "samples"


def digest(body: str) -> str:
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def etag(body: str) -> str:
    return f'"sha256:{digest(body)}"'

