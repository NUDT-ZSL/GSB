from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path
from typing import Any

from .canonical import canonical_json, sha256_json
from .declarations import load_declarations, load_requests_ndjson
from .diagnostics import build_diagnostic_report, stable_event, write_json_report
from .engine import GatewayEngine
from .errors import GatewayError, SnapshotError
from .logger import StructuredLogger
from .services import (
    BackendPool,
    GatewayTCPServer,
    ensure_ports_available,
    send_gateway_requests,
)
from .storage import SessionStore


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
GATEWAY_PORT = 24310


def _paths(work_dir: Path) -> dict[str, Path]:
    return {
        "db": work_dir / "session.db",
        "log": work_dir / "logs" / "gateway.jsonl",
        "reports": work_dir / "reports",
    }


def reset_work_dir(work_dir: Path) -> dict[str, Path]:
    paths = _paths(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    for suffix in ("", "-wal", "-shm"):
        candidate = Path(str(paths["db"]) + suffix)
        if candidate.exists():
            candidate.unlink()
    if paths["log"].exists():
        paths["log"].unlink()
    if paths["reports"].exists():
        shutil.rmtree(paths["reports"])
    paths["reports"].mkdir(parents=True, exist_ok=True)
    paths["log"].parent.mkdir(parents=True, exist_ok=True)
    return paths


def _ensure_services(engine: GatewayEngine, bundle: Any) -> None:
    ports = [GATEWAY_PORT] + [backend.port for backend in bundle.backends]
    ensure_ports_available("127.0.0.1", ports)


def _gateway_sequence(engine: GatewayEngine, bundle: Any, requests: list[Any]) -> list[dict[str, Any]]:
    _ensure_services(engine, bundle)
    pool = BackendPool.from_states(engine.states, bundle)
    gateway = GatewayTCPServer(host="127.0.0.1", port=GATEWAY_PORT, engine=engine)
    pool.start()
    try:
        gateway.start()
        try:
            return send_gateway_requests(
                host="127.0.0.1", port=GATEWAY_PORT, requests=requests
            )
        finally:
            gateway.stop()
    finally:
        pool.stop()


def _ledger_compact(engine: GatewayEngine) -> list[dict[str, Any]]:
    return [
        {
            "backend_id": row["backend_id"],
            "batch_id": row["batch_id"],
            "bound_fingerprint": row["bound_fingerprint"],
            "effective": row["effective"],
            "invalid_reason": row["invalid_reason"],
            "request_id": row["request_id"],
            "request_index": row["request_index"],
            "rule_id": row["rule_id"],
            "void": row["void"],
        }
        for row in sorted(
            engine.binding_rows(),
            key=lambda item: (item["request_index"], item["entry_id"]),
        )
    ]


def _write_scenario_report(
    paths: dict[str, Path], name: str, report: dict[str, Any]
) -> str:
    return write_json_report(paths["reports"] / f"{name}.json", report)


def _all_requests_ok(responses: list[dict[str, Any]]) -> bool:
    return bool(responses) and all(response.get("status") == "OK" for response in responses)


def run_demo(work_dir: str | Path | None = None) -> dict[str, Any]:
    work = Path(work_dir).resolve() if work_dir else PROJECT_ROOT / "artifacts" / "demo"
    paths = reset_work_dir(work)
    bundle_v1 = load_declarations(DATA_DIR / "backends.json", DATA_DIR / "rules.json")
    bundle_v2 = load_declarations(DATA_DIR / "backends_v2.json", DATA_DIR / "rules.json")
    requests = load_requests_ndjson(DATA_DIR / "requests.ndjson", bundle_v1.feature_schema)
    if bundle_v2.rule_version != bundle_v1.rule_version:
        raise RuntimeError("sample v1/v2 declarations must share one rule version")

    store = SessionStore(paths["db"])
    logger = StructuredLogger(store, paths["log"], fresh=True)
    engine_v1 = GatewayEngine(store=store, bundle=bundle_v1, logger=logger)
    initial_fingerprints = {
        backend_id: state.fingerprint
        for backend_id, state in sorted(engine_v1.states.items())
    }
    initial_evolution = engine_v1.evolution
    initial_responses = _gateway_sequence(engine_v1, bundle_v1, requests)
    if not _all_requests_ok(initial_responses):
        raise RuntimeError("initial request sequence contained non-OK responses")
    initial_snapshot = engine_v1.save_snapshot("initial-baseline")
    initial_report = build_diagnostic_report(engine_v1)
    initial_report_path = _write_scenario_report(paths, "initial", initial_report)
    initial_stats_version = int(engine_v1.state["stats_version"])
    initial_beta_hits = dict(engine_v1.state["stats"]["backend_hits"])
    initial_beta_output = engine_v1.state["stats"]["output_index"]
    initial_summary = {
        "affected_backends": initial_evolution["affected_backends"],
        "effective_bindings_valid": all(
            row["binding_valid"]
            for row in initial_report["ledger"]
            if row["effective"]
        ),
        "fingerprints": initial_fingerprints,
        "rule_hits": initial_report["stats"]["rule_hits"],
        "scenario": "initial_load_and_route",
        "snapshot_id": initial_snapshot["snapshot_id"],
        "stats_version": initial_stats_version,
        "view_version": initial_report["current_view_version"],
    }
    print(stable_event("scenario_summary", **initial_summary), flush=True)
    store.close()

    store = SessionStore(paths["db"])
    logger = StructuredLogger(store, paths["log"], fresh=False)
    engine_v2 = GatewayEngine(store=store, bundle=bundle_v2, logger=logger)
    evolution = engine_v2.evolution
    _ensure_services(engine_v2, bundle_v2)
    pool = BackendPool.from_states(engine_v2.states, bundle_v2)
    pool.start()
    drill_summary: dict[str, Any] = {}
    fired = {"value": False}

    def in_flight_reload(meta: dict[str, Any]) -> None:
        if fired["value"] or meta.get("backend_id") != "be-alpha":
            return
        fired["value"] = True
        drill_summary.update(
            engine_v2.reload_backend(
                backend_id="be-alpha",
                declaration=bundle_v2.backend_by_id["be-alpha"],
            )
        )
        pool.reload(engine_v2.states, bundle_v2)
        logger.warning(
            "INFLIGHT_RELOAD_DRILL",
            "backend fingerprint changed after batch response and before commit",
            batch_id=meta["batch_id"],
            **drill_summary,
        )

    engine_v2.set_next_execution_hook(in_flight_reload)
    gateway = GatewayTCPServer(host="127.0.0.1", port=GATEWAY_PORT, engine=engine_v2)
    try:
        gateway.start()
        try:
            evolved_responses = send_gateway_requests(
                host="127.0.0.1", port=GATEWAY_PORT, requests=requests
            )
        finally:
            gateway.stop()
    finally:
        pool.stop()
    if not _all_requests_ok(evolved_responses):
        raise RuntimeError("evolved request sequence contained non-OK responses")
    if not engine_v2.last_report or not engine_v2.last_report.void_batch_ids:
        raise RuntimeError("in-flight fingerprint drill did not void a batch")
    evolved_snapshot = engine_v2.save_snapshot("evolved-with-redispatch")
    evolved_report = build_diagnostic_report(engine_v2)
    evolved_report_path = _write_scenario_report(paths, "evolution", evolved_report)
    evolved_stats = engine_v2.state["stats"]
    unaffected_beta = {
        "backend_hit_equal": evolved_stats["backend_hits"].get("be-beta")
        == initial_beta_hits.get("be-beta"),
        "initial_be_beta_hits": initial_beta_hits.get("be-beta"),
        "evolved_be_beta_hits": evolved_stats["backend_hits"].get("be-beta"),
        "output_index_changed_globally": evolved_stats["output_index"]
        != initial_beta_output,
    }
    evolution_summary = {
        "affected_backends": evolution["affected_backends"],
        "affected_rules": evolution["affected_rules"],
        "drill": drill_summary,
        "fingerprints": {
            backend_id: state.fingerprint
            for backend_id, state in sorted(engine_v2.states.items())
        },
        "invalidated_entry_count": evolution["invalidated_entry_count"],
        "redispatched_batch_ids": engine_v2.last_report.redispatched_batch_ids,
        "scenario": "fingerprint_evolution_and_rerun",
        "snapshot_id": evolved_snapshot["snapshot_id"],
        "stats_version": engine_v2.last_report.stats_version,
        "unaffected_beta": unaffected_beta,
        "void_batch_ids": engine_v2.last_report.void_batch_ids,
    }
    print(stable_event("scenario_summary", **evolution_summary), flush=True)
    snapshot_id = evolved_snapshot["snapshot_id"]
    store.close()

    store = SessionStore(paths["db"])
    logger = StructuredLogger(store, paths["log"], fresh=False)
    engine_v3 = GatewayEngine(store=store, bundle=bundle_v2, logger=logger)
    reopen_summary = engine_v3.evolution
    _ensure_services(engine_v3, bundle_v2)
    pool = BackendPool.from_states(engine_v3.states, bundle_v2)
    pool.start()
    try:
        drift_requests = requests[:2]
        drift_report = engine_v3.execute_requests(drift_requests)
        pre_restore_ledger_count = len(engine_v3.state["ledger"])
        before_restore_digest = store.current_digest()
        snapshot = engine_v3.restore_snapshot(snapshot_id)
        restored_digest = store.current_digest()
        if restored_digest != snapshot["state_digest"]:
            raise RuntimeError("snapshot restore did not produce exact state digest")
        if before_restore_digest == restored_digest:
            raise RuntimeError("drift probe did not change the pre-restore view")
        drift_batch_ids = set(drift_report.batch_ids)
        restored_batch_ids = {
            batch["batch_id"] for batch in engine_v3.state.get("batches", [])
        }
        if drift_batch_ids & restored_batch_ids:
            raise RuntimeError("snapshot restore merged drift batches")
        replay = engine_v3.replay_snapshot(snapshot_id)
        restore_report = build_diagnostic_report(engine_v3)
        restore_report_path = _write_scenario_report(paths, "restore", restore_report)
        current_stats_row = store.get_stats_version(int(engine_v3.state["stats_version"]))
        restore_summary = {
            "current_stats_readonly": (
                None if current_stats_row is None else current_stats_row["readonly"]
            ),
            "drift_batch_ids_removed": sorted(drift_batch_ids),
            "ledger_entries_after_restore": len(engine_v3.state.get("ledger", [])),
            "ledger_entries_before_restore": pre_restore_ledger_count,
            "reopen_reason": reopen_summary["reason"],
            "replay_matched": replay["matched"],
            "replay_request_count": replay["request_count"],
            "scenario": "snapshot_restore_and_replay",
            "snapshot_id": snapshot_id,
            "state_digest": restored_digest,
            "stats_version": int(engine_v3.state["stats_version"]),
        }
        print(stable_event("scenario_summary", **restore_summary), flush=True)
    finally:
        pool.stop()
        store.close()

    checks = {
        "all_initial_requests_ok": _all_requests_ok(initial_responses),
        "all_evolved_requests_ok": _all_requests_ok(evolved_responses),
        "effective_initial_bindings_valid": initial_summary[
            "effective_bindings_valid"
        ],
        "fingerprint_evolution_invalidated_entries": evolution["invalidated_entry_count"]
        > 0,
        "in_flight_void_and_redispatch": bool(engine_v2.last_report.void_batch_ids)
        and bool(engine_v2.last_report.redispatched_batch_ids),
        "replay_matched": replay["matched"],
        "snapshot_restore_whole_replacement": restored_digest
        == snapshot["state_digest"]
        and not (drift_batch_ids & restored_batch_ids),
        "unaffected_beta_hit_count_unchanged": unaffected_beta["backend_hit_equal"],
    }
    if not all(checks.values()):
        raise RuntimeError(f"diagnostic checks failed: {checks}")
    final_summary = {
        "checks": checks,
        "reports": {
            "evolution": evolved_report_path,
            "initial": initial_report_path,
            "restore": restore_report_path,
        },
        "scenarios": [
            initial_summary,
            evolution_summary,
            restore_summary,
        ],
        "work_dir": str(work),
    }
    summary_path = write_json_report(paths["reports"] / "summary.json", final_summary)
    final_summary["reports"]["summary"] = summary_path
    print(stable_event("demo_complete", **final_summary), flush=True)
    return final_summary


def _fault_store(paths: dict[str, Path]) -> tuple[SessionStore, StructuredLogger]:
    store = SessionStore(paths["db"])
    return store, StructuredLogger(store, paths["log"], fresh=True)


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def _corrupt_declaration_fault(paths: dict[str, Path], work: Path) -> dict[str, Any]:
    store, logger = _fault_store(paths)
    try:
        fault_dir = work / "faults" / "corrupt_declaration"
        fault_dir.mkdir(parents=True, exist_ok=True)
        backend_path = fault_dir / "backends.json"
        _write_json(
            backend_path,
            {
                "schema_version": 1,
                "backends": [
                    {
                        "backend_id": "be-alpha",
                        "model_id": "model-alpha-vision",
                        "host": "127.0.0.1",
                        "port": 70000,
                        "max_concurrency": 4,
                        "parameter_length": 4,
                        "parameter_file": "params/alpha.json",
                    }
                ],
            },
        )
        try:
            load_declarations(backend_path, DATA_DIR / "rules.json")
        except GatewayError as exc:
            record = logger.error(exc.code, exc.message, error=exc.to_dict())
            report = {
                "error": exc.to_dict(),
                "expected_code": "E_RULE",
                "fault": "corrupt_declaration",
                "log_event": record,
            }
            report_path = write_json_report(paths["reports"] / "fault.json", report)
            return {**report, "report_path": report_path}
    finally:
        store.close()
    raise RuntimeError("corrupt declaration fault did not raise E_RULE")


def _vector_length_fault(paths: dict[str, Path], work: Path) -> dict[str, Any]:
    store, logger = _fault_store(paths)
    try:
        fault_dir = work / "faults" / "vector_length"
        (fault_dir / "params").mkdir(parents=True, exist_ok=True)
        _write_json(fault_dir / "params" / "short.json", [0.5, -1.25, 2.0])
        backend_path = fault_dir / "backends.json"
        _write_json(
            backend_path,
            {
                "schema_version": 1,
                "backends": [
                    {
                        "backend_id": "be-alpha",
                        "model_id": "model-alpha-vision",
                        "host": "127.0.0.1",
                        "port": 24311,
                        "max_concurrency": 4,
                        "parameter_length": 4,
                        "parameter_file": "params/short.json",
                    }
                ],
            },
        )
        try:
            load_declarations(backend_path, DATA_DIR / "rules.json")
        except GatewayError as exc:
            record = logger.error(exc.code, exc.message, error=exc.to_dict())
            report = {
                "error": exc.to_dict(),
                "expected_code": "E_VECTOR",
                "fault": "vector_length",
                "log_event": record,
            }
            report_path = write_json_report(paths["reports"] / "fault.json", report)
            return {**report, "report_path": report_path}
    finally:
        store.close()
    raise RuntimeError("vector length fault did not raise E_VECTOR")


def _snapshot_conflict_fault(paths: dict[str, Path], work: Path) -> dict[str, Any]:
    store, logger = _fault_store(paths)
    bundle_v1 = load_declarations(DATA_DIR / "backends.json", DATA_DIR / "rules.json")
    engine_v1 = GatewayEngine(store=store, bundle=bundle_v1, logger=logger)
    baseline = engine_v1.save_snapshot("conflict-baseline")
    store.close()

    store = SessionStore(paths["db"])
    logger = StructuredLogger(store, paths["log"], fresh=False)
    bundle_v2 = load_declarations(DATA_DIR / "backends_v2.json", DATA_DIR / "rules.json")
    engine_v2 = GatewayEngine(store=store, bundle=bundle_v2, logger=logger)
    try:
        engine_v2.restore_snapshot(baseline["snapshot_id"])
    except SnapshotError as exc:
        record = logger.error(exc.code, exc.message, error=exc.to_dict())
        report = {
            "error": exc.to_dict(),
            "expected_code": "E_SNAP",
            "fault": "snapshot_conflict",
            "log_event": record,
            "snapshot_id": baseline["snapshot_id"],
        }
        report_path = write_json_report(paths["reports"] / "fault.json", report)
        return {**report, "report_path": report_path}
    finally:
        store.close()
    raise RuntimeError("snapshot conflict fault did not raise E_SNAP")


def run_fault(fault: str, work_dir: str | Path | None = None) -> dict[str, Any]:
    work = Path(work_dir).resolve() if work_dir else PROJECT_ROOT / "artifacts" / "faults"
    paths = reset_work_dir(work)
    if fault == "corrupt_declaration":
        result = _corrupt_declaration_fault(paths, work)
    elif fault == "vector_length":
        result = _vector_length_fault(paths, work)
    elif fault == "snapshot_conflict":
        result = _snapshot_conflict_fault(paths, work)
    else:
        raise ValueError(f"unknown fault: {fault}")
    print(stable_event("expected_fault", **result), flush=True)
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Deterministic ML inference gateway fingerprint diagnostics demo"
    )
    parser.add_argument(
        "--fault",
        choices=("corrupt_declaration", "vector_length", "snapshot_conflict"),
        help="run one expected business-error injection instead of the three scenarios",
    )
    parser.add_argument(
        "--work-dir",
        help="local directory for SQLite, structured logs, and diagnostic reports",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.fault:
            run_fault(args.fault, args.work_dir)
            return 2
        run_demo(args.work_dir)
        return 0
    except GatewayError as exc:
        print(canonical_json({"error": exc.to_dict(), "event": "fatal_business_error"}))
        return 1


if __name__ == "__main__":
    sys.exit(main())
