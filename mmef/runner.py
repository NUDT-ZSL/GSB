"""Orchestration: concurrent rounds, snapshots, recovery, report (req 4-8)."""
from __future__ import annotations

import os
import signal
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from . import (
    E_CONFIG_MISMATCH,
    E_FUSION,
    E_TRACKING,
    SNAPSHOT_VERSION,
    VERSION,
)
from .errors import StageError
from .fusion import (
    advance_round,
    first_exceed_stage,
    is_boundary_sensitive,
    prepare_sample,
)
from .loader import load_config, load_relations, load_samples
from .snapshot import atomic_write_json, load_snapshot, save_snapshot, stable_hash

_STOP = threading.Event()


def _install_signal_handlers():
    def handler(signum, frame):  # noqa: ARG001
        _STOP.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, handler)
        except (ValueError, OSError):  # not in main thread / unsupported
            pass


def sort_key(sample_name: str, round_idx: int) -> str:
    """Stable ordering key derived from sample name and round index."""
    return f"{sample_name}#{round_idx:04d}"


def execute(data_dir, out_dir, concurrency=None) -> int:
    """Run the full pipeline. Returns the process exit code."""
    data_dir = Path(data_dir)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    _install_signal_handlers()

    cfg = load_config(data_dir / "config.json")
    if concurrency:
        cfg["concurrency"] = int(concurrency)
    relations = load_relations(data_dir / "relations.json")
    cfg_hash = stable_hash(cfg)
    rel_hash = stable_hash(relations)

    snap_path = out_dir / "snapshot.json"
    report_path = out_dir / "report.json"

    start_round = 0
    stored_scores: dict = {}
    snap = load_snapshot(snap_path)
    if snap is not None:
        if snap.get("version") != SNAPSHOT_VERSION:
            pass  # version change -> whole-replace semantics: start fresh
        elif snap.get("config_hash") != cfg_hash or snap.get("relations_hash") != rel_hash:
            print(
                f"{E_CONFIG_MISMATCH}: snapshot config/relations hash mismatch; "
                "remove the snapshot or restore the previous config",
                file=sys.stderr,
            )
            return 2
        else:
            start_round = int(snap.get("round", -1)) + 1
            stored_scores = snap.get("scores", {}) or {}

    samples, failures = load_samples(data_dir / "samples", cfg)
    rel_maps: dict = {}
    for rel in relations:
        rel_maps.setdefault(rel.get("sample"), {})[rel.get("assertion_id")] = rel

    prepared = {}
    for s in samples:
        try:
            prepared[s.name] = prepare_sample(
                s.name, s.image, s.assertions, rel_maps.get(s.name, {}), cfg
            )
        except StageError as exc:
            failures.append(
                {
                    "sample": s.name,
                    "round": None,
                    "stage": exc.stage,
                    "code": exc.code,
                    "detail": str(exc),
                }
            )
    active = [s.name for s in samples if s.name in prepared]
    rounds_data = {n: list(stored_scores.get(n, [])) for n in active}

    # Replay already-snapshotted rounds to rebuild per-sample round state.
    for name in active:
        for r in range(min(start_round, cfg["rounds"])):
            advance_round(prepared[name], r, cfg)

    delay_ms = int(os.environ.get("MMEF_ROUND_DELAY_MS", "0"))
    stopped = False
    for r in range(start_round, cfg["rounds"]):
        if _STOP.is_set():
            stopped = True
            break
        results = {}
        failed = []
        with ThreadPoolExecutor(max_workers=max(1, cfg["concurrency"])) as pool:
            futures = {
                pool.submit(advance_round, prepared[n], r, cfg): n for n in active
            }
            for fut in as_completed(futures):
                name = futures[fut]
                try:
                    results[name] = fut.result()
                except StageError as exc:
                    failed.append((name, exc))
                except Exception as exc:  # noqa: BLE001 - isolate per-sample
                    failed.append((name, StageError("fusion", E_FUSION, str(exc))))
        # Merge strictly by stable sort key, independent of completion order.
        for name in sorted(results, key=lambda n: sort_key(n, r)):
            rounds_data[name].append(results[name])
        for name, exc in sorted(failed, key=lambda item: sort_key(item[0], r)):
            failures.append(
                {
                    "sample": name,
                    "round": r,
                    "stage": exc.stage,
                    "code": exc.code,
                    "detail": str(exc),
                }
            )
            active.remove(name)  # no conclusions for this sample in later rounds
        save_snapshot(
            snap_path,
            version=SNAPSHOT_VERSION,
            round=r,
            config_hash=cfg_hash,
            relations_hash=rel_hash,
            frac_bits=cfg["frac_bits"],
            cursor={"round": r, "samples": sorted(active)},
            scores=rounds_data,
        )
        if delay_ms:
            time.sleep(delay_ms / 1000.0)
        if _STOP.is_set():
            stopped = True
            break

    if stopped:
        # Snapshot for the last fully completed round is already persisted;
        # no exit report is promised on forced termination.
        return 0

    report = build_report(cfg, cfg_hash, rel_hash, rounds_data, prepared, failures)
    atomic_write_json(report_path, report)
    return 0


