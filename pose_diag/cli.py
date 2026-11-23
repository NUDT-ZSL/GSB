"""Command-line entry points for generation, diagnosis, preview, and GUI."""

from __future__ import annotations

import argparse
import copy
import json
import shutil
import sys
import tempfile
from dataclasses import replace
from pathlib import Path
from typing import Any

from .config import PipelineConfig
from .errors import BusinessError
from .grouping import validate_metadata
from .heatmap import validate_heatmap_channels
from .io_utils import atomic_write_json, atomic_write_text, canonical_json_text
from .samples import (
    generate_samples,
    load_manifest,
    load_scene_sample,
    validate_scene_record,
)
from .scoring import validate_scoring
from .service import DiagnosticService
from .snapshots import SNAPSHOT_CATEGORIES, SnapshotStore


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pose-diag",
        description="Deterministic region-preview versus batch pose diagnostic bench",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    generate = sub.add_parser("generate", help="generate deterministic samples")
    generate.add_argument("--samples", default="samples")
    generate.add_argument("--config", default="configs/default.json")
    generate.add_argument("--scene-count", type=int, default=3)

    diagnose = sub.add_parser(
        "diagnose",
        help="run baseline batch, interactive region preview, and snapshot probes",
    )
    diagnose.add_argument("--manifest", default="samples/manifest.jsonl")
    diagnose.add_argument("--config", default="configs/default.json")
    diagnose.add_argument("--state-dir", default="artifacts/diagnostic_state")
    diagnose.add_argument("--report", default="artifacts/diagnostic_report.json")
    diagnose.add_argument("--baseline-report", default="artifacts/baseline_batch.json")
    diagnose.add_argument("--preview-report", default="artifacts/region_preview.json")
    diagnose.add_argument("--scene-limit", type=int, default=0)
    diagnose.add_argument("--parallel-preview", type=int, default=1)
    diagnose.add_argument("--budget-ms", type=float, default=None)
    diagnose.add_argument("--keep-state", action="store_true")

    batch = sub.add_parser("batch", help="run one scene batch pipeline")
    batch.add_argument("--manifest", default="samples/manifest.jsonl")
    batch.add_argument("--config", default="configs/default.json")
    batch.add_argument("--state-dir", default="artifacts/state")
    batch.add_argument("--scene-id", required=True)
    batch.add_argument("--out", default="")
    batch.add_argument("--reset-state", action="store_true")

    preview = sub.add_parser("preview", help="query one region preview")
    preview.add_argument("--manifest", default="samples/manifest.jsonl")
    preview.add_argument("--config", default="configs/default.json")
    preview.add_argument("--state-dir", default="artifacts/state")
    preview.add_argument("--scene-id", required=True)
    preview.add_argument("--region", nargs=2, type=int, required=True)
    preview.add_argument("--budget-ms", type=float, default=None)
    preview.add_argument("--out", default="")
    preview.add_argument("--reset-state", action="store_true")

    gui = sub.add_parser("gui", help="open the PySide6 visual workbench")
    gui.add_argument("--manifest", default="samples/manifest.jsonl")
    gui.add_argument("--config", default="configs/default.json")
    gui.add_argument("--state-dir", default="artifacts/workbench_state")
    gui.add_argument("--smoke-test", action="store_true")

    snapshot = sub.add_parser(
        "snapshot-update", help="atomically commit one versioned snapshot payload"
    )
    snapshot.add_argument("--state-dir", default="artifacts/state")
    snapshot.add_argument("--scene-id", required=True)
    snapshot.add_argument(
        "--category", choices=SNAPSHOT_CATEGORIES, required=True
    )
    snapshot.add_argument("--version", type=int, required=True)
    snapshot.add_argument("--payload", required=True)
    return parser


def _load_config(path: str) -> PipelineConfig:
    return PipelineConfig.load(path)


def _print_json(payload: Any) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


def _write_report(path: str | Path, payload: Any) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(target, payload)


