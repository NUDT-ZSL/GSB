from __future__ import annotations

from pathlib import Path

import pytest

from slpguard.samples import generate_samples


@pytest.fixture(scope="session")
def corpus(tmp_path_factory: pytest.TempPathFactory) -> Path:
    target = tmp_path_factory.mktemp("slp-corpus")
    generate_samples(target)
    return target


def violation_codes(result) -> set[str]:
    return {
        violation.code.name
        for session in result.sessions
        for violation in session.violations
    }
