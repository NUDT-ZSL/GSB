from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .engine import recover_store, run_to_completion, verify_replay
from .errors import EngineError, ErrorCode
from .generator import DEFAULT_SEED, generate_samples
from .model import Injection, normalize_injections
from .storage import Store


DEFAULT_DB = "state/gradient_engine.db"
DEFAULT_SAMPLES = "samples"


def print_json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, default=str))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="main.py",
        description="Deterministic ring AllReduce gradient synchronization engine",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_generate = sub.add_parser("generate", help="generate fixed-seed samples")
    p_generate.add_argument("out_positional", nargs="?", metavar="OUT")
    p_generate.add_argument("--out", "--output", dest="out")
    p_generate.add_argument("--seed", type=int, default=DEFAULT_SEED)

    p_load = sub.add_parser("load", help="validate inputs and initialize SQLite state")
    p_load.add_argument("samples_positional", nargs="?", metavar="SAMPLES")
    p_load.add_argument("--samples", "--samples-dir", "--input-dir", "--data", dest="samples")
    p_load.add_argument("--db", "--state", default=DEFAULT_DB)
    p_load.add_argument("--plan", "--plan-file", "--training-plan", dest="plan")
    p_load.add_argument("--nodes", "--nodes-file", "--node-inventory", dest="nodes")
    p_load.add_argument("--events", "--events-file", "--gradient-events", dest="events")
    p_load.add_argument("--injections", "--injections-file", "--injection-plan", dest="injections")

    p_run = sub.add_parser("run", help="run deterministic scheduling/replay")
    p_run.add_argument("samples_positional", nargs="?", metavar="SAMPLES")
    p_run.add_argument("--samples", "--samples-dir", dest="samples")
    p_run.add_argument("--db", "--state", default=DEFAULT_DB)
    p_run.add_argument("--max-rounds", "--rounds", type=int, default=100_000)
    p_run.add_argument("--crash-after-prepare-round", type=int, help=argparse.SUPPRESS)
    p_run.add_argument("--crash-before-commit-round", type=int, help=argparse.SUPPRESS)
    p_run.add_argument("--crash-after-commit-round", type=int, help=argparse.SUPPRESS)

    p_inject = sub.add_parser("inject", help="append and apply a controlled injection")
    p_inject.add_argument("file_positional", nargs="?", metavar="INJECTION_FILE")
    p_inject.add_argument("--file", "--input", "--injections", dest="file")
    p_inject.add_argument("--db", "--state", default=DEFAULT_DB)
    p_inject.add_argument("--type", "--kind", dest="kind")
    p_inject.add_argument("--node", "--node-id", dest="node_id")
    p_inject.add_argument("--clock", "--logic-clock-ms", "--at", "--time", type=int, dest="clock")
    p_inject.add_argument("--spec-file")
    p_inject.add_argument("--spec-version", type=int)
    p_inject.add_argument("--injection-id")
    p_inject.add_argument("--injection", "--payload", dest="payload")
    p_inject.add_argument("--allow-late-clock", action="store_true")

    p_resume = sub.add_parser("resume", help="recover half-committed rounds and continue")
    p_resume.add_argument("--db", "--state", default=DEFAULT_DB)
    p_resume.add_argument("--max-rounds", "--rounds", type=int, default=100_000)

    p_status = sub.add_parser("status", help="print stable sorted JSON status")
    p_status.add_argument("--db", "--state", default=DEFAULT_DB)
    p_status.add_argument("--node", "--node-id", dest="node_id")
    p_status.add_argument("--output", "--out", "-o", dest="output")
    p_status.add_argument("--json", action="store_true", help="accepted for compatibility; status is always JSON")

    p_verify = sub.add_parser("verify", help="replay and compare rounds/ledgers")
    p_verify.add_argument("samples_positional", nargs="?", metavar="SAMPLES")
    p_verify.add_argument("--samples", "--samples-dir", dest="samples")
    p_verify.add_argument("--db", "--state", default=DEFAULT_DB)

    p_demo = sub.add_parser("demo", help="run an end-to-end deterministic acceptance demo")
    p_demo.add_argument("--dir", default="demo_workspace")
    p_demo.add_argument("--seed", type=int, default=DEFAULT_SEED)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "generate":
            out = args.out or args.out_positional or DEFAULT_SAMPLES
            print_json(generate_samples(out, seed=args.seed))
            return 0
        if args.command == "load":
            return cmd_load(args)
        if args.command == "run":
            return cmd_run(args)
        if args.command == "inject":
            return cmd_inject(args)
        if args.command == "resume":
            return cmd_resume(args)
        if args.command == "status":
            return cmd_status(args)
        if args.command == "verify":
            return cmd_verify(args)
        if args.command == "demo":
            from .acceptance import run_demo

            print_json(run_demo(Path(args.dir), seed=args.seed))
            return 0
        parser.error(f"unknown command: {args.command}")
    except EngineError as exc:
        print(exc.to_json())
        return exc.exit_code
    except FileNotFoundError as exc:
        error = EngineError(ErrorCode.NOT_FOUND, str(exc))
        print(error.to_json())
        return 1
    return 2