def command_generate(args: argparse.Namespace) -> int:
    config = _load_config(args.config)
    paths = generate_samples(args.samples, config, args.scene_count)
    _print_json(
        {
            "business_code": "OK",
            "manifest": str(paths.manifest_path),
            "scene_count": len(paths.scene_paths),
            "scene_paths": [str(path) for path in paths.scene_paths],
        }
    )
    return 0


def command_batch(args: argparse.Namespace) -> int:
    config = _load_config(args.config)
    service = DiagnosticService(
        args.manifest,
        config,
        args.state_dir,
        reset_state=args.reset_state,
    )
    result = service.run_full(args.scene_id)
    payload = result.to_dict(service.engines[args.scene_id].score_decimals)
    if args.out:
        _write_report(args.out, payload)
    _print_json(payload)
    return 0


def command_preview(args: argparse.Namespace) -> int:
    config = _load_config(args.config)
    service = DiagnosticService(
        args.manifest,
        config,
        args.state_dir,
        reset_state=args.reset_state,
    )
    result = service.run_region(
        args.scene_id,
        int(args.region[0]),
        int(args.region[1]),
        latency_budget_ms=args.budget_ms,
    )
    payload = result.to_dict(service.engines[args.scene_id].score_decimals)
    if args.out:
        _write_report(args.out, payload)
    _print_json(payload)
    return 0


def command_snapshot_update(args: argparse.Namespace) -> int:
    store = SnapshotStore(args.state_dir, args.scene_id)
    payload = json.loads(Path(args.payload).read_text(encoding="utf-8"))
    record = store.commit(args.category, args.version, payload)
    _print_json(
        {
            "business_code": "OK",
            "category": record.category,
            "scene_id": record.scene_id,
            "version": record.version,
            "checksum": record.checksum,
        }
    )
    return 0


def command_gui(args: argparse.Namespace) -> int:
    from .gui import run_gui

    return run_gui(
        manifest=args.manifest,
        config_path=args.config,
        state_dir=args.state_dir,
        smoke_test=args.smoke_test,
    )


