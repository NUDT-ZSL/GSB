"""崩溃注入子进程：执行切换，在提交第 N 个动作前被 SIGKILL 强杀。

用法：python3 scripts/crash_child.py <db_path> <crash_after_commits>
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from recovery_engine.clock import LogicalClock
from recovery_engine.engine import Engine
from recovery_engine.executor import SimulatedExecutor, SimulatedHealth
from recovery_engine.store import Store

ROOT = os.path.join(os.path.dirname(__file__), "..")
SAMPLES = os.path.join(ROOT, "samples")


def main():
    db_path = sys.argv[1]
    crash_after = int(sys.argv[2])
    store = Store(db_path)
    clock = LogicalClock([10000 + i * 100 for i in range(400)], tolerance_ms=50)
    executor = SimulatedExecutor(crash_after_commits=crash_after)
    engine = Engine(store, clock, executor, SimulatedHealth())
    plan = engine.build_plan(
        os.path.join(SAMPLES, "v1", "config.json"),
        os.path.join(SAMPLES, "v2", "shards"),
        os.path.join(SAMPLES, "v2", "meta.sqlite"))
    engine.failover(plan)  # 中途被强杀
    print("unreachable")


if __name__ == "__main__":
    main()
