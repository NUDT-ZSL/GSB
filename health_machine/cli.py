"""Reproducible command line interface for the health state machine."""
from __future__ import annotations

import argparse
import json
import multiprocessing as mp
import signal
import sys
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from .engine import GracefulTermination, fault_point, process_instance, process_pending, store_observations, store_registry
from .errors import HSMError, VerificationMismatch, error_payload
from .patches import apply_patch_file
from .seed import generate_sample
from .status import build_status
from .store import Store
from .thresholds import build_unified_view, load_v2_bundle, persist_merge, validate_load_v1
from .util import canonical_json, read_json, sha256_json, write_json
from .validation import load_observations, load_registry
from .verify import verify_store


def _json_print(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, default=str, sort_keys=True))


def _handle_termination(signum: int, frame: Any) -> None:
    raise GracefulTermination(f"received signal {signum} at safe transaction boundary")


def _probe_worker(db_path: str, instance_id: str) -> dict[str, Any]:
    store = Store(db_path)
    return {"instance_id": instance_id, "results": process_instance(store, instance_id)}


def _instance_ids(db_path: str) -> list[str]:
    store = Store(db_path)
    conn = store.read_connection()
    try:
        return [row["instance_id"] for row in conn.execute("SELECT instance_id FROM instances ORDER BY instance_id")]
    finally:
        conn.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="hsm", description="Deterministic health probe state machine")
    sub = parser.add_subparsers(dest="command", required=True)

    seed = sub.add_parser("seed", help="generate deterministic local samples")
    seed.add_argument("--out", default="samples")

    load = sub.add_parser("load", help="validate and load registry and observations")
    load.add_argument("--db", default="state/hsm.sqlite")
    load.add_argument("--samples", default="samples")
    load.add_argument("--registry")
    load.add_argument("--observations")

    merge = sub.add_parser("merge", help="validate and merge v1/v2 thresholds")
    merge.add_argument("--db", default="state/hsm.sqlite")
    merge.add_argument("--samples", default="samples")
    merge.add_argument("--v1")
    merge.add_argument("--v2-dir")
    merge.add_argument("--v2-meta")
    merge.add_argument("--merge-version", default="fixed-sample")
    merge.add_argument("--output", default="artifacts/unified_thresholds.json")

    probe = sub.add_parser("probe", help="commit pending observation windows")
    probe.add_argument("--db", default="state/hsm.sqlite")
    probe.add_argument("--instance")
    probe.add_argument("--workers", type=int, default=1)

    patch = sub.add_parser("patch", help="apply an incremental threshold/rename/tombstone patch")
    patch.add_argument("--db", default="state/hsm.sqlite")
    patch.add_argument("--file", required=True)

    resume = sub.add_parser("resume", help="recover durable state and replay only missing suffixes")
    resume.add_argument("--db", default="state/hsm.sqlite")
    resume.add_argument("--instance")

    status = sub.add_parser("status", help="print stable sorted JSON ledger status")
    status.add_argument("--db", default="state/hsm.sqlite")
    status.add_argument("--instance")

    verify = sub.add_parser("verify", help="recompute and field-level compare history")
    verify.add_argument("--db", default="state/hsm.sqlite")
    verify.add_argument("--instance")
    verify.add_argument("--window-index", type=int)

    web = sub.add_parser("web", help="run optional local Flask status server")
    web.add_argument("--db", default="state/hsm.sqlite")
    web.add_argument("--host", default="127.0.0.1")
    web.add_argument("--port", type=int, default=5050)

    return parser


def _paths(args: argparse.Namespace) -> tuple[Path, Path]:
    samples = Path(args.samples)
    registry = Path(args.registry) if args.registry else samples / "registry.json"
    observations = Path(args.observations) if args.observations else samples / "observations.json"
    return registry, observations


def cmd_seed(args: argparse.Namespace) -> int:
    paths = generate_sample(args.out)
    _json_print({"ok": True, "command": "seed", "paths": paths})
    return 0


def cmd_load(args: argparse.Namespace) -> int:
    registry_path, observations_path = _paths(args)
    registry = load_registry(registry_path)
    observations = load_observations(observations_path, registry)
    store = Store(args.db)
    registry_result = store_registry(store, registry, sha256_json(registry))
    observation_result = store_observations(store, observations)
    _json_print({"ok": True, "command": "load", "registry": registry_result, "observations": observation_result})
    return 0


