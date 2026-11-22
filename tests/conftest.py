import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from quant_conv_pipeline.config import Config  # noqa: E402
from quant_conv_pipeline.pipeline import Pipeline  # noqa: E402
from quant_conv_pipeline.sample_gen import generate, read_jsonl  # noqa: E402


@pytest.fixture()
def workspace(tmp_path):
    """A fresh deterministic sample tree + pipeline in a temp dir."""
    data = tmp_path / "data"
    generate(str(data))
    cfg = Config(cache_dir=str(tmp_path / "run" / "cache"),
                 snapshot_dir=str(tmp_path / "run" / "snapshots"),
                 state_dir=str(tmp_path / "run" / "state"))
    pipe = Pipeline(cfg)
    return {"tmp": tmp_path, "data": str(data), "cfg": cfg, "pipe": pipe}


@pytest.fixture()
def ran(workspace):
    """workspace after a successful full run (snap-0001)."""
    pipe = workspace["pipe"]
    data = workspace["data"]
    result = pipe.full_run(read_jsonl(f"{data}/jobs.jsonl"),
                           read_jsonl(f"{data}/weight_params.jsonl"),
                           read_jsonl(f"{data}/calib_stats.jsonl"))
    workspace["run_result"] = result
    return workspace
