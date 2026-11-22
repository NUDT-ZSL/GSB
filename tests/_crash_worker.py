"""Helper used by the hard-kill recovery test.

Runs aggregation until a target tick then terminates the process abruptly
(``os._exit``), mimicking ``SIGKILL`` so no Python ``finally``/rollback runs.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from slo_workbench.engine import Engine  # noqa: E402
from slo_workbench.loader import read_bundle  # noqa: E402
from slo_workbench.storage import Workspace  # noqa: E402


def main() -> None:
    workspace_path, sample_dir, kill_after_tick = sys.argv[1], sys.argv[2], int(sys.argv[3])
    _, _, clock = read_bundle(Path(sample_dir))
    engine = Engine(Workspace(workspace_path))
    for index, tick_ms in enumerate(clock.ticks_ms):
        engine.advance(tick_ms, clock.rollback_tolerance_ms)
        if index == kill_after_tick:
            # Hard kill: no flush, no rollback, no atexit handlers.
            os._exit(137)


if __name__ == "__main__":
    main()
