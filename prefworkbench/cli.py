"""Command-line entry points for the deterministic preference workbench."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .canonical import read_json, verify_sealed
from .config import load_config
from .errors import E_STALE_RESULT, BusinessError
from .generator import write_sample
from .pipeline import run_pipeline
from .selfcheck import run_selfcheck
from .snapshots import SnapshotStore


def _print(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2))


def _run_summary(result: dict[str, Any]) -> dict[str, Any]:
    manifest = result["manifest"]
    return {
        "ok": True,
        "manifest_file": result["manifest_file"],
        "manifest_content_hash": manifest["content_hash"],
        "manifest_file_sha256": result["manifest_file_sha256"],
        "stage_hashes": result["stage_hashes"],
        "config": manifest["config"],
        "status_counts": manifest["status_counts"],
        "business_codes": manifest["business_codes"],
        "selected_plan": manifest["schedule"]["selected_plan"],
        "schedule_adjudications": manifest["schedule"]["adjudications"],
        "sampling": {
            "seed": manifest["sampling_seed"],
            "selected_count": len(manifest["sampling_sequence"]),
            "quota_stats": manifest["quota_stats"],
        },
        "incremental": result["diagnostics"]["incremental"],
        "stage_audit": result["diagnostics"]["stage_audit"],
        "selfcheck": manifest["selfcheck"],
    }


def command_generate(args: argparse.Namespace) -> int:
    result = write_sample(
        args.out, args.config, count=args.count, seed=args.seed
    )
    _print({"ok": True, **result})
    return 0


def command_run(args: argparse.Namespace) -> int:
    result = run_pipeline(
        input_path=args.input,
        config_path=args.config,
        out_dir=args.out,
        cache_root=args.cache,
        cold_cache=args.cold,
    )
    _print(_run_summary(result))
    return 0


def command_check_stale(args: argparse.Namespace) -> int:
    manifest = verify_sealed(read_json(args.manifest), context="manifest")
    snapshot = load_config(args.config)
    store = SnapshotStore(Path(args.cache) / "snapshots")
    current = store.current()
    if current is not None and int(current["version"]) > snapshot.version:
        raise BusinessError(
            E_STALE_RESULT,
            "config is older than the published snapshot",
            details={
                "config_version": snapshot.version,
                "published_version": int(current["version"]),
            },
        )
    store.assert_fresh(manifest, snapshot)
    _print(
        {
            "ok": True,
            "code": None,
            "config": {"version": snapshot.version, "checksum": snapshot.checksum},
            "manifest_config": manifest["config"],
        }
    )
    return 0


def command_verify(args: argparse.Namespace) -> int:
    run_dir = Path(args.run)
    manifest = read_json(run_dir / "manifest.json")
    quality = read_json(run_dir / "quality.json")
    neardup = read_json(run_dir / "neardup.json")
    consistency = read_json(run_dir / "consistency.json")
    sampling = read_json(run_dir / "sampling.json")
    schedule = read_json(run_dir / "schedule_report.json")
    verify_sealed(manifest, context="manifest")
    verify_sealed(quality, context="quality")
    verify_sealed(neardup, context="neardup")
    verify_sealed(consistency, context="consistency")
    verify_sealed(sampling, context="sampling")
    verify_sealed(schedule, context="schedule_report")
    diagnostics = read_json(run_dir / "diagnostics.json")
    active_pair_ids = list(quality.get("successful_pair_ids", []))
    selfcheck = run_selfcheck(
        manifest,
        quality,
        neardup,
        consistency,
        sampling,
        schedule,
        active_pair_ids=active_pair_ids,
        stage_audit=diagnostics.get("stage_audit", {}),
    )
    expected_hashes = {
        "quality": quality["content_hash"],
        "neardup": neardup["content_hash"],
        "consistency": consistency["content_hash"],
        "sampling": sampling["content_hash"],
        "schedule_report": schedule["content_hash"],
        "manifest": manifest["content_hash"],
    }
    recorded_hashes = read_json(run_dir / "hashes.json")
    if expected_hashes != recorded_hashes:
        raise BusinessError(
            "E_SELFCHECK_FAILED",
            "recorded stage hashes do not match artifacts",
            details={"expected": expected_hashes, "recorded": recorded_hashes},
        )
    result = {
        "ok": selfcheck["ok"],
        "manifest_content_hash": manifest["content_hash"],
        "stage_hashes": expected_hashes,
        "selfcheck": selfcheck,
    }
    _print(result)
    return 0 if selfcheck["ok"] else 1


def command_snapshot(args: argparse.Namespace) -> int:
    store = SnapshotStore(Path(args.cache) / "snapshots")
    current = store.current()
    _print({"ok": True, "current": current})
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="prefwb", description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    generate = subparsers.add_parser("generate", help="write deterministic sample JSONL and config")
    generate.add_argument("--out", default="data/sample.jsonl")
    generate.add_argument("--config", default="configs/default.json")
    generate.add_argument("--count", type=int, default=120)
    generate.add_argument("--seed", type=int, default=20261123)
    generate.set_defaults(func=command_generate)

    run = subparsers.add_parser("run", help="execute the full pipeline")
    run.add_argument("--input", default="data/sample.jsonl")
    run.add_argument("--config", default="configs/default.json")
    run.add_argument("--out", default="runs/latest")
    run.add_argument("--cache", default="cache/main")
    run.add_argument(
        "--cold",
        action="store_true",
        help="ignore cache reads while still writing deterministic artifacts",
    )
    run.set_defaults(func=command_run)

    stale = subparsers.add_parser("check-stale", help="reject a manifest from another config/version")
    stale.add_argument("--manifest", required=True)
    stale.add_argument("--config", required=True)
    stale.add_argument("--cache", default="cache/main")
    stale.set_defaults(func=command_check_stale)

    verify = subparsers.add_parser("verify", help="verify sealed artifacts and cross-module checks")
    verify.add_argument("--run", default="runs/latest")
    verify.set_defaults(func=command_verify)

    snapshot = subparsers.add_parser("snapshot", help="show the currently published config snapshot")
    snapshot.add_argument("--cache", default="cache/main")
    snapshot.set_defaults(func=command_snapshot)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except BusinessError as exc:
        _print({"ok": False, "error": exc.to_dict()})
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

