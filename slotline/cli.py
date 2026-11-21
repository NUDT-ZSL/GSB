from __future__ import annotations

import argparse
import hashlib
import json
import tempfile
import threading
from pathlib import Path
from typing import Any

from .cache import CacheStore
from .canonical import canonical_json_bytes
from .engine import Engine
from .errors import BusinessError
from .generator import generate_append_turn, generate_session
from .server import create_server


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)


def _state_projection(engine: Engine) -> dict[str, Any]:
    current = engine._last_outcome
    return {
        "state_hash": current.state_hash if current else None,
        "slots": {
            key: {
                "value": slot["value"],
                "confidence": slot["confidence"],
                "score": slot["score"],
                "source_turn": slot["source_turn"],
                "extractor_version": slot["extractor_version"],
                "conflict": slot["conflict"],
            }
            for key, slot in engine.state["slots"].items()
        },
        "pending": engine.state["pending_slots"],
    }


def command_build(args: argparse.Namespace) -> int:
    session = generate_session(args.seed)
    output = Path(args.out)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(canonical_json_bytes(session.to_dict()) + b"\n")
    engine = Engine(
        session,
        runtime_dir=args.runtime,
    )
    outcome = engine.build()
    print(
        _json(
            {
                "session_file": str(output),
                "session_sha256": hashlib.sha256(
                    output.read_bytes()
                ).hexdigest(),
                "state_hash": outcome.state_hash,
                "snapshot": outcome.snapshot_summary(),
                "slots": _state_projection(engine)["slots"],
                "pending": engine.state["pending_slots"],
            }
        )
    )
    return 0


def command_demo(args: argparse.Namespace) -> int:
    session = generate_session(args.seed)
    with tempfile.TemporaryDirectory(prefix="slotline-main-") as main_runtime:
        with tempfile.TemporaryDirectory(prefix="slotline-fresh-") as fresh_runtime:
            engine = Engine(session, runtime_dir=main_runtime)
            engine.build()
            initial_hash = engine._last_outcome.state_hash

            retract_turn = session.metadata["demo"]["retract_turn_id"]
            retract_result = engine.retract(retract_turn)
            fresh_without = Engine(
                session.without_turn_ids({retract_turn}),
                runtime_dir=fresh_runtime,
            )
            fresh_without.build()
            retract_equivalent = (
                engine._last_outcome.state_bytes
                == fresh_without._last_outcome.state_bytes
            )

            append_turn = generate_append_turn(engine.session)
            append_result = engine.append_turn(append_turn)
            fresh_full = Engine(
                engine.session.without_turn_ids(engine.retracted),
                runtime_dir=fresh_runtime,
            )
            fresh_full.build()
            append_equivalent = (
                engine._last_outcome.state_bytes
                == fresh_full._last_outcome.state_bytes
            )
            trace = engine.trace_slot("budget")

            result = {
                "seed": args.seed,
                "initial_state_hash": initial_hash,
                "retract": {
                    "turn_id": retract_turn,
                    "affected_slots": retract_result["affected_slots"],
                    "recomputed_nodes": retract_result["recomputed_nodes"],
                    "reused_nodes": retract_result["reused_nodes"],
                    "removed_nodes": retract_result["removed_nodes"],
                    "equivalent_to_direct_rebuild": retract_equivalent,
                },
                "append": {
                    "turn_id": append_turn.turn_id,
                    "affected_slots": append_result["affected_slots"],
                    "recomputed_nodes": append_result["recomputed_nodes"],
                    "reused_nodes": append_result["reused_nodes"],
                    "committed_turns_rescored": append_result[
                        "committed_turns_rescored"
                    ],
                    "equivalent_to_full_rebuild": append_equivalent,
                },
                "trace": {
                    "slot": trace["slot_key"],
                    "value": trace["value"],
                    "covered": [item["value"] for item in trace["covered"]],
                    "winner": trace["winner"],
                    "merge_cache_key": trace["merge_cache_key"],
                    "verified": trace["verified"],
                },
                "final_snapshot": engine.snapshot_summary(),
                "final_state": _state_projection(engine),
            }
            print(_json(result))
            return 0 if retract_equivalent and append_equivalent else 1


