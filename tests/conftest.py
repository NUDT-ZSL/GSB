import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from memguard.engine import import_batch  # noqa: E402
from tools import gen_samples  # noqa: E402


@pytest.fixture()
def sample_dir(tmp_path):
    out = tmp_path / "samples"
    gen_samples.generate(str(out), seed=20261125)
    return out


@pytest.fixture()
def decl_file(sample_dir):
    return str(sample_dir / "declarations.json")


@pytest.fixture()
def batch_files(sample_dir):
    return [str(sample_dir / "traces_batch1.jsonl"),
            str(sample_dir / "traces_batch2.jsonl")]


def load_truth(sample_dir):
    with open(sample_dir / "truth.json", "r", encoding="utf-8") as fh:
        return json.load(fh)


def import_all(workdir, decls, batches):
    """Import declarations first, then each trace file as its own batch."""
    outcome = import_batch(workdir, decls, [])
    assert outcome["batch_seq"] is not None, outcome
    for batch in batches:
        outcome = import_batch(workdir, [], [batch])
        assert outcome["batch_seq"] is not None, outcome
    return outcome
