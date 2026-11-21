from __future__ import annotations

import argparse
import json
import multiprocessing
import os
import platform
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

import numpy as np

from .cache import CacheStore
from .canonical import pretty_json_bytes
from .config import config_digest
from .engine import PipelineEngine
from .errors import PipelineError
from .fixtures import write_fixtures
from .replay import load_results_bytes, replay_hit, replay_snapshot
from .samples import load_bundle
from .state import StateStore
from .verify import _load_manifest, verify_snapshot


def _emit(value: Any, path: str | None = None) -> None:
    data = pretty_json_bytes(value)
    if path:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    sys.stdout.buffer.write(data)


def _engine(
    *,
    data: str,
    config: str,
    vocab: str,
    codebook: str,
    cache: str,
    state: str,
    workers: int,
) -> PipelineEngine:
    bundle = load_bundle(data, config, vocab, codebook)
    return PipelineEngine(
        bundle,
        cache_root=cache,
        state_root=state,
        workers=workers,
    )


def _report_summary(report: dict[str, Any]) -> dict[str, Any]:
    return {
        "build_kind": report["build_kind"],
        "snapshot_id": report["snapshot_id"],
        "snapshot_version": report["snapshot_version"],
        "affected_pair_ids": report["affected_pair_ids"],
        "recomputed_pair_ids": report["recomputed_pair_ids"],
        "reused_pair_ids": report["reused_pair_ids"],
        "recomputed_nodes": report["recomputed_nodes"],
        "reused_nodes": report["reused_nodes"],
        "recomputed_index_blocks": report["recomputed_index_blocks"],
        "reused_index_blocks": report["reused_index_blocks"],
        "degraded": report["degraded"],
        "cache": report["cache"],
        "retrieval": report["retrieval"],
    }


def _error_doc(exc: PipelineError) -> dict[str, Any]:
    return {"ok": False, "error": exc.to_dict()}


def _capture_error(callable_obj, *args, **kwargs) -> dict[str, Any]:
    try:
        callable_obj(*args, **kwargs)
    except PipelineError as exc:
        return _error_doc(exc)
    return {"ok": True}


def _environment_report() -> dict[str, Any]:
    temp_dir = tempfile.mkdtemp(prefix="cm-env-check-")
    cache_dir = tempfile.mkdtemp(prefix="cm-cache-check-")
    writable = os.access(temp_dir, os.W_OK) and os.access(cache_dir, os.W_OK)
    platform_text = platform.platform()
    is_wsl = "microsoft" in platform_text.lower() or "wsl" in platform_text.lower()
    return {
        "python_version": platform.python_version(),
        "python_implementation": platform.python_implementation(),
        "numpy_version": np.__version__,
        "os_name": os.name,
        "platform": platform_text,
        "machine": platform.machine(),
        "is_wsl": is_wsl,
        "windows_runtime_verified": os.name == "nt",
        "windows_interop_available": False if is_wsl else os.name == "nt",
        "temp_and_cache_writable": bool(writable),
        "process_start_method": multiprocessing.get_start_method(),
        "cpu_count": multiprocessing.cpu_count(),
        "notes": (
            "The active runtime is Linux under WSL2, not a native Windows Python "
            "process. The implementation uses pathlib, os.replace, O_EXCL locks "
            "and multiprocessing APIs supported by Windows; native Windows "
            "execution must be re-run on a Windows host."
            if is_wsl
            else "Native platform execution was detected."
        ),
    }


def command_init(args: argparse.Namespace) -> dict[str, Any]:
    manifest = write_fixtures(Path(args.root))
    return {"ok": True, "fixtures": manifest}


def command_validate(args: argparse.Namespace) -> dict[str, Any]:
    bundle = load_bundle(args.data, args.config, args.vocab, args.codebook)
    return {
        "ok": True,
        "sample_count": len(bundle.samples),
        "config_digest": config_digest(bundle.config),
        "vocab": {
            "version": bundle.vocab["version"],
            "checksum": bundle.vocab["checksum"],
        },
        "codebook": {
            "version": bundle.codebook["version"],
            "checksum": bundle.codebook["checksum"],
        },
    }


def command_build_full(args: argparse.Namespace) -> dict[str, Any]:
    engine = _engine(
        data=args.data,
        config=args.config,
        vocab=args.vocab,
        codebook=args.codebook,
        cache=args.cache,
        state=args.state,
        workers=args.workers,
    )
    return engine.build_full(report_path=args.report)


