"""pytest 公共夹具：在临时目录确定性重建样本并运行一次主流程。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from animstrip.pipeline import run_pipeline
from animstrip.samplegen import generate


@pytest.fixture(scope="session")
def sample_dir(tmp_path_factory) -> Path:
    path = tmp_path_factory.mktemp("sample")
    generate(path)
    return path


@pytest.fixture(scope="session")
def manifest_path(sample_dir) -> Path:
    return sample_dir / "manifest.json"


@pytest.fixture()
def output_dir(tmp_path) -> Path:
    return tmp_path / "strips"


@pytest.fixture()
def report(manifest_path, output_dir) -> dict:
    return run_pipeline(manifest_path, output_dir)


@pytest.fixture()
def rows_by_id(report) -> dict[str, dict]:
    return {row["animation_id"]: row for row in report["animations"]}


@pytest.fixture()
def manifest_data(manifest_path) -> dict:
    return json.loads(manifest_path.read_text(encoding="utf-8"))
