"""Staged snapshots with atomic replace (tmp file + os.replace)."""

import json
import os

from . import SNAPSHOT_VERSION
from .clock import monotonic_ns

SNAPSHOT_NAME = "snapshot.json"
RESULTS_DIR = "results"
REPORT_NAME = "report.json"


def atomic_write_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=True, sort_keys=True)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def snapshot_path(out_dir):
    return os.path.join(out_dir, SNAPSHOT_NAME)


def result_path(out_dir, task_key):
    safe = task_key.replace("/", "_").replace(" ", "_")
    return os.path.join(out_dir, RESULTS_DIR, safe + ".json")


def report_path(out_dir):
    return os.path.join(out_dir, REPORT_NAME)


def write_snapshot(out_dir, phase, config_hash, data_fingerprint,
                   completed_keys, budget_consumed_ms, summary, cursor):
    payload = {
        "version": SNAPSHOT_VERSION,
        "phase": phase,
        "config_hash": config_hash,
        "data_fingerprint": data_fingerprint,
        "completed": list(completed_keys),
        "budget_consumed_ms": budget_consumed_ms,
        "summary": summary,
        "cursor": cursor,
        "written_monotonic_ns": monotonic_ns(),
    }
    atomic_write_json(snapshot_path(out_dir), payload)
    return payload


def load_snapshot(out_dir):
    path = snapshot_path(out_dir)
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def load_completed_results(out_dir, completed_keys):
    """Reloads per-task results written before the snapshot. Missing or
    corrupt entries are simply recomputed (deterministic)."""
    results = {}
    for key in completed_keys:
        path = result_path(out_dir, key)
        if not os.path.exists(path):
            continue
        try:
            with open(path, "r", encoding="utf-8") as fh:
                results[key] = json.load(fh)
        except (OSError, ValueError):
            continue
    return results
