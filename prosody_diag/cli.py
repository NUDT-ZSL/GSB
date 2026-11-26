from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

from .canonical import read_json, read_jsonl, write_json_atomic
from .config import Config
from .cache import ContentCache
from .config import PRECISION_PROFILES
from .diagnostics import verify_deterministic_reruns, verify_migrations
from .pipeline import DiagnosticPipeline
from .samplegen import generate_sample_workspace
from .state import StateStore
from .trees import write_builtin_trees


def _parse_interrupt(value: str | None) -> tuple[str, int] | None:
    if not value:
        return None
    if ":" not in value:
        raise ValueError("--interrupt-at must be STAGE:N, for example adjudication:2")
    stage, number = value.split(":", 1)
    return stage, int(number)


def _apply_env_defaults(argv: list[str] | None) -> argparse.Namespace:
    parser = build_parser()
    args = parser.parse_args(argv)
    args.precision = (
        args.precision
        or os.environ.get("PROSODY_PRECISION")
        or os.environ.get("PRECISION_PROFILE")
        or "all"
    )
    args.tree_version = (
        args.tree_version
        or os.environ.get("PROSODY_TREE_VERSION")
        or os.environ.get("DECISION_TREE_VERSION")
        or "both"
    )
    args.interrupt_at = (
        args.interrupt_at
        or os.environ.get("PROSODY_INTERRUPT_AT")
        or os.environ.get("PROSODY_INTERRUPT_SIGNAL_POINT")
        or os.environ.get("INTERRUPT_AT")
    )
    args.sample_dir = args.sample_dir or os.environ.get("PROSODY_SAMPLE_DIR", "samples")
    args.output_dir = args.output_dir or os.environ.get("PROSODY_OUTPUT_DIR", "artifacts")
    return args


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Prosody boundary migration diagnostics")
    parser.add_argument("--mode", default="full", choices=["full", "generate", "interactive", "lock-hold", "trace"])
    parser.add_argument("--sample-dir", default=None)
    parser.add_argument("--output-dir", default=None)
    parser.add_argument(
        "--precision",
        "--precision-profile",
        dest="precision",
        default=None,
        help="fp64/float64, fp32/float32, q15/fixed, all, or fixed:scale=...,rounding=...",
    )
    parser.add_argument(
        "--tree-version",
        "--decision-tree-version",
        dest="tree_version",
        default=None,
        choices=["v1", "v2", "both"],
    )
    parser.add_argument(
        "--interrupt-at",
        "--interrupt-point",
        "--interrupt-signal-point",
        dest="interrupt_at",
        default=None,
        help="features:N, adjudication:N, or error_spectrum:N",
    )
    parser.add_argument("--conflict-threshold", type=int, default=None)
    parser.add_argument("--max-utterances", type=int, default=None, dest="max_utterances_per_run")
    parser.add_argument("--max-resident-frames", type=int, default=None)
    parser.add_argument("--max-cache-entries", type=int, default=None)
    parser.add_argument("--near-threshold-band", type=float, default=None)
    parser.add_argument("--bump-revision", default=None, metavar="UTTERANCE_ID")
    parser.add_argument("--change-rule", default=None, metavar="RULE.KEY=VALUE")
    parser.add_argument("--switch-profile", default=None, metavar="UTTERANCE_ID=PROFILE")
    parser.add_argument("--utterance", default=None, help="utterance id for trace/interactive queries")
    parser.add_argument("--hold-seconds", type=float, default=2.0)
    return parser


def ensure_samples(sample_dir: Path, cfg: Config, *, force: bool = False) -> None:
    if force or not (sample_dir / "manifest.json").exists():
        generate_sample_workspace(sample_dir, cfg)
        write_builtin_trees(sample_dir, cfg)


def _load_or_create_overrides(output_dir: Path) -> tuple[dict[str, Any], Path]:
    path = output_dir / "input_overrides.json"
    if path.exists():
        return read_json(path), path
    return {"revision_overrides": {}, "profile_overrides": {}, "rule_overrides": {}}, path


