"""Orchestration: deterministic concurrent execution, caching, snapshots."""

import os
import signal
import sys
from concurrent.futures import ThreadPoolExecutor

import numpy as np

from . import (
    E_CONFIG_MISMATCH,
    E_SCHEME_UNKNOWN,
    EXIT_CONFIG_MISMATCH,
    EXIT_OK,
    EXIT_SCHEME_UNKNOWN,
    GLOBAL_VERSION,
)
from .cache import ResultCache
from .injection import apply_injections
from .io_utils import (
    atomic_write_json,
    atomic_write_text,
    hash_obj,
    load_json,
    load_jsonl,
)
from .pipeline import FaultError, compute_encode, compute_score, compute_track
from .schemes import SchemeUnknown, all_scheme_names, get_scheme
from .sensitivity import scan_scheme
from .snapshots import ConfigMismatch, SnapshotManager

COMPUTE_STAGES = ("encode", "score", "track")

_STOP_REQUESTED = False


def _handle_signal(signum, frame):
    global _STOP_REQUESTED
    _STOP_REQUESTED = True


def install_signal_handlers():
    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)


def _run_compute_stage(stage, schemes, samples, config, cache, sample_hashes,
                       overrides, faulted_ids, prior, failures, max_workers,
                       reused, recomputed):
    failed_pairs = {(f["scheme"], f["sample_id"]) for f in failures}
    tasks = [(scheme, sample) for scheme in schemes for sample in samples
             if (scheme.name, sample["sample_id"]) not in failed_pairs]

    def work(scheme, sample):
        sid = sample["sample_id"]
        sort_key = (scheme.name, sid, stage)
        cacheable = sid not in faulted_ids
        key = (cache.key_for(scheme, stage, config, sample_hashes[sid])
               if cacheable else None)
        if key is not None:
            hit = cache.get(key)
            if hit is not None:
                return sort_key, {"kind": "reused", "key": key, "result": hit}
        try:
            if stage == "encode":
                override = overrides.get(sid, {})
                result = compute_encode(
                    scheme, sample, config,
                    override.get("base", config["base_freq"]),
                    override.get("scale", config["scale_factor"]))
            elif stage == "score":
                result = compute_score(scheme, sample, config,
                                       prior["encode"][scheme.name][sid])
            else:
                result = compute_track(scheme, sample, config,
                                       prior["encode"][scheme.name][sid],
                                       prior["score"][scheme.name][sid])
        except FaultError as exc:
            return sort_key, {"kind": "fault", "code": exc.code,
                              "message": str(exc)}
        if key is not None:
            cache.put(key, result)
        return sort_key, {"kind": "computed", "key": key, "result": result}

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        outcomes = list(pool.map(lambda task: work(*task), tasks))
    outcomes.sort(key=lambda item: item[0])

    stage_results = {scheme.name: {} for scheme in schemes}
    stage_failures = []
    for (scheme_name, sid, _), outcome in outcomes:
        kind = outcome["kind"]
        if kind == "fault":
            stage_failures.append({
                "scheme": scheme_name,
                "sample_id": sid,
                "stage": stage,
                "code": outcome["code"],
                "message": outcome["message"],
            })
            continue
        entry = {"scheme": scheme_name, "sample_id": sid, "stage": stage,
                 "cache_key": outcome["key"]}
        (reused if kind == "reused" else recomputed).append(entry)
        stage_results[scheme_name][sid] = outcome["result"]
    failures.extend(stage_failures)
    return stage_results, stage_failures


def _stats(values):
    if not values:
        return {"count": 0, "mean": None, "min": None, "max": None, "std": None}
    arr = np.asarray(values, dtype=np.float64)
    return {
        "count": int(arr.size),
        "mean": float(arr.mean()),
        "min": float(arr.min()),
        "max": float(arr.max()),
        "std": float(arr.std()),
    }