def command_build_incremental(args: argparse.Namespace) -> dict[str, Any]:
    engine = _engine(
        data=args.data,
        config=args.config,
        vocab=args.vocab,
        codebook=args.codebook,
        cache=args.cache,
        state=args.state,
        workers=args.workers,
    )
    return engine.build_incremental(
        expected_version=args.expected_version,
        target_version=args.target_version,
        report_path=args.report,
    )


def command_inspect_key(args: argparse.Namespace) -> dict[str, Any]:
    from .cache import CacheStore

    return {"ok": True, "entry": CacheStore(args.cache).inspect(args.key)}


def command_verify(args: argparse.Namespace) -> dict[str, Any]:
    report = verify_snapshot(
        cache_root=args.cache,
        state_root=args.state,
        snapshot_version=args.snapshot_version,
        require_current=not args.historical,
    )
    if args.report:
        Path(args.report).parent.mkdir(parents=True, exist_ok=True)
        Path(args.report).write_bytes(pretty_json_bytes(report))
    return report


def command_replay(args: argparse.Namespace) -> dict[str, Any]:
    report = replay_snapshot(
        cache_root=args.cache,
        state_root=args.state,
        snapshot_version=args.snapshot_version,
        require_current=not args.historical,
    )
    if args.report:
        Path(args.report).parent.mkdir(parents=True, exist_ok=True)
        Path(args.report).write_bytes(pretty_json_bytes(report))
    return report


def command_results(args: argparse.Namespace) -> dict[str, Any]:
    data = load_results_bytes(
        cache_root=args.cache,
        state_root=args.state,
        snapshot_version=args.snapshot_version,
    )
    if args.output:
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output).write_bytes(data)
    return {
        "ok": True,
        "snapshot_version": args.snapshot_version,
        "bytes": len(data),
        "sha256": __import__("hashlib").sha256(data).hexdigest(),
        "output": args.output,
    }


def command_show(args: argparse.Namespace) -> dict[str, Any]:
    with Path(args.path).open("r", encoding="utf-8") as handle:
        return json.load(handle)


