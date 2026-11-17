"""Budget & scheduling report assembly and the fixed-format summary line."""

from . import REPORT_VERSION, SNAPSHOT_VERSION
from .errors import E_BUDGET_EXHAUSTED, E_TASK_FAILED


def build_task_entries(specs, results, exhausted_keys, failures):
    failed_by_key = {}
    for f in failures:
        if f.get("task_key"):
            failed_by_key[f["task_key"]] = f
    exhausted = set(exhausted_keys)
    entries = []
    for spec in specs:  # enqueue (stable-key) order
        key = spec.key
        base = {
            "task_key": key,
            "model": spec.model,
            "params": spec.params,
            "seed": spec.seed,
            "enqueue_seq": spec.enqueue_seq,
            "estimated_ms": getattr(spec, "estimated_ms", None),
        }
        if key in results:
            r = results[key]
            base.update({
                "status": r["status"],
                "code": r["code"],
                "timed_out": r["timed_out"],
                "folds": r["folds"],
                "folds_completed": r["folds_completed"],
                "losses": r["losses"],
                "mean_accuracy": r["mean_accuracy"],
                "billed_ms": r["billed_ms"],
                "schedule": {
                    "slot": getattr(spec, "slot", None),
                    "virtual_start_ms": getattr(spec, "virtual_start_ms", None),
                    "virtual_end_ms": getattr(spec, "virtual_end_ms", None),
                },
                "observability": r.get("observability"),
            })
        elif key in exhausted:
            base.update({
                "status": "exhausted",
                "code": E_BUDGET_EXHAUSTED,
                "timed_out": False,
                "folds": [],
                "folds_completed": 0,
                "losses": [],
                "mean_accuracy": None,
                "billed_ms": 0.0,
                "schedule": None,
                "observability": None,
            })
        elif key in failed_by_key:
            base.update({
                "status": "failed",
                "code": E_TASK_FAILED,
                "timed_out": False,
                "folds": [],
                "folds_completed": 0,
                "losses": [],
                "mean_accuracy": None,
                "billed_ms": 0.0,
                "schedule": None,
                "observability": None,
            })
        else:
            base.update({
                "status": "pending",
                "code": None,
                "timed_out": False,
                "folds": [],
                "folds_completed": 0,
                "losses": [],
                "mean_accuracy": None,
                "billed_ms": 0.0,
                "schedule": None,
                "observability": None,
            })
        entries.append(base)
    return entries


def budget_summary(cfg, plan, results):
    global_ms = float(cfg["scheduler"]["global_budget_ms"])
    consumed = 0.0
    for key in sorted(results):
        consumed += results[key]["billed_ms"]
    return {
        "global_ms": global_ms,
        "task_quota_ms": float(cfg["scheduler"]["task_quota_ms"]),
        "reserved_ms": plan["reserved_ms"] if plan else 0.0,
        "consumed_ms": consumed,
        "remaining_ms": global_ms - consumed,
    }


def build_report(cfg, config_hash, data_fingerprint, specs, results, plan,
                 failures, observability):
    entries = build_task_entries(specs, results,
                                 plan["exhausted"] if plan else [], failures)
    completion_order = [spec.key for spec in specs if spec.key in results]
    budget = budget_summary(cfg, plan or {"reserved_ms": 0.0}, results)
    report = {
        "report_version": REPORT_VERSION,
        "snapshot_version": SNAPSHOT_VERSION,
        "config_hash": config_hash,
        "data_fingerprint": data_fingerprint,
        "tasks": entries,
        "completion_order": completion_order,
        "decision_log": plan["decisions"] if plan else [],
        "budget": budget,
        "budget_exhausted": list(plan["exhausted"]) if plan else [],
        "failures": failures,
        "observability": observability,
    }
    report["summary_line"] = summary_line(report)
    return report


def summary_line(report):
    tasks = report["tasks"]
    completed = [t for t in tasks if t["status"] in ("ok", "timeout")]
    timeouts = [t for t in tasks if t["timed_out"]]
    exhausted = report["budget_exhausted"]
    failed = report["failures"]
    ok_means = [t["mean_accuracy"] for t in tasks
                if t["status"] == "ok" and t["mean_accuracy"] is not None]
    mean_acc = sum(ok_means) / len(ok_means) if ok_means else 0.0
    budget = report["budget"]
    return (
        "SUMMARY|config_hash=%s|tasks=%d|completed=%d|timeout=%d"
        "|exhausted=%d|failed=%d|consumed_ms=%.3f|remaining_ms=%.3f"
        "|mean_accuracy=%.6f|snapshot_version=%d"
        % (
            report["config_hash"][:12],
            len(tasks),
            len(completed),
            len(timeouts),
            len(exhausted),
            len(failed),
            budget["consumed_ms"],
            budget["remaining_ms"],
            mean_acc,
            report["snapshot_version"],
        )
    )
