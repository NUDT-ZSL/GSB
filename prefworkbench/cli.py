"""Command-line entry points for the local workbench."""

from __future__ import annotations

import argparse
import copy
import json
import sys
from pathlib import Path
from typing import Any

from .canonical import content_hash, read_json, read_jsonl, write_json
from .config import default_config, normalize_config, require_valid_config
from .errors import BusinessError, E_CONFIG_INVALID, E_SAMPLE_INVALID
from .generation import write_dataset
from .pipeline import PreferencePipeline
from .report import write_snapshot_report
from .snapshots import SnapshotStore, build_snapshot_categories


def _load_samples(path: str | Path) -> list[dict[str, Any]]:
    try:
        return read_jsonl(path)
    except (OSError, ValueError) as exc:
        raise BusinessError(
            E_SAMPLE_INVALID,
            f"cannot read JSONL samples: {exc}",
            field="samples",
            stage="input",
        ) from exc


def _load_config(path: str | Path) -> dict[str, Any]:
    try:
        raw = read_json(path)
    except (OSError, ValueError) as exc:
        raise BusinessError(
            E_CONFIG_INVALID,
            f"cannot read configuration: {exc}",
            field="config",
            stage="config_validation",
        ) from exc
    if not isinstance(raw, dict):
        raise BusinessError(
            E_CONFIG_INVALID,
            "configuration root must be an object",
            field="config",
            stage="config_validation",
        )
    config = normalize_config(raw)
    require_valid_config(config)
    return config


def _execute_run(
    samples_path: str | Path,
    config_path: str | Path,
    cache_root: str | Path,
    snapshots_root: str | Path,
    html_path: str | Path,
    incremental: bool,
    fresh: bool,
) -> dict[str, Any]:
    config = _load_config(config_path)
    samples = _load_samples(samples_path)
    store = SnapshotStore(snapshots_root)
    previous = store.previous_state() if incremental and not fresh else None
    pipeline = PreferencePipeline(config, cache_root)
    state = pipeline.run(samples, previous=previous, use_cache=not fresh)
    categories = build_snapshot_categories(state)
    committed = store.commit(categories)
    snapshot = store.load()
    write_snapshot_report(snapshot, html_path)
    state["execution"]["snapshot_version"] = committed["version"]
    state["execution"]["snapshot_checksum"] = committed["overall_checksum"]
    return {
        "snapshot": committed,
        "logic_digest": state["execution"]["logic_digest"],
        "mode": state["execution"]["mode"],
        "valid_samples": state["valid_sample_count"],
        "invalid_samples": state["invalid_sample_count"],
        "selected": {
            tier: len(state["precision_results"][tier]["participation"]["selected"])
            for tier in state["precision_order"]
        },
        "dominant_sources": {
            tier: state["error_spectrum"]["tiers"][tier]["dominant_source_counts"]
            for tier in state["precision_order"]
        },
        "recomputed_levels": state["execution"]["invalidation"]["recomputed_levels"],
        "reused_levels": state["execution"]["invalidation"]["reused_levels"],
        "checks": state["diagnostics"]["checks"],
        "html": str(html_path),
        "cache": str(cache_root),
    }


def _command_generate(args: argparse.Namespace) -> dict[str, Any]:
    config = copy.deepcopy(default_config(args.seed))
    if args.count > int(config["limits"]["max_samples"]):
        config["limits"]["max_samples"] = args.count
    result = write_dataset(args.out, seed=args.seed, count=args.count, config=config)
    return result


def _command_run(args: argparse.Namespace) -> dict[str, Any]:
    return _execute_run(
        args.samples,
        args.config,
        args.cache,
        args.snapshots,
        args.html,
        args.incremental,
        args.fresh,
    )


def _mutate_verification_sample(samples: list[dict[str, Any]]) -> list[dict[str, Any]]:
    mutated = copy.deepcopy(samples)
    for sample in mutated:
        if sample["pair_id"] == "pair-0001":
            sample["chosen"] += " Correction: retain an independent reviewer note and the exact rollback condition."
            sample["annotator_group"] = "g-beta"
            sample["quality_notes"] += "+reviewer-corrected"
            break
    return mutated


