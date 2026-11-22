"""Command-line demo producing a complete JSON acceptance report."""

from __future__ import annotations

import argparse
import json
import multiprocessing as mp
import platform
import shutil
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any, Dict, Optional, Sequence

import numpy as np

from .cache import ResourcePolicy
from .canonical import SCHEMA_VERSION
from .errors import (
    E_CONFIDENCE_CONFLICT,
    E_INCREMENTAL_MISMATCH,
    E_INCONSISTENT,
    E_LINEAGE_STALE,
    E_RESOURCE_LIMIT,
    E_SNAPSHOT_CORRUPT,
    E_SNAPSHOT_ROLLBACK,
    OK,
    BusinessError,
)
from .generator import build_default_config, generate_samples, shift_prototype
from .models import ThresholdConfig
from .pipeline import validate_confidence_combination
from .service import DigitOCRService, RunResult


def _error_record(exc: BusinessError) -> Dict[str, Any]:
    return exc.to_dict()


def _node_kinds(nodes: Sequence[str]) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for node in nodes:
        kind = node.split(":", 1)[0]
        counts[kind] = counts.get(kind, 0) + 1
    return counts


def _decision_digest_map(result: RunResult) -> Dict[str, str]:
    return {
        image_id: str(artifact["content_hash"])
        for image_id, artifact in sorted(result.decision_artifacts.items())
    }


