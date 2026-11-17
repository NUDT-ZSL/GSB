"""Subprocess used to exercise real forced-termination checkpoint recovery."""

from __future__ import annotations

import sys
import time
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from trustman.engine import run_workspace_validation
from trustman.scheduler import SchedulerConfig


def main() -> int:
    (
        workspace,
        certificates,
        chains,
        snapshot_v1,
        snapshot_v2,
        snapshot_v3,
        report,
    ) = sys.argv[1:]

    def observer(result: dict) -> None:
        if result.get("chain_id") == "chain-empty":
            time.sleep(60)

    run_workspace_validation(
        workspace=workspace,
        certificate_paths=[certificates],
        chain_paths=[chains],
        snapshot_paths=[snapshot_v1, snapshot_v2, snapshot_v3],
        config=SchedulerConfig(workers=1),
        report_path=report,
        task_observer=observer,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
