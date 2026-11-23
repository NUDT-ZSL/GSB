"""Command line interface."""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import sys
from typing import Any

import numpy as np

from .cache import ArtifactCache
from .config import PipelineConfig
from .errors import SceneOptError
from .models import load_scene_manifest
from .pipeline import Pipeline
from .samples import ensure_samples
from .serialization import canonical_bytes
from .snapshot import SnapshotStore
from .verify import verify_snapshot


def _write_json(path: str, value: Any) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "wb") as handle:
        handle.write(canonical_bytes(value))


def _load_config(path: str | None) -> PipelineConfig:
    if path:
        return PipelineConfig.from_json_file(path)
    return PipelineConfig()


def _summary(report: dict[str, Any]) -> dict[str, Any]:
    return {
        "run_mode": report["run_mode"],
        "snapshot_version": report["snapshot_version"],
        "snapshot_checksum": report["snapshot_checksum"],
        "affected_scenes": report["affected_scenes"],
        "recomputed_scenes": report["recomputed_scenes"],
        "reused_scenes": report["reused_scenes"],
        "stage_recomputed": report["stage_recomputed"],
        "stage_reused": report["stage_reused"],
        "self_check_passed": report["self_check"]["passed"],
        "full_replay_verified": report["full_replay_verified"],
        "scenes": report["scenes"],
    }


def _cmd_generate(args: argparse.Namespace) -> int:
    paths = ensure_samples(args.samples, force=args.force)
    print(json.dumps(paths, indent=2, sort_keys=True))
    return 0


def _cmd_run(args: argparse.Namespace) -> int:
    if args.manifest == "samples/scenes.jsonl" and not os.path.isfile(args.manifest):
        ensure_samples(os.path.dirname(args.manifest) or "samples")
    config = _load_config(args.config)
    pipeline = Pipeline(
        args.manifest,
        cache_dir=args.cache,
        snapshot_path=args.snapshot,
        config=config,
        verify_incremental=not args.no_full_replay,
    )
    result = pipeline.run()
    if args.report:
        result.write_report(args.report)
    print(json.dumps(_summary(result.report), indent=2, sort_keys=True))
    return 0


def _cmd_verify(args: argparse.Namespace) -> int:
    config = _load_config(args.config)
    scenes = load_scene_manifest(args.manifest)
    store = SnapshotStore(args.snapshot or os.path.join(args.cache, "snapshot.json"))
    snapshot = store.load()
    if snapshot is None:
        raise FileNotFoundError(f"snapshot does not exist: {store.path}")
    report = verify_snapshot(
        scenes,
        snapshot,
        ArtifactCache(args.cache),
        expected_config_digest=config.digest,
        raise_on_error=False,
    )
    report["config_digest"] = config.digest
    if args.report:
        _write_json(args.report, report)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["passed"] else 2


