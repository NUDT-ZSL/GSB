"""调度会话核心：固定顺序准入、配额核算与拓扑放置。

调度顺序完全确定：优先级降序（数值越大越优先），同优先级按清单出现顺序升序，
不依赖字典或哈希遍历，因此同一输入永远得到同一结论与同一拒绝原因链。
"""

import datetime as _dt

from .estimator import estimate_job
from .models import JobResult, ScheduleRun
from .placement import try_place
from .quota import QuotaLedger


def scheduling_order(jobs):
    return sorted(jobs, key=lambda job: (-job.priority, job.manifest_index))


def run_schedule(jobs, pools_by_name, quotas, scale_params, parameter_version,
                 drained_by_pool=None, scale_bounds=None, now=None):
    """对整组作业从头执行一次完整调度，返回不可变语义的 :class:`ScheduleRun`。"""
    drained_by_pool = drained_by_pool or {}
    ledger = QuotaLedger(quotas)
    occupied = {pool_name: set() for pool_name in pools_by_name}
    results = []

    for job in scheduling_order(jobs):
        pool = pools_by_name[job.pool_name]
        estimate = estimate_job(job, scale_params, parameter_version, scale_bounds)
        reasons = ledger.check(job, estimate)
        if reasons:
            results.append(JobResult(
                job_id=job.job_id, admitted=False, code=reasons[0].code,
                code_name=reasons[0].code_name, estimate=estimate,
                pool_name=pool.name, slots=[], topology_group="", reasons=reasons,
            ))
            continue

        slots, topology_group, place_reason = try_place(
            pool, job, estimate, occupied[pool.name])
        if place_reason is not None:
            results.append(JobResult(
                job_id=job.job_id, admitted=False, code=place_reason.code,
                code_name=place_reason.code_name, estimate=estimate,
                pool_name=pool.name, slots=[], topology_group="", reasons=[place_reason],
            ))
            continue

        occupied[pool.name].update(slots)
        ledger.commit(job, estimate)
        results.append(JobResult(
            job_id=job.job_id, admitted=True, code=0, code_name="OK",
            estimate=estimate, pool_name=pool.name, slots=slots,
            topology_group=topology_group, reasons=[],
        ))

    timestamp = now or _dt.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    return ScheduleRun(
        parameter_version=parameter_version,
        scale_params=scale_params,
        results=results,
        quota_usage=ledger.snapshot_usage(),
        generated_at=timestamp,
    )


def compare_runs(expected_run, actual_run, ordered_jobs=None):
    """逐项比较两次调度，返回首个分歧；无分歧返回 None。

    比较顺序固定为清单顺序（作业 manifest_index 升序），保证首个分歧稳定。
    """
    expected = {result.job_id: result for result in expected_run.results}
    actual = {result.job_id: result for result in actual_run.results}
    if ordered_jobs is None:
        ordered_ids = sorted(expected)
    else:
        ordered_ids = [job.job_id for job in sorted(ordered_jobs, key=lambda item: item.manifest_index)]
    for job_id in ordered_ids:
        if job_id not in actual:
            return {"job_id": job_id, "field": "job_presence",
                    "expected": "present", "actual": "missing"}
        left = expected[job_id]
        right = actual[job_id]
        checks = (
            ("admitted", left.admitted, right.admitted),
            ("code", left.code, right.code),
            ("slots", list(left.slots), list(right.slots)),
            ("per_card_mib", left.estimate.per_card_mib, right.estimate.per_card_mib),
            ("total_mib", left.estimate.total_mib, right.estimate.total_mib),
            ("reason_chain", [reason.to_dict() for reason in left.reasons],
             [reason.to_dict() for reason in right.reasons]),
        )
        for field_name, expected_value, actual_value in checks:
            if expected_value != actual_value:
                return {
                    "job_id": job_id,
                    "field": field_name,
                    "expected": expected_value,
                    "actual": actual_value,
                }
    return None
