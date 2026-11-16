"""命令行入口：三场景演示 + 三类异常注入，结构化日志落盘。"""
from __future__ import annotations

import argparse
import json
import os
from typing import Any

from .engine import GovernanceEngine
from .errors import GovernanceError
from .events import load_events
from .fixedpoint import Grid
from .logging_utils import configure_logger, log_governance_error
from .policy import bump_version, load_policy
from .replay import replay_snapshot
from .snapshot import restore_snapshot, save_snapshot


def _inject_policy_fault(path: str) -> str:
    """注入策略字段越界（失败率阈值 > 100），返回临时文件路径。"""
    with open(path, "r", encoding="utf-8") as handle:
        data = json.load(handle)
    data["services"]["payment-api"]["breaker"]["failure_threshold_percent"] = 150
    out = os.path.join(os.path.dirname(path), "policy_bad.json")
    with open(out, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
    return out


def _inject_event_fault(path: str) -> str:
    """注入事件时间戳回退，返回临时 NDJSON 路径。"""
    out = os.path.join(os.path.dirname(path), "events_bad.ndjson")
    with open(path, "r", encoding="utf-8") as handle:
        lines = handle.readlines()
    if len(lines) >= 4:
        payload = json.loads(lines[3])
        payload["timestamp_ms"] = 1  # 明显早于前面的时间戳
        lines[3] = json.dumps(payload, ensure_ascii=False) + "\n"
    with open(out, "w", encoding="utf-8") as handle:
        handle.writelines(lines)
    return out


def _inject_grid_fault(snapshot_path: str) -> str:
    """删除快照中的定点缩放因子，返回被破坏的快照路径。"""
    with open(snapshot_path, "r", encoding="utf-8") as handle:
        data = json.load(handle)
    data.pop("grid", None)
    out = os.path.join(os.path.dirname(snapshot_path), "snapshot_no_grid.json")
    with open(out, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2, sort_keys=True)
    return out


def _summary(engine: GovernanceEngine) -> dict[str, Any]:
    from collections import Counter
    return {
        "decisions": len(engine.decisions),
        "outcomes": dict(Counter(record.outcome for record in engine.decisions)),
        "breaker_states": {sid: breaker.state for sid, breaker in engine.breakers.items()},
    }


def run_demo(base_dir: str, inject: str, log_path: str) -> int:
    logger = configure_logger(log_path)
    policy_path = os.path.join(base_dir, "config", "policy.json")
    events_path = os.path.join(base_dir, "data", "events.ndjson")
    snapshot_path = os.path.join(base_dir, "data", "snapshot.json")
    grid = Grid.from_exponent(4)

    # 异常注入分支：分别对应 E_POLICY / E_EVENT / E_GRID，均提前独立返回
    if inject == "bad-policy":
        bad_path = _inject_policy_fault(policy_path)
        try:
            load_policy(bad_path)
        except GovernanceError as error:
            log_governance_error(logger, error)
            print(json.dumps({"injected": inject, "rejected": error.to_dict()},
                             ensure_ascii=False, indent=2))
            return 2
    if inject == "bad-event":
        bad_path = _inject_event_fault(events_path)
        try:
            load_events(bad_path)
        except GovernanceError as error:
            log_governance_error(logger, error)
            print(json.dumps({"injected": inject, "rejected": error.to_dict()},
                             ensure_ascii=False, indent=2))
            return 2
    if inject == "bad-grid":
        # 先构造一份合法快照，再删除缩放因子，模拟 E_GRID
        policy_seed = load_policy(policy_path)
        events_seed = load_events(events_path)
        seed_engine = GovernanceEngine(policy_seed, grid)
        seed_engine.run(events_seed)
        valid_snapshot = os.path.join(base_dir, "data", "snapshot_seed.json")
        save_snapshot(valid_snapshot, seed_engine, events_seed)
        broken = _inject_grid_fault(valid_snapshot)
        try:
            restore_snapshot(broken, policy_seed)
        except GovernanceError as error:
            log_governance_error(logger, error)
            print(json.dumps({"injected": inject, "rejected": error.to_dict()},
                             ensure_ascii=False, indent=2))
            return 2

    # 场景一：冷启动决策
    policy_v1 = load_policy(policy_path)
    events = load_events(events_path)
    engine_v1 = GovernanceEngine(policy_v1, grid)
    engine_v1.run(events)
    scene1 = {"scene": "cold_start", **_summary(engine_v1)}
    print(json.dumps(scene1, ensure_ascii=False, indent=2))

    # 场景二：修改失败率阈值后，版本单调递增并重新决策
    policy_v2 = bump_version(policy_v1, {"payment-api": {"failure_threshold_percent": 25}})
    engine_v2 = GovernanceEngine(policy_v2, grid)
    engine_v2.run(events)
    scene2 = {
        "scene": "threshold_changed",
        "version": policy_v2.version,
        "old_threshold": 50,
        "new_threshold": 25,
        **_summary(engine_v2),
    }
    print(json.dumps(scene2, ensure_ascii=False, indent=2))

    # 持久化 v1 会话快照（含完整策略、网格、状态与轨迹）
    save_snapshot(snapshot_path, engine_v1, events)

    # 场景三：快照恢复 + 重放校验；当前策略为 v2，触发整体替换
    restore_result = restore_snapshot(snapshot_path, policy_v2)
    replay_report = replay_snapshot(snapshot_path)
    scene3 = {
        "scene": "snapshot_restore_replay",
        "snapshot_version": restore_result.snapshot_version,
        "current_version": restore_result.current_version,
        "state_replaced": restore_result.version_replaced,
        "policy_diffs": restore_result.policy_diffs,
        "replay": replay_report.to_dict(),
    }
    print(json.dumps(scene3, ensure_ascii=False, indent=2))
    logger.info("demo completed", extra={"extra_payload": {
        "scene1": scene1, "scene2": scene2, "replay_matched": replay_report.matched}})
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="sre_guard 限流熔断降级演示")
    parser.add_argument("--base-dir", default=os.getcwd())
    parser.add_argument("--log-path", default=None)
    parser.add_argument(
        "--inject",
        choices=["none", "bad-policy", "bad-event", "bad-grid"],
        default="none",
        help="注入异常：bad-policy=E_POLICY, bad-event=E_EVENT, bad-grid=E_GRID",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    log_path = args.log_path or os.path.join(args.base_dir, "logs", "structured.ndjson")
    return run_demo(args.base_dir, args.inject, log_path)


if __name__ == "__main__":
    raise SystemExit(main())
