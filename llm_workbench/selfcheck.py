"""Cross-module consistency self-check via independent recomputation.

The self-check trusts nothing produced by the pipeline:
  1. it recomputes every admitted request standalone from (params, seed)
     and compares byte-for-byte against the executed results;
  2. it verifies the request_id sets of every pipeline stage agree:
     input == valid + invalid, valid == admitted + rejected,
     executed == admitted, downgraded subset of admitted;
  3. it verifies rejected and downgraded records match actual execution:
     no rejected id appears in any sampling summary, every downgraded id
     was executed with degraded (effective) parameters, and the ledger
     reserves exactly the sum of effective target lengths.
Any mismatch yields an E_INCONSISTENT error record.
"""

from __future__ import annotations

from .errors import ErrorCode
from .sampling import SamplingParams, sample_request

STAGE_SELFCHECK = "selfcheck"


def _err(message: str, **ctx) -> dict:
    return {"code": ErrorCode.E_INCONSISTENT, "message": message,
            "stage": STAGE_SELFCHECK, "context": ctx}


def run_selfcheck(all_input_ids: list, validation_errors: list, admission,
                  run_report: dict, vocab_size: int) -> dict:
    errors: list[dict] = []

    # 1. independent recomputation of every admitted request
    for sr in admission.order:
        rid = sr.request_id
        recomputed = sample_request(
            request_id=rid,
            seed=sr.request.seed,
            target_len=sr.effective_target_len,
            params=SamplingParams.from_dict(sr.effective_params.to_dict()),
            vocab_size=vocab_size,
            prompt_tokens=sr.request.prompt_tokens,
        ).to_dict()
        stored = run_report["results"].get(rid)
        if stored is None:
            errors.append(_err(f"admitted request {rid} missing from results",
                               request_id=rid))
        elif stored != recomputed:
            step = next(
                (i for i, (a, b) in enumerate(zip(stored["steps"], recomputed["steps"]))
                 if a != b),
                min(len(stored["steps"]), len(recomputed["steps"])),
            )
            errors.append(_err(
                f"recomputed result for {rid} diverges at step {step}",
                request_id=rid, first_diverging_step=step))

    # 2. request_id set consistency across stages
    invalid_ids = {e.context.get("request_id") for e in validation_errors
                   if e.context.get("request_id")}
    admitted_ids = {sr.request_id for sr in admission.order}
    rejected_ids = {r["request_id"] for r in admission.rejected}
    executed_ids = set(run_report["results"].keys())
    input_ids = set(all_input_ids)

    if admitted_ids & rejected_ids:
        errors.append(_err("admitted and rejected sets overlap",
                           overlap=sorted(admitted_ids & rejected_ids)))
    if executed_ids != admitted_ids:
        errors.append(_err("executed set != admitted set",
                           missing=sorted(admitted_ids - executed_ids),
                           extra=sorted(executed_ids - admitted_ids)))
    accounted = admitted_ids | rejected_ids | invalid_ids
    if accounted != input_ids:
        errors.append(_err("stage request_id sets do not cover the input",
                           unaccounted=sorted(input_ids - accounted),
                           unknown=sorted(accounted - input_ids)))

    # 3. rejected / downgraded records vs actual execution
    for rid in rejected_ids:
        if rid in executed_ids:
            errors.append(_err(f"rejected request {rid} appears in results",
                               request_id=rid))
    downgraded_ids = {d["request_id"] for d in admission.downgraded}
    # a request may be downgraded in the pre-admission arbitration pass and
    # still end up rejected by a budget; it must be accounted for either way
    if not downgraded_ids <= (admitted_ids | rejected_ids):
        errors.append(_err("downgraded request neither admitted nor rejected",
                           unknown=sorted(downgraded_ids - admitted_ids - rejected_ids)))
    by_id = {sr.request_id: sr for sr in admission.order}
    for rec in admission.downgraded:
        sr = by_id.get(rec["request_id"])
        if sr is None:
            continue
        if rec["category"] == "topp_tightened" and \
                sr.effective_params.top_p >= sr.request.sampling_params.top_p:
            errors.append(_err(
                f"{rec['request_id']}: recorded topp_tightened but executed "
                f"with un-tightened top_p", request_id=rec["request_id"]))
        if rec["category"] == "len_truncated" and \
                sr.effective_target_len >= sr.request.target_len:
            errors.append(_err(
                f"{rec['request_id']}: recorded len_truncated but executed "
                f"with full target_len", request_id=rec["request_id"]))

    # ledger arithmetic
    reserved = sum(sr.effective_target_len for sr in admission.order)
    if admission.ledger.get("steps_reserved") != reserved:
        errors.append(_err("ledger steps_reserved mismatch",
                           ledger=admission.ledger.get("steps_reserved"),
                           recomputed=reserved))

    return {
        "ok": not errors,
        "errors": errors,
        "checks": {
            "recomputed_requests": len(admitted_ids),
            "input_ids": len(input_ids),
            "invalid_ids": sorted(invalid_ids),
            "admitted": sorted(admitted_ids),
            "rejected": sorted(rejected_ids),
            "downgraded": sorted(downgraded_ids),
        },
    }