def build_report(cfg, cfg_hash, rel_hash, rounds_data, prepared, failures) -> dict:
    samples_out = []
    precision_sensitive = []
    boundary_sensitive = []
    for name in sorted(rounds_data):
        rounds = rounds_data[name]
        entry = {"name": name, "rounds": rounds}
        if rounds:
            final = rounds[-1]
            prep = prepared[name]
            try:
                diffs = {
                    "quantization": prep.quant_diff,
                    "softmax": final["softmax_diff"],
                    "weighted_sum": prep.weighted_sum_diff,
                }
                first_stage = first_exceed_stage(diffs, cfg["error_tolerance"])
            except Exception as exc:  # noqa: BLE001
                raise StageError("tracking", E_TRACKING, str(exc))
            is_prec = final["decision"] != final["decision_hi"]
            is_bound = is_boundary_sensitive(
                final["confidence_fp"],
                final["confidence_hi_fp"],
                prep.thr_fp["lo"],
                prep.thr_fp["hi"],
                prep.margin_fp["lo"],
                prep.margin_fp["hi"],
            )
            dominant = final["contributions"][0] if final["contributions"] else None
            entry.update(
                {
                    "conclusion": final["decision"] if final["accepted"] else "undecided",
                    "conclusion_hi": (
                        final["decision_hi"] if final["accepted_hi"] else "undecided"
                    ),
                    "error_tracking": {
                        **diffs,
                        "tolerance": cfg["error_tolerance"],
                        "first_exceed_stage": first_stage,
                    },
                    "error_sources": prep.error_sources,
                    "precision_sensitive": is_prec,
                    "boundary_sensitive": is_bound,
                    "dominant_evidence": dominant,
                }
            )
            if is_prec:
                precision_sensitive.append(name)
            if is_bound:
                boundary_sensitive.append(name)
        else:
            entry.update(
                {
                    "conclusion": None,
                    "conclusion_hi": None,
                    "error_tracking": None,
                    "error_sources": [],
                    "precision_sensitive": False,
                    "boundary_sensitive": False,
                    "dominant_evidence": None,
                }
            )
        samples_out.append(entry)
    return {
        "component": "mmef",
        "version": VERSION,
        "snapshot_version": SNAPSHOT_VERSION,
        "config_hash": cfg_hash,
        "relations_hash": rel_hash,
        "frac_bits": cfg["frac_bits"],
        "rounds": cfg["rounds"],
        "samples": samples_out,
        "sensitive": {
            "precision": sorted(precision_sensitive),
            "boundary": sorted(boundary_sensitive),
            "precision_count": len(precision_sensitive),
            "boundary_count": len(boundary_sensitive),
        },
        "failures": failures,
    }
