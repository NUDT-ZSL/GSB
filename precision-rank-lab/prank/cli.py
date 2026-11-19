"""Command line entry points. All process exit codes are portable integers."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__
from .analysis import build_report, report_hash
from .config import load_config
from .data_gen import apply_data_injections, generate_dataset, write_jsonl
from .errors import BusinessError, exit_code_for
from .pipeline import execute_dataset
from .precision import make_context
from .snapshot import load_snapshot, next_version, submit_snapshot


def _emit_error(exc: BusinessError) -> int:
    payload = {"business_error": exc.to_dict()}
    print(json.dumps(payload, ensure_ascii=False, indent=2), file=sys.stderr)
    return exit_code_for(exc.code)


def cmd_generate(args) -> int:
    candidates = generate_dataset(seed=args.seed, n_queries=args.queries,
                                  min_items=args.min_items, max_items=args.max_items)
    q_path, i_path = write_jsonl(candidates, args.out_dir)
    manifest = {
        "seed": args.seed, "n_queries": args.queries,
        "min_items": args.min_items, "max_items": args.max_items,
        "queries_file": str(q_path), "items_file": str(i_path),
    }
    manifest_path = Path(args.out_dir) / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n",
                             encoding="utf-8")
    print(json.dumps(manifest, sort_keys=True))
    return 0


def _load_overrides(injection_path: str | None) -> tuple[dict, list, dict]:
    if not injection_path:
        return {}, [], {}
    manifest = json.loads(Path(injection_path).read_text(encoding="utf-8"))
    overrides = {}
    for dotted, value in manifest.get("config_overrides", {}).items():
        overrides[dotted] = value
    return overrides, manifest.get("data_injections", []), manifest


def cmd_run(args) -> int:
    cfg = load_config(args.config, _parse_overrides(args.override))
    overrides, data_injections, injection_manifest = _load_overrides(args.injections)
    if overrides:
        cfg = load_config(args.config, {**_parse_overrides(args.override), **overrides})

    data_dir = Path(args.data_dir)
    injection_record = []
    if data_injections:
        from .data_gen import read_jsonl
        candidates = read_jsonl(data_dir)
        mutated, injection_record = apply_data_injections(
            candidates, data_injections, cfg.bounds)
        injected_dir = data_dir / "injected"
        write_jsonl(mutated, injected_dir)
        data_dir = injected_dir

    runtime_injections = injection_manifest.get("runtime_injections", [])
    for inj in runtime_injections:
        if inj["type"] == "unknown_precision":
            make_context(inj["bad_value"], cfg.fixed)
        if inj["type"] == "non_positive_scale":
            bad_cfg = load_config(args.config,
                                  {"precision.fixed.scale": inj["bad_value"]})
            make_context("fixed", bad_cfg.fixed)

    modes = args.modes.split(",") if args.modes else None
    try:
        result = execute_dataset(data_dir, cfg, modes)
    except BusinessError as exc:
        if args.error_report:
            error_payload = {
                "report_schema_version": 1,
                "seed": _read_seed(args.data_dir),
                "data_dir": str(data_dir),
                "fatal_business_error": exc.to_dict(),
                "injections": injection_record,
            }
            Path(args.error_report).write_text(
                json.dumps(error_payload, indent=2, sort_keys=True,
                           ensure_ascii=False) + "\n", encoding="utf-8")
        raise
    seed = _read_seed(args.data_dir)
    report = build_report(result, cfg, seed=seed, data_dir=str(data_dir),
                          extra={"injections": injection_record,
                                 "injection_file": args.injections}
                          if injection_record or args.injections else None)
    out_path = Path(args.report)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, indent=2, sort_keys=True,
                                   ensure_ascii=False) + "\n", encoding="utf-8")
    summary = {
        "report": str(out_path),
        "report_hash": report["report_hash"],
        "n_queries": report["n_queries"],
        "business_events": len(report["business_events"]),
        "degraded_queries": len(report["degraded_queries"]),
        "tier_attribution": {m: a["dominant_attribution"]["dominant_source"]
                             for m, a in report["tier_error_analysis"].items()},
    }
    print(json.dumps(summary, indent=2, sort_keys=True, ensure_ascii=False))
    return 0


def cmd_hash_check(args) -> int:
    reports = [json.loads(Path(p).read_text(encoding="utf-8")) for p in args.reports]
    hashes = [r["report_hash"] for r in reports]
    recomputed = [report_hash(r) for r in reports]
    ok = len(set(hashes)) == 1 and all(h == rc for h, rc in zip(hashes, recomputed))
    print(json.dumps({"identical": ok, "hashes": hashes,
                      "recomputed_match": [h == rc for h, rc in zip(hashes, recomputed)]},
                     indent=2, sort_keys=True))
    return 0 if ok else 1


def cmd_snapshot(args) -> int:
    if args.snapshot_action == "submit-config":
        cfg = load_config(args.config)
        version = next_version(args.path)
        envelope = submit_snapshot(args.path, "config", cfg.raw, version)
        print(json.dumps({"submitted": str(args.path), "version": envelope["version"],
                          "checksum": envelope["checksum"]}, indent=2, sort_keys=True))
        return 0
    if args.snapshot_action == "submit-stats":
        if not args.report:
            raise BusinessError("E_CONFIG_INVALID", "snapshot",
                                "submit-stats requires --report", field="report")
        report = json.loads(Path(args.report).read_text(encoding="utf-8"))
        payload = {k: report[k] for k in
                   ("tier_error_analysis", "threshold_stability", "cross_level_checks")}
        version = next_version(args.path)
        envelope = submit_snapshot(args.path, "statistics", payload, version)
        print(json.dumps({"submitted": str(args.path), "version": envelope["version"],
                          "checksum": envelope["checksum"]}, indent=2, sort_keys=True))
        return 0
    envelope = load_snapshot(args.path, expected_category=args.category)
    print(json.dumps({"version": envelope["version"],
                      "checksum": envelope["checksum"],
                      "category": envelope["snapshot_category"]}, indent=2, sort_keys=True))
    return 0


def cmd_features(args) -> int:
    cfg = load_config(args.config)
    from .data_gen import read_jsonl
    from .pipeline import run_tier
    candidates = read_jsonl(args.data_dir)
    out = {}
    for cand in candidates[: args.limit]:
        run = run_tier(cand, cfg, "float64")
        out[cand.query.query_id] = {
            "item_ids": run.item_ids,
            "feature_columns": ["term_match", "category_match", "price_bucket",
                                "quality_scaled"],
            "features": run.features.tolist(),
            "scores": run.scores.tolist(),
        }
    print(json.dumps(out, indent=2, sort_keys=True))
    return 0


def _read_seed(data_dir: str) -> int:
    manifest = Path(data_dir) / "manifest.json"
    if manifest.is_file():
        return int(json.loads(manifest.read_text(encoding="utf-8"))["seed"])
    return 0


def _parse_overrides(values: list[str] | None) -> dict:
    overrides = {}
    for value in values or []:
        key, raw = value.split("=", 1)
        try:
            overrides[key] = json.loads(raw)
        except json.JSONDecodeError:
            overrides[key] = raw
    return overrides


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="prank",
                                     description="Precision-aware ranking lab")
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)

    gen = sub.add_parser("generate", help="generate deterministic JSONL samples")
    gen.add_argument("--out-dir", required=True)
    gen.add_argument("--seed", type=int, default=20261120)
    gen.add_argument("--queries", type=int, default=40)
    gen.add_argument("--min-items", type=int, default=60)
    gen.add_argument("--max-items", type=int, default=120)
    gen.set_defaults(func=cmd_generate)

    run = sub.add_parser("run", help="run the full precision pipeline")
    run.add_argument("--data-dir", required=True)
    run.add_argument("--config", required=True)
    run.add_argument("--report", required=True)
    run.add_argument("--injections")
    run.add_argument("--modes")
    run.add_argument("--override", action="append",
                     help="dotted config override, e.g. threshold=0.5")
    run.add_argument("--error-report",
                     help="optional path to persist fatal dataset-level errors")
    run.set_defaults(func=cmd_run)

    hc = sub.add_parser("hash-check", help="compare report hashes across reruns")
    hc.add_argument("--reports", nargs="+", required=True)
    hc.set_defaults(func=cmd_hash_check)

    snap = sub.add_parser("snapshot", help="versioned snapshot management")
    snap.add_argument("--snapshot-action",
                      choices=["submit-config", "submit-stats", "load"],
                      default="load")
    snap.add_argument("--path", required=True)
    snap.add_argument("--config")
    snap.add_argument("--report")
    snap.add_argument("--category")
    snap.set_defaults(func=cmd_snapshot)

    feat = sub.add_parser("features", help="dump float64 feature inputs")
    feat.add_argument("--data-dir", required=True)
    feat.add_argument("--config", required=True)
    feat.add_argument("--limit", type=int, default=2)
    feat.set_defaults(func=cmd_features)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except BusinessError as exc:
        return _emit_error(exc)


if __name__ == "__main__":
    raise SystemExit(main())
