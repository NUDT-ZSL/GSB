"""端到端示例：状态演进与恢复一致性诊断组件全流程演示。

运行：python3 examples/run_demo.py
全程离线、确定性，输出可复现的状态、误差账本、失效闭包与恢复裁决。
"""

import hashlib
import json
import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from statediag.checkpoints import CheckpointStore  # noqa: E402
from statediag.clock import LogicalClock  # noqa: E402
from statediag.concurrency import CommitGate  # noqa: E402
from statediag.consistency import run_self_check  # noqa: E402
from statediag.errors import (  # noqa: E402
    EStageBusy,
    EToleranceExceeded,
    ESnapshotRollback,
)
from statediag.ingestion import ingest  # noqa: E402
from statediag.invalidation import plan_invalidation  # noqa: E402
from statediag.model import TransformConfig  # noqa: E402
from statediag.pipeline import DiagnosticPipeline, _Interrupt  # noqa: E402
from statediag.cache import content_key  # noqa: E402
from statediag.snapshots import (  # noqa: E402
    guard_no_rollback,
    migrate_v1_to_v2,
    migrate_v2_to_v1,
    read_v1,
    read_v2,
)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SAMPLE = os.path.join(ROOT, "data", "sample")
STATE_DIM = 12


def config(precision, algebra=1, strict=False):
    if precision == "float64":
        return TransformConfig(algebra, "float64", "half-even", 1e-12, 1e-9,
                               1_005_000)
    if precision == "float32":
        return TransformConfig(algebra, "float32", "half-even", 1e-5, 1e-4,
                               1_005_000)
    tol = (1e-9, 1e-9) if strict else (1e-1, 5e-2)
    return TransformConfig(algebra, "fixed", "half-even", tol[0], tol[1],
                           1_005_000)


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def main():
    out = os.path.join(ROOT, "data", "demo_output")
    if os.path.exists(out):
        shutil.rmtree(out)
    os.makedirs(out, exist_ok=True)
    report = {}

    # 1) 确定性摄入与初始校验。
    records = ingest(os.path.join(SAMPLE, "tasks.jsonl"), STATE_DIM)
    report["ingestion"] = {
        "task_count": len(records),
        "order": [r["task"].task_id for r in records],
        "first_checksum": records[0]["initial_checksum"],
        "first_bounds": records[0]["block_bounds"],
    }

    # 2) 三档精度完整 DAG，落盘状态与误差账本。
    precision_runs = {}
    for precision in ("float64", "float32", "fixed"):
        work = os.path.join(out, f"run_{precision}")
        pipe = DiagnosticPipeline(
            work, STATE_DIM, config(precision),
            clock=LogicalClock(start_ms=10_000, tick_ms=7),
            memory_limit_bytes=4 * 1024 * 1024,
        )
        result = pipe.run(os.path.join(SAMPLE, "tasks.jsonl"))
        precision_runs[precision] = {
            "processed": result["processed"],
            "ledger_hashes": {
                tid: sha256_bytes(pipe.ledger_bytes(tid))
                for tid in result["processed"]
            },
            "memory_high_water": result["memory_high_water"],
        }
    report["precision_runs"] = precision_runs

    # 3) 容差超限时返回 E_TOLERANCE_EXCEEDED。
    strict_work = os.path.join(out, "run_fixed_strict")
    strict_pipe = DiagnosticPipeline(
        strict_work, STATE_DIM, config("fixed", strict=True),
        clock=LogicalClock(),
    )
    try:
        strict_pipe.run(os.path.join(SAMPLE, "tasks.jsonl"))
        tolerance = {"raised": False}
    except EToleranceExceeded as exc:
        tolerance = {"raised": True, "code": exc.code,
                     "first_task": exc.details["task_id"],
                     "stages": exc.details["exceeded_stages"]}
    report["tolerance"] = tolerance

    # 4) 三类失效传播与增量闭包。
    base_pipe = DiagnosticPipeline(
        os.path.join(out, "incr"), STATE_DIM, config("float64"),
        clock=LogicalClock(),
    )
    base_pipe.run(os.path.join(SAMPLE, "tasks.jsonl"))
    old_records = ingest(os.path.join(SAMPLE, "tasks.jsonl"), STATE_DIM)
    old_by_id = {r["task"].task_id: r["task"] for r in old_records}
    committed = {e["key"] for e in base_pipe.cache._entries.values()}

    rev_records = ingest(os.path.join(SAMPLE, "tasks_revision.jsonl"),
                         STATE_DIM)
    rev_plan = plan_invalidation(
        [r["task"] for r in rev_records], old_by_id, config("float64"),
        config("float64"), committed,
    )
    alg_plan = plan_invalidation(
        [r["task"] for r in old_records], old_by_id, config("float64"),
        config("float64", algebra=2), set(),
    )
    blk_records = ingest(os.path.join(SAMPLE, "tasks_blocksize.jsonl"),
                         STATE_DIM)
    blk_plan = plan_invalidation(
        [r["task"] for r in blk_records], old_by_id, config("float64"),
        config("float64"), committed,
    )
    report["invalidation"] = {
        "revision_bump": rev_plan.to_dict(),
        "algebra_bump": alg_plan.to_dict(),
        "block_size_change": blk_plan.to_dict(),
    }

    # 5) 两代快照迁移、整体替换与回退保护。
    v1_path = os.path.join(SAMPLE, "snapshots", "v1", "config_v1.json")
    v2_dir = os.path.join(out, "snapshots", "v2_migrated")
    _, provenance = migrate_v1_to_v2(v2_dir, v1_path, origin_ms=1_000_000)
    v2 = read_v2(v2_dir)
    v1_back_dir = os.path.join(out, "snapshots", "v1_downgraded")
    v1_back_path, dropped = migrate_v2_to_v1(
        v1_back_dir,
        os.path.join(SAMPLE, "snapshots", "v2"),
        origin_ms=1_000_000,
    )
    v1_back = read_v1(v1_back_path, origin_ms=1_000_000)
    current = read_v1(v1_path, origin_ms=1_000_000)
    rollback = {}
    try:
        guard_no_rollback(v2, current)
    except ESnapshotRollback as exc:
        rollback = {"raised": True, "code": exc.code,
                    "detail": exc.details}
    report["snapshots"] = {
        "migration_provenance": provenance,
        "v2_algebra": v2["algebra"],
        "v2_tombstones": v2["tombstones"],
        "downgrade_dropped": dropped,
        "v1_back_effective_ms": v1_back["effective_ms"],
        "rollback_blocked": rollback,
    }

    # 6) 中断恢复：先无中断跑一遍得到账本字节基线，再中断后恢复对比。
    clean_dir = os.path.join(out, "recovery_clean")
    clean_pipe = DiagnosticPipeline(
        clean_dir, STATE_DIM, config("float64"),
        clock=LogicalClock(start_ms=10_000, tick_ms=7),
    )
    clean_pipe.run(os.path.join(SAMPLE, "tasks.jsonl"))
    baseline = {
        tid: sha256_bytes(clean_pipe.ledger_bytes(tid))
        for tid in report["ingestion"]["order"]
    }

    interrupted_dir = os.path.join(out, "recovery_interrupted")
    interrupted_pipe = DiagnosticPipeline(
        interrupted_dir, STATE_DIM, config("float64"),
        clock=LogicalClock(start_ms=10_000, tick_ms=7),
    )
    try:
        interrupted_pipe.run(
            os.path.join(SAMPLE, "tasks.jsonl"), fail_after_task="t02"
        )
    except _Interrupt:
        pass
    cp_before = CheckpointStore(
        os.path.join(interrupted_dir, "checkpoints"),
        clock=LogicalClock(start_ms=10_000, tick_ms=7),
    ).latest()
    resumed = interrupted_pipe.run(os.path.join(SAMPLE, "tasks.jsonl"))
    recovered = {
        tid: sha256_bytes(interrupted_pipe.ledger_bytes(tid))
        for tid in report["ingestion"]["order"]
    }
    report["recovery"] = {
        "checkpoint_at_interrupt": {
            "version": cp_before["version"],
            "completed": cp_before["completed_task_ids"],
        },
        "resumed_to_process": resumed["recovery"]["pending_at_resume"],
        "ledger_byte_identical": baseline == recovered,
        "baseline_hashes": baseline,
        "recovered_hashes": recovered,
    }

    # 7) 并发提交门：后到者立即 E_STAGE_BUSY，不等待。
    gate = CommitGate()
    gate.try_acquire("owner-A")
    busy = {}
    try:
        gate.try_acquire("owner-B")
    except EStageBusy as exc:
        busy = {"raised": True, "code": exc.code, "holder": exc.details["holder"]}
    gate.release("owner-A")
    report["concurrency"] = busy

    # 8) 跨模块一致性自检。
    task_by_id = {r["task"].task_id: r["task"] for r in rev_records}
    recorded = {
        task.task_id: base_pipe.cache.get(
            content_key(task, config("float64"))
        )["ledger"]
        for task in task_by_id.values()
        if base_pipe.cache.has(content_key(task, config("float64")))
    }
    self_check = run_self_check(
        rev_plan, [t.task_id for t in task_by_id.values()], task_by_id,
        STATE_DIM, {"float64": config("float64")}, recorded,
    )
    report["self_check"] = self_check

    report_path = os.path.join(out, "demo_report.json")
    with open(report_path, "w", encoding="utf-8") as handle:
        json.dump(report, handle, sort_keys=True, indent=2, ensure_ascii=False)
    print(json.dumps({
        "tolerance_raised": report["tolerance"]["raised"],
        "recovery_byte_identical": report["recovery"]["ledger_byte_identical"],
        "rollback_blocked": bool(report["snapshots"]["rollback_blocked"]),
        "stage_busy": report["concurrency"]["raised"],
        "self_check_ok": report["self_check"]["ok"],
        "report": os.path.relpath(report_path, ROOT),
    }, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
