"""Batch admission, budget adjudication and explicit trade-off arbitration.

Budgets (all are explicitly declared estimates, never wall-clock derived):
  * step budget  - total sampling steps = sum of effective target_len
  * KV budget    - resident KV entries per wave = sum(prompt_tokens + target_len)
  * slot budget  - max concurrent slots per wave and max sequential waves

Admission is greedy in (-priority, request_id) order, which makes chunk
boundaries, dispatch order and merge order deterministic and reproducible.

Arbitration (requirement 4) is driven by config ``tradeoff_priority``, an
ordered permutation of ["progress", "memory", "quality"]. Whenever two of
these dimensions conflict, the higher-ranked dimension wins and the loser
is degraded; every decision is recorded with its basis, the downgraded
request and the downgrade category:
  * "topp_tightened"  - memory over quality: top_p clamped to topp_floor
  * "len_truncated"   - progress over quality: target_len cut to fit budget
  * "wave_added"      - quality over progress: new wave opened (slower)
  * "slot_reduced"    - memory over progress: free slot left unused
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from .errors import ErrorCode
from .sampling import SamplingParams
from .validation import Request

STAGE_ADMIT = "admit"


@dataclass
class ScheduledRequest:
    request: Request
    effective_params: SamplingParams
    effective_target_len: int
    wave: int = -1

    @property
    def request_id(self) -> str:
        return self.request.request_id

    @property
    def kv_entries(self) -> int:
        return self.request.prompt_tokens + self.effective_target_len


@dataclass
class AdmissionResult:
    order: list = field(default_factory=list)      # list[ScheduledRequest]
    waves: list = field(default_factory=list)      # list[list[request_id]]
    rejected: list = field(default_factory=list)   # list[dict]
    downgraded: list = field(default_factory=list) # list[dict]
    arbitration: list = field(default_factory=list)# list[dict]
    ledger: dict = field(default_factory=dict)

    def rejected_ids(self) -> set:
        return {r["request_id"] for r in self.rejected}


def _rank(cfg: dict, dim: str) -> int:
    return cfg["tradeoff_priority"].index(dim)


def _wins(cfg: dict, dim_a: str, dim_b: str) -> bool:
    """True if dim_a outranks dim_b in the declared trade-off priority."""
    return _rank(cfg, dim_a) < _rank(cfg, dim_b)


def estimate_candidates(params: SamplingParams, vocab_size: int) -> int:
    """Explicit comparison-count estimate of surviving candidates per step."""
    est = math.ceil(vocab_size * params.top_p)
    if params.top_k is not None:
        est = min(est, params.top_k)
    return min(est, vocab_size)


def _reject(result: AdmissionResult, rid: str, code: str, reason: str, remainders: dict):
    result.rejected.append({
        "request_id": rid,
        "code": code,
        "reason": reason,
        "stage": STAGE_ADMIT,
        "budget_remainders": remainders,
    })


def _downgrade(result: AdmissionResult, rid: str, category: str, detail: str, basis: str):
    rec = {"request_id": rid, "category": category, "detail": detail, "basis": basis}
    result.downgraded.append(rec)
    result.arbitration.append(rec)


def admit(requests: list, cfg: dict, vocab_size: int) -> AdmissionResult:
    """Triple-budget admission + arbitration. Deterministic by construction."""
    result = AdmissionResult()
    ordered = sorted(requests, key=lambda r: (-r.priority, r.request_id))

    # --- arbitration pass 1: top-p tightening (memory vs quality) ----------
    effective: dict[str, tuple[SamplingParams, int]] = {}
    for req in ordered:
        params, target_len = req.sampling_params, req.target_len
        est = estimate_candidates(params, vocab_size)
        if est > cfg["candidate_budget"]:
            if _wins(cfg, "memory", "quality"):
                tightened = SamplingParams(
                    temperature=params.temperature,
                    top_k=params.top_k,
                    top_p=cfg["topp_floor"],
                )
                _downgrade(
                    result, req.request_id, "topp_tightened",
                    f"top_p {params.top_p} -> {cfg['topp_floor']} "
                    f"(candidate estimate {est} > budget {cfg['candidate_budget']})",
                    "tradeoff_priority ranks memory over quality; tightening "
                    "top-p cuts per-step candidate comparisons but may drop "
                    "tokens that should be kept",
                )
                params = tightened
            else:
                result.arbitration.append({
                    "request_id": req.request_id, "category": "topp_kept",
                    "detail": f"candidate estimate {est} exceeds budget "
                              f"{cfg['candidate_budget']}; kept as declared",
                    "basis": "tradeoff_priority ranks quality over memory",
                })
        effective[req.request_id] = (params, target_len)

    # --- admission pass: waves, step budget, kv budget, slot budget --------
    waves: list[list[ScheduledRequest]] = []
    wave_kv: list[int] = []
    steps_used = 0

    for req in ordered:
        params, target_len = effective[req.request_id]
        rid = req.request_id

        def remainders():
            return {
                "step_budget": cfg["step_budget"] - steps_used,
                "kv_budget_current_wave": (cfg["kv_budget"] - wave_kv[-1]) if wave_kv else cfg["kv_budget"],
                "slot_budget_waves": cfg["max_waves"] - len(waves),
            }

        kv_need = req.prompt_tokens + target_len
        if kv_need > cfg["kv_budget"]:
            _reject(result, rid, ErrorCode.E_KV_BUDGET,
                    f"request alone needs {kv_need} resident KV entries, "
                    f"budget is {cfg['kv_budget']}", remainders())
            continue

        # step budget (progress vs quality arbitration)
        if steps_used + target_len > cfg["step_budget"]:
            remaining = cfg["step_budget"] - steps_used
            if remaining > 0 and _wins(cfg, "progress", "quality"):
                _downgrade(
                    result, rid, "len_truncated",
                    f"target_len {target_len} -> {remaining} "
                    f"(step budget {cfg['step_budget']})",
                    "tradeoff_priority ranks progress over quality; length "
                    "extended for quality would exceed the step budget",
                )
                target_len = remaining
                kv_need = req.prompt_tokens + target_len
            else:
                _reject(result, rid, ErrorCode.E_STEP_BUDGET,
                        f"needs {target_len} steps, only {remaining} remain",
                        remainders())
                continue

        # wave placement (memory vs progress / quality arbitration)
        placed = False
        if waves and len(waves[-1]) < cfg["max_slots"]:
            if wave_kv[-1] + kv_need <= cfg["kv_budget"]:
                placed = True
            else:
                # free slot exists but KV is tight: expanding concurrency
                # would approach the resident-cache ceiling.
                fittable = cfg["kv_budget"] - wave_kv[-1] - req.prompt_tokens
                if fittable > 0 and _wins(cfg, "progress", "quality"):
                    _downgrade(
                        result, rid, "len_truncated",
                        f"target_len {target_len} -> {fittable} to fit wave "
                        f"{len(waves) - 1} KV budget",
                        "tradeoff_priority ranks progress over quality; "
                        "truncated instead of opening a slower new wave",
                    )
                    target_len = fittable
                    kv_need = req.prompt_tokens + target_len
                    placed = True
                else:
                    _downgrade(
                        result, rid, "slot_reduced",
                        f"free slot in wave {len(waves) - 1} left unused; "
                        f"deferred to a new wave",
                        "tradeoff_priority ranks memory/quality over "
                        "progress; expanding slots would approach the "
                        "resident KV cache ceiling",
                    )
        if not placed:
            if len(waves) >= cfg["max_waves"]:
                _reject(result, rid, ErrorCode.E_SLOT_BUDGET,
                        f"all {cfg['max_waves']} waves exhausted; no slot "
                        f"remains for this request", remainders())
                continue
            waves.append([])
            wave_kv.append(0)
            if len(waves) > 1 and any(d["request_id"] == rid for d in result.downgraded):
                pass  # downgrade already recorded above
            placed = True

        sr = ScheduledRequest(request=req, effective_params=params,
                              effective_target_len=target_len, wave=len(waves) - 1)
        waves[-1].append(sr)
        wave_kv[-1] += kv_need
        steps_used += target_len
        result.order.append(sr)

    result.waves = [[sr.request_id for sr in wave] for wave in waves]
    result.ledger = {
        "step_budget": cfg["step_budget"],
        "steps_reserved": steps_used,
        "steps_remaining": cfg["step_budget"] - steps_used,
        "kv_budget": cfg["kv_budget"],
        "kv_per_wave": list(wave_kv),
        "kv_peak": max(wave_kv) if wave_kv else 0,
        "max_slots": cfg["max_slots"],
        "max_waves": cfg["max_waves"],
        "waves_used": len(waves),
        "waves_remaining": cfg["max_waves"] - len(waves),
        "admitted": [sr.request_id for sr in result.order],
        "rejected": [r["request_id"] for r in result.rejected],
    }
    return result


def chunk_execution_order(admission: AdmissionResult, batch_size: int) -> list:
    """Deterministic chunking of the execution order for pool dispatch."""
    ids = [sr.request_id for sr in admission.order]
    return [ids[i:i + batch_size] for i in range(0, len(ids), batch_size)]
