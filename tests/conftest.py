from __future__ import annotations

import pytest

from mtstream.models import DecodeConfig
from mtstream.sample_data import make_sample_case


@pytest.fixture()
def case():
    return make_sample_case()


@pytest.fixture()
def config():
    return DecodeConfig(block_size=4, latency_budget_ms=1000.0)

