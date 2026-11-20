from __future__ import annotations

import pytest

from dialog_emotion.synth import generate_dataset


@pytest.fixture(scope="session")
def sample_dataset(tmp_path_factory: pytest.TempPathFactory):
    root = tmp_path_factory.mktemp("deterministic_data")
    manifest = generate_dataset(root, force=True)
    return root, manifest