def _score_stats(schemes, score_results):
    stats = {}
    for scheme in schemes:
        per_scheme = score_results[scheme.name]
        in_scores, out_scores = [], []
        profile = {}
        in_samples = out_samples = 0
        for sid in sorted(per_scheme):
            res = per_scheme[sid]
            if any(res["in_range"]):
                in_samples += 1
            if not all(res["in_range"]):
                out_samples += 1
            for idx, distance in enumerate(res["distances"]):
                bucket = "in_range" if res["in_range"][idx] else "extrapolated"
                value = res["scores"][idx]
                (in_scores if bucket == "in_range" else out_scores).append(value)
                entry = profile.setdefault(
                    distance, {"in_range": [], "extrapolated": []})
                entry[bucket].append(value)
        distance_profile = {}
        for distance in sorted(profile):
            buckets = profile[distance]
            distance_profile[str(distance)] = {
                name: {"count": len(vals),
                       "mean": (float(np.mean(vals)) if vals else None)}
                for name, vals in sorted(buckets.items())
            }
        stats[scheme.name] = {
            "in_range": _stats(in_scores),
            "extrapolated": _stats(out_scores),
            "sample_counts": {"in_range": in_samples, "extrapolated": out_samples},
            "distance_profile": distance_profile,
        }
    return stats