def cmd_merge(args: argparse.Namespace) -> int:
    samples = Path(args.samples)
    registry_path = samples / "registry.json"
    registry = load_registry(registry_path)
    v1_path = Path(args.v1) if args.v1 else samples / "thresholds_v1.json"
    v2_dir = Path(args.v2_dir) if args.v2_dir else samples / "thresholds_v2"
    v2_meta = Path(args.v2_meta) if args.v2_meta else v2_dir / "meta.sqlite"
    v1 = validate_load_v1(v1_path, registry) if v1_path.exists() else None
    v2 = load_v2_bundle(v2_dir, v2_meta, registry) if v2_meta.exists() else None
    views = build_unified_view(registry, v1, v2)
    store = Store(args.db)
    result = persist_merge(store, registry, v1, v2, views, merge_version=args.merge_version)
    fault_point("after_merge_commit")
    export_conn = store.read_connection()
    try:
        exported = []
        for row in export_conn.execute("SELECT * FROM threshold_view ORDER BY instance_id, level"):
            item = dict(row)
            for key in ("raw_failure_counts_json", "time_conversion_json", "provenance_json", "decision_json"):
                item[key.removesuffix("_json")] = json.loads(item.pop(key))
            exported.append(item)
    finally:
        export_conn.close()
    if args.output:
        write_json(args.output, {"ok": True, "merge_version": args.merge_version, "thresholds": exported})
    active = sum(1 for view in views if view["active"])
    excluded = [
        {"instance_id": view["instance_id"], "level": view["level"],
         "generation": view["excluded_generation"] if view["excluded_generation"] is not None else view["generation"],
         "reason": view["exclusion_reason"]}
        for view in views if not view["active"]
    ]
    _json_print({"ok": True, "command": "merge", **result, "output_file": args.output,
                 "active_thresholds": active, "excluded_views": excluded})
    return 0


def cmd_probe(args: argparse.Namespace) -> int:
    store = Store(args.db)
    if args.workers <= 1 or args.instance:
        result = process_pending(store, args.instance)
    else:
        instance_ids = _instance_ids(args.db)
        results = []
        ctx = mp.get_context("fork")
        with ProcessPoolExecutor(max_workers=args.workers, mp_context=ctx) as executor:
            futures = {executor.submit(_probe_worker, args.db, iid): iid for iid in instance_ids}
            for future in as_completed(futures):
                results.extend(future.result()["results"])
        result = {"processed_windows": len(results), "results": results}
    _json_print({"ok": True, "command": "probe", **result})
    return 0


def cmd_patch(args: argparse.Namespace) -> int:
    store = Store(args.db)
    result = apply_patch_file(store, args.file)
    _json_print({"command": "patch", **result})
    return 0


def _recovery_snapshot(store: Store, instance_id: str | None) -> dict[str, Any]:
    conn = store.read_connection()
    try:
        params: tuple[Any, ...] = (instance_id,) if instance_id else ()
        where = "WHERE instance_id=?" if instance_id else ""
        committed_windows = conn.execute(f"SELECT COUNT(*) AS c FROM window_summaries {where}", params).fetchone()["c"]
        half = conn.execute(
            f"SELECT COUNT(*) AS c FROM observation_inbox {where + (' AND ' if where else 'WHERE ')} status='received'",
            params,
        ).fetchone()["c"]
        committed_obs = conn.execute(
            f"SELECT COUNT(*) AS c FROM observation_inbox {where + (' AND ' if where else 'WHERE ')} status='committed'",
            params,
        ).fetchone()["c"]
        return {
            "committed_windows": committed_windows,
            "half_committed_received_observations": half,
            "committed_observations": committed_obs,
        }
    finally:
        conn.close()


def cmd_resume(args: argparse.Namespace) -> int:
    store = Store(args.db)
    before = _recovery_snapshot(store, args.instance)
    result = process_pending(store, args.instance)
    verification = verify_store(store, args.instance)
    _json_print({
        "ok": verification["ok"],
        "command": "resume",
        "recovery_before": before,
        "replayed": result,
        "verification": verification,
    })
    return 0 if verification["ok"] else 6


def cmd_status(args: argparse.Namespace) -> int:
    result = build_status(Store(args.db), args.instance)
    _json_print({"command": "status", **result})
    return 0 if result.get("ok", True) else 2


def cmd_verify(args: argparse.Namespace) -> int:
    result = verify_store(Store(args.db), args.instance, args.window_index)
    _json_print({"command": "verify", **result})
    return 0 if result["ok"] else 6


def cmd_web(args: argparse.Namespace) -> int:
    from .web import create_app

    app = create_app(args.db)
    app.run(host=args.host, port=args.port, debug=False)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    signal.signal(signal.SIGTERM, _handle_termination)
    try:
        if args.command == "seed":
            return cmd_seed(args)
        if args.command == "load":
            return cmd_load(args)
        if args.command == "merge":
            return cmd_merge(args)
        if args.command == "probe":
            return cmd_probe(args)
        if args.command == "patch":
            return cmd_patch(args)
        if args.command == "resume":
            return cmd_resume(args)
        if args.command == "status":
            return cmd_status(args)
        if args.command == "verify":
            return cmd_verify(args)
        if args.command == "web":
            return cmd_web(args)
        parser.error(f"unknown command {args.command}")
        return 2
    except BaseException as exc:  # CLI is the stable process boundary.
        payload = error_payload(exc)
        _json_print(payload)
        if isinstance(exc, HSMError):
            return exc.exit_code
        if isinstance(exc, KeyboardInterrupt):
            return 130
        return 1


if __name__ == "__main__":
    sys.exit(main())