def _cmd_demo(args: argparse.Namespace) -> int:
    root = os.path.abspath(args.root)
    samples_dir = os.path.join(root, "samples")
    paths = ensure_samples(samples_dir, force=args.force)
    demo_cache = os.path.join(root, "cache", "demo")
    if os.path.isdir(demo_cache):
        shutil.rmtree(demo_cache)
    os.makedirs(demo_cache, exist_ok=True)
    artifacts_dir = os.path.join(root, "artifacts")
    os.makedirs(artifacts_dir, exist_ok=True)
    config = _load_config(args.config)

    baseline = Pipeline(
        paths["baseline"],
        cache_dir=demo_cache,
        snapshot_path=os.path.join(demo_cache, "snapshot.json"),
        config=config,
        verify_incremental=True,
    ).run()
    baseline_report_path = os.path.join(artifacts_dir, "baseline_report.json")
    baseline.write_report(baseline_report_path)

    revision = Pipeline(
        paths["revision"],
        cache_dir=demo_cache,
        snapshot_path=os.path.join(demo_cache, "snapshot.json"),
        config=config,
        verify_incremental=True,
    ).run()
    revision_report_path = os.path.join(artifacts_dir, "revision_report.json")
    revision.write_report(revision_report_path)

    verification = verify_snapshot(
        load_scene_manifest(paths["revision"]),
        revision.snapshot,
        revision.cache,
        expected_config_digest=config.digest,
        raise_on_error=False,
    )
    acceptance = {
        "format": "sceneopt-acceptance-v1",
        "environment": {
            "python": platform.python_version(),
            "implementation": platform.python_implementation(),
            "platform": platform.platform(),
            "numpy": np.__version__,
        },
        "baseline": {
            "report": baseline_report_path,
            "snapshot_version": baseline.report["snapshot_version"],
            "snapshot_checksum": baseline.report["snapshot_checksum"],
            "affected_scenes": baseline.report["affected_scenes"],
            "full_replay_verified": baseline.report["full_replay_verified"],
        },
        "revision": {
            "report": revision_report_path,
            "snapshot_version": revision.report["snapshot_version"],
            "snapshot_checksum": revision.report["snapshot_checksum"],
            "affected_scenes": revision.report["affected_scenes"],
            "recomputed_scenes": revision.report["recomputed_scenes"],
            "reused_scenes": revision.report["reused_scenes"],
            "stage_recomputed": revision.report["stage_recomputed"],
            "stage_reused": revision.report["stage_reused"],
            "full_replay_verified": revision.report["full_replay_verified"],
            "snapshot_replaced": revision.report["snapshot_replaced"],
            "snapshot_checksum_changed": revision.report[
                "snapshot_checksum_changed"
            ],
            "self_check_passed": revision.report["self_check"]["passed"],
        },
        "verification": verification,
        "scene_results": {
            scene_id: {
                "change_type": data["change_type"],
                "accepted": data["accepted"],
                "texture_exact": data["summary"]["texture_exact"],
                "max_abs_texture_diff": data["summary"]["max_abs_texture_diff"],
                "dominant_error_level": data["summary"]["dominant_error_level"],
                "error_spectrum": revision.scene_runs[scene_id]
                .artifacts["block_compare"]
                .payload["error_spectrum"],
            }
            for scene_id, data in revision.report["scenes"].items()
        },
    }
    acceptance_path = os.path.join(artifacts_dir, "acceptance.json")
    _write_json(acceptance_path, acceptance)
    print(
        json.dumps(
            {
                "acceptance": acceptance_path,
                "baseline_snapshot_version": baseline.report["snapshot_version"],
                "revision_snapshot_version": revision.report["snapshot_version"],
                "revision_affected_scenes": revision.report["affected_scenes"],
                "revision_reused_scenes": revision.report["reused_scenes"],
                "full_replay_verified": revision.report["full_replay_verified"],
                "verification_passed": verification["passed"],
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0 if verification["passed"] else 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="sceneopt")
    subparsers = parser.add_subparsers(dest="command", required=True)

    generate = subparsers.add_parser("generate", help="generate deterministic samples")
    generate.add_argument("--samples", default="samples")
    generate.add_argument("--force", action="store_true")
    generate.set_defaults(func=_cmd_generate)

    run = subparsers.add_parser("run", help="run or incrementally revise scenes")
    run.add_argument("--manifest", default="samples/scenes.jsonl")
    run.add_argument("--cache", default="cache")
    run.add_argument("--snapshot", default=None)
    run.add_argument("--config", default=None)
    run.add_argument("--report", default=None)
    run.add_argument("--no-full-replay", action="store_true")
    run.set_defaults(func=_cmd_run)

    verify = subparsers.add_parser("verify", help="verify a committed snapshot")
    verify.add_argument("--manifest", default="samples/revision.jsonl")
    verify.add_argument("--cache", default="cache")
    verify.add_argument("--snapshot", default=None)
    verify.add_argument("--config", default=None)
    verify.add_argument("--report", default=None)
    verify.set_defaults(func=_cmd_verify)

    demo = subparsers.add_parser("demo", help="run baseline and revision acceptance")
    demo.add_argument("--root", default=".")
    demo.add_argument("--config", default=None)
    demo.add_argument("--force", action="store_true")
    demo.set_defaults(func=_cmd_demo)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except SceneOptError as exc:
        print(json.dumps(exc.as_dict(), indent=2, sort_keys=True), file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