def command_diagnose(args: argparse.Namespace) -> int:
    config = _load_config(args.config)
    reset_state = not args.keep_state
    service = DiagnosticService(
        args.manifest,
        config,
        args.state_dir,
        reset_state=reset_state,
    )
    records = service.records
    if args.scene_limit > 0:
        records = records[: args.scene_limit]
    validation_codes = run_validation_matrix(
        service.manifest_path, records[0], config, args.state_dir
    )
    scenes: list[dict[str, Any]] = []
    all_consistent = True
    all_snapshot = True
    budget_code = "NOT_RUN"
    first_baseline_payload: dict[str, Any] | None = None
    first_preview_payload: dict[str, Any] | None = None

    for record in records:
        scene_id = record.scene_id
        batch = service.run_full(scene_id)
        score_decimals = service.engines[scene_id].score_decimals
        batch_payload = batch.to_dict(score_decimals)
        if first_baseline_payload is None:
            first_baseline_payload = batch_payload

        preview = service.preview_center_then_rest(
            scene_id,
            requested_parallel=args.parallel_preview,
            latency_budget_ms=args.budget_ms,
        )
        comparison = service.compare_scene(scene_id, preview)
        all_consistent = all_consistent and comparison["business_code"] == "OK"
        center = preview["center"]
        region_payloads = [
            result.to_dict(score_decimals)
            for result in [center, *preview["rest"]]
        ]
        if first_preview_payload is None:
            first_preview_payload = {
                "business_code": "OK",
                "scene_id": scene_id,
                "first_block": region_payloads[0],
                "first_block_observed_ms": preview["first_block_observed_ms"],
                "degradation": preview["degradation"],
                "regions": region_payloads,
                "consistency": comparison,
            }

        budget_probe = run_budget_probe(service, scene_id, center.region.row, center.region.col)
        if budget_probe.get("business_code") == "E_LATENCY_BUDGET":
            budget_code = "E_LATENCY_BUDGET"

        old_instance = batch.instances[0] if batch.instances else None
        snapshots = run_snapshot_probes(
            service,
            scene_id,
            old_instance,
            args.state_dir,
            score_decimals,
        )
        all_snapshot = all_snapshot and snapshots["verified"]
        speedup = (
            batch.elapsed_ms / center.elapsed_ms
            if center.elapsed_ms > 0
            else None
        )
        scenes.append(
            {
                "business_code": "OK",
                "scene_id": scene_id,
                "canvas_shape": list(record.canvas_shape),
                "person_count": record.person_count,
                "batch": batch_payload,
                "first_preview": region_payloads[0],
                "first_block_observed_ms": preview["first_block_observed_ms"],
                "first_block_speedup_vs_batch": (
                    round(speedup, 6) if speedup is not None else None
                ),
                "region_progress": {
                    "ready": len(region_payloads),
                    "total": config.region_rows * config.region_cols,
                    "completed_regions": preview["completed_regions"],
                },
                "degradation": preview["degradation"],
                "consistency": comparison,
                "snapshot_invalidation": snapshots,
                "latency_budget_probe": budget_probe,
            }
        )

    acceptance = {
        "region_preview_batch_byte_identical": all_consistent,
        "snapshot_invalidation_verified": all_snapshot,
        "latency_budget_code_verified": budget_code == "E_LATENCY_BUDGET",
        "validation_codes_verified": all(
            item["observed_code"] == item["expected_code"]
            for item in validation_codes
        ),
    }
    report = {
        "business_code": "OK" if all(acceptance.values()) else "E_ACCEPTANCE_FAILED",
        "report_version": 1,
        "environment": {
            "python": sys.version,
            "executable": sys.executable,
        },
        "config": {
            "quantization_decimals": config.quantization_decimals,
            "refine_radius": config.refine_radius,
            "peak_threshold": config.peak_threshold,
            "region_grid": [config.region_rows, config.region_cols],
            "center_region": list(config.center_region),
            "latency_budget_ms": config.latency_budget_ms,
            "max_scene_pixels": config.max_scene_pixels,
            "max_peak_memory_mb": config.max_peak_memory_mb,
            "max_parallel_previews": config.max_parallel_previews,
        },
        "validation_codes": validation_codes,
        "acceptance": acceptance,
        "business_codes_observed": sorted(
            {
                item["observed_code"] for item in validation_codes
            }
            | {budget_code}
            | {
                code
                for scene in scenes
                for code in _codes_from_scene(scene)
            }
        ),
        "scenes": scenes,
    }
    _write_report(args.report, report)
    if first_baseline_payload is not None:
        _write_report(args.baseline_report, first_baseline_payload)
    if first_preview_payload is not None:
        _write_report(args.preview_report, first_preview_payload)
    _print_json(
        {
            "business_code": report["business_code"],
            "report": str(Path(args.report)),
            "acceptance": acceptance,
            "scene_count": len(scenes),
            "business_codes_observed": report["business_codes_observed"],
        }
    )
    return 0 if report["business_code"] == "OK" else 1


def _codes_from_scene(scene: dict[str, Any]) -> list[str]:
    codes = [scene["consistency"]["business_code"]]
    codes.append(scene["snapshot_invalidation"]["business_code"])
    for key in (
        "scoring_only",
        "metadata_correction",
        "heatmap_replacement",
        "rollback_attempt",
        "corruption_probe",
        "stale_result",
        "replay",
    ):
        item = scene["snapshot_invalidation"].get(key)
        if isinstance(item, dict):
            codes.append(str(item.get("business_code", "OK")))
    probe = scene["latency_budget_probe"]
    if isinstance(probe, dict):
        codes.append(str(probe.get("business_code", "OK")))
    return codes


def run_budget_probe(
    service: DiagnosticService, scene_id: str, row: int, col: int
) -> dict[str, Any]:
    try:
        service.run_region(
            scene_id,
            row,
            col,
            latency_budget_ms=1e-9,
            completed_regions=["r00c00"],
        )
    except BusinessError as exc:
        return exc.to_dict()
    return {
        "business_code": "OK",
        "message": "budget probe unexpectedly remained under budget",
    }


