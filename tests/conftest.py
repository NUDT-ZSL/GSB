import json
import os
import shutil
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from gateway.loader import load_samples  # noqa: E402

SAMPLES = os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "samples")


@pytest.fixture()
def samples_dir():
    return SAMPLES


@pytest.fixture()
def loaded():
    return load_samples(SAMPLES)


@pytest.fixture()
def tmp_samples(tmp_path):
    """A writable copy of the samples directory for mutation tests."""
    dst = tmp_path / "samples"
    shutil.copytree(SAMPLES, dst)
    return str(dst)


def write_json(path, obj):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(obj, fh)


def read_json(path):
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)
