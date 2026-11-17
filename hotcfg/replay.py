"""恢复后的重放校验: 按快照版本链重新推算并逐项比较。"""
from __future__ import annotations

from .center import ConfigCenter
from .errors import BizError, E_REPLAY
from .latency import LatencyObserver
from .snapshot import load_snapshot
from .tasks import ComputeTask
from .loader import TaskSpec


def _fail(basis: str, details: dict) -> BizError:
    return BizError(E_REPLAY, f"replay failed: {basis}", {"basis": basis, **details})


def replay_snapshot(path, series: dict[str, list[float]], logger=None) -> dict:
    snap = load_snapshot(path)
    center = ConfigCenter.from_state(snap, logger=logger)
    records = {r.version: r for r in center.records()}

    # 1. 版本链重放: 由父版本生效值 + 变更键集合推算每次提交的完整生效值
    for v in sorted(records):
        rec = records[v]
        if rec.parent is None:
            continue
        parent = records.get(rec.parent)
        if parent is None:
            raise _fail("missing parent version", {"version": v, "parent": rec.parent})
        derived = {k: dict(entry) for k, entry in parent.effective.items()}
        for k in rec.changed_keys:
            if k not in rec.effective:
                raise _fail("changed key missing from effective set",
                            {"version": v, "key": k})
            derived[k] = rec.effective[k]
        if derived != rec.effective:
            raise _fail("effective set not reproducible from parent + changed keys",
                        {"version": v})

    # 2. 推送集合重放: 订阅关系 ∩ 变更键集合 == 推送确认记录
    subs = snap.get("subscriptions", {})
    acks = snap.get("push_acks", [])
    for v in sorted(records):
        rec = records[v]
        if rec.parent is None:
            continue
        expected = {
            tid for tid, sub in subs.items()
            if set(sub["keys"]) & set(rec.changed_keys)
        }
        actual = {a["task_id"] for a in acks if a["version"] == v}
        if expected != actual:
            raise _fail("push target set mismatch",
                        {"version": v, "expected": sorted(expected),
                         "actual": sorted(actual)})

    # 3. 任务结果摘要重放
    tasks = {}
    for tid, sub in subs.items():
        spec = TaskSpec(id=tid, keys=tuple(sub["keys"]),
                        aggregation=_result_agg(snap, tid),
                        expected_version=sub.get("expected_version"))
        tasks[tid] = ComputeTask(spec, series)
    for result in snap.get("results", []):
        task = tasks.get(result["task_id"])
        if task is None:
            raise _fail("result references unknown subscription",
                        {"task_id": result["task_id"]})
        effective = center.get_effective(result["version"], keys=task.spec.keys)
        recomputed = task.compute_digest(effective, result["version"])
        if recomputed["value_fp"] != result["value_fp"]:
            raise _fail("result digest not reproducible",
                        {"task_id": result["task_id"], "version": result["version"],
                         "expected_value_fp": recomputed["value_fp"],
                         "actual_value_fp": result["value_fp"]})

    # 4. 延迟统计重放: 由快照样本重算统计口径并与快照存储口径比较
    observer = LatencyObserver()
    observer.load_samples(snap.get("latency_samples", []))
    stored_stats = snap.get("latency_stats")
    if stored_stats is not None:
        try:
            recomputed = observer.stats_json(group_version=False)
        except BizError as exc:
            raise _fail("latency stats not reproducible",
                        {"error": exc.to_dict()}) from exc
        if recomputed != stored_stats:
            raise _fail("latency stats mismatch",
                        {"groups": sorted(set(recomputed) | set(stored_stats))})

    return {
        "ok": True,
        "versions_checked": len(records),
        "push_acks_checked": len(acks),
        "results_checked": len(snap.get("results", [])),
        "latency_groups_checked": len(stored_stats or {}),
    }


def _result_agg(snap: dict, task_id: str) -> str:
    for result in snap.get("results", []):
        if result["task_id"] == task_id:
            return result["aggregation"]
    return "sum"
