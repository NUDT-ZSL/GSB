from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any, Callable

import pytest

from deploy_convergence.engine import ConvergenceEngine


ROOT = Path(__file__).resolve().parents[1]
SAMPLES = ROOT / "samples"
CONFIG = json.loads((SAMPLES / "config.json").read_text(encoding="utf-8"))
INITIAL_CLOCK_MS = json.loads(
    (SAMPLES / "clock_steps.json").read_text(encoding="utf-8")
)["initial_clock_ms"]


@pytest.fixture
def clock_ms() -> int:
    return int(INITIAL_CLOCK_MS)


@pytest.fixture
def sample_config() -> dict[str, Any]:
    return json.loads(json.dumps(CONFIG))


@pytest.fixture
def engine_factory(
    tmp_path: Path,
) -> Callable[..., ConvergenceEngine]:
    def make(
        *,
        state_name: str = "state.sqlite",
        v1_path: Path | None = None,
        v2_meta: Path | None = None,
        v2_shards: Path | None = None,
        injection_path: Path | None = None,
        rollback_policy: str | None = None,
        tolerances: dict[str, Any] | None = None,
    ) -> ConvergenceEngine:
        paths = CONFIG["paths"]
        return ConvergenceEngine(
            v1_path=v1_path or SAMPLES / paths["v1"],
            v2_meta_path=v2_meta or SAMPLES / paths["v2_meta"],
            v2_shards_dir=v2_shards or SAMPLES / paths["v2_shards"],
            state_path=tmp_path / state_name,
            environment_order=CONFIG["environment_order"],
            tolerances=tolerances or CONFIG["tolerances"],
            clock_tolerance_ms=CONFIG["clock_tolerance_ms"],
            rollback_policy=rollback_policy or CONFIG["rollback_policy"],
            rollback_window_ms=CONFIG["rollback_window_ms"],
            injection_path=injection_path or SAMPLES / paths["injections"],
        )

    return make


@pytest.fixture
def copied_samples(tmp_path: Path) -> Path:
    target = tmp_path / "samples"
    shutil.copytree(SAMPLES, target)
    return target
