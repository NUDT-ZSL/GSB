"""Command line entry point for the local drift monitoring application."""

from __future__ import annotations

import argparse
import json
import os
import shutil
from typing import Any, Dict, List, Optional

from .cache import ContentStore
from .config_migration import (
    assert_roundtrip,
    downgrade_to_v1,
    serialize_v1,
    serialize_v2,
    serialize_v3,
    upgrade_to_v2,
    upgrade_to_v3,
)
from .config_reader import read_config
from .data import (
    DriftInjection,
    generate_batch,
    generate_stream,
    read_batches,
)
from .engine import MonitoringEngine
from .errors import DriftError
from .resources import ResourceGuard, ResourceLimits
from .snapshot import AlertStore, SnapshotStore


def _dump(document: Dict[str, Any]) -> str:
    return json.dumps(document, sort_keys=True, indent=2, ensure_ascii=False, allow_nan=False)


def _runtime_paths(runtime_dir: str) -> Dict[str, str]:
    paths = {}
    for name in ("cache", "alerts", "snapshots", "reports"):
        target = os.path.join(runtime_dir, name)
        os.makedirs(target, exist_ok=True)
        paths[name] = target
    return paths


def _build_engine(runtime_dir: str, config_text: str, limits: ResourceLimits) -> MonitoringEngine:
    paths = _runtime_paths(runtime_dir)
    config = read_config(config_text)
    guard = ResourceGuard(limits=limits)
    return MonitoringEngine(
        config=config,
        cache=ContentStore(paths["cache"], max_entries=limits.max_cache_entries),
        snapshots=SnapshotStore(paths["snapshots"]),
        alerts=AlertStore(paths["alerts"]),
        guard=guard,
    )


def _default_config(samples_dir: str) -> str:
    with open(os.path.join(samples_dir, "monitor_v3.json"), "r", encoding="utf-8") as handle:
        return handle.read()