def cmd_load(args: argparse.Namespace) -> int:
    samples_dir = args.samples or args.samples_positional
    if samples_dir:
        path = Path(samples_dir)
        if path.is_file():
            if args.plan is None and path.name.endswith(".json") and "plan" in path.name:
                args.plan = str(path)
            samples_dir = str(path.parent)
    if not any([samples_dir, args.plan, args.nodes, args.events, args.injections]):
        samples_dir = DEFAULT_SAMPLES
    from .model import load_inputs

    inputs = load_inputs(
        samples_dir,
        plan_path=args.plan,
        nodes_path=args.nodes,
        events_path=args.events,
        injections_path=args.injections,
    )
    store = Store.reset_and_load(args.db, inputs)
    try:
        result = {
            "ok": True,
            "command": "load",
            "db": str(Path(args.db)),
            "initial_spec_version": inputs.initial_spec.version,
            "layers": [layer.name for layer in inputs.initial_spec.layers],
            "nodes": [node.node_id for node in inputs.nodes],
            "events": len(inputs.events),
            "injections": len(inputs.injections),
            "max_concurrency": inputs.scheduling["max_concurrency"],
            "time_budget_ms": inputs.scheduling["time_budget_ms"],
        }
        print_json(result)
    finally:
        store.close()
    return 0


def open_store(db_path: str) -> Store:
    path = Path(db_path)
    if str(path) != ":memory:" and not path.exists():
        raise EngineError(ErrorCode.NOT_FOUND, f"state database does not exist: {path}")
    store = Store.connect(path)
    store.verify_database_integrity()
    return store


def open_or_bootstrap_store(args: argparse.Namespace) -> Store:
    path = Path(args.db)
    if path.exists():
        return open_store(args.db)
    samples_dir = getattr(args, "samples", None) or getattr(args, "samples_positional", None)
    if samples_dir is None and Path(DEFAULT_SAMPLES).exists():
        samples_dir = DEFAULT_SAMPLES
    if samples_dir is None:
        raise EngineError(ErrorCode.NOT_FOUND, f"state database does not exist: {path}")
    from .model import load_inputs

    inputs = load_inputs(samples_dir)
    return Store.reset_and_load(path, inputs)


def crash_dict(args: argparse.Namespace) -> dict[str, int] | None:
    crash: dict[str, int] = {}
    for key, attr in (
        ("after_prepare_round", "crash_after_prepare_round"),
        ("before_commit_round", "crash_before_commit_round"),
        ("after_commit_round", "crash_after_commit_round"),
    ):
        value = getattr(args, attr, None)
        if value is not None:
            crash[key] = value
    return crash or None


def cmd_run(args: argparse.Namespace) -> int:
    store = open_or_bootstrap_store(args)
    try:
        result = run_to_completion(
            store,
            max_rounds=args.max_rounds,
            crash=crash_dict(args),
        )
        result["command"] = "run"
        result["db"] = str(Path(args.db))
        print_json(result)
    finally:
        store.close()
    return 0


def cmd_resume(args: argparse.Namespace) -> int:
    store = open_store(args.db)
    try:
        recovered = recover_store(store)
        result = run_to_completion(store, max_rounds=args.max_rounds, recover=False)
        result["command"] = "resume"
        result["recovered_rounds"] = recovered
        result["db"] = str(Path(args.db))
        print_json(result)
    finally:
        store.close()
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    store = open_store(args.db)
    try:
        result = store.status()
        if args.node_id:
            if args.node_id not in result["nodes"]:
                raise EngineError(ErrorCode.NOT_FOUND, f"unknown node: {args.node_id}")
            result = {
                "ok": True,
                "node_id": args.node_id,
                "status": result["nodes"][args.node_id],
                "clock_watermark_ms": result["clock_watermark_ms"],
                "active_spec_version": result["active_spec_version"],
            }
        text = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True, default=str)
        if args.output:
            output_path = Path(args.output)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(text + "\n", encoding="utf-8")
        print(text)
    finally:
        store.close()
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    store = open_store(args.db)
    try:
        inputs = None
        samples_dir = args.samples or args.samples_positional
        if samples_dir:
            from .model import load_inputs

            inputs = load_inputs(samples_dir)
        result = verify_replay(store, inputs)
        result["command"] = "verify"
        result["db"] = str(Path(args.db))
        print_json(result)
        return 0 if result["ok"] else 1
    finally:
        store.close()