def command_demo(args: argparse.Namespace) -> dict[str, Any]:
    demo_root = Path(args.root)
    if demo_root.exists():
        shutil.rmtree(demo_root)
    demo_root.mkdir(parents=True, exist_ok=True)
    fixture_manifest = write_fixtures(demo_root)
    data = str(demo_root / "data" / "samples.jsonl")
    data_updated = str(demo_root / "data" / "samples_updated.jsonl")
    vocab = str(demo_root / "config" / "vocab.json")
    config_v1 = str(demo_root / "config" / "pipeline.json")
    config_v2 = str(demo_root / "config" / "pipeline_align_v2.json")
    codebook_v1 = str(demo_root / "config" / "codebook_v1.json")
    codebook_v2 = str(demo_root / "config" / "codebook_v2.json")
    cache = str(demo_root / "cache")
    state = str(demo_root / "state")

    def build(
        name: str,
        *,
        kind: str,
        data_path: str,
        config_path: str,
        codebook_path: str,
        expected_version: int | None = None,
        workers: int = 1,
    ) -> dict[str, Any]:
        engine = _engine(
            data=data_path,
            config=config_path,
            vocab=vocab,
            codebook=codebook_path,
            cache=cache,
            state=state,
            workers=workers,
        )
        if kind == "full":
            report = engine.build_full()
        else:
            report = engine.build_incremental(expected_version=expected_version)
        summary = _report_summary(report)
        summary["name"] = name
        return summary

    environment = _environment_report()
    builds: list[dict[str, Any]] = []
    builds.append(
        build(
            "full_initial",
            kind="full",
            data_path=data,
            config_path=config_v1,
            codebook_path=codebook_v1,
            workers=args.workers,
        )
    )
    builds.append(
        build(
            "incremental_text_and_image",
            kind="incremental",
            data_path=data_updated,
            config_path=config_v1,
            codebook_path=codebook_v1,
            expected_version=1,
            workers=args.workers,
        )
    )
    builds.append(
        build(
            "incremental_codebook_v2",
            kind="incremental",
            data_path=data_updated,
            config_path=config_v1,
            codebook_path=codebook_v2,
            expected_version=2,
            workers=args.workers,
        )
    )
    builds.append(
        build(
            "incremental_align_v2",
            kind="incremental",
            data_path=data_updated,
            config_path=config_v2,
            codebook_path=codebook_v2,
            expected_version=3,
            workers=args.workers,
        )
    )

    fresh_root = demo_root / "fresh_full"
    fresh_engine = _engine(
        data=data_updated,
        config=config_v2,
        vocab=vocab,
        codebook=codebook_v2,
        cache=str(fresh_root / "cache"),
        state=str(fresh_root / "state"),
        workers=1,
    )
    fresh_report = fresh_engine.build_full()
    fresh_summary = _report_summary(fresh_report)
    fresh_summary["name"] = "fresh_full_final"
    builds.append(fresh_summary)

    incremental_bytes = load_results_bytes(
        cache_root=cache,
        state_root=state,
        snapshot_version=4,
    )
    fresh_bytes = load_results_bytes(
        cache_root=str(fresh_root / "cache"),
        state_root=str(fresh_root / "state"),
        snapshot_version=1,
    )
    (demo_root / "results_incremental.json").write_bytes(incremental_bytes)
    (demo_root / "results_fresh_full.json").write_bytes(fresh_bytes)
    verification = verify_snapshot(
        cache_root=cache,
        state_root=state,
        snapshot_version=4,
    )
    replay = replay_snapshot(
        cache_root=cache,
        state_root=state,
        snapshot_version=4,
    )
    initial_snapshot = StateStore(state).load_snapshot(1)
    initial_doc, _ = _load_manifest(
        CacheStore(cache),
        initial_snapshot,
    )
    initial_hit = initial_doc["queries"][0]["hits"][0]
    stale_guard = _capture_error(
        replay_hit,
        initial_hit,
        cache_root=cache,
        state_root=state,
        require_current=True,
    )
    old_version_guard = _capture_error(
        _engine(
            data=data_updated,
            config=config_v2,
            vocab=vocab,
            codebook=codebook_v2,
            cache=cache,
            state=state,
            workers=1,
        ).build_incremental,
        expected_version=1,
    )
    codebook_rollback_guard = _capture_error(
        _engine(
            data=data_updated,
            config=config_v2,
            vocab=vocab,
            codebook=codebook_v1,
            cache=cache,
            state=state,
            workers=1,
        ).build_incremental,
        expected_version=4,
    )
    state_store = StateStore(state)
    with state_store.commit_lock():
        concurrency_guard = _capture_error(
            _engine(
                data=data_updated,
                config=config_v2,
                vocab=vocab,
                codebook=codebook_v2,
                cache=cache,
                state=state,
                workers=1,
            ).build_incremental,
            expected_version=4,
        )

    invalid_results: dict[str, Any] = {}
    for name, path in fixture_manifest["invalid"].items():
        invalid_results[name] = _capture_error(
            load_bundle,
            path,
            config_v1,
            vocab,
            codebook_v1,
        )
    checks = {
        "incremental_matches_fresh_full_bytes": incremental_bytes == fresh_bytes,
        "verification_ok": bool(verification["ok"]),
        "replay_ok": bool(replay["ok"]),
        "stale_hit_rejected": stale_guard.get("error", {}).get("code")
        == "E_LINEAGE_BROKEN",
        "old_version_commit_rejected": old_version_guard.get("error", {}).get("code")
        == "E_SNAPSHOT_ROLLBACK",
        "codebook_rollback_rejected": codebook_rollback_guard.get("error", {}).get(
            "code"
        )
        == "E_SNAPSHOT_ROLLBACK",
        "concurrent_commit_busy": concurrency_guard.get("error", {}).get("code")
        == "E_STAGE_BUSY",
        "all_invalid_inputs_have_business_codes": all(
            not item.get("ok") for item in invalid_results.values()
        ),
        "process_pool_and_serial_produce_same_bytes": incremental_bytes
        == fresh_bytes,
    }
    report = {
        "schema": "cm-demo-report-v1",
        "environment": environment,
        "fixtures": fixture_manifest,
        "builds": builds,
        "result_comparison": {
            "incremental_snapshot_version": 4,
            "fresh_full_snapshot_version": 1,
            "incremental_results_sha256": __import__("hashlib").sha256(
                incremental_bytes
            ).hexdigest(),
            "fresh_full_results_sha256": __import__("hashlib").sha256(
                fresh_bytes
            ).hexdigest(),
            "incremental_results_bytes": len(incremental_bytes),
            "fresh_full_results_bytes": len(fresh_bytes),
            "byte_identical": incremental_bytes == fresh_bytes,
            "incremental_file": str(demo_root / "results_incremental.json"),
            "fresh_full_file": str(demo_root / "results_fresh_full.json"),
        },
        "verification": verification,
        "replay": replay,
        "lineage_guard": stale_guard,
        "snapshot_rollback_guard": old_version_guard,
        "codebook_rollback_guard": codebook_rollback_guard,
        "concurrency_guard": concurrency_guard,
        "invalid_inputs": invalid_results,
        "checks": checks,
        "ok": all(checks.values()),
    }
    if not report["ok"]:
        report["error"] = {
            "code": "E_VERIFY_FAIL",
            "message": "One or more demo checks failed",
            "failed_checks": sorted(
                name for name, passed in checks.items() if not passed
            ),
        }
    return report


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cm-pipeline",
        description="Deterministic cross-modal incremental retrieval pipeline",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init", help="Generate deterministic local fixtures")
    init.add_argument("--root", default=".")
    init.set_defaults(func=command_init)

    validate = sub.add_parser("validate", help="Validate inputs and emit business codes")
    validate.add_argument("--data", required=True)
    validate.add_argument("--config", required=True)
    validate.add_argument("--vocab", required=True)
    validate.add_argument("--codebook", required=True)
    validate.set_defaults(func=command_validate)

    full = sub.add_parser("build-full", help="Build a full snapshot")
    full.add_argument("--data", required=True)
    full.add_argument("--config", required=True)
    full.add_argument("--vocab", required=True)
    full.add_argument("--codebook", required=True)
    full.add_argument("--cache", required=True)
    full.add_argument("--state", required=True)
    full.add_argument("--workers", type=int, default=1)
    full.add_argument("--report")
    full.set_defaults(func=command_build_full)

    incremental = sub.add_parser("build-incremental", help="Apply an incremental commit")
    incremental.add_argument("--data", required=True)
    incremental.add_argument("--config", required=True)
    incremental.add_argument("--vocab", required=True)
    incremental.add_argument("--codebook", required=True)
    incremental.add_argument("--cache", required=True)
    incremental.add_argument("--state", required=True)
    incremental.add_argument("--expected-version", type=int, required=True)
    incremental.add_argument("--target-version", type=int)
    incremental.add_argument("--workers", type=int, default=1)
    incremental.add_argument("--report")
    incremental.set_defaults(func=command_build_incremental)

    inspect_key = sub.add_parser("inspect-key", help="Enumerate cache key components")
    inspect_key.add_argument("--cache", required=True)
    inspect_key.add_argument("--key", required=True)
    inspect_key.set_defaults(func=command_inspect_key)

    verify = sub.add_parser("verify", help="Run independent consistency verification")
    verify.add_argument("--cache", required=True)
    verify.add_argument("--state", required=True)
    verify.add_argument("--snapshot-version", type=int)
    verify.add_argument("--historical", action="store_true")
    verify.add_argument("--report")
    verify.set_defaults(func=command_verify)

    replay = sub.add_parser("replay", help="Replay scores and top-k ordering")
    replay.add_argument("--cache", required=True)
    replay.add_argument("--state", required=True)
    replay.add_argument("--snapshot-version", type=int)
    replay.add_argument("--historical", action="store_true")
    replay.add_argument("--report")
    replay.set_defaults(func=command_replay)

    results = sub.add_parser("export-results", help="Write canonical retrieval JSON")
    results.add_argument("--cache", required=True)
    results.add_argument("--state", required=True)
    results.add_argument("--snapshot-version", type=int)
    results.add_argument("--output", required=True)
    results.set_defaults(func=command_results)

    show = sub.add_parser("show-report", help="Print a JSON report")
    show.add_argument("path")
    show.set_defaults(func=command_show)

    demo = sub.add_parser("demo", help="Run the complete acceptance demonstration")
    demo.add_argument("--root", default="artifacts/demo")
    demo.add_argument("--report", default="artifacts/report.json")
    demo.add_argument("--workers", type=int, default=2)
    demo.set_defaults(func=command_demo)
    return parser


def main(argv: list[str] | None = None) -> int:
    multiprocessing.freeze_support()
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        result = args.func(args)
    except PipelineError as exc:
        _emit(_error_doc(exc))
        return 2
    except Exception as exc:  # pragma: no cover - defensive CLI boundary
        _emit(
            {
                "ok": False,
                "error": {
                    "code": "E_VERIFY_FAIL",
                    "message": f"Unhandled exception: {exc}",
                    "stage": "cli",
                    "field": None,
                    "pair_id": None,
                },
            }
        )
        return 2
    if args.command == "demo" and args.report:
        target = Path(args.report)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(pretty_json_bytes(result))
    _emit(result)
    return 0 if result.get("ok", True) else 2


if __name__ == "__main__":
    raise SystemExit(main())

