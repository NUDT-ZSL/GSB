"""Command-line entry points for validation, routing, demos, and anomalies."""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .config import load_config
from .errors import E_CONFIG, E_RING, E_VERSION, RouterError
from .logging_utils import JsonlLogger
from .session import RouterSession
from .ui import build_ui_model, launch_ui
from .util import canonical_json, read_json, write_json
from .verification import canonical_text, compare_canonical, verify_repeated


ROOT = Path(__file__).resolve().parent.parent
SAMPLES = ROOT / "samples"


def _print(value: Any) -> None:
    print(canonical_json(value))


def _error_payload(error: RouterError, **extra: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {"status": "error"}
    payload.update(extra)
    payload["error"] = error.to_dict()
    return payload


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--log-file", default=None, help="optional deterministic JSONL log path")
    parser.add_argument("--cache-capacity", type=int, default=32)
    parser.add_argument("--history-limit", type=int, default=32)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="shardrouter",
        description="Deterministic local sharding router teaching component",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    demo = subparsers.add_parser("demo", help="run normal, incremental, and snapshot scenarios")
    demo.add_argument("--config", default=str(SAMPLES / "config.json"))
    demo.add_argument("--config-v2", default=str(SAMPLES / "config_v2.json"))
    demo.add_argument("--output-dir", default=str(ROOT / "artifacts" / "demo"))
    _add_common(demo)

    route = subparsers.add_parser("route", help="route one logical predicate")
    route.add_argument("--config", default=str(SAMPLES / "config.json"))
    route.add_argument("--table", required=True)
    route.add_argument("--predicate", required=True)
    route.add_argument("--inject", choices=["none", "ring-conflict"], default="none")
    route.add_argument("--metadata-version", type=int, default=None)
    _add_common(route)

    validate = subparsers.add_parser("validate", help="validate and summarize a configuration")
    validate.add_argument("--config", default=str(SAMPLES / "config.json"))
    validate.add_argument("--inject", choices=["none", "ring-conflict"], default="none")
    _add_common(validate)

    anomaly = subparsers.add_parser("anomaly", help="reproduce one injected business error")
    anomaly.add_argument(
        "--type",
        dest="anomaly_type",
        choices=["broken-config", "ring-conflict", "version-mismatch"],
        required=True,
    )
    anomaly.add_argument("--config", default=None)
    anomaly.add_argument("--metadata-version", type=int, default=None)
    anomaly.add_argument("--metadata-file", default=None)
    anomaly.add_argument("--log-file", default=None)

    ui = subparsers.add_parser("ui", help="open Tkinter UI or print its stable view model")
    ui.add_argument("--config", default=str(SAMPLES / "config.json"))
    ui.add_argument("--headless", action="store_true")
    _add_common(ui)
    return parser


def _default_anomaly_config(anomaly_type: str) -> Path:
    if anomaly_type == "broken-config":
        return SAMPLES / "anomalies" / "broken_config.json"
    return SAMPLES / "config.json"


def _metadata_version_expected(args: argparse.Namespace) -> int:
    if args.metadata_version is not None:
        return int(args.metadata_version)
    metadata_file = (
        Path(args.metadata_file)
        if args.metadata_file is not None
        else SAMPLES / "anomalies" / "version_mismatch_metadata.json"
    )
    try:
        payload = read_json(metadata_file)
    except Exception:
        return 999
    if not isinstance(payload, Mapping) or not isinstance(payload.get("expected_metadata_version"), int):
        return 999
    return int(payload["expected_metadata_version"])


def _run_anomaly(args: argparse.Namespace) -> int:
    config_path = Path(args.config) if args.config is not None else _default_anomaly_config(args.anomaly_type)
    logger = JsonlLogger(args.log_file)
    expected_codes = {
        "broken-config": E_CONFIG,
        "ring-conflict": E_RING,
        "version-mismatch": E_VERSION,
    }
    expected = expected_codes[args.anomaly_type]
    try:
        config = load_config(
            config_path,
            inject_ring_conflict=args.anomaly_type == "ring-conflict",
        )
        if args.anomaly_type == "version-mismatch":
            RouterSession(
                config,
                expected_metadata_version=_metadata_version_expected(args),
            )
        elif args.anomaly_type == "broken-config":
            raise AssertionError("broken configuration unexpectedly loaded")
        else:
            raise AssertionError("ring conflict unexpectedly loaded")
    except RouterError as error:
        error_payload = error.to_dict()
        logger.emit_error(
            "anomaly_rejected",
            error_payload,
            injection=args.anomaly_type,
            expected_code=expected,
        )
        logger.flush()
        payload = _error_payload(error, injection=args.anomaly_type, expected_code=expected)
        payload["status"] = "rejected" if error.code == expected else "error"
        _print(payload)
        return 0 if error.code == expected else 2
    _print({"status": "error", "message": "expected anomaly was not triggered"})
    return 2


def _route_outcome(label: str, session: RouterSession, table: str, predicate: str) -> dict[str, Any]:
    outcome = session.route_with_cache(table, predicate)
    return {
        "cache": outcome["cache"],
        "label": label,
        "plan": outcome["plan"],
    }


def _capture_error(
    logger: JsonlLogger,
    injection: str,
    expected_code: str,
    callback: Callable[[], Any],
) -> dict[str, Any]:
    try:
        callback()
    except RouterError as error:
        payload = error.to_dict()
        logger.emit_error(
            "anomaly_rejected",
            payload,
            injection=injection,
            expected_code=expected_code,
        )
        return {
            "code": error.code,
            "expected": expected_code,
            "field_path": error.field_path,
            "message": error.message,
            "status": "rejected" if error.code == expected_code else "unexpected",
        }
    return {"code": None, "expected": expected_code, "message": "anomaly not triggered", "status": "missed"}


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _run_demo(args: argparse.Namespace) -> int:
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    logger = JsonlLogger(args.log_file or output_dir / "structured_log.jsonl")
    config_v1 = load_config(args.config)
    config_v2 = load_config(args.config_v2)
    session = RouterSession(
        config_v1,
        cache_capacity=args.cache_capacity,
        history_limit=args.history_limit,
    )

    route_inputs = [
        ("orders_point", "orders", "tenant_id = 7 AND region = 'east'"),
        ("ledger_boundary", "ledger", "amount BETWEEN 100 AND 500"),
        ("sessions_in", "sessions", "session_id IN ('s-02', 's-01')"),
        ("orders_broadcast", "orders", "region = 'east'"),
    ]
    normal_routes = [
        _route_outcome(label, session, table, predicate)
        for label, table, predicate in route_inputs
    ]
    normal_routes.append(
        _route_outcome(
            "orders_point_repeat",
            session,
            "orders",
            " tenant_id=7 AND region IN ('east') ",
        )
    )
    route_units = {
        unit: session.route_unit(unit)["result"]
        for unit in ("orders_lookup", "orders_rollup", "session_pair")
    }
    original_orders_plan = normal_routes[0]["plan"]
    original_orders_bytes = canonical_text(original_orders_plan)

    independent = RouterSession(config_v1)
    independent_plan_a = independent.route("orders", "tenant_id = 7 AND region = 'east'")
    independent_plan_b = independent.route("orders", "TENANT_ID=7 and REGION IN ('east')")
    independent_stability = compare_canonical(independent_plan_a, independent_plan_b)
    repeated_plan_stability = verify_repeated(
        lambda: RouterSession(config_v1).route("ledger", "amount BETWEEN 100 AND 500"),
        runs=2,
    )

    normal_payload = {
        "route_units": route_units,
        "routes": normal_routes,
        "schema": "shardrouter.demo.normal.v1",
        "stability": {
            "independent_sessions": independent_stability,
            "repeated_render": repeated_plan_stability,
        },
    }
    write_json(output_dir / "normal_routing.json", normal_payload)

    snapshot_path = output_dir / "snapshot_v1.json"
    save_report = session.save_snapshot(snapshot_path)
    snapshot = RouterSession.load_snapshot(snapshot_path)
    snapshot_cache_count = len(snapshot["cache"]["entries"])

    apply_report = session.apply_config(config_v2)
    incremental_routes = [
        _route_outcome("orders_after_rule_change", session, "orders", "tenant_id = 7 AND region = 'east'"),
        _route_outcome("sessions_after_ring_change", session, "sessions", "session_id IN ('s-02', 's-01')"),
        _route_outcome("ledger_after_unrelated_change", session, "ledger", "amount BETWEEN 100 AND 500"),
    ]
    incremental_rollup = session.route_unit("orders_rollup")["result"]
    incremental_payload = {
        "apply_config": apply_report,
        "rollup_unit": incremental_rollup,
        "routes": incremental_routes,
        "schema": "shardrouter.demo.incremental.v1",
        "versions": session.version_state(),
    }
    write_json(output_dir / "incremental_routing.json", incremental_payload)
    prior_cache_count = len(session.cache)

    restore_report = session.restore_snapshot(snapshot)
    restored_cache_snapshot = session.cache.to_dict()
    cache_exact = canonical_json(restored_cache_snapshot) == canonical_json(snapshot["cache"])
    replay = _route_outcome("orders_snapshot_replay", session, "orders", "tenant_id = 7 AND region = 'east'")
    replay_bytes = canonical_text(replay["plan"])
    replay_equal = replay_bytes == original_orders_bytes
    restored_unit_names = sorted(session.config.unit_map())
    snapshot_payload = {
        "cache": {
            "exact_snapshot_match_before_replay": cache_exact,
            "prior_v2_entries": prior_cache_count,
            "restored_entries": save_report["cache_entries"],
            "snapshot_entries": snapshot_cache_count,
        },
        "plan_comparison": {
            "original_sha256": hashlib.sha256(original_orders_bytes.encode("utf-8")).hexdigest(),
            "replay_sha256": hashlib.sha256(replay_bytes.encode("utf-8")).hexdigest(),
            "replay_byte_identical": replay_equal,
        },
        "replay": replay,
        "restore": restore_report,
        "restored_route_units": restored_unit_names,
        "save": save_report,
        "schema": "shardrouter.demo.snapshot.v1",
    }
    write_json(output_dir / "snapshot_restore.json", snapshot_payload)

    anomaly_results = {
        "broken_config": _capture_error(
            logger,
            "broken-config",
            E_CONFIG,
            lambda: load_config(SAMPLES / "anomalies" / "broken_config.json"),
        ),
        "ring_conflict": _capture_error(
            logger,
            "ring-conflict",
            E_RING,
            lambda: load_config(Path(args.config), inject_ring_conflict=True),
        ),
        "version_mismatch": _capture_error(
            logger,
            "version-mismatch",
            E_VERSION,
            lambda: RouterSession(config_v1, expected_metadata_version=999),
        ),
    }
    algorithm_names = sorted(
        {
            outcome["plan"]["routing"]["algorithm"]
            for outcome in normal_routes
        }
    )
    ledger_plan = next(
        outcome["plan"] for outcome in normal_routes if outcome["label"] == "ledger_boundary"
    )
    ledger_buckets = sorted(
        target["algorithm_bucket"] for target in ledger_plan["routing"]["targets"]
    )
    broadcast_plan = next(
        outcome["plan"] for outcome in normal_routes if outcome["label"] == "orders_broadcast"
    )
    cache_hit = any(outcome["cache"]["status"] == "hit" for outcome in normal_routes)
    anomaly_codes_ok = all(
        result["status"] == "rejected" and result["code"] == result["expected"]
        for result in anomaly_results.values()
    )
    invalidation_order = apply_report["invalidation"]["order"]
    acceptance = {
        "anomaly_codes": anomaly_codes_ok,
        "boundary_values": ledger_buckets == [1, 2],
        "broadcast_marked": broadcast_plan["routing"]["broadcast"]
        and broadcast_plan["routing"]["reason_code"] == "BROADCAST_MISSING_SHARD_KEYS",
        "cache_hit": cache_hit,
        "invalidation_order": invalidation_order,
        "invalidation_propagated": "orders_rollup" in invalidation_order,
        "replay_byte_identical": replay_equal,
        "snapshot_whole_replace": restore_report["mode"] == "whole_replace"
        and cache_exact
        and "orders_audit" not in restored_unit_names,
        "stable_plans": independent_stability["stable"] and repeated_plan_stability["stable"],
        "three_algorithms": algorithm_names == ["consistent_hash", "mod", "range"],
    }
    summary = {
        "acceptance": acceptance,
        "algorithms": algorithm_names,
        "anomalies": anomaly_results,
        "cache": {
            "hit_observed": cache_hit,
            "snapshot_entries_restored": save_report["cache_entries"],
        },
        "invalidation": apply_report["invalidation"],
        "schema": "shardrouter.demo.summary.v1",
        "snapshot_restore": {
            "differences": restore_report["differences"],
            "mode": restore_report["mode"],
            "replay_byte_identical": replay_equal,
        },
        "status": "ok" if all(value is True for key, value in acceptance.items() if key not in ("invalidation_order",)) else "failed",
        "versions_after_change": {
            "config_version": apply_report["config_version"],
            "ring_versions": incremental_payload["versions"]["ring_versions"],
            "table_versions": incremental_payload["versions"]["table_versions"],
        },
    }
    write_json(output_dir / "summary.json", summary)

    logger.emit("demo_complete", status=summary["status"])
    logger.flush()
    artifact_names = [
        "incremental_routing.json",
        "normal_routing.json",
        "snapshot_restore.json",
        "snapshot_v1.json",
        "structured_log.jsonl",
        "summary.json",
    ]
    verification = {
        "artifacts": {
            name: _sha256_file(output_dir / name) for name in artifact_names
        },
        "schema": "shardrouter.demo.verification.v1",
        "status": summary["status"],
    }
    write_json(output_dir / "verification.json", verification)
    _print(summary)
    return 0 if summary["status"] == "ok" else 1
def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "demo":
            return _run_demo(args)
        if args.command == "anomaly":
            return _run_anomaly(args)
        if args.command == "route":
            config = load_config(
                args.config,
                inject_ring_conflict=args.inject == "ring-conflict",
            )
            session = RouterSession(
                config,
                cache_capacity=args.cache_capacity,
                history_limit=args.history_limit,
                expected_metadata_version=args.metadata_version,
            )
            outcome = session.route_with_cache(args.table, args.predicate)
            logger = JsonlLogger(args.log_file)
            logger.emit(
                "route",
                table=args.table,
                algorithm=outcome["plan"]["routing"]["algorithm"],
                cache_status=outcome["cache"]["status"],
                target_count=outcome["plan"]["routing"]["target_count"],
            )
            logger.flush()
            _print(outcome)
            return 0
        if args.command == "validate":
            config = load_config(
                args.config,
                inject_ring_conflict=args.inject == "ring-conflict",
            )
            _print(
                {
                    "config": config.to_dict(),
                    "status": "ok",
                    "validation": "accepted",
                }
            )
            return 0
        if args.command == "ui":
            config = load_config(args.config)
            session = RouterSession(
                config,
                cache_capacity=args.cache_capacity,
                history_limit=args.history_limit,
            )
            model = build_ui_model(session)
            if args.headless:
                _print({"model": model, "status": "headless_view_model"})
                return 0
            result = launch_ui(session, model=model)
            _print(result)
            return 0
    except RouterError as error:
        log_file = getattr(args, "log_file", None)
        if log_file is not None and args.command != "anomaly":
            logger = JsonlLogger(log_file)
            logger.emit_error("request_rejected", error.to_dict(), command=args.command)
            logger.flush()
        _print(_error_payload(error))
        return 2
    except Exception as error:  # stable CLI envelope for unexpected local failures
        _print(
            {
                "error": {"code": "E_INTERNAL", "message": f"{type(error).__name__}: {error}"},
                "status": "error",
            }
        )
        return 1
    return 2
