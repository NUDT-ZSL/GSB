"""Deterministic parallel scheduling of calibrator x group combos (req. 4).

Determinism contract:
  * combos are ordered by (calibrator, group) ascending;
  * tasks are chunked into batches of ``batch_size`` in that order;
  * batches are dispatched in order to a local process pool; within a
    batch, results are gathered with ``pool.map`` (order-preserving);
  * results are merged strictly by task index, so the final ordering is
    identical to serial execution and every combo's fit/apply output is
    byte-identical to running it alone;
  * worker exceptions are caught, wrapped as structured dicts with a
    business code, stage name and combo attribution, and returned to the
    main process -- a failing combo never disappears silently.
"""

from __future__ import annotations

import multiprocessing as mp
import traceback

from .calibrators.core import APPLIERS, FITTERS
from .errors import E_WORKER_FAILURE, WorkbenchError
from .metrics import compute_metrics

STAGE_FIT = "fit"
STAGE_APPLY = "apply"


def plan_combos(cfg: dict, groups: list[str]) -> list[dict]:
    """Deterministic combo list: sorted by (calibrator, group)."""
    combos = []
    for cal in sorted(cfg["calibrators"]):
        for group in sorted(groups):
            combos.append({"calibrator": cal, "group": group})
    return combos


def build_tasks(combos: list[dict], split_data: dict, cfg: dict) -> list[dict]:
    """Materialize picklable task payloads in combo order."""
    tasks = []
    for idx, combo in enumerate(combos):
        gdata = split_data["groups"][combo["group"]]
        tasks.append({
            "index": idx,
            "calibrator": combo["calibrator"],
            "group": combo["group"],
            "cal_params": cfg["calibrators"][combo["calibrator"]],
            "ece_bins": cfg["metrics"]["ece_bins"],
            "ref": gdata["ref"],
            "eval": gdata["eval"],
        })
    return tasks


def run_combo_task(task: dict) -> dict:
    """Fit + apply + metrics for one combo.  Runs in worker processes."""
    cal = task["calibrator"]
    group = task["group"]
    try:
        ref, ev = task["ref"], task["eval"]
        model, trace = FITTERS[cal](ref["scores"], ref["y"], ref["sample_ids"],
                                    task["cal_params"])
        probs = APPLIERS[cal](model, ev["scores"])
        metrics = compute_metrics(probs, ev["y"], task["ece_bins"])
        return {
            "ok": True,
            "index": task["index"],
            "calibrator": cal,
            "group": group,
            "model": model,
            "trace": trace,
            "detail": {"sample_ids": ev["sample_ids"], "probs": probs, "y": ev["y"]},
            "metrics": metrics,
        }
    except WorkbenchError as exc:  # structured business error passthrough
        err = exc.to_dict()
    except Exception as exc:  # noqa: BLE001 - workers must never die silently
        err = WorkbenchError(
            E_WORKER_FAILURE, STAGE_FIT, f"{type(exc).__name__}: {exc}",
            details={"traceback": traceback.format_exc(limit=8)},
        ).to_dict()
    return {
        "ok": False,
        "index": task["index"],
        "calibrator": cal,
        "group": group,
        "error": err,
    }


def execute_plan(tasks: list[dict], workers: int, batch_size: int) -> list[dict]:
    """Execute tasks in deterministic batches; merge by task index."""
    if not tasks:
        return []
    batches = [tasks[i:i + batch_size] for i in range(0, len(tasks), batch_size)]
    results = {}
    if workers <= 1 or len(tasks) == 1:
        for batch in batches:
            for task in batch:
                res = run_combo_task(task)
                results[res["index"]] = res
    else:
        ctx = mp.get_context("spawn")
        with ctx.Pool(processes=workers) as pool:
            for batch in batches:
                for res in pool.map(run_combo_task, batch):
                    results[res["index"]] = res
    return [results[i] for i in sorted(results)]
