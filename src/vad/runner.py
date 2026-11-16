"""Orchestration: scanning, serial/parallel runs, snapshots, signals, reports."""
from __future__ import annotations

import signal
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from .config import VadConfig
from .errors import E_CONFIG_MISMATCH, VadError
from .injection import InjectionManifest
from .pipeline import process_sample, sort_key_for
from .snapshot import Snapshot, read_snapshot, validate_snapshot, write_snapshot
from .util import atomic_write_json


@dataclass
class RunContext:
    shutdown_requested: bool = False


def scan_wavs(input_dir: Path) -> list[Path]:
    return sorted((p for p in Path(input_dir).iterdir()
                   if p.is_file() and p.suffix.lower() == ".wav"),
                  key=lambda p: p.name)


def _run_serial(paths: list[Path], config: VadConfig, manifest: InjectionManifest) -> list[dict]:
    results = []
    for path in paths:
        results.append(process_sample(path, config, manifest.for_sample(path.name)))
    return results


def _run_parallel(paths: list[Path], config: VadConfig,
                  manifest: InjectionManifest, ctx: RunContext) -> list[dict]:
    indexed: list[tuple[int, dict]] = []
    with ThreadPoolExecutor(max_workers=config.max_workers) as pool:
        futures = {
            pool.submit(process_sample, path, config, manifest.for_sample(path.name)): (idx, path.name)
            for idx, path in enumerate(paths)
        }
        for future in futures:
            idx, name = futures[future]
            indexed.append((idx, future.result()))
            if ctx.shutdown_requested:
                break
    indexed.sort(key=lambda pair: pair[0])
    return [result for _, result in indexed]


def _failure_list(results: list[dict]) -> list[dict]:
    return [
        {"sample": result["sample"], "sort_key": result["sort_key"],
         "code": result["failure"]["code"], "message": result["failure"]["message"]}
        for result in results if result["status"] == "failed"
    ]


def _hit_list(results: list[dict]) -> list[dict]:
    hits = []
    for result in results:
        for hit in result["injection_hits"]:
            hits.append({"sample": result["sample"], **hit})
    return sorted(hits, key=lambda item: (item["sample"], item["injection_id"]))


def _partial_summary(results: list[dict]) -> dict:
    return {
        "processed": [result["sample"] for result in results],
        "segments_per_sample": {
            result["sample"]: len(result["segments"]) for result in results
        },
        "failures": _failure_list(results),
    }


def run(input_dir: Path, output_dir: Path, config: VadConfig,
        manifest: InjectionManifest, force_restart: bool = False,
        graceful_after: int | None = None) -> dict:
    snapshot_dir = Path(output_dir) / "snapshots"
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    paths = scan_wavs(input_dir)
    all_keys = [sort_key_for(path) for path in paths]

    ctx = RunContext()
    installed: list = []

    def _handler(signum, frame):  # graceful: finish current sample, persist, exit 0
        ctx.shutdown_requested = True

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            installed.append((sig, signal.signal(sig, _handler)))
        except (ValueError, OSError):
            pass

    resume_from: set[str] = set()
    snapshot_meta = {"used": False, "version": None, "cursor": []}
    if not force_restart:
        raw_snap = read_snapshot(snapshot_dir)
        if raw_snap is not None:
            validate_snapshot(raw_snap, config.config_hash, manifest.manifest_hash)
            resume_from = set(raw_snap["cursor"])
            snapshot_meta = {"used": True,
                             "version": raw_snap["snapshot_version"],
                             "cursor": list(raw_snap["cursor"])}

    pending = [path for path in paths if sort_key_for(path) not in resume_from]
    completed: list[dict] = []

    def _persist(stage: str) -> None:
        ordered = sorted(completed, key=lambda r: r["sort_key"])
        snap = Snapshot(
            stage=stage,
            snapshot_version=config.snapshot_version,
            config_hash=config.config_hash,
            manifest_hash=manifest.manifest_hash,
            cursor=[result["sort_key"] for result in ordered],
            partial_summary=_partial_summary(ordered),
            run_mode="resume" if snapshot_meta["used"] else "fresh",
        )
        write_snapshot(snapshot_dir, snap)

    for idx, path in enumerate(pending):
        result = process_sample(path, config, manifest.for_sample(path.name))
        completed.append(result)
        _persist("endpoint_decision" if result["status"] == "ok" else "audio_validation")
        if ctx.shutdown_requested or (graceful_after is not None
                                      and len(completed) >= graceful_after):
            ctx.shutdown_requested = True
            for sig, original in installed:
                signal.signal(sig, original)
            report = _build_report(
                all_keys, sorted(completed, key=lambda r: r["sort_key"]),
                config, manifest, snapshot_meta,
                comparison={"status": "interrupted", "identical": None},
                run_state="interrupted",
            )
            atomic_write_json(Path(output_dir) / "diagnostic_report.json", report)
            raise _GracefulExit(report)

    for sig, original in installed:
        signal.signal(sig, original)

    serial_results = _run_serial(paths, config, manifest)
    parallel_results = _run_parallel(paths, config, manifest, ctx)
    serial_public = _public_view(serial_results)
    parallel_public = _public_view(parallel_results)
    comparison = {
        "status": "match",
        "identical": serial_public == parallel_public,
        "serial_sample_order": [r["sample"] for r in serial_public],
        "parallel_sample_order": [r["sample"] for r in parallel_public],
        "max_workers": config.max_workers,
        "field_level_equal": serial_public == parallel_public,
    }

    # Persist final snapshot covering every sample, then the full report.
    _persist("complete")
    report = _build_report(all_keys, serial_public, config, manifest, snapshot_meta,
                           comparison=comparison, run_state="complete")
    atomic_write_json(Path(output_dir) / "diagnostic_report.json", report)
    return report


def _public_view(results: list[dict]) -> list[dict]:
    """Strip runtime-only keys so serial vs parallel compare on persisted fields."""
    view = []
    for result in results:
        view.append({
            "sample": result["sample"],
            "sort_key": result["sort_key"],
            "status": result["status"],
            "failure": result["failure"],
            "injection_hits": result["injection_hits"],
            "noise_floor_log_energy": result["noise_floor_log_energy"],
            "frame_count": result["frame_count"],
            "frame_records": result["frame_records"],
            "state_trace_summary": result["state_trace_summary"],
            "segments": result["segments"],
            "audit": result["audit"],
            "thresholds": result.get("thresholds"),
        })
    return view


def _build_report(all_keys: list[str], results: list[dict], config: VadConfig,
                  manifest: InjectionManifest, snapshot_meta: dict,
                  comparison: dict, run_state: str) -> dict:
    return {
        "report_schema": 1,
        "run_state": run_state,
        "sample_order": all_keys,
        "results": results,
        "failures": _failure_list(results),
        "injection_hits": _hit_list(results),
        "concurrency_comparison": comparison,
        "snapshot": {
            "version": config.snapshot_version,
            "resumed": snapshot_meta["used"],
            "resume_cursor": snapshot_meta["cursor"],
        },
        "hashes": {
            "config_hash": config.config_hash,
            "manifest_hash": manifest.manifest_hash,
        },
    }


class _GracefulExit(Exception):
    def __init__(self, report: dict):
        self.report = report
