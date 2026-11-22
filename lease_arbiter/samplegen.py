from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


def _write_json(path: Path, value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        indent=2,
        separators=(",", ": "),
    ).encode("utf-8") + b"\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return hashlib.sha256(payload).hexdigest()


def sample_documents() -> dict[str, Any]:
    cards = {
        "schema_version": 1,
        "cards": [
            {
                "card_id": "card-a",
                "total_compute_shares": 64,
                "total_memory_shares": 48,
                "granularity": 4,
                "health": "healthy",
                "topology_domain": "node-a",
            },
            {
                "card_id": "card-b",
                "total_compute_shares": 64,
                "total_memory_shares": 32,
                "granularity": 8,
                "health": "healthy",
                "topology_domain": "node-a",
            },
            {
                "card_id": "card-c",
                "total_compute_shares": 80,
                "total_memory_shares": 48,
                "granularity": 4,
                "health": "healthy",
                "topology_domain": "node-b",
            },
            {
                "card_id": "card-d",
                "total_compute_shares": 48,
                "total_memory_shares": 32,
                "granularity": 4,
                "health": "degraded",
                "topology_domain": "node-b",
            },
            {
                "card_id": "card-e",
                "total_compute_shares": 64,
                "total_memory_shares": 32,
                "granularity": 4,
                "health": "unhealthy",
                "topology_domain": "node-b",
            },
        ],
    }

    def job(
        job_id: str,
        compute: int,
        memory: int,
        duration: int,
        priority: str,
        group: str,
        preemptible: bool,
        cross: bool,
        arrival: int,
    ) -> dict[str, Any]:
        return {
            "job_id": job_id,
            "compute_shares": compute,
            "memory_shares": memory,
            "lease_duration_ms": duration,
            "priority": priority,
            "quota_group": group,
            "preemptible": preemptible,
            "allow_cross_card": cross,
            "arrival_round": arrival,
        }

    jobs = {
        "schema_version": 1,
        "jobs": [
            job("job-100", 16, 8, 12000, "normal", "research", True, False, 1),
            job("job-200", 24, 12, 30000, "normal", "batch", True, False, 1),
            job("job-300", 32, 16, 18000, "normal", "interactive", False, False, 1),
            job("job-400", 40, 32, 120000, "high", "research", False, False, 1),
            job("job-500", 12, 8, 120000, "normal", "batch", True, True, 1),
            job("job-600", 16, 8, 5000, "high", "interactive", False, False, 1),
            job("job-700", 32, 8, 10000, "normal", "batch", True, False, 2),
            job("job-710", 24, 12, 120000, "normal", "batch", True, False, 2),
            job("job-800", 20, 16, 60000, "normal", "research", True, True, 2),
            job("job-900", 32, 16, 120000, "high", "batch", False, False, 3),
            job("job-1100", 48, 16, 25000, "normal", "fragment", True, False, 8),
            job("job-1150", 8, 8, 5000, "normal", "anomaly", True, False, 9),
            job("job-1200", 8, 8, 20000, "normal", "anomaly", True, False, 10),
            job("job-1201", 20, 12, 20000, "normal", "anomaly", True, False, 10),
        ],
    }

    config = {
        "schema_version": 1,
        "quota_group_compute_limits": {
            "anomaly": 32,
            "batch": 96,
            "fragment": 64,
            "interactive": 48,
            "research": 100,
        },
        "alignment_granularity": 4,
        "renewal_grace_ms": 3000,
        "fragmentation_threshold": 0.25,
        "clock_steps_ms": [
            1000,
            5000,
            10000,
            15000,
            20000,
            25000,
            30000,
            40000,
            50000,
            60000,
        ],
        "clock_rollback_tolerance_ms": 0,
    }

    anomalies = {
        "schema_version": 1,
        "injections": [
            {
                "anomaly_id": "anom-grant-overlap",
                "stage": "grant",
                "type": "interval_overlap",
                "round": 10,
                "card_id": "card-a",
                "job_id": "job-1200",
                "enabled": True,
                "payload": {"note": "synthetic overlap is rejected before commit"},
            },
            {
                "anomaly_id": "anom-renew-regression",
                "stage": "renew",
                "type": "lease_generation_regression",
                "round": 10,
                "card_id": "card-c",
                "job_id": "job-900",
                "enabled": True,
                "payload": {"note": "renewal is skipped for the targeted lease only"},
            },
            {
                "anomaly_id": "anom-reclaim-nonconservation",
                "stage": "reclaim",
                "type": "share_nonconservation",
                "round": 10,
                "card_id": "card-a",
                "job_id": "job-1150",
                "enabled": True,
                "payload": {"note": "reclaim for this card is deferred one round"},
            },
            {
                "anomaly_id": "anom-recombine-digest",
                "stage": "recombine",
                "type": "fragment_digest_mismatch",
                "round": 10,
                "card_id": "card-c",
                "job_id": "job-1100",
                "enabled": True,
                "payload": {"note": "digest check fails only for the target card analysis"},
            },
        ],
    }

    return {
        "accelerator_cards.json": cards,
        "jobs.json": jobs,
        "lease_config.json": config,
        "anomaly_injections.json": anomalies,
    }


def generate_samples(directory: str | Path) -> dict[str, str]:
    directory = Path(directory)
    documents = sample_documents()
    hashes: dict[str, str] = {}
    for filename, document in documents.items():
        hashes[filename] = _write_json(directory / filename, document)
    manifest = {
        "schema_version": 1,
        "generator": "lease_arbiter.samplegen",
        "files": hashes,
        "clock_rounds": len(documents["lease_config.json"]["clock_steps_ms"]),
    }
    _write_json(directory / "sample_manifest.json", manifest)
    return hashes