def _build_report(args: argparse.Namespace) -> Dict[str, Any]:
    cache_dir = Path(args.cache_dir)
    if not args.keep_cache and cache_dir.exists():
        shutil.rmtree(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    samples = generate_samples(seed=args.seed, per_class=args.per_class)
    initial_config = build_default_config(seed=args.seed)
    policy = ResourcePolicy(
        max_feature_items=max(args.per_class * 10 + 8, 32),
        max_feature_bytes=max(2 * 1024 * 1024, len(samples) * 4096),
        max_parallel_nodes=max(2, args.parallel_workers),
        memory_policy="evict",
        allow_parallel_degrade=True,
    )
    service = DigitOCRService(initial_config, cache_dir, policy)

    initial = service.recognize_batch(
        samples, parallel_workers=args.parallel_workers
    )
    initial_self_check = service.self_check(initial)
    initial_decision = initial.decisions[0]
    current_replay = service.replay_decision(initial_decision)

    threshold_config = initial_config.with_thresholds(
        ThresholdConfig(
            version=initial_config.thresholds.version + 1,
            reject_threshold=args.calibrated_reject_threshold,
            high_max=initial_config.thresholds.high_max,
            medium_max=initial_config.thresholds.medium_max,
        )
    )
    threshold_run = service.calibrate(
        samples,
        threshold_config,
        parallel_workers=args.parallel_workers,
    )
    threshold_self_check = service.self_check(threshold_run)
    stale_rejection: Dict[str, Any]
    try:
        service.replay_decision(initial_decision)
        stale_rejection = {
            "business_code": E_LINEAGE_STALE,
            "unexpected_success": True,
        }
    except BusinessError as exc:
        stale_rejection = _error_record(exc)
    archived_replay = service.replay_decision(
        initial_decision, allow_archived=True
    )
    threshold_replay = service.replay_decision(threshold_run.decisions[0])

    prototype_config = threshold_config.with_prototypes(
        shift_prototype(threshold_config.prototypes, args.prototype_class, dx=1, dy=0)
    )
    prototype_run = service.calibrate(
        samples,
        prototype_config,
        parallel_workers=args.parallel_workers,
    )
    prototype_self_check = service.self_check(prototype_run)
    prototype_distance_classes = sorted(
        {
            int(node.rsplit(":", 1)[1])
            for node in prototype_run.recomputed_nodes
            if node.startswith("distance:")
        }
    )

    normalization_config = prototype_config.with_normalization(
        replace(
            prototype_config.normalization,
            version=prototype_config.normalization.version + 1,
            input_max=254.0,
        )
    )
    normalization_run = service.calibrate(
        samples,
        normalization_config,
        parallel_workers=args.parallel_workers,
    )
    normalization_self_check = service.self_check(normalization_run)
    final_replay = service.replay_decision(normalization_run.decisions[0])

    # Degraded execution on an isolated cache. It must not alter committed
    # decisions, and its self-check must still cover every affected node.
    degraded_policy = ResourcePolicy(
        max_feature_items=max(1, args.degraded_feature_limit),
        max_feature_bytes=2 * 1024 * 1024,
        max_parallel_nodes=args.parallel_workers,
        memory_policy="evict",
        allow_parallel_degrade=True,
    )
    degraded_service = DigitOCRService(
        initial_config, cache_dir / "resource_degraded", degraded_policy
    )
    degraded_run = degraded_service.recognize_batch(
        samples, parallel_workers=args.parallel_workers + 6
    )
    degraded_self_check = degraded_service.self_check(degraded_run)
    baseline_digests = _decision_digest_map(initial)
    degraded_digests = _decision_digest_map(degraded_run)
    mismatched_images = [
        image_id
        for image_id in sorted(baseline_digests)
        if baseline_digests[image_id] != degraded_digests.get(image_id)
    ]

    strict_memory_error: Dict[str, Any]
    strict_memory_service = DigitOCRService(
        initial_config,
        cache_dir / "resource_strict_memory",
        ResourcePolicy(
            max_feature_items=1,
            max_feature_bytes=2 * 1024 * 1024,
            max_parallel_nodes=args.parallel_workers,
            memory_policy="error",
            allow_parallel_degrade=True,
        ),
    )
    try:
        strict_memory_service.recognize_batch(samples[:3], parallel_workers=1)
        strict_memory_error = {
            "business_code": E_RESOURCE_LIMIT,
            "unexpected_success": True,
        }
    except BusinessError as exc:
        strict_memory_error = _error_record(exc)

    strict_parallel_error: Dict[str, Any]
    strict_parallel_service = DigitOCRService(
        initial_config,
        cache_dir / "resource_strict_parallel",
        ResourcePolicy(
            max_feature_items=64,
            max_feature_bytes=2 * 1024 * 1024,
            max_parallel_nodes=args.parallel_workers,
            memory_policy="evict",
            allow_parallel_degrade=False,
        ),
    )
    try:
        strict_parallel_service.recognize_batch(
            samples[:2], parallel_workers=args.parallel_workers + 1
        )
        strict_parallel_error = {
            "business_code": E_RESOURCE_LIMIT,
            "unexpected_success": True,
        }
    except BusinessError as exc:
        strict_parallel_error = _error_record(exc)

    # Snapshot guards are exercised without damaging the live snapshots.
    snapshot_rollback: Dict[str, Any]
    try:
        service.snapshots.replace(
            "thresholds", 1, initial_config.thresholds.to_payload()
        )
        snapshot_rollback = {
            "business_code": E_SNAPSHOT_ROLLBACK,
            "unexpected_success": True,
        }
    except BusinessError as exc:
        snapshot_rollback = _error_record(exc)

    snapshot_corrupt: Dict[str, Any]
    try:
        service.snapshots.replace(
            "normalization",
            normalization_config.normalization.version + 1,
            normalization_config.normalization.to_payload(),
            expected_checksum="0" * 64,
        )
        snapshot_corrupt = {
            "business_code": E_SNAPSHOT_CORRUPT,
            "unexpected_success": True,
        }
    except BusinessError as exc:
        snapshot_corrupt = _error_record(exc)

    cache_verification = service.cache.verify_all()
    confidence_conflict: Dict[str, Any]
    try:
        validate_confidence_combination(
            rejected=True,
            confidence="high",
            image_id=samples[0].image_id,
        )
        confidence_conflict = {
            "business_code": E_CONFIDENCE_CONFLICT,
            "unexpected_success": True,
        }
    except BusinessError as exc:
        confidence_conflict = _error_record(exc)

    business_codes = {
        OK,
        E_CONFIDENCE_CONFLICT,
        E_INCREMENTAL_MISMATCH,
        E_LINEAGE_STALE,
        E_RESOURCE_LIMIT,
        E_SNAPSHOT_ROLLBACK,
        E_SNAPSHOT_CORRUPT,
        E_INCONSISTENT,
    }
    observed = {OK}
    for item in (
        stale_rejection,
        strict_memory_error,
        strict_parallel_error,
        confidence_conflict,
        snapshot_rollback,
        snapshot_corrupt,
    ):
        if item.get("business_code") in business_codes:
            observed.add(str(item["business_code"]))

    report: Dict[str, Any] = {
        "schema": SCHEMA_VERSION,
        "business_code": OK,
        "command": {
            "seed": args.seed,
            "per_class": args.per_class,
            "samples": len(samples),
            "parallel_workers_requested": args.parallel_workers,
            "calibrated_reject_threshold": args.calibrated_reject_threshold,
            "prototype_class_replaced": args.prototype_class,
            "cache_dir": str(cache_dir),
            "network_used": False,
            "container_used": False,
        },
        "environment": {
            "python": platform.python_version(),
            "python_executable": sys.executable,
            "numpy": np.__version__,
            "platform": platform.platform(),
            "spawn_available": "spawn" in mp.get_all_start_methods(),
            "required_spawn_context": "spawn",
        },
        "samples": {
            "count": len(samples),
            "classes": list(range(10)),
            "per_class": args.per_class,
            "uncertain_count": sum(1 for sample in samples if sample.uncertain),
            "image_ids": [sample.image_id for sample in samples],
            "sample_hashes": {
                sample.image_id: sample.image_sha256 for sample in samples
            },
        },
        "initial": initial.to_report(),
        "initial_self_check": initial_self_check,
        "traceability": {
            "initial_current_replay": current_replay,
            "stale_rejection_after_threshold": stale_rejection,
            "archived_replay_after_threshold": archived_replay,
            "threshold_current_replay": threshold_replay,
            "final_current_replay": final_replay,
        },
        "calibrations": {
            "threshold_only": {
                "run": threshold_run.to_report(),
                "node_kinds_recomputed": _node_kinds(
                    threshold_run.recomputed_nodes
                ),
                "node_kinds_reused": _node_kinds(threshold_run.reused_nodes),
                "self_check": threshold_self_check,
            },
            "single_prototype": {
                "run": prototype_run.to_report(),
                "changed_class": args.prototype_class,
                "distance_classes_recomputed": prototype_distance_classes,
                "node_kinds_recomputed": _node_kinds(
                    prototype_run.recomputed_nodes
                ),
                "node_kinds_reused": _node_kinds(prototype_run.reused_nodes),
                "self_check": prototype_self_check,
            },
            "normalization": {
                "run": normalization_run.to_report(),
                "node_kinds_recomputed": _node_kinds(
                    normalization_run.recomputed_nodes
                ),
                "node_kinds_reused": _node_kinds(
                    normalization_run.reused_nodes
                ),
                "self_check": normalization_self_check,
            },
        },
        "resource_validation": {
            "degraded_run": {
                "business_code": OK,
                "degraded": degraded_run.execution["degraded"],
                "degradation_categories": degraded_run.execution[
                    "degradation_categories"
                ],
                "decisions_unchanged": not bool(mismatched_images),
                "mismatched_image_ids": mismatched_images,
                "self_check": degraded_self_check,
                "node_invariants": degraded_run.to_report()["nodes"][
                    "invariants"
                ],
                "summary": {
                    key: degraded_run.metrics_artifact[key]
                    for key in (
                        "total",
                        "correct",
                        "accepted",
                        "rejected",
                        "accuracy",
                    )
                },
            },
            "strict_feature_memory": strict_memory_error,
            "strict_parallelism": strict_parallel_error,
        },
        "snapshot_validation": {
            "rollback_attempt": snapshot_rollback,
            "corrupt_checksum_attempt": snapshot_corrupt,
            "current_versions": service.snapshots.all_current_metadata(),
            "version_history": {
                name: service.snapshots.versions(name)
                for name in (
                    "normalization",
                    "prototypes",
                    "thresholds",
                    "distance_metric",
                    "metrics",
                )
            },
            "cache_verification": cache_verification,
        },
        "confidence_validation": confidence_conflict,
        "business_codes": {
            "declared": sorted(business_codes),
            "observed": sorted(observed),
            "success_code": OK,
        },
    }
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run the deterministic incremental OCR demo and emit a JSON report."
        )
    )
    parser.add_argument("--seed", type=int, default=20261123)
    parser.add_argument("--per-class", type=int, default=3)
    parser.add_argument(
        "--parallel-workers",
        type=int,
        default=2,
        help="Requested parallel node count; spawn is always used when >1.",
    )
    parser.add_argument(
        "--calibrated-reject-threshold",
        type=float,
        default=0.25,
    )
    parser.add_argument("--prototype-class", type=int, default=4)
    parser.add_argument("--degraded-feature-limit", type=int, default=4)
    parser.add_argument(
        "--cache-dir", default="artifacts/demo_cache"
    )
    parser.add_argument(
        "--output", default="artifacts/demo_report.json"
    )
    parser.add_argument(
        "--keep-cache",
        action="store_true",
        help="Keep an existing demo cache instead of starting clean.",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        report = _build_report(args)
    except BusinessError as exc:
        print(
            json.dumps(
                exc.to_dict(),
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        )
        return 2
    encoded = json.dumps(
        report, ensure_ascii=False, indent=2, sort_keys=True
    )
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(encoded + "\n", encoding="utf-8")
    print(encoded)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
