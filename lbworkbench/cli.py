from __future__ import annotations

import argparse
import sys
import threading
from pathlib import Path
from typing import Any

from .api import create_app
from .db import WorkbenchDB
from .engine import resume_checks, run_check
from .errors import WorkbenchError
from .probes import ProbeHTTPServer
from .store import (
    get_merge_view,
    get_status,
    list_rounds,
    list_snapshots,
    load_package,
    verify_state,
)
from .util import pretty_json


def _print(value: Any) -> None:
    sys.stdout.write(pretty_json(value))
    sys.stdout.flush()


def _workdir(value: str) -> WorkbenchDB:
    db = WorkbenchDB(value)
    db.initialize()
    return db


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="lbworkbench",
        description="Cross-version load-balancer state and drift diagnostics",
    )
    parser.add_argument(
        "--workdir",
        default="work",
        help="SQLite state directory (default: ./work)",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    load = subparsers.add_parser("load", help="validate and atomically load a snapshot package")
    load.add_argument("--package", required=True, help="combined package JSON path")

    check = subparsers.add_parser("check", help="run one or all planned check rounds")
    check.add_argument("--plan", required=True, help="check plan JSON path")
    check.add_argument("--probe-base-url", default=None, help="loopback fixture base URL")
    check.add_argument("--round", type=int, default=None, dest="round_index")
    check.add_argument(
        "--instance",
        action="append",
        default=None,
        help="probe only this instance (repeatable; round finalizes after all peers)",
    )

    resume = subparsers.add_parser("resume", help="replay all incomplete check rounds")
    resume.add_argument("--probe-base-url", default=None, help="loopback fixture base URL")

    subparsers.add_parser("merge", help="print the persisted unified merge view")
    subparsers.add_parser("status", help="print stable status JSON")
    subparsers.add_parser("verify", help="verify persisted state against snapshot metadata")
    subparsers.add_parser("snapshots", help="list retained snapshot versions")
    subparsers.add_parser("rounds", help="list check rounds and missing observations")

    serve = subparsers.add_parser("serve", help="run the Flask diagnostic UI and API")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8765)

    probe = subparsers.add_parser("probe-server", help="run the local deterministic HTTP fixture")
    probe.add_argument(
        "--ready-file",
        default=None,
        help="write the selected random base URL to this file once listening",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "serve":
            app = create_app(args.workdir)
            app.run(host=args.host, port=args.port, debug=False, threaded=True)
            return 0
        if args.command == "probe-server":
            server = ProbeHTTPServer().start()
            ready = {
                "ok": True,
                "base_url": server.base_url,
                "port": server.port,
            }
            if args.ready_file:
                Path(args.ready_file).write_text(
                    pretty_json(ready), encoding="utf-8"
                )
            _print({"schema": "lb-probe-server/v1", **ready})
            try:
                threading.Event().wait()
            except KeyboardInterrupt:
                pass
            finally:
                server.stop()
            return 0

        if args.command == "load":
            db = WorkbenchDB(args.workdir)
            _print(load_package(db, args.package))
            return 0
        db = _workdir(args.workdir)
        if args.command == "check":
            result = run_check(
                db,
                plan_path=args.plan,
                probe_base_url=args.probe_base_url,
                round_index=args.round_index,
                instances=args.instance,
            )
            _print(result)
            return 0 if result["ok"] else 4
        if args.command == "resume":
            result = resume_checks(db, probe_base_url=args.probe_base_url)
            _print(result)
            return 0 if result["ok"] else 4
        if args.command == "merge":
            _print(get_merge_view(db))
            return 0
        if args.command == "status":
            _print(get_status(db))
            return 0
        if args.command == "verify":
            result = verify_state(db)
            _print(result)
            return 0 if result["ok"] else 5
        if args.command == "snapshots":
            _print(list_snapshots(db))
            return 0
        if args.command == "rounds":
            _print(list_rounds(db))
            return 0
        raise WorkbenchError(
            "E_BAD_COMMAND", "unknown command", {"command": args.command}
        )
    except WorkbenchError as exc:
        _print(exc.to_dict())
        return exc.exit_code
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
