from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

from .engine import Autoscaler
from .errors import BusinessError
from .loader import load_bundle
from .models import canonical_json
from .state import get_spec
from .storage import SQLiteStore
from .stub import StubServer, load_scenario

DEFAULT_SERVICES = "samples/services.json"
DEFAULT_NODES = "samples/nodes.json"
DEFAULT_TRACE = "samples/trace.json"
DEFAULT_CONFIG = "samples/config.json"
DEFAULT_DB = "data/scaler.sqlite"


ERROR_EXIT_CODES = {
    "E_INPUT_INVALID": 2,
    "E_DUPLICATE_SERVICE": 2,
    "E_DUPLICATE_NODE": 2,
    "E_DUPLICATE_WINDOW_TIME": 2,
    "E_SERVICE_UNKNOWN": 2,
    "E_CLOCK_REGRESSION": 3,
    "E_NO_OBSERVATION": 3,
    "E_CONFIG_VERSION_REGRESSION": 3,
    "E_CONFIG_VERSION_CONFLICT": 3,
    "E_UNSAFE_SPEC_CHANGE": 3,
    "E_REQUEST_CONFLICT": 3,
    "E_REQUEST_IN_PROGRESS": 3,
    "E_LAUNCH_CONNECTION_REFUSED": 4,
    "E_COLD_START_BUDGET_EXCEEDED": 4,
    "E_LAUNCH_HTTP_ERROR": 4,
    "E_LAUNCH_RESPONSE_INVALID": 4,
    "E_STUB_UNAVAILABLE": 4,
    "E_STATE_VERSION_CONFLICT": 5,
    "E_LEDGER_MISMATCH": 5,
    "E_STORAGE_ERROR": 5,
}


