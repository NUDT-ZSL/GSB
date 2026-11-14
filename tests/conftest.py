"""pytest 公共夹具：在临时目录确定性生成样本并执行批处理。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from cover_preview.pipeline import run_batch  # noqa: E402
from cover_preview.sample_data import build_samples  # noqa: E402


@pytest.fixture(scope="session")
def workspace(tmp_path_factory) -> Path:
    work = tmp_path_factory.mktemp("cover106")
    sample_dir = work / "samples"
    manifest = build_samples(sample_dir)
    run_batch(manifest, work / "output")
    return work


@pytest.fixture(scope="session")
def payload(workspace: Path) -> dict:
    with (workspace / "output" / "results.json").open("r", encoding="utf-8") as fh:
        return json.load(fh)


@pytest.fixture(scope="session")
def previews_dir(workspace: Path) -> Path:
    return workspace / "output" / "previews"


def result_by_id(payload: dict) -> dict[str, dict]:
    return {item["video_id"]: item for item in payload["results"]}
