from __future__ import annotations

import json
import signal

import pytest

from relief_workbench.engine import WorkbenchEngine
from relief_workbench.errors import WorkbenchError


def test_termination_during_initial_stats_has_marker_and_signal_code(
    small_config, tmp_path
) -> None:
    engine = WorkbenchEngine(
        small_config, tmp_path / "state", tmp_path / "out"
    )
    original = engine.renderer.compute_global_stats

    def interrupted_stats(should_stop=None):
        engine.request_termination(signal.SIGTERM)
        return original(should_stop=should_stop)

    engine.renderer.compute_global_stats = interrupted_stats  # type: ignore[method-assign]
    with pytest.raises(WorkbenchError) as caught:
        engine.preview_tile()
    assert caught.value.code == "E_INTERRUPTED"
    assert caught.value.exit_code == 143
    marker = json.loads(
        (tmp_path / "state" / "interrupted.json").read_text(encoding="utf-8")
    )
    assert marker["exit_code"] == 143
    assert marker["completed"] == []


def test_preview_accepts_one_axis_override(small_config, tmp_path) -> None:
    engine = WorkbenchEngine(
        small_config, tmp_path / "state", tmp_path / "out"
    )
    result = engine.preview_tile(row=2)
    assert result.spec.row == 2
    assert result.spec.col == small_config.preview_tile[1]
