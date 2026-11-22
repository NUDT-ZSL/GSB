import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from scripts.gen_samples import generate, load_data  # noqa: E402
from tts_app.cache import ContentCache  # noqa: E402
from tts_app.config import load_config_file  # noqa: E402
from tts_app.dag import Engine  # noqa: E402

REPO = os.path.join(os.path.dirname(__file__), "..")


@pytest.fixture(scope="session")
def sample_data(tmp_path_factory):
    data_dir = tmp_path_factory.mktemp("data")
    generate(str(data_dir))
    return load_data(str(data_dir))


@pytest.fixture()
def speakers(sample_data):
    return sample_data[0]


@pytest.fixture()
def tasks(sample_data):
    return sample_data[1]


@pytest.fixture()
def cfg_v2():
    return load_config_file(os.path.join(REPO, "configs", "config_v2.txt"))


@pytest.fixture()
def engine(tmp_path, speakers, cfg_v2):
    cache = ContentCache(str(tmp_path / "cache"))
    return Engine(cache, speakers, cfg_v2,
                  lineage_dir=str(tmp_path / "lineage"))