def cmd_demo(args: argparse.Namespace) -> Dict[str, Any]:
    if os.path.exists(args.runtime):
        shutil.rmtree(args.runtime)
    paths = _runtime_paths(args.runtime)
    limits = ResourceLimits(
        max_batch_samples=args.max_batch_samples,
        max_cache_entries=args.max_cache_entries,
        max_resident_bytes=args.max_resident_bytes,
    )
    with open(args.config, "r", encoding="utf-8") as handle:
        config_text = handle.read()
    engine = _build_engine(args.runtime, config_text, limits)

    batches = read_batches(args.stream)
    reports = []
    errors: List[Dict[str, Any]] = []
    for batch in batches:
        try:
            report = engine.evaluate_batch(batch)
            reports.append(report.to_dict())
        except DriftError as exc:
            errors.append(exc.to_dict())

    report_doc = {
        "command": "demo",
        "config_source_format": engine.config.source_format,
        "config_definition_digest": engine.config.definition_digest(),
        "batch_count": len(batches),
        "reports": reports,
        "business_errors": errors,
        "cache": {
            "hits": engine.cache.hits,
            "misses": engine.cache.misses,
            "entries": engine.cache.entry_count(),
        },
        "alerts": engine.alerts.all_alerts(),
        "snapshot_versions": [
            entry["snapshot_version"] for entry in engine.snapshots._load_index()["snapshots"]
        ],
    }
    out_path = os.path.join(paths["reports"], "demo_report.json")
    with open(out_path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(_dump(report_doc))
    report_doc["report_path"] = out_path
    return report_doc


def cmd_step(args: argparse.Namespace) -> Dict[str, Any]:
    paths = _runtime_paths(args.runtime)
    limits = ResourceLimits()
    with open(args.config, "r", encoding="utf-8") as handle:
        engine = _build_engine(args.runtime, handle.read(), limits)
    batch = generate_batch(
        batch_id=args.batch_id,
        sample_count=args.sample_count,
        master_seed=args.seed,
        injection=DriftInjection(
            batch_id=args.batch_id,
            mean_shift=args.mean_shift,
            variance_multiplier=args.variance_multiplier,
            category_shift=args.category_shift,
            missing_rate=args.missing_rate,
        ),
    )
    report = engine.evaluate_batch(batch)
    doc = {"command": "step", "report": report.to_dict(), "alerts": engine.alerts.all_alerts()}
    out_path = os.path.join(paths["reports"], f"step_{args.batch_id}.json")
    with open(out_path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(_dump(doc))
    doc["report_path"] = out_path
    return doc


def cmd_validate_config(args: argparse.Namespace) -> Dict[str, Any]:
    with open(args.config, "r", encoding="utf-8") as handle:
        text = handle.read()
    try:
        config = read_config(text)
        return {
            "command": "validate-config",
            "ok": True,
            "source_format": config.source_format,
            "config_id": config.config_id,
            "features": config.feature_names(),
            "definition_digest": config.definition_digest(),
        }
    except DriftError as exc:
        return {"command": "validate-config", "ok": False, "error": exc.to_dict()}


def cmd_migrate(args: argparse.Namespace) -> Dict[str, Any]:
    with open(args.config, "r", encoding="utf-8") as handle:
        config = read_config(handle.read())
    result: Dict[str, Any] = {"command": "migrate", "from": config.source_format}
    if args.target == "v2":
        migrated = upgrade_to_v2(config)
        result["text"] = serialize_v2(migrated)
    elif args.target == "v3":
        migrated = upgrade_to_v3(config)
        result["text"] = serialize_v3(migrated)
    elif args.target == "v1":
        migrated, discarded = downgrade_to_v1(config, policy=args.policy)
        result["text"] = serialize_v1(migrated)
        result["discarded"] = discarded
    else:
        raise DriftError(
            "E_VERSION_UNSUPPORTED",
            f"migration target {args.target!r} is not supported",
            {"field": "target"},
        )
    result["provenance"] = migrated.provenance
    return result


def cmd_roundtrip(args: argparse.Namespace) -> Dict[str, Any]:
    with open(args.config, "r", encoding="utf-8") as handle:
        original = read_config(handle.read())
    v3 = upgrade_to_v3(original)
    v1, discarded = downgrade_to_v1(v3, policy="freeze")
    recovered = upgrade_to_v3(v1)
    assert_roundtrip(v3, recovered)
    return {
        "command": "roundtrip",
        "ok": True,
        "source_format": original.source_format,
        "discarded_on_downgrade": discarded,
        "definition_digest": v3.definition_digest(),
        "recovered_digest": recovered.definition_digest(),
    }


def cmd_replay_cache(args: argparse.Namespace) -> Dict[str, Any]:
    store = ContentStore(os.path.join(args.runtime, "cache"))
    document = store.replay(args.cache_key)
    return {"command": "replay-cache", "cache_key": args.cache_key, "artifact": document}


def cmd_trace_alert(args: argparse.Namespace) -> Dict[str, Any]:
    alerts = AlertStore(os.path.join(args.runtime, "alerts"))
    snapshots = SnapshotStore(os.path.join(args.runtime, "snapshots"))
    alert = alerts.get(args.alert_id)
    if alert is None:
        raise DriftError(
            "E_ALERT_STALE",
            "alert id does not exist in the local alert store",
            {"alert_id": args.alert_id},
        )
    snapshot = snapshots.latest()
    cache = ContentStore(os.path.join(args.runtime, "cache"))
    artifact = cache.replay(alert["cache_key"])
    try:
        alerts.assert_fresh(
            alert,
            current_snapshot=snapshot,
            current_cache_key=alert["cache_key"],
            current_window=[alert["window_start"], alert["window_end"]],
        )
        fresh = True
    except DriftError as exc:
        fresh = False
        freshness_error = exc.to_dict()
    else:
        freshness_error = None
    return {
        "command": "trace-alert",
        "alert": alert,
        "config_snapshot": snapshot,
        "cache_artifact": artifact,
        "fresh_against_current": fresh,
        "freshness_error": freshness_error,
    }


def cmd_error_demo(args: argparse.Namespace) -> Dict[str, Any]:
    outcomes: Dict[str, Any] = {"command": "error-demo", "cases": {}}

    # 1. Corrupt configuration: truncated header.
    try:
        read_config("# DRIFT_MONITOR_CONFIG\nincome_score|numeric\n")
    except DriftError as exc:
        outcomes["cases"]["truncated_header"] = exc.to_dict()

    # 2. Unsupported version.
    try:
        read_config("# DRIFT_MONITOR_CONFIG v9\n")
    except DriftError as exc:
        outcomes["cases"]["unsupported_version"] = exc.to_dict()

    # 3. Pipe field count mismatch.
    bad_v1 = "# DRIFT_MONITOR_CONFIG v1\nincome_score|numeric|1.0\n"
    try:
        read_config(bad_v1)
    except DriftError as exc:
        outcomes["cases"]["pipe_field_count"] = exc.to_dict()

    # 4. Window invalid: step greater than length.
    from .windowing import validate_window_params

    try:
        validate_window_params(length=3, step=5)
    except DriftError as exc:
        outcomes["cases"]["window_invalid"] = exc.to_dict()

    # 5. Resource limit: tiny batch sample cap.
    tiny = ResourceLimits(max_batch_samples=5)
    guard = ResourceGuard(limits=tiny)
    batch = generate_batch(batch_id=0, sample_count=50)
    try:
        guard.check_batch_size(batch.sample_count, batch.batch_id)
    except DriftError as exc:
        outcomes["cases"]["resource_limit"] = exc.to_dict()

    # 6. Snapshot rollback.
    snapshot_dir = os.path.join(args.runtime, "snapshots_err")
    store = SnapshotStore(snapshot_dir)
    with open(args.config, "r", encoding="utf-8") as handle:
        config = read_config(handle.read())
    store.replace(config.to_dict())
    try:
        store.attempt_rollback(1)
    except DriftError as exc:
        outcomes["cases"]["snapshot_rollback"] = exc.to_dict()

    return outcomes


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Local deterministic feature drift monitor")
    parser.add_argument("--runtime", default="runtime", help="persistent working directory")
    sub = parser.add_subparsers(dest="command", required=True)

    demo = sub.add_parser("demo", help="run the full deterministic demo")
    demo.add_argument("--config", required=True)
    demo.add_argument("--stream", required=True)
    demo.add_argument("--clean", action="store_true")
    demo.add_argument("--max-batch-samples", type=int, default=100_000)
    demo.add_argument("--max-cache-entries", type=int, default=512)
    demo.add_argument("--max-resident-bytes", type=int, default=64 * 1024 * 1024)
    demo.set_defaults(func=cmd_demo)

    step = sub.add_parser("step", help="evaluate one freshly generated batch")
    step.add_argument("--config", required=True)
    step.add_argument("--batch-id", type=int, default=0)
    step.add_argument("--sample-count", type=int, default=200)
    step.add_argument("--seed", type=int, default=20260101)
    step.add_argument("--mean-shift", type=float, default=0.0)
    step.add_argument("--variance-multiplier", type=float, default=1.0)
    step.add_argument("--category-shift", action="store_true")
    step.add_argument("--missing-rate", type=float, default=0.0)
    step.set_defaults(func=cmd_step)

    validate = sub.add_parser("validate-config", help="parse and validate a config file")
    validate.add_argument("--config", required=True)
    validate.set_defaults(func=cmd_validate_config)

    migrate = sub.add_parser("migrate", help="convert between v1/v2/v3 dialects")
    migrate.add_argument("--config", required=True)
    migrate.add_argument("--target", choices=["v1", "v2", "v3"], required=True)
    migrate.add_argument("--policy", choices=["drop", "freeze"], default="freeze")
    migrate.set_defaults(func=cmd_migrate)

    roundtrip = sub.add_parser("roundtrip", help="verify v3->v1->v3 round trip")
    roundtrip.add_argument("--config", required=True)
    roundtrip.set_defaults(func=cmd_roundtrip)

    replay = sub.add_parser("replay-cache", help="replay an artifact by cache key")
    replay.add_argument("--cache-key", required=True)
    replay.set_defaults(func=cmd_replay_cache)

    trace = sub.add_parser("trace-alert", help="trace an alert to its dependencies")
    trace.add_argument("--alert-id", required=True)
    trace.set_defaults(func=cmd_trace_alert)

    error_demo = sub.add_parser("error-demo", help="trigger representative business errors")
    error_demo.add_argument("--config", required=True)
    error_demo.set_defaults(func=cmd_error_demo)
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        document = args.func(args)
    except DriftError as exc:
        document = {"ok": False, "error": exc.to_dict()}
        print(_dump(document))
        return 2
    print(_dump(document))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
