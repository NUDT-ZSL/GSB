import json
from pathlib import Path

import pytest

from xretrieval.canon import canonical_text, checksum
from xretrieval.engine import Engine
from xretrieval.samplegen import generate_all, generate_bad_samples


@pytest.fixture(scope="session")
def sample_dir(tmp_path_factory):
    data = tmp_path_factory.mktemp("data")
    generate_all(data)
    return data


@pytest.fixture(scope="session")
def paths(sample_dir):
    return {
        "docs": str(sample_dir / "documents.jsonl"),
        "queries": str(sample_dir / "queries.jsonl"),
        "v1": str(sample_dir / "bridge_v1.json"),
        "v2": str(sample_dir / "bridge_v2.json"),
        "v3": str(sample_dir / "bridge_v3.json"),
    }


@pytest.fixture()
def state(tmp_path):
    return tmp_path / "state"


@pytest.fixture()
def built_v1(state, paths):
    Engine(state).build(paths["v1"], paths["docs"])
    return state


def tweak_resource(src, dst, **overrides):
    data = json.loads(Path(src).read_text(encoding="utf-8"))
    params = data["scoring_weights"]["params"]
    params.update(overrides)
    data["scoring_weights"]["checksum"] = checksum(params)
    Path(dst).write_text(canonical_text(data), encoding="utf-8")
    return str(dst)
