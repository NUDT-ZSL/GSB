"""断点续写示例：恢复快照后从断点继续推进新事件。

流程：
1. 冷启动处理前半段事件并保存快照；
2. 恢复快照（整体替换内存状态）；
3. 用恢复出的引擎继续推进后半段事件；
4. 与「单引擎一次性处理全部事件」的轨迹逐位比对。
"""
from __future__ import annotations

import json
import os
import tempfile

from sre_guard import (
    GovernanceEngine,
    Grid,
    load_events,
    load_policy,
    restore_snapshot,
    save_snapshot,
)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main() -> None:
    policy = load_policy(os.path.join(ROOT, "config", "policy.json"))
    events = load_events(os.path.join(ROOT, "data", "events.ndjson"))
    grid = Grid.from_exponent(4)
    split = len(events) // 2
    first_half, second_half = events[:split], events[split:]

    tmp_dir = tempfile.mkdtemp(prefix="sre-guard-continue-")
    snapshot_path = os.path.join(tmp_dir, "snapshot.json")

    engine_a = GovernanceEngine(policy, grid)
    engine_a.run(first_half)
    save_snapshot(snapshot_path, engine_a, first_half)

    restored = restore_snapshot(snapshot_path, policy)
    engine_b = restored.engine
    for offset, event in enumerate(second_half, start=split):
        engine_b.process_event(event, offset)

    engine_full = GovernanceEngine(policy, grid)
    engine_full.run(events)

    reference = [record.to_dict() for record in engine_full.decisions]
    continued = [record.to_dict() for record in engine_b.decisions]
    # 续写引擎只保留后半段轨迹，按事件下标与完整运行的对应区间对齐比较
    reference_tail = reference[split:]
    matched = reference_tail == continued
    print(json.dumps({
        "split_at": split,
        "total_events": len(events),
        "continued_decisions": len(continued),
        "bit_identical_to_full_run": matched,
        "aligned_range": [split, len(events) - 1],
    }, ensure_ascii=False, indent=2))
    if not matched:
        raise SystemExit("断点续写轨迹与一次性运行不一致")


if __name__ == "__main__":
    main()