def _command_demo(args: argparse.Namespace) -> dict[str, Any]:
    root = Path(args.root)
    data_root = root / "data"
    cache_root = root / "cache"
    snapshots_root = root / "snapshots"
    html_path = root / "workbench.html"
    root.mkdir(parents=True, exist_ok=True)
    generated = write_dataset(data_root, seed=args.seed, count=args.count)
    config_v1 = _load_config(generated["config_path"])
    samples_v1 = _load_samples(generated["samples_path"])
    pipeline = PreferencePipeline(config_v1, cache_root)
    store = SnapshotStore(snapshots_root)

    state_v1 = pipeline.run(samples_v1, use_cache=False)
    commit_v1 = store.commit(build_snapshot_categories(state_v1))
    write_snapshot_report(store.load(), html_path)

    threshold_config = copy.deepcopy(config_v1)
    threshold_config["near_duplicate"]["similarity_threshold"] += 0.03
    state_v2 = PreferencePipeline(threshold_config, cache_root).run(
        samples_v1,
        previous=store.previous_state(),
        use_cache=True,
    )
    commit_v2 = store.commit(build_snapshot_categories(state_v2))

    corrected_samples = _mutate_verification_sample(samples_v1)
    state_v3 = PreferencePipeline(threshold_config, cache_root).run(
        corrected_samples,
        previous=store.previous_state(),
        use_cache=True,
    )
    commit_v3 = store.commit(build_snapshot_categories(state_v3))
    write_snapshot_report(store.load(), html_path)

    full_state = PreferencePipeline(threshold_config, cache_root).run(
        corrected_samples,
        previous=None,
        use_cache=False,
    )
    digest_equal = full_state["execution"]["logic_digest"] == state_v3["execution"]["logic_digest"]
    spectrum_equal = content_hash(full_state["error_spectrum"]) == content_hash(state_v3["error_spectrum"])
    manifests_equal = all(
        content_hash(full_state["precision_results"][tier]["manifest"])
        == content_hash(state_v3["precision_results"][tier]["manifest"])
        for tier in ("float64", "float32", "fixed")
    )
    participation_equal = all(
        full_state["precision_results"][tier]["participation"]
        == state_v3["precision_results"][tier]["participation"]
        for tier in ("float64", "float32", "fixed")
    )
    verification = {
        "snapshots": {
            "v1": commit_v1,
            "v2_incremental_similarity_threshold": commit_v2,
            "v3_incremental_sample_correction": commit_v3,
        },
        "v2_invalidation": state_v2["execution"]["invalidation"],
        "v3_invalidation": state_v3["execution"]["invalidation"],
        "incremental_vs_full": {
            "logic_digest_equal": digest_equal,
            "error_spectrum_byte_equal": spectrum_equal,
            "manifests_byte_equal": manifests_equal,
            "participation_equal": participation_equal,
            "incremental_digest": state_v3["execution"]["logic_digest"],
            "full_digest": full_state["execution"]["logic_digest"],
        },
        "snapshot_idempotence": store.commit(build_snapshot_categories(state_v3)),
        "checks": state_v3["diagnostics"]["checks"],
        "html": str(html_path),
    }
    write_json(root / "verification.json", verification)
    return verification


def _command_serve(args: argparse.Namespace) -> dict[str, Any]:
    import http.server
    import socketserver

    directory = str(Path(args.root).resolve())

    class Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *handler_args: Any, **handler_kwargs: Any) -> None:
            super().__init__(*handler_args, directory=directory, **handler_kwargs)

    with socketserver.TCPServer((args.host, args.port), Handler) as server:
        print(f"serving {directory} at http://{args.host}:{args.port}/workbench.html", flush=True)
        server.serve_forever()
    return {"served": directory}


def _write_error_journal(path: Path, error: BusinessError) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        write_json(path, {"latest_business_error": error.to_dict()})
    except OSError:
        pass


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pref-workbench",
        description="Local deterministic preference-alignment data diagnostics",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    generate = subparsers.add_parser("generate", help="generate deterministic JSONL and configuration")
    generate.add_argument("--out", default=".workbench/data")
    generate.add_argument("--seed", type=int, default=20261122)
    generate.add_argument("--count", type=int, default=42)
    generate.set_defaults(handler=_command_generate)

    run = subparsers.add_parser("run", help="run the full or incremental pipeline and commit a snapshot")
    run.add_argument("--samples", default=".workbench/data/preference_pairs.jsonl")
    run.add_argument("--config", default=".workbench/data/construction_config.json")
    run.add_argument("--cache", default=".workbench/cache")
    run.add_argument("--snapshots", default=".workbench/snapshots")
    run.add_argument("--html", default=".workbench/workbench.html")
    run.add_argument("--incremental", action="store_true")
    run.add_argument("--fresh", action="store_true", help="bypass cache reads for a full recomputation")
    run.set_defaults(handler=_command_run)

    demo = subparsers.add_parser(
        "demo",
        help="run full, threshold-incremental, sample-correction-incremental, and full-equivalence checks",
    )
    demo.add_argument("--root", default=".workbench-demo")
    demo.add_argument("--seed", type=int, default=20261122)
    demo.add_argument("--count", type=int, default=42)
    demo.set_defaults(handler=_command_demo)

    serve = subparsers.add_parser("serve", help="serve the generated static report locally")
    serve.add_argument("--root", default=".workbench")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8765)
    serve.set_defaults(handler=_command_serve)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        result = args.handler(args)
    except BusinessError as error:
        if hasattr(args, "snapshots"):
            journal = Path(args.snapshots).parent
        else:
            journal = Path(getattr(args, "root", getattr(args, "out", ".workbench")))
        if journal.suffix == ".json":
            journal = journal.parent
        _write_error_journal(journal / "last_error.json", error)
        print(json.dumps({"ok": False, "error": error.to_dict()}, ensure_ascii=False, indent=2))
        return 2
    print(json.dumps({"ok": True, "result": result}, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())

