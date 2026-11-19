"""Subprocess worker that is hard-killed (os._exit) mid-failover.

Usage: python tests/kill_worker.py <samples_dir> <db_path> <target_url>
Installs the plan, starts failover, then dies abruptly after dispatching
the 'alpha-switch' action -- no cleanup, no finally blocks, like SIGKILL.
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from recovery_pipeline.clock import LogicalClock  # noqa: E402
from recovery_pipeline.executor import Orchestrator  # noqa: E402
from recovery_pipeline.merge import merge_configs  # noqa: E402
from recovery_pipeline.store import Store  # noqa: E402
from recovery_pipeline.v1_loader import load_v1  # noqa: E402
from recovery_pipeline.v2_loader import load_v2  # noqa: E402

CLOCK_BASE = 1_000_000
CLOCK_SEQ = list(range(CLOCK_BASE, CLOCK_BASE + 1000 * 100, 100))


def main() -> None:
    samples_dir, db_path, target_url = sys.argv[1], sys.argv[2], sys.argv[3]
    v1 = load_v1(Path(samples_dir) / "v1" / "recovery_v1.json")
    v2 = load_v2(Path(samples_dir) / "v2")
    plan = merge_configs(v1, v2, CLOCK_BASE)
    store = Store(db_path)
    store.install_plan(2, plan.view(), plan.migration_map)

    def killer(phase, ctx):
        if phase == "after_dispatch" and ctx["action_id"] == "alpha-switch":
            os._exit(137)  # simulate SIGKILL: no cleanup at all

    orch = Orchestrator(store, LogicalClock(CLOCK_SEQ), target_url,
                        crash_hook=killer)
    orch.run_failover(plan, 2)


if __name__ == "__main__":
    main()
