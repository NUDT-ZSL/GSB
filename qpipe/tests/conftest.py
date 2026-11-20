"""Shared pytest fixtures: freshly generated deterministic workspaces."""

from __future__ import annotations

import os

import numpy as np
import pytest

from qpipe import QuantPipeline, ResourceBudget, RunContext, samplegen


@pytest.fixture
def seed() -> int:
    return samplegen.DEFAULT_SEED


@pytest.fixture
def generated(tmp_path, seed):
    data_dir = tmp_path / "data"
    descriptor = samplegen.generate_workspace(
        str(data_dir), seed=seed, depth=5, calibration_split=4
    )
    work_dir = tmp_path / "work"
    return {
        "data_dir": str(data_dir),
        "work_dir": str(work_dir),
        "descriptor": descriptor,
        "seed": seed,
    }


@pytest.fixture
def pipeline_factory(generated):
    def _factory(
        *,
        budget=None,
        context=None,
        calibration_revision=1,
    ):
        return QuantPipeline(
            generated["data_dir"],
            generated["work_dir"],
            budget=budget or ResourceBudget(),
            context=context or RunContext(seed=generated["seed"]),
            calibration_revision=calibration_revision,
        )

    return _factory


@pytest.fixture
def package(generated):
    return samplegen.load_manifest(
        os.path.join(generated["data_dir"], "manifest.jsonl")
    )
