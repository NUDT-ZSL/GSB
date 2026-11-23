"""Cross-module consistency self-checks and lineage replay (req. 7, 8).

Checks:
  1. sample_id sets are consistent across stages: per group, every apply
     detail table covers a subset of the manifest group ids with no
     duplicates;
  2. every metric is exactly recomputable from its per-sample detail;
  3. snapshot versions are cross-consistent: pointers exist, checksums
     verify against stored bodies, and all snapshots share the run's
     config_version;
  4. lineage replay: each final metric record is recomputed from the
     cached artifacts named by its cache keys; a missing key or a
     mismatch raises E_LINEAGE_BROKEN.
Any inconsistency raises WorkbenchError(E_INCONSISTENT) (or
E_LINEAGE_BROKEN for replay) with full details.
"""

from __future__ import annotations

import json
import os

from .cache import FileCache
from .errors import E_INCONSISTENT, E_LINEAGE_BROKEN, WorkbenchError
from .metrics import compute_metrics
from .util import content_hash

STAGE = "selfcheck"


def _fail(check, message, details):
    raise WorkbenchError(E_INCONSISTENT, STAGE, message,
                         details={"check": check, **details})


def run_selfcheck(cfg: dict, rows: list[dict], report: dict) -> dict:
    checks = []

    # 1. sample_id set consistency --------------------------------------
    manifest_ids = {}
    for row in rows:
        manifest_ids.setdefault(row["group"], set()).add(row["sample_id"])
    for combo_key, payload in report["per_combo"].items():
        _, group = combo_key.split(":", 1)
        detail_ids = payload["detail"]["sample_ids"]
        if len(detail_ids) != len(set(detail_ids)):
            _fail("sample_id_sets", f"{combo_key}: duplicate ids in apply detail",
                  {"combo": combo_key})
        if not set(detail_ids) <= manifest_ids[group]:
            _fail("sample_id_sets", f"{combo_key}: apply detail ids not in manifest group",
                  {"combo": combo_key})
    checks.append({"check": "sample_id_sets", "ok": True})

    # 2. metrics recomputable from details -------------------------------
    for combo_key, payload in report["per_combo"].items():
        detail = payload["detail"]
        recomputed = compute_metrics(detail["probs"], detail["y"],
                                     payload["metrics"]["reliability"]["n_bins"])
        for name in ("ece", "brier"):
            if recomputed[name] != payload["metrics"][name]:
                _fail("metrics_recompute",
                      f"{combo_key}: {name} mismatch "
                      f"({payload['metrics'][name]} != {recomputed[name]})",
                      {"combo": combo_key, "metric": name})
    checks.append({"check": "metrics_recompute", "ok": True})

    # 3. snapshot cross-version consistency ------------------------------
    snap_root = os.path.join(cfg["state_dir"], "snapshots")
    versions = {}
    for kind in ("plan", "calibrators", "metrics"):
        ptr_path = os.path.join(snap_root, kind, "current.json")
        if not os.path.exists(ptr_path):
            _fail("snapshot_versions", f"missing snapshot pointer for {kind}", {"kind": kind})
        with open(ptr_path, "r", encoding="utf-8") as fh:
            ptr = json.load(fh)
        with open(os.path.join(snap_root, kind, ptr["file"]), "r", encoding="utf-8") as fh:
            record = json.load(fh)
        if record["version"] != ptr["version"] or record["checksum"] != ptr["checksum"]:
            _fail("snapshot_versions", f"{kind}: pointer/record mismatch",
                  {"kind": kind, "ptr": ptr, "record_version": record["version"]})
        if content_hash(record["body"]) != ptr["checksum"]:
            _fail("snapshot_versions", f"{kind}: body checksum mismatch", {"kind": kind})
        if record["body"].get("config_version") != report["config_version"]:
            _fail("snapshot_versions",
                  f"{kind}: config_version {record['body'].get('config_version')} != "
                  f"report {report['config_version']}", {"kind": kind})
        versions[kind] = ptr["version"]
    checks.append({"check": "snapshot_versions", "ok": True, "versions": versions})

    return {"ok": True, "checks": checks}


def replay_lineage(cfg: dict, report: dict) -> dict:
    """Recompute every final metric from its recorded cache keys."""
    cache = FileCache(os.path.join(cfg["state_dir"], "cache"))
    replayed = []
    for rec in report["lineage"]:
        label = f"{rec['calibrator']}:{rec['group']}"
        keys = rec["cache_keys"]
        try:
            if rec["group"] == "__overall__":
                metrics_node = cache.get(keys["metrics"])["value"]
                probs, y = [], []
                for group_metrics_key in keys["group_metrics"]:
                    detail = _detail_for_metrics_key(cache, report, group_metrics_key)
                    probs.extend(detail["probs"])
                    y.extend(detail["y"])
                recomputed = compute_metrics(probs, y, metrics_node["reliability"]["n_bins"])
            else:
                metrics_node = cache.get(keys["metrics"])["value"]
                detail = cache.get(keys["apply"])["value"]["detail"]
                recomputed = compute_metrics(detail["probs"], detail["y"],
                                             metrics_node["reliability"]["n_bins"])
        except FileNotFoundError as exc:
            raise WorkbenchError(
                E_LINEAGE_BROKEN, STAGE,
                f"{label}: cached artifact missing ({exc.filename})",
                details={"record": rec},
            )
        except KeyError as exc:
            raise WorkbenchError(
                E_LINEAGE_BROKEN, STAGE,
                f"{label}: lineage chain broken at key {exc}",
                details={"record": rec},
            )
        for name in ("ece", "brier"):
            if recomputed[name] != rec[name]:
                raise WorkbenchError(
                    E_LINEAGE_BROKEN, STAGE,
                    f"{label}: replayed {name}={recomputed[name]} != recorded {rec[name]}",
                    details={"record": rec, "recomputed": recomputed[name]},
                )
        replayed.append({"combo": label, "ece": rec["ece"], "brier": rec["brier"], "ok": True})
    return {"ok": True, "replayed": replayed}


def _detail_for_metrics_key(cache, report, metrics_key):
    for rec in report["lineage"]:
        if rec["group"] != "__overall__" and rec["cache_keys"].get("metrics") == metrics_key:
            return cache.get(rec["cache_keys"]["apply"])["value"]["detail"]
    raise KeyError(metrics_key)