def _visual_summary(schemes, config, score_results, track_records):
    lines = [
        "Position-Encoding Extrapolation Diagnostics - Visual Summary",
        "train_ctx_len=%d extrap_target_len=%d precision=%s tolerance=%g" % (
            config["train_ctx_len"], config["extrap_target_len"],
            config["precision"], config["tolerance"]),
    ]
    n_bins = 8
    width = config["extrap_target_len"]
    for scheme in schemes:
        lines.append("")
        lines.append("[%s] mean |score deviation| (precision vs float64) "
                     "by relative-distance bucket" % scheme.name)
        bins = [[] for _ in range(n_bins)]
        for sid in sorted(score_results[scheme.name]):
            res = score_results[scheme.name][sid]
            for idx, distance in enumerate(res["distances"]):
                dev = abs(res["scores"][idx] - res["scores_ref"][idx])
                bins[min(n_bins - 1, distance * n_bins // width)].append(dev)
        max_mean = max((float(np.mean(b)) for b in bins if b), default=0.0)
        for idx, bucket in enumerate(bins):
            lo = idx * width // n_bins
            hi = (idx + 1) * width // n_bins
            if bucket:
                mean = float(np.mean(bucket))
                bar = ("#" * (1 + int(39 * mean / max_mean))
                       if max_mean > 0 else "")
                lines.append("  [%5d, %5d)  n=%3d  dev=%.6e  |%s"
                             % (lo, hi, len(bucket), mean, bar))
            else:
                lines.append("  [%5d, %5d)  n=  0  dev=-           |"
                             % (lo, hi))
        exceeded = [t for t in track_records
                    if t["scheme"] == scheme.name and t["exceeded"]]
        lines.append("  tolerance-exceeded samples: %d" % len(exceeded))
    return "\n".join(lines) + "\n"


def run(config_path, samples_path, injections_path, workdir,
        report_path=None, allow_reconfigure=False):
    global _STOP_REQUESTED
    _STOP_REQUESTED = False
    install_signal_handlers()

    config = load_json(config_path)
    scheme_cfg = config.get("scheme", "all")
    names = all_scheme_names() if scheme_cfg == "all" else [scheme_cfg]
    try:
        schemes = [get_scheme(name) for name in names]
    except SchemeUnknown as exc:
        print("%s: %s" % (E_SCHEME_UNKNOWN, exc), file=sys.stderr)
        return EXIT_SCHEME_UNKNOWN

    samples = sorted(load_jsonl(samples_path), key=lambda s: s["sample_id"])
    injection_manifest = load_json(injections_path)
    config_hash = hash_obj(config)
    injection_hash = hash_obj(injection_manifest)
    sample_hashes = {s["sample_id"]: hash_obj(s) for s in samples}
    samples_hash = hash_obj(sample_hashes)

    os.makedirs(workdir, exist_ok=True)
    manager = SnapshotManager(workdir)
    try:
        mode = manager.begin(config_hash, injection_hash, samples_hash)
    except ConfigMismatch:
        if not allow_reconfigure:
            print(E_CONFIG_MISMATCH, file=sys.stderr)
            return EXIT_CONFIG_MISMATCH
        mode = "stale"
    if mode == "stale":
        manager.reset()
    mutated, overrides, hits, faulted_ids = apply_injections(
        samples, injection_manifest)
    cache = ResultCache(os.path.join(workdir, "cache"))
    max_workers = int(config.get("max_workers", 4))

    reused, recomputed, failures = [], [], []
    results = {}
    for stage in COMPUTE_STAGES:
        if manager.stage_completed(stage):
            payload = manager.load_stage(stage)
            results[stage] = payload["results"]
            failures.extend(payload["failures"])
            for scheme_name in sorted(payload["results"]):
                for sid in sorted(payload["results"][scheme_name]):
                    reused.append({
                        "scheme": scheme_name,
                        "sample_id": sid,
                        "stage": stage,
                        "cache_key": None,
                        "source": "snapshot",
                    })
        else:
            stage_results, stage_failures = _run_compute_stage(
                stage, schemes, mutated, config, cache, sample_hashes,
                overrides, faulted_ids, results, failures, max_workers,
                reused, recomputed)
            results[stage] = stage_results
            manager.complete_stage(
                stage, {"results": stage_results, "failures": stage_failures},
                config_hash, injection_hash, samples_hash,
                [s.name for s in schemes], [s["sample_id"] for s in samples])
        if _STOP_REQUESTED:
            return EXIT_OK

    valid_samples = [s for s in mutated if s["sample_id"] not in faulted_ids]
    if manager.stage_completed("sensitivity"):
        sensitivity = manager.load_stage("sensitivity")
    else:
        def scan(scheme):
            return scheme.name, scan_scheme(scheme, config, valid_samples)

        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            scanned = sorted(pool.map(scan, schemes), key=lambda item: item[0])
        sensitivity = {name: result for name, result in scanned}
        manager.complete_stage(
            "sensitivity", sensitivity, config_hash, injection_hash, samples_hash,
            [s.name for s in schemes], [s["sample_id"] for s in valid_samples])
    if _STOP_REQUESTED:
        return EXIT_OK

    track_records = []
    for scheme in schemes:
        per_scheme = results["track"][scheme.name]
        for sid in sorted(per_scheme):
            record = per_scheme[sid]
            if record["has_extrapolation"]:
                track_records.append(record)

    if report_path is None:
        report_path = os.path.join(workdir, "report.json")
    report = {
        "meta": {
            "global_version": GLOBAL_VERSION,
            "config_hash": config_hash,
            "injection_hash": injection_hash,
            "schemes": [{"name": s.name, "version": s.version} for s in schemes],
            "precision": config["precision"],
            "tolerance": config["tolerance"],
            "train_ctx_len": config["train_ctx_len"],
            "extrap_target_len": config["extrap_target_len"],
            "max_workers": max_workers,
        },
        "score_stats": _score_stats(schemes, results["score"]),
        "error_tracking": track_records,
        "sensitivity": sensitivity,
        "injection_hits": hits,
        "cache": {"reused": reused, "recomputed": recomputed},
        "snapshot_version": GLOBAL_VERSION,
        "failures": failures,
    }
    atomic_write_json(report_path, report)
    atomic_write_text(
        os.path.join(workdir, "visual_summary.txt"),
        _visual_summary(schemes, config, results["score"], track_records))
    manager.complete_stage(
        "report", {"report_path": os.path.abspath(report_path)},
        config_hash, injection_hash, samples_hash,
        [s.name for s in schemes], [s["sample_id"] for s in samples])
    return EXIT_OK