def _parse_scalar(value: str) -> Any:
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def _update_change_options(
    args: argparse.Namespace, tasks: list[dict[str, Any]], output_dir: Path
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    overrides, path = _load_or_create_overrides(output_dir)
    change_spec: dict[str, Any] | None = None
    if args.bump_revision:
        uid = args.bump_revision
        task = next((item for item in tasks if item["utterance_id"] == uid), None)
        if task is None:
            raise ValueError(f"unknown utterance_id for --bump-revision: {uid}")
        current = int(overrides["revision_overrides"].get(uid, task.get("revision", 1)))
        overrides["revision_overrides"][uid] = current + 1
        change_spec = {"type": "revision", "utterance_id": uid}
    if args.switch_profile:
        uid, profile = args.switch_profile.split("=", 1)
        overrides["profile_overrides"][uid] = profile
        change_spec = {"type": "profile", "utterance_id": uid, "profile": profile}
    if args.change_rule:
        token = args.change_rule.replace(":", ".", 1)
        rule_id, assignment = token.split(".", 1)
        key, raw_value = assignment.split("=", 1)
        value = _parse_scalar(raw_value)
        overrides["rule_overrides"].setdefault(rule_id, {})[key] = value
        kind = "threshold" if key in {"threshold", "op", "feature"} else "confidence"
        change_spec = {"type": "rule", "rule_id": rule_id, "change_kind": kind, "key": key}
    if any([args.bump_revision, args.switch_profile, args.change_rule]):
        output_dir.mkdir(parents=True, exist_ok=True)
        write_json_atomic(path, overrides)
    return overrides, change_spec


def run_interactive(output_dir: str | Path) -> int:
    root = Path(output_dir)
    report_path = root / "diagnostic_report.json"
    ledger_path = root / "annotation_ledger.jsonl"
    report = read_json(report_path) if report_path.exists() else {}
    ledger = read_jsonl(ledger_path) if ledger_path.exists() else []
    print("Prosody diagnostic console. Commands: summary, utterance <ID>, cache <ID>, errors, exit")
    while True:
        try:
            line = input("prosody> ").strip()
        except EOFError:
            break
        if line in {"exit", "quit"}:
            break
        if not line or line == "help":
            print("summary | utterance U001 | cache U001 | errors | exit")
        elif line == "summary":
            print(json.dumps(report.get("counts", {}), ensure_ascii=False, indent=2))
            print("dominant:", report.get("error_summary", {}).get("dominant_error_source"))
        elif line.startswith("utterance "):
            uid = line.split(" ", 1)[1]
            rows = [row for row in ledger if row["utterance_id"] == uid]
            print(json.dumps(rows, ensure_ascii=False, indent=2))
        elif line.startswith("cache "):
            key = line.split(" ", 1)[1]
            from .cache import ContentCache

            cache = ContentCache(root / "cache")
            print(json.dumps(cache.trace(key), ensure_ascii=False, indent=2))
        elif line == "errors":
            spectrum_path = root / "error_spectrum.json"
            spectrum = read_json(spectrum_path) if spectrum_path.exists() else {}
            print(json.dumps(spectrum.get("summary", {}), ensure_ascii=False, indent=2))
        else:
            print("unknown command; try help")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _apply_env_defaults(argv)
    sample_dir = Path(args.sample_dir)
    output_dir = Path(args.output_dir)
    config_overrides = {
        "tree_version": args.tree_version,
        "conflict_threshold": args.conflict_threshold,
        "max_utterances_per_run": args.max_utterances_per_run,
        "max_resident_frames": args.max_resident_frames,
        "max_cache_entries": args.max_cache_entries,
        "near_threshold_band": args.near_threshold_band,
    }
    if args.precision in PRECISION_PROFILES:
        config_overrides["precision_profile"] = args.precision.split(",")[0]
    cfg = Config().with_overrides(
        **{key: value for key, value in config_overrides.items() if value is not None}
    )

    if args.mode == "generate":
        manifest = generate_sample_workspace(sample_dir, cfg)
        write_builtin_trees(sample_dir, cfg)
        print(json.dumps({"ok": True, "manifest": manifest}, ensure_ascii=False, indent=2))
        return 0

    if args.mode == "lock-hold":
        StateStore(output_dir).hold_lock(args.hold_seconds)
        print(json.dumps({"ok": True, "held_seconds": args.hold_seconds}))
        return 0

    if args.mode == "interactive":
        return run_interactive(output_dir)

    if args.mode == "trace":
        cache = ContentCache(output_dir / "cache")
        registry_path = output_dir / "state" / "key_registry.json"
        registry = read_json(registry_path) if registry_path.exists() else {}
        query = args.utterance
        if not query:
            raise ValueError("--utterance U001 or --utterance <cache-key> is required for trace mode")
        entries = cache.trace(query)
        registry_matches = {
            logical_key: cache_key
            for logical_key, cache_key in sorted(registry.items())
            if query in logical_key or cache_key == query
        }
        print(
            json.dumps(
                {
                    "ok": True,
                    "query": query,
                    "registry_matches": registry_matches,
                    "cache_entries": entries,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    ensure_samples(sample_dir, cfg)
    tasks = read_jsonl(sample_dir / "tasks.jsonl")
    overrides, change_spec = _update_change_options(args, tasks, output_dir)

    interrupt_at = _parse_interrupt(args.interrupt_at)
    profiles = ["fp64", "fp32", "q15"] if args.precision == "all" else [args.precision]
    migration_report = verify_migrations(sample_dir, output_dir)
    determinism = verify_deterministic_reruns(sample_dir, cfg)
    pipeline = DiagnosticPipeline(
        sample_dir,
        output_dir,
        cfg,
        profiles=profiles,
        interrupt_at=interrupt_at,
        overrides=overrides,
        change_spec=change_spec,
    )
    plan = pipeline.run(migration_report=migration_report, determinism=determinism)
    summary = {
        "ok": True,
        "mode": "full",
        "report": str(output_dir / "diagnostic_report.json"),
        "ledger": str(output_dir / "annotation_ledger.jsonl"),
        "recomputed_utterance_ids": plan["recomputed_utterance_ids"],
        "reused_utterance_ids": plan["reused_utterance_ids"],
        "affected_utterance_ids": plan["affected_utterance_ids"],
        "degradations": plan["degradations"],
        "snapshot_versions": {
            stage: (pipeline.state.load_latest(stage) or {}).get("version")
            for stage in ("features", "adjudication", "error_spectrum", "report")
        },
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0
