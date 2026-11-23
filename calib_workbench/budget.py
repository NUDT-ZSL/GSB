"""Dual budget admission: total fit iterations + resident memory (req. 5).

Estimates are deterministic closed-form functions of (calibrator type,
n_ref, n_eval, n_bins):
  * platt:     iters = min(max_iter, 8 + n_ref // 50); mem = 16*(n_ref+n_eval) + 4096
  * isotonic:  iters = 2 * n_ref;                      mem = 40*n_ref + 16*n_eval + 4096
  * histogram: iters = n_ref + n_bins;                 mem = 16*(n_ref+n_bins+n_eval) + 2048

Admission walks combos in deterministic (calibrator, group) order and
admits greedily while both budgets hold.  Resident memory is modeled as
the peak per-batch sum under the deterministic batching of the admitted
set.  Rejected combos are reported with their deficit; nothing is ever
silently downsampled.
"""

from __future__ import annotations

from .errors import E_ITER_BUDGET, E_MEMORY_BUDGET


def estimate_combo(calibrator: str, n_ref: int, n_eval: int, cfg: dict) -> dict:
    params = cfg["calibrators"][calibrator]
    if calibrator == "platt":
        iters = min(int(params.get("max_iter", 50)), 8 + n_ref // 50)
        mem = 16 * (n_ref + n_eval) + 4096
    elif calibrator == "isotonic":
        iters = 2 * n_ref
        mem = 40 * n_ref + 16 * n_eval + 4096
    elif calibrator == "histogram":
        n_bins = int(params["n_bins"])
        iters = n_ref + n_bins
        mem = 16 * (n_ref + n_bins + n_eval) + 2048
    else:  # pragma: no cover - config validation rejects unknown types
        raise ValueError(calibrator)
    return {"est_iters": iters, "est_mem_bytes": mem}


def _peak_batch_mem(mems, batch_size):
    peak = 0
    for i in range(0, len(mems), batch_size):
        peak = max(peak, sum(mems[i:i + batch_size]))
    return peak


def admit(combos: list[dict], cfg: dict) -> dict:
    """Decide the admitted combo set under both budgets.

    ``combos`` entries: {"calibrator", "group", "n_ref", "n_eval"} in
    deterministic order.  Returns a ledger with per-combo decisions.
    """
    max_iters = cfg["budget"]["max_total_iters"]
    max_mem = cfg["budget"]["max_resident_bytes"]
    batch_size = cfg["scheduler"]["batch_size"]

    ledger = []
    admitted = []
    total_iters = 0
    for combo in combos:
        est = estimate_combo(combo["calibrator"], combo["n_ref"], combo["n_eval"], cfg)
        entry = {**combo, **est}
        cand_iters = total_iters + est["est_iters"]
        cand_mems = [a["est_mem_bytes"] for a in admitted] + [est["est_mem_bytes"]]
        cand_peak = _peak_batch_mem(cand_mems, batch_size)
        if cand_iters > max_iters:
            entry.update({
                "admitted": False,
                "code": E_ITER_BUDGET,
                "deficit": {"iters": cand_iters - max_iters},
                "message": f"admitting {combo['calibrator']}:{combo['group']} would need "
                           f"{cand_iters} total iters > budget {max_iters}",
            })
        elif cand_peak > max_mem:
            entry.update({
                "admitted": False,
                "code": E_MEMORY_BUDGET,
                "deficit": {"bytes": cand_peak - max_mem},
                "message": f"admitting {combo['calibrator']}:{combo['group']} would peak at "
                           f"{cand_peak} resident bytes > budget {max_mem}",
            })
        else:
            entry.update({"admitted": True, "code": None, "deficit": None, "message": "admitted"})
            admitted.append(entry)
            total_iters = cand_iters
        ledger.append(entry)

    peak_mem = _peak_batch_mem([a["est_mem_bytes"] for a in admitted], batch_size)
    return {
        "ledger": ledger,
        "admitted": [a for a in ledger if a["admitted"]],
        "rejected": [a for a in ledger if not a["admitted"]],
        "totals": {
            "est_iters": total_iters,
            "max_total_iters": max_iters,
            "peak_resident_bytes": peak_mem,
            "max_resident_bytes": max_mem,
        },
    }
