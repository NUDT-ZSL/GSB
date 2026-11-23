"""跨模块一致性自检。

校验：

1. 参与采样的请求集合 == 准入集合（校验通过的请求中被准入的部分）；
2. 预算扣减与释放可由逐请求明细完整重算；
3. 被拒请求不出现在任何采样结果中；
4. 采样结果内部分配的步数不超过各自目标长度。

任一不一致返回 ``E_INCONSISTENT`` 并指出首个偏差位置。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .budget import BudgetLedger, request_cache_entries, request_step_demand
from .errors import BenchError, E_INCONSISTENT
from .models import Request, SampleResult


@dataclass
class SelfCheckReport:
    ok: bool
    checks: list[dict[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "checks": self.checks}


def _record(checks: list[dict[str, Any]], name: str, ok: bool, detail: str) -> None:
    checks.append({"name": name, "ok": ok, "detail": detail})
    if not ok:
        raise BenchError(
            E_INCONSISTENT,
            f"self-check failed at {name}: {detail}",
            stage="selfcheck",
            field=name,
            detail={"first_deviation": detail},
        )


def run_selfcheck(
    valid_requests: list[Request],
    admitted_ids: list[str],
    rejected_ids: list[str],
    results: list[SampleResult],
    ledger: BudgetLedger,
    effective_lengths: dict[str, int],
) -> SelfCheckReport:
    checks: list[dict[str, Any]] = []

    # 1. 采样集合 == 准入集合
    sampled_ids = sorted(result.request_id for result in results)
    admitted_sorted = sorted(admitted_ids)
    _record(
        checks,
        "sampled_set_equals_admitted_set",
        sampled_ids == admitted_sorted,
        (
            f"sampled={sampled_ids} admitted={admitted_sorted}"
            if sampled_ids != admitted_sorted
            else f"{len(sampled_ids)} requests match"
        ),
    )

    # 2. 被拒请求不出现在采样结果中
    sampled_set = set(sampled_ids)
    leaked = sorted(sampled_set & set(rejected_ids))
    _record(
        checks,
        "rejected_absent_from_samples",
        not leaked,
        f"leaked rejected requests={leaked}" if leaked else "no rejected request sampled",
    )

    # 3. 预算事件可由逐请求明细重算
    rebuilt = BudgetLedger(limits=ledger.limits)
    by_id = {request.request_id: request for request in valid_requests}
    for request_id in admitted_sorted:
        request = by_id[request_id]
        effective = Request(
            request_id=request.request_id,
            prompt_tokens=request.prompt_tokens,
            sampling_params=request.sampling_params,
            target_length=effective_lengths.get(request_id, request.target_length),
            seed=request.seed,
            priority=request.priority,
        )
        rebuilt.reserve(effective)
        rebuilt.release(effective)
    rebuilt_signature = [event.to_dict() for event in rebuilt.events]
    actual_signature = [event.to_dict() for event in ledger.events]
    _record(
        checks,
        "ledger_recomputable",
        rebuilt_signature == actual_signature,
        (
            "ledger events fully recomputed from per-request detail"
            if rebuilt_signature == actual_signature
            else "first ledger event mismatch between rebuilt and actual ledger"
        ),
    )

    # 4. 最终用量归零（全部请求完成后缓存与槽位均释放）
    used = ledger.used()
    _record(
        checks,
        "resources_released",
        used["cache"] == 0 and used["slots"] == 0 and used["steps"] == 0,
        f"final used={used}",
    )

    # 5. 每个结果的实际步数不超过目标长度
    for result in results:
        target = effective_lengths.get(result.request_id)
        if target is None:
            target = by_id[result.request_id].target_length
        if len(result.generated_tokens) > target:
            _record(
                checks,
                "step_count_within_target",
                False,
                (
                    f"request={result.request_id} generated "
                    f"{len(result.generated_tokens)} > target {target}"
                ),
            )
    _record(
        checks,
        "step_count_within_target",
        True,
        "all requests within their effective target length",
    )

    return SelfCheckReport(ok=True, checks=checks)
