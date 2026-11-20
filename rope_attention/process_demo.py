"""Process-pool and forced-kill recovery demonstrations."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import numpy as np

from .cache import CacheStore
from .config import parse_config
from .errors import RopeError
from .snapshot import ConfigSnapshot, SnapshotStore
from .util import atomic_write_bytes, canonical_json_bytes, hash_array


PROBE_NODE = "process_pool_probe"


def _probe_key_parts(index: int) -> dict[str, Any]:
    return {
        "block_size": 4,
        "causal": True,
        "config_version": "v1",
        "probe_index": index,
        "probe_schema": "process-demo/v1",
    }


def _probe_fingerprint(index: int) -> str:
    return f"process-demo-v1-{index:06d}"


def _probe_compute(index: int) -> tuple[dict[str, np.ndarray], dict[str, Any]]:
    values = np.arange(index * 8, index * 8 + 8, dtype=np.float64)
    return (
        {"probe": np.ascontiguousarray(values)},
        {
            "index": index,
            "probe": "deterministic-cross-process-cache-write",
        },
    )


def pool_probe_write(cache_root: str, index: int) -> dict[str, Any]:
    store = CacheStore(Path(cache_root))
    result = store.get_or_compute(
        node=PROBE_NODE,
        case_id=f"probe-{index}",
        key_parts=_probe_key_parts(index),
        semantic_fingerprint=_probe_fingerprint(index),
        compute=lambda: _probe_compute(index),
        max_entry_count=10_000,
        overflow_policy="error",
        allow_semantic_alias=False,
    )
    return {
        "cache_version": result.cache_version,
        "key": result.key,
        "payload_hash": result.payload_hash,
        "pid": os.getpid(),
        "probe_hash": hash_array(result.arrays["probe"]),
        "status": result.status,
    }


def pool_probe_read(cache_root: str, index: int) -> dict[str, Any]:
    store = CacheStore(Path(cache_root))
    key = store.key_for(PROBE_NODE, _probe_key_parts(index))
    document = store.read_entry(key)
    return {
        "cache_version": int(document["cache_version"]),
        "key": key,
        "payload_hash": str(document["payload_hash"]),
        "pid": os.getpid(),
        "status": "read_back",
    }


def snapshot_commit_worker(
    workspace: str,
    version: int,
    config_text: str,
    source_name: str,
) -> dict[str, Any]:
    store = SnapshotStore(Path(workspace))
    snapshot = ConfigSnapshot.create(
        parse_config(config_text),
        version=version,
        source_name=source_name,
    )
    try:
        committed, changed = store.commit(snapshot)
        return {
            "business_code": "OK",
            "changed": changed,
            "checksum": committed.checksum,
            "pid": os.getpid(),
            "version": committed.version,
        }
    except RopeError as exc:
        return {
            "business_code": exc.code,
            "details": exc.details,
            "message": exc.message,
            "pid": os.getpid(),
        }


def run_process_pool_demo(root: Path, *, workers: int = 4) -> dict[str, Any]:
    import multiprocessing

    root = root.resolve()
    cache_root = root / "runtime" / "process-pool" / "cache"
    shutil.rmtree(cache_root.parent, ignore_errors=True)
    cache_root.mkdir(parents=True, exist_ok=True)
    context = multiprocessing.get_context("spawn")
    arguments = [(str(cache_root), 7) for _ in range(workers)]
    with context.Pool(processes=workers) as pool:
        writes = pool.starmap(pool_probe_write, arguments)
        reads = pool.starmap(pool_probe_read, arguments)
    created = [
        item
        for item in writes
        if item["status"] in {"computed", "computed_uncached"}
    ]
    reused = [
        item
        for item in writes
        if item["status"] in {"hit_exact", "hit_semantic"}
    ]
    store = CacheStore(cache_root)
    verification = store.verify_all_entries()
    return {
        "business_code": "OK" if len(created) == 1 and len(reused) == workers - 1 else "E_INCONSISTENT",
        "cache_diagnostics": store.diagnostics(),
        "cache_verification": verification,
        "exactly_one_effective_writer": len(created) == 1,
        "ok": (
            len(created) == 1
            and len(reused) == workers - 1
            and len({item["payload_hash"] for item in writes}) == 1
            and all(item["status"] == "read_back" for item in reads)
        ),
        "reads": reads,
        "schema": "rope-attention-process-pool-demo/v1",
        "start_method": "spawn",
        "worker_count": workers,
        "writes": writes,
    }


def _fill_entries(cache_root: Path, count: int, marker: Path | None = None) -> list[str]:
    statuses: list[str] = []
    for index in range(count):
        response = pool_probe_write(str(cache_root), index)
        statuses.append(str(response["status"]))
        if marker is not None and index == 3:
            atomic_write_bytes(
                marker,
                canonical_json_bytes({"pid": os.getpid(), "written_index": index})
                + b"\n",
            )
    return statuses


def run_kill_restart_demo(root: Path, *, total_entries: int = 40) -> dict[str, Any]:
    root = root.resolve()
    demo_root = root / "runtime" / "kill-restart"
    cache_root = demo_root / "cache"
    marker = demo_root / "ready.json"
    shutil.rmtree(demo_root, ignore_errors=True)
    cache_root.mkdir(parents=True, exist_ok=True)
    package_root = Path(__file__).resolve().parents[1]
    environment = dict(os.environ)
    environment["PYTHONPATH"] = (
        str(package_root)
        + os.pathsep
        + environment.get("PYTHONPATH", "")
    )
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "rope_attention.process_demo",
            "fill",
            "--cache-root",
            str(cache_root),
            "--count",
            str(total_entries),
            "--marker",
            str(marker),
        ],
        cwd=str(package_root),
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    deadline = time.monotonic() + 20.0
    while time.monotonic() < deadline and not marker.exists():
        if process.poll() is not None:
            break
        time.sleep(0.02)
    marker_seen = marker.exists()
    if process.poll() is None:
        process.kill()
    process.wait(timeout=20)
    stderr_after_kill = process.stderr.read() if process.stderr is not None else ""

    store = CacheStore(cache_root)
    entries_after_kill = store.entry_count()
    verification_after_kill = store.verify_all_entries()
    restart_statuses = _fill_entries(cache_root, total_entries)
    verification_after_restart = store.verify_all_entries()
    expected_keys = {
        store.key_for(PROBE_NODE, _probe_key_parts(index))
        for index in range(total_entries)
    }
    actual_keys = {path.stem for path in store.entries.glob("*.json")}
    ok = (
        marker_seen
        and process.returncode != 0
        and verification_after_kill["ok"]
        and verification_after_restart["ok"]
        and expected_keys == actual_keys
        and entries_after_kill < total_entries
    )
    return {
        "business_code": "OK" if ok else "E_INCONSISTENT",
        "entries_after_force_kill": entries_after_kill,
        "expected_entry_count": total_entries,
        "force_kill_marker_seen": marker_seen,
        "ok": ok,
        "process_returncode": process.returncode,
        "restart_statuses": restart_statuses,
        "schema": "rope-attention-kill-restart-demo/v1",
        "stderr_after_kill": stderr_after_kill.strip(),
        "verification_after_force_kill": verification_after_kill,
        "verification_after_restart": verification_after_restart,
    }


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    fill = subparsers.add_parser("fill")
    fill.add_argument("--cache-root", required=True)
    fill.add_argument("--count", type=int, required=True)
    fill.add_argument("--marker", default=None)
    args = parser.parse_args(argv)
    if args.command == "fill":
        statuses = _fill_entries(
            Path(args.cache_root),
            args.count,
            Path(args.marker) if args.marker else None,
        )
        print(json.dumps(statuses))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())