def run_snapshot_probes(
    service: DiagnosticService,
    scene_id: str,
    old_instance: Any,
    state_dir: str | Path,
    score_decimals: int,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "business_code": "OK",
        "verified": False,
        "initial_versions": service.validation_snapshot(scene_id)["versions"],
    }
    current_versions = dict(result["initial_versions"])
    try:
        scoring_plan = service.make_scoring_update(
            scene_id, int(current_versions["scoring"]) + 1
        )
        scoring_batch = service.run_full(scene_id)
        result["scoring_only"] = {
            **scoring_plan,
            "cache_stats": scoring_batch.cache_stats,
            "instance_count": len(scoring_batch.instances),
            "metrics": scoring_batch.metrics,
        }
        stale = _capture_error(
            lambda: service.reject_if_stale(scene_id, old_instance)
        )
        result["stale_result"] = stale
        fresh_record = (
            scoring_batch.instances[0].instance_id
            if scoring_batch.instances
            else None
        )
        result["replay"] = (
            service.replay_instance(scene_id, fresh_record)
            if fresh_record is not None
            else {"business_code": "E_INSTANCE_NOT_FOUND"}
        )

        metadata_plan = service.make_metadata_correction(
            scene_id, int(current_versions["keypoint_metadata"]) + 1
        )
        metadata_batch = service.run_full(scene_id)
        result["metadata_correction"] = {
            **metadata_plan,
            "cache_stats": metadata_batch.cache_stats,
            "metrics": metadata_batch.metrics,
        }

        heatmap_plan = service.make_heatmap_update(
            scene_id, int(current_versions["heatmap_observation"]) + 1
        )
        heatmap_batch = service.run_full(scene_id)
        result["heatmap_replacement"] = {
            **heatmap_plan,
            "cache_stats": heatmap_batch.cache_stats,
            "instance_count": len(heatmap_batch.instances),
            "metrics": heatmap_batch.metrics,
        }
        rollback = _capture_error(
            lambda: service.commit_snapshot(
                scene_id,
                "scoring",
                1,
                service.bundles[scene_id].scoring_config,
            )
        )
        result["rollback_attempt"] = rollback
        result["corruption_probe"] = _corruption_probe(
            service, state_dir, scene_id
        )
        result["final_versions"] = service.validation_snapshot(scene_id)["versions"]
        result["verified"] = (
            scoring_plan["change_category"] == "scoring"
            and scoring_batch.cache_stats.get("peaks", {}).get("hits", 0) >= 1
            and scoring_batch.cache_stats.get("group", {}).get("hits", 0) >= 1
            and metadata_plan["change_category"] == "keypoint_metadata"
            and metadata_batch.cache_stats.get("peaks", {}).get("hits", 0) >= 1
            and heatmap_plan["change_category"] == "heatmap_observation"
            and heatmap_batch.cache_stats.get("peaks", {}).get("hits", 0) == 0
            and stale.get("business_code") == "E_STALE_RESULT"
            and rollback.get("business_code") == "E_SNAPSHOT_ROLLBACK"
            and result["corruption_probe"].get("business_code")
            == "E_SNAPSHOT_CORRUPT"
            and result["replay"].get("business_code") == "OK"
        )
    except BusinessError as exc:
        result["business_code"] = exc.code
        result["error"] = exc.to_dict()
    return result


def _capture_error(callback: Any) -> dict[str, Any]:
    try:
        payload = callback()
    except BusinessError as exc:
        return exc.to_dict()
    if isinstance(payload, dict):
        return payload
    return {"business_code": "OK"}


def _corruption_probe(
    service: DiagnosticService, state_dir: str | Path, scene_id: str
) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="pose-snapshot-corrupt-") as temp:
        temp_root = Path(temp)
        shutil.copytree(
            Path(state_dir) / scene_id,
            temp_root / scene_id,
            dirs_exist_ok=True,
        )
        store = SnapshotStore(temp_root, scene_id)
        category = "scoring"
        path = store.path_for(category)
        document = json.loads(path.read_text(encoding="utf-8"))
        document["payload"]["tampered_probe"] = True
        atomic_write_text(path, canonical_json_text(document))
        return _capture_error(lambda: store.load(category))


