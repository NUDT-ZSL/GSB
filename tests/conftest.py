from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from cm_pipeline.engine import PipelineEngine
from cm_pipeline.fixtures import write_fixtures
from cm_pipeline.samples import load_bundle


@pytest.fixture()
def fixtures(tmp_path: Path) -> dict[str, Any]:
    return write_fixtures(tmp_path / "fixtures")


def make_engine(
    fixtures: dict[str, Any],
    root: Path,
    *,
    data: str | None = None,
    config: str | None = None,
    codebook: str | None = None,
    workers: int = 1,
) -> PipelineEngine:
    bundle = load_bundle(
        data or fixtures["samples"],
        config or fixtures["pipeline"],
        fixtures["vocab"],
        codebook or fixtures["codebook_v1"],
    )
    return PipelineEngine(
        bundle,
        cache_root=root / "cache",
        state_root=root / "state",
        workers=workers,
    )


def write_config_variant(
    fixtures: dict[str, Any],
    tmp_path: Path,
    *,
    name: str,
    resources: dict[str, Any] | None = None,
    graph_edges: list[list[str]] | None = None,
) -> str:
    with Path(fixtures["pipeline"]).open("r", encoding="utf-8") as handle:
        config = json.load(handle)
    if resources:
        config["resources"].update(resources)
    if graph_edges is not None:
        config["graph"]["edges"] = graph_edges
    target = tmp_path / f"{name}.json"
    target.write_text(json.dumps(config), encoding="utf-8")
    return str(target)

