from __future__ import annotations

import pytest

from georope.generator import generate_sample


@pytest.fixture(scope="session")
def sample_payload() -> dict:
    return generate_sample(42)