def _add_spec_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--services", default=DEFAULT_SERVICES)
    parser.add_argument("--nodes", default=DEFAULT_NODES)
    parser.add_argument("--trace", default=DEFAULT_TRACE)
    parser.add_argument("--config", default=DEFAULT_CONFIG)
    parser.add_argument("--db", default=DEFAULT_DB)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m scaler",
        description="Deterministic local inference autoscaler control plane",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init", help="validate inputs and initialize state")
    _add_spec_args(init_parser)

    run_parser = subparsers.add_parser("run", help="run deterministic scaling rounds")
    _add_spec_args(run_parser)
    run_parser.add_argument("--rounds", type=int, default=1)
    run_parser.add_argument("--start-clock-ms", type=int, default=None)
    run_parser.add_argument("--service", action="append", dest="services_filter")
    run_parser.add_argument("--request-id", default=None)
    run_parser.add_argument("--fault-round", type=int, default=None)
    run_parser.add_argument("--fault-after-dispatch", type=int, default=None)

    recover_parser = subparsers.add_parser(
        "recover", help="reconcile and finalize persisted in-flight rounds"
    )
    _add_spec_args(recover_parser)

    status_parser = subparsers.add_parser("status", help="print stable state JSON")
    _add_spec_args(status_parser)
    status_parser.add_argument("--events", type=int, default=20)

    stub_parser = subparsers.add_parser("stub", help="run the loopback launch stub")
    stub_parser.add_argument("--host", default="127.0.0.1")
    stub_parser.add_argument("--port", type=int, default=18765)
    stub_parser.add_argument("--scenario", default="samples/scenario.json")

    serve_parser = subparsers.add_parser("serve", help="run the diagnostic workbench")
    _add_spec_args(serve_parser)
    serve_parser.add_argument("--host", default="127.0.0.1")
    serve_parser.add_argument("--port", type=int, default=8080)

    demo_parser = subparsers.add_parser(
        "demo", help="start an embedded stub and run sample rounds"
    )
    _add_spec_args(demo_parser)
    demo_parser.add_argument("--rounds", type=int, default=3)
    demo_parser.add_argument("--scenario", default="samples/scenario.json")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "stub":
            scenario = load_scenario(args.scenario)
            server = StubServer(args.host, args.port, scenario)
            print(
                canonical_json(
                    {
                        "ok": True,
                        "message": "launch stub listening",
                        "url": server.url,
                    }
                ),
                flush=True,
            )
            try:
                server.httpd.serve_forever()
            except KeyboardInterrupt:
                pass
            finally:
                server.httpd.server_close()
            return 0

        if args.command in {"recover", "status"}:
            store = SQLiteStore(args.db)
            autoscaler = Autoscaler.from_store(store)

            if args.command == "recover":
                output = autoscaler.recover()
                print(canonical_json(output))
                return 0

            state, snapshot_id = store.load()
            output = autoscaler.status_dict(state)
            output["snapshot_id"] = snapshot_id
            output["events"] = store.read_events(limit=args.events)
            output["recovery_audit"] = store.read_recovery_audit()
            output["snapshot_versions"] = store.snapshot_versions()
            print(canonical_json(output))
            return 0

        spec = load_bundle(args.services, args.nodes, args.trace, args.config)
        if args.command == "run":
            if args.rounds < 1:
                raise BusinessError(
                    "E_INPUT_INVALID", "rounds must be at least 1", {"rounds": args.rounds}
                )
            if args.start_clock_ms is not None and args.start_clock_ms < 0:
                raise BusinessError(
                    "E_INPUT_INVALID",
                    "start clock must be non-negative",
                    {"start_clock_ms": args.start_clock_ms},
                )
            if (
                args.fault_after_dispatch is not None
                and args.fault_after_dispatch < 1
            ):
                raise BusinessError(
                    "E_INPUT_INVALID",
                    "fault-after-dispatch must be positive",
                    {"fault_after_dispatch": args.fault_after_dispatch},
                )
        store = SQLiteStore(args.db)
        autoscaler = Autoscaler(store, spec)

        if args.command == "init":
            state, snapshot_id = store.load()
            output = {
                "ok": True,
                "snapshot_id": snapshot_id,
                "config_version": state["config_version"],
                "spec_digest": state["spec_digest"],
                "service_count": len(get_spec(state).services),
                "node_count": len(get_spec(state).nodes),
                "ledger_conserved": True,
            }
            print(canonical_json(output))
            return 0

        if args.command == "run":
            output = autoscaler.run(
                rounds=args.rounds,
                start_clock_ms=args.start_clock_ms,
                services=args.services_filter,
                request_id=args.request_id,
                fault_round=args.fault_round,
                fault_after_dispatch=args.fault_after_dispatch,
            )
            print(canonical_json(output))
            return 0

        if args.command == "serve":
            return run_workbench(args, spec, store, autoscaler)

        if args.command == "demo":
            scenario = load_scenario(args.scenario)
            server = StubServer("127.0.0.1", 0, scenario)
            server.start()
            try:
                config = spec.to_dict()
                config["config"]["launch_target_url"] = server.url
                from .models import ScalerSpec

                demo_spec = ScalerSpec.from_dict(config)
                demo_store = SQLiteStore(args.db)
                demo_autoscaler = Autoscaler(demo_store, demo_spec)
                output = demo_autoscaler.run(rounds=args.rounds)
                output["stub_url"] = server.url
                print(canonical_json(output))
                return 0
            finally:
                server.stop()
    except BusinessError as exc:
        print(canonical_json(exc.to_dict()), flush=True)
        return ERROR_EXIT_CODES.get(exc.code, 1)
    except KeyboardInterrupt:
        return 130
    except Exception as exc:  # pragma: no cover - defensive CLI boundary
        error = BusinessError(
            "E_STORAGE_ERROR",
            f"unhandled failure: {type(exc).__name__}: {exc}",
        )
        print(canonical_json(error.to_dict()), flush=True)
        return 5
    return 0


def run_workbench(
    args: argparse.Namespace, spec: Any, store: SQLiteStore, autoscaler: Autoscaler
) -> int:
    from .workbench import create_app

    app = create_app(
        store=store,
        autoscaler=autoscaler,
        spec=spec,
        spec_paths={
            "services": str(Path(args.services)),
            "nodes": str(Path(args.nodes)),
            "trace": str(Path(args.trace)),
            "config": str(Path(args.config)),
        },
    )
    print(
        canonical_json(
            {
                "ok": True,
                "message": "workbench listening",
                "url": f"http://{args.host}:{args.port}/",
            }
        ),
        flush=True,
    )
    app.run(host=args.host, port=args.port, debug=False, use_reloader=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
