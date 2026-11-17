"""管线执行引擎（业务要求 3/4/5/6/7）。

- 每条请求的交互延迟从提交时刻（perf_counter_ns）计到结论完成可见
  时刻，真实墙钟测量；
- 超出请求清单期望时限者标记 timed_out 并给出实测延迟与超出量，
  超时请求不丢弃、不中断同批其余请求；
- 服务摘要与管线本地独立计算逐条比对：不符 E_MISMATCH（记录差异
  字段），摘要版本陈旧 E_STALE（拒绝采纳，不覆盖本地结果）；
- 并发度 / 单请求硬耗时上限 / 待处理队列上限均可配置；队列超限
  拒绝入队（E_QUEUE_FULL）并计入报告；
- 报告按请求稳定键（request_id）归一化排序，与返回顺序、完成顺序、
  并发度无关，保证乱序与并发等价；
- 每完成一条请求即持久化检查点；崩溃后不生成退出报告，恢复时从
  最后一个完整检查点继续。
"""

from __future__ import annotations

import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass

from . import errors, localcalc, normalize
from .checkpoint import CheckpointStore, atomic_write_json_locked

REPORT_VERSION = 1


class SimulatedCrash(Exception):
    """测试注入的强制终止：已写检查点不撤销，且不生成退出报告。"""


@dataclass
class EngineConfig:
    workers: int = 4                 # 并发度
    hard_timeout_ms: int | None = None   # 单请求真实耗时硬上限
    queue_limit: int | None = None       # 待处理请求队列上限
    resume: bool = True                  # 是否从检查点恢复
    crash_after: int | None = None       # 测试注入：完成 N 条后强制终止


def _query_with_latency(service, request, policy_version, submit_ns):
    resp = service.query(request["group_id"], policy_version)
    visible_ns = time.perf_counter_ns()
    latency_ms = (visible_ns - submit_ns) / 1_000_000.0
    return resp, latency_ms


def _make_verdict(request, resp, latency_ms, local_summary, config,
                  policy_version):
    deadline = request["deadline_ms"]
    timed_out = latency_ms > deadline
    excess_ms = max(0.0, latency_ms - deadline)

    status = errors.OK
    diff_fields = []
    summary = resp.get("summary") if isinstance(resp, dict) else None

    if config.hard_timeout_ms is not None and latency_ms > config.hard_timeout_ms:
        status = errors.E_TIMEOUT
    elif not isinstance(summary, dict):
        status = errors.E_MISMATCH
        diff_fields = ["<summary>"]
    elif summary.get("summary_version") != policy_version:
        # 陈旧版本结论：拒绝采纳，不得覆盖本地计算结果。
        status = errors.E_STALE
    else:
        diff_fields = localcalc.diff_summaries(local_summary, summary)
        if diff_fields:
            status = errors.E_MISMATCH

    return {
        "request_id": request["request_id"],
        "group_id": request["group_id"],
        "status": status,
        "adopted": status == errors.OK,
        "timed_out": timed_out,
        "latency_ms": round(latency_ms, 3),
        "deadline_ms": deadline,
        "excess_ms": round(excess_ms, 3),
        "diff_fields": diff_fields,
        "received_summary": summary,
        "local_summary": local_summary,
    }


def _projection(report):
    """报告的时序无关投影：用于并发/串行等价与恢复等价比较。"""
    return {
        "requests": [
            {
                "request_id": r["request_id"],
                "group_id": r["group_id"],
                "status": r["status"],
                "adopted": r["adopted"],
                "timed_out": r["timed_out"],
                "diff_fields": r["diff_fields"],
                "received_summary": r["received_summary"],
                "local_summary": r["local_summary"],
            }
            for r in report["requests"]
        ],
        "timeouts": report["timeouts"],
        "stale_rejected": report["stale_rejected"],
        "mismatches": report["mismatches"],
        "queue_rejected": report["queue_rejected"],
        "rejected_entries": report["rejected_entries"],
        "counts": report["counts"],
    }


