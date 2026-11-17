"""并发编排、退出信号处理与诊断报告组装。"""

import os
import signal
import threading

from . import injection as inj
from .atomicio import atomic_write_json
from .cache import CacheBook
from .config import config_hash, group_indices, groups_hash
from .errors import RankerError
from .fixedpoint import FixedSpec
from .loader import load_candidates
from .cache import normalization_key
from .pipeline import prepare_candidates, run_request
from .snapshot import SnapshotStore

STOP_EVENT = threading.Event()


def _install_signal_handlers():
    def handler(signum, frame):
        STOP_EVENT.set()

    for name in ("SIGTERM", "SIGINT"):
        sig = getattr(signal, name, None)
        if sig is not None:
            try:
                signal.signal(sig, handler)
            except (ValueError, OSError):
                pass


class RunOptions:
    def __init__(self, snapshot_dir, injection_path="", incremental=False,
                 stop_after_requests=None, install_handlers=True):
        self.snapshot_dir = snapshot_dir
        self.injection_path = injection_path
        self.incremental = incremental
        self.stop_after_requests = stop_after_requests
        self.install_handlers = install_handlers


def run(candidates_path, config, groups_table, options):
    if options.install_handlers:
        _install_signal_handlers()
    STOP_EVENT.clear()
    if os.environ.get("RANKER_STOP_AFTER"):
        options.stop_after_requests = int(os.environ["RANKER_STOP_AFTER"])

    groups_resolved = group_indices(groups_table, config["feature_names"])
    spec = FixedSpec(config["frac_bits"], config["levels"], config["rounding"])
    injection_bundle = inj.load_injection(options.injection_path)
    store = SnapshotStore(options.snapshot_dir)
    manifest, fresh = store.verify_or_init(
        config_hash(config),
        groups_hash(groups_table),
        injection_bundle["hash"],
        allow_mismatch=options.incremental,
    )

    recall_names = [n for n in config["feature_names"] if n.startswith("recall_")]
    dense_names = [n for n in config["feature_names"] if n.startswith("dense_")]
    sparse_names = [n for n in config["feature_names"] if n.startswith("sparse_")]
    requests, load_failures, _dropped = load_candidates(
        candidates_path, len(dense_names), recall_names, sparse_names
    )
    if fresh:
        for failure in load_failures:
            store.append_failure(failure)
    elif options.incremental:
        retained = [f for f in store.read_failures() if f["stage"] != "load"]
        atomic_write_json(store.failures_path, retained + load_failures)

    cache_book = CacheBook()
    store_lock = threading.Lock()
    request_by_id = {req["request_id"]: req for req in requests}
    valid_cursor = []
    results = []
    for rid in manifest["cursor"]:
        if rid not in request_by_id:
            continue
        req = request_by_id[rid]
        artifact = store.load_request_artifact(rid)
        if artifact is None:
            continue
        prepared = prepare_candidates(
            req, config["feature_names"],
            [n for n in config["feature_names"] if n.startswith("recall_")],
            [n for n in config["feature_names"] if n.startswith("dense_")],
            [n for n in config["feature_names"] if n.startswith("sparse_")],
        )
        items = inj.injections_for(injection_bundle["items"], rid)
        injection_token = sorted(item["id"] for item in items)
        expected_norm_key = normalization_key(prepared, config, spec.as_dict(), injection_token)
        if artifact["cache_keys"].get("normalize") != expected_norm_key:
            cache_book.record(rid, "normalize", "invalidated", expected_norm_key)
            continue
        for stage, key in artifact["cache_keys"].items():
            cache_book.record(rid, stage, "hit", key)
        valid_cursor.append(rid)
        view = _request_view(artifact)
        view["cache_events"] = _sorted_events(cache_book.for_request(rid))
        results.append({
            "_order": req["candidates"][0]["line_no"],
            "result": view,
            "failure": None,
        })
    manifest["cursor"] = valid_cursor
    completed_ids = set(valid_cursor)
    pending = [req for req in requests if req["request_id"] not in completed_ids]
    signalled = False
    processed = 0

    max_workers = config["max_workers"]
    if max_workers == 1:
        for req in pending:
            outcome = _process_one(req, config, groups_resolved, groups_table, spec,
                                   injection_bundle["items"], cache_book, store, manifest,
                                   store_lock)
            results.append(outcome)
            processed += 1
            if _should_stop(options, processed):
                signalled = True
                break
    else:
        from concurrent.futures import ThreadPoolExecutor

        ordered = list(pending)
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            future_map = {
                pool.submit(
                    _process_one, req, config, groups_resolved, groups_table, spec,
                    injection_bundle["items"], cache_book, store, manifest, store_lock,
                ): idx
                for idx, req in enumerate(ordered)
            }
            for future in list(future_map):
                results.append(future.result())
                processed += 1
                if _should_stop(options, processed):
                    signalled = True
                    STOP_EVENT.set()
                    break
            for future in future_map:
                future.cancel()

    results.sort(key=lambda item: item["_order"])
    ordered_results = [item["result"] for item in results if item["result"] is not None]

    all_failures = store.read_failures()
    seen = {(f["line_no"], f.get("request_id"), f["stage"], f["message"]) for f in all_failures}
    for item in results:
        failure = item["failure"]
        if failure is None:
            continue
        key = (failure["line_no"], failure.get("request_id"), failure["stage"], failure["message"])
        if key not in seen:
            seen.add(key)
            all_failures.append(failure)
    all_failures.sort(key=lambda f: (f["line_no"], f.get("request_id") or "", f["message"]))

    report = build_report(
        config, groups_table, injection_bundle, manifest, ordered_results,
        all_failures, cache_book, signalled,
    )
    if signalled:
        report["exit_code"] = 0
        report["exit_reason"] = "stop_signal_after_stage_commit"
    elif manifest.get("completed"):
        report["exit_code"] = 0
        report["exit_reason"] = "completed"
    else:
        if _all_done(manifest, requests):
            store.mark_completed(manifest)
            report["exit_code"] = 0
            report["exit_reason"] = "completed"
        else:
            report["exit_code"] = 0
            report["exit_reason"] = "partial"
    report["manifest"] = {
        "global_version": manifest["global_version"],
        "cursor_size": len(manifest["cursor"]),
        "stage_versions": manifest["stage_versions"],
        "completed": manifest["completed"],
    }
    return report, manifest, signalled


