#!/usr/bin/env python3
"""Generate deterministic sample bundles and their content manifest."""

from __future__ import annotations

import hashlib
import json
import random
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dynabatch.canonical import canonical_json, digest_json
from dynabatch.scheduler import SchedulerEngine
from dynabatch.status import verify_store
from dynabatch.store import Store
from dynabatch.validation import validate_bundle


SEED = 20261123
SAMPLE_DIR = ROOT / "sample"


def _replica(
    replica_id: str,
    compute: float,
    window: int,
    *,
    memory: int = 64,
    batch_size: int = 3,
    concurrency: int = 2,
) -> dict[str, Any]:
    return {
        "replica_id": replica_id,
        "compute_units": compute,
        "memory_mib": memory,
        "batch_window_ms": window,
        "max_batch_size": batch_size,
        "max_sequence_length": 64,
        "max_concurrent_batches": concurrency,
    }


def build_bundle() -> dict[str, Any]:
    rng = random.Random(SEED)
    versions = [
        {
            "version": 1,
            "effective_at_ms": 0,
            "replicas": [
                _replica("r-a", 0.90, 20),
                _replica("r-b", 0.88, 25),
            ],
        },
        {
            "version": 2,
            "effective_at_ms": 55,
            "replicas": [
                _replica("r-a", 0.90, 20),
                _replica("r-b", 0.88, 25),
                _replica("r-c", 0.92, 15, memory=72),
            ],
        },
        {
            "version": 3,
            "effective_at_ms": 115,
            "replicas": [
                _replica("r-b", 1.05, 20, memory=96),
                _replica("r-c", 0.94, 15, memory=72),
            ],
        },
    ]
    arrivals = [0, 4, 9, 13, 18, 23, 29, 36, 44, 53, 61, 72, 84, 97, 112, 126, 141, 157]
    priorities = [0, 1, 3, 0, 2, 0, 1, 3, 0, 2, 1, 0, 2, 1, 3, 0, 2, 1]
    requests = []
    for index, (arrival, priority) in enumerate(zip(arrivals, priorities), start=1):
        input_tokens = rng.randint(6, 16)
        output_tokens = rng.randint(4, 12)
        requests.append(
            {
                "seq": index,
                "arrival_ms": arrival,
                "priority": priority,
                "input_tokens": input_tokens,
                "max_output_tokens": output_tokens,
                "deadline_ms": arrival + rng.randint(120, 220),
            }
        )
    return {
        "schema_version": 1,
        "sample": {
            "name": "deterministic-multiversion",
            "seed": SEED,
            "generator": "scripts/generate_sample.py",
        },
        "model": {
            "model_id": "tiny-local-transformer",
            "layers": [
                {"name": "embedding", "parameters": 1_800_000},
                {"name": "encoder-0", "parameters": 1_600_000},
                {"name": "encoder-1", "parameters": 1_400_000},
            ],
            "input_tokens": 12,
            "output_tokens": 8,
            "bytes_per_parameter": 2,
            "activation_bytes_per_token": 8192,
            "latency_ms_per_compute_unit": 10.0,
        },
        "replica_versions": versions,
        "requests": requests,
        "precision": {
            "compute_scale": 1_000_000,
            "memory_scale": 1_024,
            "latency_scale": 1_000,
            "tolerance": 0.35,
            "rounding": "half_up",
        },
        "scheduler": {
            "logical_time_budget_ms": 60,
            "max_concurrent_batches": 4,
        },
        "execution": {
            "mode": "deterministic",
            "fail_batches": [],
            "latency_overhead_ms": 1,
        },
    }


def _write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2, sort_keys=True)
        + "\n",
        encoding="utf-8",
    )


def main() -> int:
    SAMPLE_DIR.mkdir(parents=True, exist_ok=True)
    bundle = build_bundle()
    validated = validate_bundle(bundle)
    bundle_path = SAMPLE_DIR / "bundle.json"
    _write_json(bundle_path, bundle)
    file_bytes = bundle_path.read_bytes()
    manifest = {
        "sample": bundle["sample"],
        "bundle_file": "bundle.json",
        "file_sha256": hashlib.sha256(file_bytes).hexdigest(),
        "canonical_sha256": digest_json(bundle),
        "request_count": len(validated.requests),
        "replica_versions": [
            {
                "version": version.version,
                "effective_at_ms": version.effective_at_ms,
                "replica_ids": [replica.replica_id for replica in version.replicas],
                "fingerprint": version.fingerprint,
            }
            for version in validated.replica_versions
        ],
    }
    _write_json(SAMPLE_DIR / "manifest.json", manifest)

    rollback = json.loads(canonical_json(bundle))
    rollback["replica_versions"] = rollback["replica_versions"][:2]
    rollback["replica_versions"][-1]["replicas"] = [
        _replica("r-a", 0.90, 20),
        _replica("r-b", 0.88, 25),
        _replica("r-old", 0.50, 30),
    ]
    rollback["sample"]["name"] = "rollback-probe"
    _write_json(SAMPLE_DIR / "rollback_bundle.json", rollback)

    with tempfile.TemporaryDirectory() as temp:
        db = Path(temp) / "sample-smoke.db"
        with Store(db, create=True) as store:
            store.ingest_bundle(validated)
            with store.lock():
                result = SchedulerEngine(store).run(300)
            verification = verify_store(store)
            pending = store.fetchone(
                "SELECT COUNT(*) AS count FROM requests WHERE status = 'PENDING'"
            )
            batched = store.fetchone(
                "SELECT COUNT(*) AS count FROM requests WHERE status = 'BATCHED'"
            )
            sensitive = store.fetchone(
                "SELECT COUNT(*) AS count FROM precision_flags"
            )
        print(
            canonical_json(
                {
                    "sample": str(bundle_path),
                    "manifest": str(SAMPLE_DIR / "manifest.json"),
                    "file_sha256": manifest["file_sha256"],
                    "canonical_sha256": manifest["canonical_sha256"],
                    "smoke": {
                        "windows_committed": result["windows_committed"],
                        "batches_committed": result["batches_committed"],
                        "pending_requests": int(pending["count"]),
                        "batched_requests": int(batched["count"]),
                        "precision_sensitive_flags": int(sensitive["count"]),
                        "verify_ok": verification["ok"],
                        "state_digest": verification["state_digest"],
                    },
                }
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