def cmd_inject(args: argparse.Namespace) -> int:
    store = open_store(args.db)
    try:
        inputs = store.load_inputs_from_db()
        known_nodes = inputs.node_ids
        raw_injections: list[dict[str, Any]] = []
        file_path = args.file or args.file_positional
        if args.payload:
            try:
                payload = json.loads(args.payload)
            except json.JSONDecodeError as exc:
                raise EngineError(ErrorCode.INJECTIONS_INVALID, f"invalid --injection JSON: {exc.msg}") from exc
            if isinstance(payload, list):
                raw_injections = payload
            elif isinstance(payload, dict) and isinstance(payload.get("injections"), list):
                raw_injections = payload["injections"]
            elif isinstance(payload, dict):
                raw_injections = [payload]
            else:
                raise EngineError(ErrorCode.INJECTIONS_INVALID, "--injection must be a JSON object or list")
        elif file_path:
            path = Path(file_path)
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except FileNotFoundError as exc:
                raise EngineError(ErrorCode.NOT_FOUND, f"injection file not found: {path}") from exc
            except json.JSONDecodeError as exc:
                raise EngineError(ErrorCode.INJECTIONS_INVALID, f"invalid injection JSON: {exc.msg}") from exc
            if isinstance(raw, dict):
                raw_injections = raw.get("injections", raw.get("items", []))
            elif isinstance(raw, list):
                raw_injections = raw
            else:
                raise EngineError(ErrorCode.INJECTIONS_INVALID, "injection file must be object or list")
        else:
            if not args.kind:
                raise EngineError(ErrorCode.CLI_INVALID, "inject requires --type or --file")
            if args.clock is None:
                raise EngineError(ErrorCode.CLI_INVALID, "inject requires --clock LOGIC_CLOCK_MS")
            injection_id = args.injection_id or f"manual-{args.kind}-{args.clock}-{args.node_id or 'spec'}"
            item: dict[str, Any] = {
                "id": injection_id,
                "type": args.kind,
                "logic_clock_ms": args.clock,
            }
            if args.node_id:
                item["node_id"] = args.node_id
            if args.spec_file:
                spec_path = Path(args.spec_file)
                try:
                    item["spec"] = json.loads(spec_path.read_text(encoding="utf-8"))
                except FileNotFoundError as exc:
                    raise EngineError(ErrorCode.NOT_FOUND, f"spec file not found: {spec_path}") from exc
                except json.JSONDecodeError as exc:
                    raise EngineError(ErrorCode.SPEC_INVALID, f"invalid spec JSON: {exc.msg}") from exc
                if args.spec_version is not None:
                    item["spec"]["version"] = args.spec_version
            raw_injections = [item]

        injections = normalize_injections(
            raw_injections, known_nodes, inputs.initial_spec.version
        )
        existing_ids = {
            row["injection_id"]
            for row in store.conn.execute("SELECT injection_id FROM injections").fetchall()
        }
        latest_version = max(inputs.spec_versions())
        new_injections = []
        for injection in injections:
            if injection.injection_id in existing_ids:
                continue
            if injection.spec is not None and injection.spec.version <= latest_version:
                raise EngineError(
                    ErrorCode.SPEC_VERSION_CONFLICT,
                    "new spec injection version must be greater than active/latest version",
                    injection_id=injection.injection_id,
                    version=injection.spec.version,
                    latest_version=latest_version,
                )
            if injection.logic_clock_ms < store.clock_watermark() and not args.allow_late_clock:
                raise EngineError(
                    ErrorCode.STATE_INVALID,
                    "injection clock is behind persisted logical clock watermark",
                    injection_id=injection.injection_id,
                    injection_clock_ms=injection.logic_clock_ms,
                    clock_watermark_ms=store.clock_watermark(),
                )
            insert_manual_injection(store, injection)
            existing_ids.add(injection.injection_id)
            new_injections.append(injection)

        result = run_to_completion(store)
        result["command"] = "inject"
        result["applied_injections"] = [inj.injection_id for inj in new_injections]
        result["existing_injections_skipped"] = [
            inj.injection_id for inj in injections if inj.injection_id in existing_ids and inj not in new_injections
        ]
        print_json(result)
        return 0
    finally:
        store.close()


def insert_manual_injection(store: Store, injection: Injection) -> None:
    row = store.conn.execute("SELECT COALESCE(MAX(order_index), -1) + 1 AS next_index FROM injections").fetchone()
    order_index = int(row["next_index"])
    with store.conn:
        store.conn.execute(
            "INSERT INTO injections(order_index,injection_id,logic_clock_ms,kind,node_id,"
            "spec_version,payload_json,applied,applied_round,applied_clock)"
            " VALUES(?,?,?,?,?,?,?,0,NULL,NULL)",
            (
                order_index,
                injection.injection_id,
                injection.logic_clock_ms,
                injection.kind,
                injection.node_id,
                injection.spec.version if injection.spec else None,
                json.dumps(injection.to_dict(), ensure_ascii=False, sort_keys=True),
            ),
        )
        if injection.spec is not None:
            spec = injection.spec
            store.conn.execute(
                "INSERT OR IGNORE INTO specs(version,method,spec_json,sha256,status,created_clock)"
                " VALUES(?,?,?,?, 'pending', ?)",
                (
                    spec.version,
                    spec.method,
                    json.dumps(spec.to_dict(), sort_keys=True, ensure_ascii=False),
                    __import__("gradient_engine.canon", fromlist=["sha256_json"]).sha256_json(spec.to_dict()),
                    injection.logic_clock_ms,
                ),
            )


if __name__ == "__main__":
    sys.exit(main())
