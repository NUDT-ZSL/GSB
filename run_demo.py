"""End-to-end local demo of the recovery interop & adjudication pipeline.

Runs entirely on the local filesystem with deterministic samples:
  1. clean the run directory (temp files, locks, SQLite state);
  2. generate fixed v1/v2 samples;
  3. load + validate + merge both generations into a unified plan;
  4. run failover / failback adjudication against a loopback HTTP target
     under an injected logical clock;
  5. simulate a hard kill mid-failover in a second store, recover from
     persisted state only, and verify the view matches the clean run.
Exit code 0 means every check passed.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from recovery_pipeline.clock import LogicalClock  # noqa: E402
from recovery_pipeline.executor import Orchestrator, SimulatedCrash  # noqa: E402
from recovery_pipeline.http_target import TargetServer  # noqa: E402
from recovery_pipeline.merge import merge_configs  # noqa: E402
from recovery_pipeline.migrate import migration_invariant_holds  # noqa: E402
from recovery_pipeline.recovery import recover  # noqa: E402
from recovery_pipeline.store import Store  # noqa: E402
from recovery_pipeline.v1_loader import load_v1  # noqa: E402
from recovery_pipeline.v2_loader import load_v2  # noqa: E402
from samples.gen_samples import generate  # noqa: E402

RUN_DIR = Path("artifacts/run")
CLOCK_BASE = 1_000_000
CLOCK_SEQ = list(range(CLOCK_BASE, CLOCK_BASE + 1000 * 100, 100))
PLAN_VERSION = 2


def step(msg: str) -> None:
    print(f"== {msg}")


def main() -> int:
    step("clean run directory (temp files, locks, sqlite state)")
    if RUN_DIR.exists():
        shutil.rmtree(RUN_DIR)
    RUN_DIR.mkdir(parents=True)

    step("generate deterministic v1/v2 samples")
    samples = generate(RUN_DIR / "samples")
    print(json.dumps(samples, indent=2))

    step("load + validate both generations")
    v1 = load_v1(samples["v1"])
    v2 = load_v2(samples["v2"])
    print(f"v1 services: {[s.name for s in v1.services]}")
    print(f"v2 services: {[s.name for s in v2.services]}")

    step("merge into unified plan under injected logical clock")
    plan = merge_configs(v1, v2, LogicalClock(CLOCK_SEQ).now_ms())
    for name in sorted(plan.services):
        sp = plan.services[name]
        srcs = {a.source_version for a in sp.actions}
        print(f"  {name}: gen={sp.generation} prio={sp.priority} "
              f"sources={sorted(srcs)} rollback_abs_ms={sp.rollback_abs_ms}")
    assert all(migration_invariant_holds(plan, s, CLOCK_BASE)
               for s in plan.services)
    print(f"  migration map: {sorted(plan.migration_map)} "
          "(should-rollback invariant verified)")

    with TargetServer() as target:
        step("run failover + failback adjudication (clean run)")
        store = Store(RUN_DIR / "clean" / "pipeline.sqlite3")
        store.install_plan(PLAN_VERSION, plan.view(), plan.migration_map)
        orch = Orchestrator(store, LogicalClock(CLOCK_SEQ), target.url)
        chain = orch.run_failover(plan, PLAN_VERSION)
        orch.run_failback(plan, PLAN_VERSION)
        for entry in chain:
            kinds = [d["decision"] for d in entry["decisions"]]
            print(f"  {entry['service']}: first_trigger="
                  f"{entry['first_trigger']} decisions={kinds}")
        clean_view = orch.persisted_view(PLAN_VERSION)

        step("simulate hard kill mid-failover, then recover from SQLite")
        crashed = Store(RUN_DIR / "crashed" / "pipeline.sqlite3")
        crashed.install_plan(PLAN_VERSION, plan.view(), plan.migration_map)
        clock2 = LogicalClock(CLOCK_SEQ)

        def killer(phase, ctx):
            if phase == "after_intent" and ctx["action_id"] == "alpha-switch":
                raise SimulatedCrash()

        orch2 = Orchestrator(crashed, clock2, target.url, crash_hook=killer)
        try:
            orch2.run_failover(plan, PLAN_VERSION)
        except SimulatedCrash:
            print("  process killed after intent of alpha-switch")
        orch3 = Orchestrator(crashed, clock2, target.url)
        report = recover(crashed, plan, PLAN_VERSION, orch3,
                         clock_ms=CLOCK_BASE)
        print(f"  recovery report: {json.dumps(report)}")
        orch3.run_failover(plan, PLAN_VERSION)
        orch3.run_failback(plan, PLAN_VERSION)
        recovered_view = orch3.persisted_view(PLAN_VERSION)

    step("verify recovered view matches the uninterrupted run")
    identical = json.dumps(clean_view, sort_keys=True) == \
        json.dumps(recovered_view, sort_keys=True)
    print(f"  recovered == uninterrupted: {identical}")
    stages = {s["service"]: (s["stage"], s["traffic_rung"])
              for s in recovered_view["stages"]}
    print(f"  final stages: {stages}")
    ok = identical and all(st == "failed_back" and rung == 4
                           for st, rung in stages.values())
    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
