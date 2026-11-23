"""Pipeline orchestration: split -> fit -> apply -> metrics -> snapshots.

Every stage is content-addressed in the cache; every run commits
versioned snapshots of the schedule plan, calibrator parameters and
metric statistics.  Final metrics carry lineage records (cache keys,
config version, node path) that support exact replay.
"""

from __future__ import annotations

import json
import os

from . import invalidation
from .budget import admit
from .cache import FileCache, cache_key
from .config import validate_config
from .data_gen import load_manifest
from .errors import E_MEM_TABLE_LIMIT, WorkbenchError
from .metrics import compute_metrics
from .scheduler import build_tasks, execute_plan, plan_combos
from .snapshots import SnapshotStore
from .split import split_and_preprocess
from .util import content_hash, file_sha256
from .validate import validate_manifest

STAGE = "pipeline"


def _manifest_checksum(rows):
    clean = [{k: v for k, v in r.items() if not k.startswith("_")} for r in rows]
    return content_hash(clean)


def run_pipeline(cfg: dict, rows=None, manifest_path=None) -> dict:
    validate_config(cfg)
    state_dir = cfg["state_dir"]
    out_dir = cfg["output_dir"]
    os.makedirs(out_dir, exist_ok=True)
    cache = FileCache(os.path.join(state_dir, "cache"))
    store = SnapshotStore(os.path.join(state_dir, "snapshots"))
    ver = cfg["config_version"]

    if rows is None:
        manifest_path = manifest_path or cfg["data"]["path"]
        rows = load_manifest(manifest_path)
        manifest_checksum = file_sha256(manifest_path)
    else:
        manifest_checksum = _manifest_checksum(rows)
    validate_manifest(rows, cfg)

    # ---- invalidation plan vs previous config (requirement 7) --------
    last_cfg_path = os.path.join(state_dir, "last_config.json")
    last_cfg = None
    if os.path.exists(last_cfg_path):
        with open(last_cfg_path, "r", encoding="utf-8") as fh:
            last_cfg = json.load(fh)
    change = (invalidation.diff_configs(last_cfg, cfg) if last_cfg
              else {"kind": "initial", "affected_nodes": ["*"],
                    "affected_groups": sorted(cfg["data"]["groups"]),
                    "details": "no previous config; full compute"})

    # ---- node: split + preprocess (cached) ---------------------------
    node_log = []
    split_inputs = {
        "seed": cfg["seed"],
        "ref_ratio": cfg["split"]["ref_ratio"],
        "manifest": manifest_checksum,
        "numeric_features": cfg["data"]["numeric_features"],
        "categorical_features": cfg["data"]["categorical_features"],
    }
    split_key = cache_key("split", split_inputs)
    if cache.has(split_key):
        split_data = cache.get(split_key)["value"]
        node_log.append({"node": "split", "key": split_key, "status": "reused"})
    else:
        split_data = split_and_preprocess(rows, cfg)
        cache.put(split_key, {"value": split_data})
        node_log.append({"node": "split", "key": split_key, "status": "computed"})

    # ---- resource limit: resident probability table ------------------
    groups = sorted(split_data["groups"])
    total_eval = sum(len(split_data["groups"][g]["eval"]["sample_ids"]) for g in groups)
    prob_table_bytes = len(cfg["calibrators"]) * total_eval * 8
    if prob_table_bytes > cfg["limits"]["max_prob_table_bytes"]:
        raise WorkbenchError(
            E_MEM_TABLE_LIMIT, STAGE,
            f"resident probability table needs {prob_table_bytes} bytes > "
            f"limits.max_prob_table_bytes={cfg['limits']['max_prob_table_bytes']}",
            field="limits.max_prob_table_bytes",
            details={"needed": prob_table_bytes,
                     "limit": cfg["limits"]["max_prob_table_bytes"]},
        )

    # ---- combo planning + per-combo cache probe ----------------------
    combos = plan_combos(cfg, groups)
    combo_entries = []
    to_compute = []
    for combo in combos:
        cal, group = combo["calibrator"], combo["group"]
        gdata = split_data["groups"][group]
        fit_key = cache_key(f"fit:{cal}:{group}",
                            {"split": split_key, "params": cfg["calibrators"][cal]})
        apply_key = cache_key(f"apply:{cal}:{group}", {"fit": fit_key})
        metrics_key = cache_key(f"metrics:{cal}:{group}",
                                {"apply": apply_key, "ece_bins": cfg["metrics"]["ece_bins"]})
        entry = {
            **combo,
            "n_ref": len(gdata["ref"]["sample_ids"]),
            "n_eval": len(gdata["eval"]["sample_ids"]),
            "fit_key": fit_key,
            "apply_key": apply_key,
            "metrics_key": metrics_key,
            "cached": cache.has(fit_key) and cache.has(apply_key) and cache.has(metrics_key),
        }
        combo_entries.append(entry)
        if not entry["cached"]:
            to_compute.append(entry)

    # ---- budget admission over combos that need compute --------------
    budget = admit(to_compute, cfg)
    admitted_ids = {(e["calibrator"], e["group"]) for e in budget["admitted"]}
    ledger = []
    for entry in combo_entries:
        if entry["cached"]:
            ledger.append({"calibrator": entry["calibrator"], "group": entry["group"],
                           "decision": "cached", "code": None,
                           "est_iters": None, "est_mem_bytes": None, "deficit": None,
                           "message": "reused from content-addressed cache"})
    for e in budget["ledger"]:
        ledger.append({"calibrator": e["calibrator"], "group": e["group"],
                       "est_iters": e["est_iters"], "est_mem_bytes": e["est_mem_bytes"],
                       "code": e["code"], "deficit": e["deficit"], "message": e["message"],
                       "decision": "admitted" if e["admitted"] else "rejected"})

    # ---- deterministic parallel execution ----------------------------
    compute_entries = [e for e in combo_entries
                       if not e["cached"] and (e["calibrator"], e["group"]) in admitted_ids]
    tasks = build_tasks(compute_entries, split_data, cfg)
    workers = cfg["scheduler"]["workers"]
    batch_size = cfg["scheduler"]["batch_size"]
    results = execute_plan(tasks, workers, batch_size)

    errors = []
    result_by_combo = {}
    for entry, res in zip(compute_entries, results):
        key = (entry["calibrator"], entry["group"])
        if res["ok"]:
            cache.put(entry["fit_key"], {"value": {"model": res["model"], "trace": res["trace"]}})
            cache.put(entry["apply_key"], {"value": {"detail": res["detail"]}})
            cache.put(entry["metrics_key"], {"value": res["metrics"]})
            node_log.append({"node": f"fit:{entry['calibrator']}:{entry['group']}",
                             "key": entry["fit_key"], "status": "computed"})
            result_by_combo[key] = res
        else:
            err = dict(res["error"])
            err["details"] = {**err.get("details", {}),
                              "calibrator": entry["calibrator"], "group": entry["group"]}
            errors.append(err)
            node_log.append({"node": f"fit:{entry['calibrator']}:{entry['group']}",
                             "key": entry["fit_key"], "status": "failed"})

    # ---- assemble per-combo outputs (cached or fresh) ----------------
    per_combo = {}
    for entry in combo_entries:
        key = (entry["calibrator"], entry["group"])
        if entry["cached"]:
            node_log.append({"node": f"fit:{entry['calibrator']}:{entry['group']}",
                             "key": entry["fit_key"], "status": "reused"})
            per_combo[key] = {
                "model": cache.get(entry["fit_key"])["value"]["model"],
                "trace": cache.get(entry["fit_key"])["value"]["trace"],
                "detail": cache.get(entry["apply_key"])["value"]["detail"],
                "metrics": cache.get(entry["metrics_key"])["value"],
            }
        elif key in result_by_combo:
            res = result_by_combo[key]
            per_combo[key] = {"model": res["model"], "trace": res["trace"],
                              "detail": res["detail"], "metrics": res["metrics"]}

    # ---- overall metrics per calibrator + lineage --------------------
    lineage = []
    group_metrics = {}
    overall_metrics = {}
    for cal in sorted(cfg["calibrators"]):
        all_probs, all_y, group_keys = [], [], []
        for group in groups:
            combo = per_combo.get((cal, group))
            if combo is None:
                continue
            entry = next(e for e in combo_entries
                         if e["calibrator"] == cal and e["group"] == group)
            group_keys.append(entry["metrics_key"])
            m = combo["metrics"]
            group_metrics[f"{cal}:{group}"] = m
            all_probs.extend(combo["detail"]["probs"])
            all_y.extend(combo["detail"]["y"])
            lineage.append({
                "calibrator": cal, "group": group,
                "config_version": ver,
                "node_path": ["split", f"fit:{cal}:{group}",
                              f"apply:{cal}:{group}", f"metrics:{cal}:{group}"],
                "cache_keys": {"split": split_key, "fit": entry["fit_key"],
                               "apply": entry["apply_key"], "metrics": entry["metrics_key"]},
                "ece": m["ece"], "brier": m["brier"],
            })
        if not all_probs:
            continue
        overall_key = cache_key(f"metrics:__overall__:{cal}",
                                {"group_metrics": sorted(group_keys),
                                 "ece_bins": cfg["metrics"]["ece_bins"]})
        if cache.has(overall_key):
            overall = cache.get(overall_key)["value"]
            node_log.append({"node": f"metrics:__overall__:{cal}",
                             "key": overall_key, "status": "reused"})
        else:
            overall = compute_metrics(all_probs, all_y, cfg["metrics"]["ece_bins"])
            cache.put(overall_key, {"value": overall})
            node_log.append({"node": f"metrics:__overall__:{cal}",
                             "key": overall_key, "status": "computed"})
        overall_metrics[cal] = overall
        lineage.append({
            "calibrator": cal, "group": "__overall__",
            "config_version": ver,
            "node_path": ["split", f"apply:{cal}:*", f"metrics:__overall__:{cal}"],
            "cache_keys": {"split": split_key, "metrics": overall_key,
                           "group_metrics": list(group_keys)},
            "ece": overall["ece"], "brier": overall["brier"],
        })

    # ---- versioned snapshots (whole replacement) ---------------------
    plan_body = {
        "config_version": ver,
        "workers": workers,
        "batch_size": batch_size,
        "combos": [{"index": i, **c} for i, c in enumerate(combos)],
        "batches": [[t["index"] for t in tasks[i:i + batch_size]]
                    for i in range(0, len(tasks), batch_size)],
        "worker_assignment": {str(t["index"]): t["index"] % workers for t in tasks},
        "budget": budget["totals"],
        "ledger": ledger,
    }
    calibrators_body = {
        "config_version": ver,
        "models": {f"{cal}:{group}": per_combo[(cal, group)]["model"]
                   for (cal, group) in sorted(per_combo)},
        "traces": {f"{cal}:{group}": per_combo[(cal, group)]["trace"]
                   for (cal, group) in sorted(per_combo)},
    }
    metrics_body = {
        "config_version": ver,
        "per_group": group_metrics,
        "overall": overall_metrics,
        "lineage": lineage,
    }
    ptr_plan = store.commit("plan", plan_body)
    ptr_cals = store.commit("calibrators", calibrators_body)
    ptr_metrics = store.commit("metrics", metrics_body)

    os.makedirs(state_dir, exist_ok=True)
    with open(last_cfg_path, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, sort_keys=True, indent=2)

    # ---- report data --------------------------------------------------
    report = {
        "config_version": ver,
        "change": change,
        "groups": groups,
        "calibrators": sorted(cfg["calibrators"]),
        "plan": plan_body,
        "snapshots": {"plan": ptr_plan, "calibrators": ptr_cals, "metrics": ptr_metrics},
        "node_log": node_log,
        "errors": errors,
        "per_combo": {
            f"{cal}:{group}": {
                "metrics": per_combo[(cal, group)]["metrics"],
                "detail": per_combo[(cal, group)]["detail"],
                "trace": per_combo[(cal, group)]["trace"],
            }
            for (cal, group) in sorted(per_combo)
        },
        "overall": overall_metrics,
        "lineage": lineage,
        "split_key": split_key,
        "manifest_checksum": manifest_checksum,
        "status": "ok" if not errors else "completed_with_errors",
    }
    with open(os.path.join(out_dir, "report_data.json"), "w", encoding="utf-8") as fh:
        json.dump(report, fh, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return report