def _should_stop(options, processed):
    if STOP_EVENT.is_set():
        return True
    return options.stop_after_requests is not None and processed >= options.stop_after_requests


def _all_done(manifest, requests):
    return len(manifest["cursor"]) == len(requests)


def _process_one(req, config, groups_resolved, groups_table, spec, injection_items,
                 cache_book, store, manifest, store_lock):
    rid = req["request_id"]
    order = req["candidates"][0]["line_no"]
    items = inj.injections_for(injection_items, rid)
    existing = store.load_request_artifact(rid)
    try:
        artifact, stage_versions = run_request(
            req, config, groups_resolved, groups_hash(groups_table), spec,
            items, cache_book, existing,
        )
        with store_lock:
            store.write_request_artifact(rid, artifact)
            store.commit_cursor(manifest, rid, stage_versions)
        result = _request_view(artifact)
        result["cache_events"] = _sorted_events(cache_book.for_request(rid))
        return {"_order": order, "result": result, "failure": None}
    except RankerError as exc:
        failure = {
            "line_no": order,
            "request_id": rid,
            "stage": exc.stage,
            "code": exc.code,
            "error_type": type(exc).__name__,
            "message": str(exc),
            "injection_ids": [item["id"] for item in items],
        }
        with store_lock:
            store.append_failure(failure)
        return {"_order": order, "result": None, "failure": failure}


STAGE_ORDER = {"normalize": 0, "fuse": 1, "rerank": 2}


def _sorted_events(events):
    return sorted(events, key=lambda e: (STAGE_ORDER.get(e["stage"], 9), e["cache_key"], e["status"]))


def _request_view(artifact):
    return {
        "request_id": artifact["request_id"],
        "item_ids": artifact["item_ids"],
        "stable_keys": artifact["stable_keys"],
        "stage_versions": artifact["stage_versions"],
        "cache": artifact["cache_keys"],
        "cache_events": None,
        "ranking": artifact["rerank"]["ranking"],
        "penalties": artifact["rerank"]["penalties"],
        "fused_scores": [f["score"] for f in artifact["fused"]],
        "norm_codes": artifact["norm"]["codes"],
        "norm_stats": artifact["norm"]["stats"],
        "residuals": artifact["residuals"],
        "diagnostics": artifact["diagnostics"],
        "injection_hits": artifact["injection_hits"],
    }


def build_report(config, groups_table, injection_bundle, manifest, results,
                 failures, cache_book, signalled):
    report = {
        "config_fingerprint": config_hash(config),
        "groups_fingerprint": groups_hash(groups_table),
        "injection_fingerprint": injection_bundle["hash"],
        "fixed_point_meta": {
            "frac_bits": config["frac_bits"],
            "higher_frac_bits": config["frac_bits"] + 1,
            "levels": config["levels"],
            "rounding": config["rounding"],
            "temperature": config["temperature"],
            "score_clip": config["score_clip"],
        },
        "requests": results,
        "error_attribution_summary": _error_summary(results),
        "cache": {
            "totals": cache_book.totals,
            "per_request": cache_book.entries,
            "reuse_requests": sorted(
                rid for rid, ev in cache_book.entries.items()
                if all(e["status"] == "hit" for e in ev)
            ),
            "recompute_requests": sorted(
                rid for rid, ev in cache_book.entries.items()
                if any(e["status"] in ("miss", "recompute") for e in ev)
            ),
        },
        "failures": failures,
        "injection_items": injection_bundle["items"],
        "signalled": signalled,
    }
    return report


def _error_summary(results):
    summary = {
        "boundary_sensitive_requests": [],
        "first_over_tolerance_counts": {},
        "rank_diff_requests": [],
    }
    for result in results:
        diag = result["diagnostics"]
        if diag["boundary_sensitive"]:
            summary["boundary_sensitive_requests"].append(result["request_id"])
        for stage, count in diag["first_over_tolerance_summary"].items():
            summary["first_over_tolerance_counts"][stage] = (
                summary["first_over_tolerance_counts"].get(stage, 0) + count
            )
        if diag["rank_diffs_sorted"]:
            summary["rank_diff_requests"].append(result["request_id"])
    return summary