def build_report(results, rejected_entries, policy_version,
                 service_summary_version, config):
    """按稳定键归一化排序生成可复现报告。"""
    records = [results[key] for key in sorted(results.keys())]
    timeouts = sorted(r["request_id"] for r in records
                      if r["timed_out"] or r["status"] == errors.E_TIMEOUT)
    stale = sorted(r["request_id"] for r in records
                   if r["status"] == errors.E_STALE)
    mismatches = sorted(r["request_id"] for r in records
                        if r["status"] == errors.E_MISMATCH)
    queue_rejected = sorted(r["request_id"] for r in records
                            if r["status"] == errors.E_QUEUE_FULL)
    counts = {
        "total": len(records),
        "ok": sum(1 for r in records if r["status"] == errors.OK),
        "mismatch": len(mismatches),
        "stale": len(stale),
        "timeout": len(timeouts),
        "queue_rejected": len(queue_rejected),
        "rejected_entries": len(rejected_entries),
    }
    return {
        "report_version": REPORT_VERSION,
        "policy_version": policy_version,
        "service_summary_version": service_summary_version,
        "config": {
            "workers": config.workers,
            "hard_timeout_ms": config.hard_timeout_ms,
            "queue_limit": config.queue_limit,
        },
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "rejected_entries": rejected_entries,
        "requests": records,
        "timeouts": timeouts,
        "stale_rejected": stale,
        "mismatches": mismatches,
        "queue_rejected": queue_rejected,
        "counts": counts,
    }


def run_pipeline(decl, manifest, service, config, workdir):
    """执行整批请求并输出报告；返回报告 dict。"""
    os.makedirs(workdir, exist_ok=True)

    groups, policy_version, rule_rejects = normalize.normalize_policy(decl)
    requests, req_rejects = normalize.normalize_requests(manifest, groups)
    rejected_entries = rule_rejects + req_rejects

    local_summaries = {
        gid: localcalc.compute_summary(group, policy_version)
        for gid, group in groups.items()
    }

    store = CheckpointStore(workdir)
    results = {}
    if config.resume:
        state = store.load(policy_version, service.summary_version)
        if state is not None:
            results.update(state["completed"])

    pending = [r for r in requests if r["request_id"] not in results]
    pending.sort(key=lambda r: r["request_id"])

    # 队列上限：超出部分拒绝入队（E_QUEUE_FULL），不中断其余请求。
    if config.queue_limit is not None and len(pending) > config.queue_limit:
        admitted = pending[: config.queue_limit]
        for req in pending[config.queue_limit:]:
            results[req["request_id"]] = {
                "request_id": req["request_id"],
                "group_id": req["group_id"],
                "status": errors.E_QUEUE_FULL,
                "adopted": False,
                "timed_out": False,
                "latency_ms": 0.0,
                "deadline_ms": req["deadline_ms"],
                "excess_ms": 0.0,
                "diff_fields": [],
                "received_summary": None,
                "local_summary": local_summaries[req["group_id"]],
            }
    else:
        admitted = pending

    completed_this_run = 0
    if admitted:
        with ThreadPoolExecutor(max_workers=max(1, config.workers)) as pool:
            futures = {}
            for req in admitted:
                submit_ns = time.perf_counter_ns()
                fut = pool.submit(_query_with_latency, service, req,
                                  policy_version, submit_ns)
                futures[fut] = req
            for fut in as_completed(futures):
                req = futures[fut]
                resp, latency_ms = fut.result()
                record = _make_verdict(
                    req, resp, latency_ms,
                    local_summaries[req["group_id"]], config, policy_version)
                results[req["request_id"]] = record
                store.save(policy_version, service.summary_version,
                           results, rejected_entries)
                completed_this_run += 1
                if (config.crash_after is not None
                        and completed_this_run >= config.crash_after):
                    raise SimulatedCrash(
                        "模拟强制终止：已完成 %d 条请求" % completed_this_run)

    report = build_report(results, rejected_entries, policy_version,
                          service.summary_version, config)
    atomic_write_json_locked(os.path.join(workdir, "report.json"), report)
    return report
