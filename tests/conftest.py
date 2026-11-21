import json

import pytest

from meeting_align.config import AlignConfig
from meeting_align.pipeline import Batch, Pipeline
from meeting_align.synth import generate_sample_set, load_pcm


@pytest.fixture(scope="session")
def samples_dir(tmp_path_factory):
    d = tmp_path_factory.mktemp("samples")
    generate_sample_set(d)
    return d


@pytest.fixture(scope="session")
def sample_data(samples_dir):
    transcripts = json.loads((samples_dir / "transcripts.json").read_text())
    manifest = json.loads((samples_dir / "manifest.json").read_text())
    truth = json.loads((samples_dir / "seam_truth.json").read_text())

    def batches(b_file="batchB.pcm"):
        return {
            "A": Batch("A", load_pcm(samples_dir / "batchA.pcm"),
                       manifest["batches"]["A"]["declared_sample_rate"],
                       transcripts["A"]["segments"]),
            "B": Batch("B", load_pcm(samples_dir / b_file),
                       manifest["batches"]["B"]["declared_sample_rate"],
                       transcripts["B"]["segments"]),
        }

    return {"batches": batches, "truth": truth, "manifest": manifest,
            "transcripts": transcripts}


@pytest.fixture()
def config():
    return AlignConfig()


@pytest.fixture()
def pipeline(tmp_path, config):
    return Pipeline(tmp_path / "pipe", config)