def run_validation_matrix(
    manifest_path: str | Path,
    record: Any,
    config: PipelineConfig,
    state_dir: str | Path,
) -> list[dict[str, Any]]:
    sample = load_scene_sample(manifest_path, record, config)
    probes: list[tuple[str, str, Any]] = []

    def duplicate_probe() -> None:
        with tempfile.TemporaryDirectory(prefix="pose-manifest-") as temp:
            path = Path(temp) / "manifest.jsonl"
            line = canonical_json_text(record.to_dict())
            atomic_write_text(path, f"{line}\n{line}\n")
            load_manifest(path)

    probes.append(("duplicate_scene_id", "E_DUPLICATE_SCENE_ID", duplicate_probe))
    probes.append(
        (
            "canvas_shape",
            "E_CANVAS_INVALID",
            lambda: validate_scene_record(
                replace(record, canvas_shape=(0, 640, 3)), config
            ),
        )
    )
    probes.append(
        (
            "occlusion_ratio",
            "E_OCCLUSION_RATIO",
            lambda: validate_scene_record(replace(record, occlusion_ratio=1.5), config),
        )
    )
    bad_metadata = copy.deepcopy(sample.metadata)
    bad_metadata["persons"] = bad_metadata["persons"][:-1]
    probes.append(
        (
            "person_count_metadata",
            "E_PERSON_COUNT_MISMATCH",
            lambda: validate_metadata(
                bad_metadata,
                expected_channels=record.num_keypoints,
                expected_persons=record.person_count,
                scene_id=record.scene_id,
            ),
        )
    )
    bad_scoring = copy.deepcopy(sample.scoring)
    bad_scoring["match_threshold"] = 0
    probes.append(
        (
            "match_threshold",
            "E_MATCH_THRESHOLD",
            lambda: validate_scoring(bad_scoring, scene_id=record.scene_id),
        )
    )
    probes.append(
        (
            "channel_mismatch",
            "E_CHANNEL_MISMATCH",
            lambda: validate_heatmap_channels(
                sample.observation_heatmaps[:-1],
                record.num_keypoints,
                record.scene_id,
            ),
        )
    )
    probes.append(
        (
            "quantization",
            "E_QUANT_INVALID",
            lambda: PipelineConfig.from_dict(
                {
                    **config.__dict__,
                    "quantization_decimals": 9,
                    "region_grid": [config.region_rows, config.region_cols],
                    "center_region": list(config.center_region),
                    "scoring": config.scoring,
                }
            ),
        )
    )
    probes.append(
        (
            "pixel_resource_limit",
            "E_RESOURCE_LIMIT",
            lambda: validate_scene_record(
                record, replace(config, max_scene_pixels=1)
            ),
        )
    )
    probes.append(
        (
            "memory_resource_limit",
            "E_RESOURCE_LIMIT",
            lambda: validate_scene_record(
                record, replace(config, max_peak_memory_mb=0.000001)
            ),
        )
    )

    results: list[dict[str, Any]] = []
    for name, expected, callback in probes:
        observed = _capture_error(callback)
        results.append(
            {
                "probe": name,
                "expected_code": expected,
                "observed_code": observed.get("business_code"),
                "field": observed.get("field"),
                "scene_id": observed.get("scene_id"),
                "verified": observed.get("business_code") == expected,
            }
        )
    return results


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "generate":
            return command_generate(args)
        if args.command == "diagnose":
            return command_diagnose(args)
        if args.command == "batch":
            return command_batch(args)
        if args.command == "preview":
            return command_preview(args)
        if args.command == "snapshot-update":
            return command_snapshot_update(args)
        if args.command == "gui":
            return command_gui(args)
    except BusinessError as exc:
        _print_json({**exc.to_dict(), "status": "ERROR"})
        return 2
    except Exception as exc:  # pragma: no cover - last-resort CLI guard
        _print_json(
            {
                "business_code": "E_INTERNAL",
                "message": str(exc),
                "status": "ERROR",
            }
        )
        return 3
    parser.error("unknown command")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