def command_serve(args: argparse.Namespace) -> int:
    server = create_server(
        host=args.host,
        port=args.port,
        seed=args.seed,
        runtime_dir=args.runtime,
    )
    host, port = server.server_address[:2]
    print(f"SlotLine 本地诊断应用: http://{host}:{port}", flush=True)
    print("按 Ctrl+C 停止。", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


def command_trace(args: argparse.Namespace) -> int:
    session = generate_session(args.seed)
    with tempfile.TemporaryDirectory(prefix="slotline-trace-") as runtime:
        engine = Engine(session, runtime_dir=runtime)
        engine.build()
        print(_json(engine.trace_slot(args.slot)))
    return 0


def command_diagnose(args: argparse.Namespace) -> int:
    codes: dict[str, Any] = {}
    with tempfile.TemporaryDirectory(prefix="slotline-diagnose-") as runtime:
        session = generate_session(args.seed)
        engine = Engine(session, runtime_dir=runtime)
        engine.build()

        current_version = engine.snapshot_summary()["version"]
        try:
            engine.snapshots.commit(
                config=engine.config,
                cache_keys=engine._last_outcome.node_keys.values(),
                version=current_version,
            )
        except BusinessError as exc:
            codes[exc.code] = exc.to_dict()

        lock_key = "0" * 64
        started = threading.Event()
        release = threading.Event()

        def hold_lock() -> None:
            try:
                with engine.cache.begin_write(lock_key):
                    started.set()
                    release.wait(timeout=3)
            except BusinessError as exc:
                codes[f"holder_{exc.code}"] = exc.to_dict()

        holder = threading.Thread(target=hold_lock, daemon=True)
        holder.start()
        started.wait(timeout=3)
        try:
            with engine.cache.begin_write(lock_key):
                pass
        except BusinessError as exc:
            codes[exc.code] = exc.to_dict()
        finally:
            release.set()
            holder.join(timeout=3)

        append_turn = generate_append_turn(engine.session)
        try:
            engine.apply_batch(
                [
                    {"op": "append", "turn": append_turn.to_dict()},
                    {"op": "retract", "turn_id": "u2"},
                ]
            )
        except BusinessError as exc:
            codes[exc.code] = exc.to_dict()

    with tempfile.TemporaryDirectory(prefix="slotline-diagnose-broken-") as runtime:
        engine = Engine(generate_session(args.seed), runtime_dir=runtime)
        engine.build()
        slot = engine.state["slots"]["budget"]
        engine.cache.path_for(slot["merge_cache_key"]).unlink()
        try:
            engine.trace_slot("budget")
        except BusinessError as exc:
            codes[exc.code] = exc.to_dict()

    with tempfile.TemporaryDirectory(prefix="slotline-diagnose-corrupt-") as runtime:
        store = CacheStore(Path(runtime) / "cache")
        key = "a" * 64
        data = canonical_json_bytes(
            {
                "key": key,
                "entry_checksum": "not-the-checksum",
                "payload": {},
            }
        )
        store.path_for(key).write_bytes(data)
        try:
            store.get_entry(key)
        except BusinessError as exc:
            codes[exc.code] = exc.to_dict()

    print(_json({"reproduced_codes": sorted(codes), "errors": codes}))
    expected = {
        "E_SNAPSHOT_ROLLBACK",
        "E_STAGE_BUSY",
        "E_OPERATION_ORDER",
        "E_LINEAGE_BROKEN",
        "E_CACHE_CORRUPT",
    }
    return 0 if expected.issubset(codes) else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="slotline",
        description="Deterministic multi-turn slot state diagnostics",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    build = subparsers.add_parser("build", help="generate the seed fixture and build state")
    build.add_argument("--seed", type=int, default=42)
    build.add_argument("--out", default="data/session_seed42.json")
    build.add_argument("--runtime", default=".runtime")
    build.set_defaults(func=command_build)

    demo = subparsers.add_parser("demo", help="run retract/append equivalence demo")
    demo.add_argument("--seed", type=int, default=42)
    demo.set_defaults(func=command_demo)

    serve = subparsers.add_parser("serve", help="start the local interactive dashboard")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8765)
    serve.add_argument("--seed", type=int, default=42)
    serve.add_argument("--runtime", default=".runtime/web")
    serve.set_defaults(func=command_serve)

    trace = subparsers.add_parser("trace", help="replay one slot lineage")
    trace.add_argument("--slot", default="budget")
    trace.add_argument("--seed", type=int, default=42)
    trace.set_defaults(func=command_trace)

    diagnose = subparsers.add_parser(
        "diagnose",
        help="reproduce snapshot, concurrency, lineage and order business codes",
    )
    diagnose.add_argument("--seed", type=int, default=42)
    diagnose.set_defaults(func=command_diagnose)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
