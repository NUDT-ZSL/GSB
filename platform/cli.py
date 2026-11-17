"""Platform CLI: upgrade / run / verify / snapshot / inject."""
import argparse
import json
import os
import socket
import subprocess
import sys
import time

from . import manifest as mf
from .distribute import Distributor
from .errors import E_FORMAT, E_PORT, E_VERSION, PlatformError, StructuredLogger
from .plan import compile_plan
from .snapshot import load_snapshot, make_read_only, restore_snapshot, save_snapshot
from .store import Store
from .upgrade import InjectedKill, upgrade_directory
from .verify import verify_db

DEFAULT_DB = os.path.join("work", "platform.db")
DEFAULT_LOG = os.path.join("logs", "platform.log")


def check_port_free(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", port))
        except OSError:
            raise PlatformError(E_PORT, f"loopback port {port} is already in use",
                                port=port)


def launch_node(node_id, port, extra=None):
    cmd = [sys.executable, "-m", "platform.node_sim", "--id", node_id,
           "--port", str(port)] + (extra or [])
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True)
    deadline = time.time() + 5
    while time.time() < deadline:
        line = proc.stdout.readline()
        if line.startswith("READY"):
            return proc
        if proc.poll() is not None:
            break
    proc.kill()
    raise PlatformError(E_PORT, f"node {node_id} failed to start on {port}",
                        port=port)


def stop_nodes(procs):
    for proc in procs:
        proc.terminate()
    for proc in procs:
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()


def cmd_upgrade(args, logger):
    try:
        report = upgrade_directory(args.dir, logger=logger,
                                   fail_after=args.simulate_kill_after)
    except InjectedKill as exc:
        logger.log("upgrade_interrupted", reason=str(exc))
        print(json.dumps({"status": "interrupted", "detail": str(exc)}))
        return 2
    print(json.dumps({"status": "ok", **report}, indent=2, sort_keys=True))
    return 0


def cmd_run(args, logger):
    manifest = mf.load(args.manifest)
    nodes = [f"node-{i+1}" for i in range(args.nodes)]
    ports = {n: args.base_port + i for i, n in enumerate(nodes)}
    for p in ports.values():
        check_port_free(p)

    store = Store(args.db)
    procs = []
    try:
        for n in nodes:
            extra = []
            if args.flaky_node == n:
                extra = ["--fail-attempts", "1"]
            procs.append(launch_node(n, ports[n], extra))
        plan = compile_plan(manifest, nodes)
        version = max((e["version"] for e in manifest["entries"]), default=0)

        sid = store.create_session(version)
        store.transition(sid, "distributing")
        store.record_plan(sid, plan)
        store.set_meta(f"manifest_v{version}", mf.dumps(manifest))
        try:
            receipts = Distributor(store, logger).run(sid, plan, ports,
                                                      manifest["policy"])
        except PlatformError as exc:
            store.fail_session(sid)
            logger.log_error(exc)
            print(json.dumps(exc.to_dict(), indent=2, sort_keys=True))
            return 1
        result = store.commit(sid)
        logger.log("session_committed", session=sid, version=version)
        summary = {"status": "committed", "session": sid, "version": version,
                   "commit": result,
                   "nodes": {n: {"version": receipts[n]["version"],
                                 "seq": receipts[n]["seq"],
                                 "attempt": receipts[n]["attempt"]}
                             for n in sorted(receipts)}}
        print(json.dumps(summary, indent=2, sort_keys=True))
        return 0
    finally:
        stop_nodes(procs)
        store.close()


def cmd_verify(args, logger):
    try:
        summary = verify_db(args.db)
    except PlatformError as exc:
        logger.log_error(exc)
        print(json.dumps(exc.to_dict(), indent=2, sort_keys=True))
        return 1
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0 if summary["status"] == "ok" else 1


def cmd_snapshot_save(args, logger):
    manifest = mf.load(args.manifest)
    store = Store(args.db)
    try:
        snap = save_snapshot(store, manifest, args.out)
    finally:
        store.close()
    logger.log("snapshot_saved", path=args.out,
               manifest_version=snap["manifest_version"])
    print(json.dumps({"status": "ok", "path": args.out,
                      "manifest_version": snap["manifest_version"],
                      "committed_version": snap["committed_version"]}))
    return 0


def cmd_snapshot_restore(args, logger):
    snap = load_snapshot(args.snapshot)
    store = Store(args.db)
    try:
        result = restore_snapshot(store, snap, args.current_manifest_version,
                                  logger=logger)
    except PlatformError as exc:
        store.close()
        logger.log_error(exc)
        print(json.dumps(exc.to_dict(), indent=2, sort_keys=True))
        return 1
    store.close()
    make_read_only(args.snapshot)  # old snapshot kept read-only
    print(json.dumps({"status": "ok", **{k: v for k, v in result.items()
                                         if k != "manifest"}}))
    return 0


def cmd_inject(args, logger):
    """Fault injection: only alters input files / parameters, never source."""
    kind = args.kind
    try:
        if kind == "corrupt-manifest":
            mf.load(args.file)  # raises E_FORMAT on the corrupted input
        elif kind == "port-busy":
            blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            blocker.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            blocker.bind(("127.0.0.1", args.port))
            blocker.listen(1)
            try:
                check_port_free(args.port)  # raises E_PORT
            finally:
                blocker.close()
        elif kind == "snapshot-conflict":
            snap = load_snapshot(args.file)
            store = Store(args.db)
            try:
                restore_snapshot(store, snap, args.current_manifest_version,
                                 logger=logger)
            finally:
                store.close()
    except PlatformError as exc:
        logger.log_error(exc, event=f"inject_{kind}")
        print(json.dumps({"injected": kind, **exc.to_dict()}, indent=2,
                         sort_keys=True))
        return 1
    print(json.dumps({"injected": kind, "status": "no error raised"}))
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(prog="platform")
    parser.add_argument("--db", default=DEFAULT_DB)
    parser.add_argument("--log", default=DEFAULT_LOG)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("upgrade")
    p.add_argument("--dir", required=True)
    p.add_argument("--simulate-kill-after", type=int, default=None)

    p = sub.add_parser("run")
    p.add_argument("--manifest", required=True)
    p.add_argument("--nodes", type=int, default=3)
    p.add_argument("--base-port", type=int, default=19500)
    p.add_argument("--flaky-node", default=None)

    p = sub.add_parser("verify")

    p = sub.add_parser("snapshot-save")
    p.add_argument("--manifest", required=True)
    p.add_argument("--out", required=True)

    p = sub.add_parser("snapshot-restore")
    p.add_argument("--snapshot", required=True)
    p.add_argument("--current-manifest-version", type=int, required=True)

    p = sub.add_parser("inject")
    p.add_argument("kind", choices=["corrupt-manifest", "port-busy",
                                    "snapshot-conflict"])
    p.add_argument("--file", default=None)
    p.add_argument("--port", type=int, default=19500)
    p.add_argument("--current-manifest-version", type=int, default=3)

    args = parser.parse_args(argv)
    logger = StructuredLogger(args.log)
    handlers = {"upgrade": cmd_upgrade, "run": cmd_run, "verify": cmd_verify,
                "snapshot-save": cmd_snapshot_save,
                "snapshot-restore": cmd_snapshot_restore, "inject": cmd_inject}
    try:
        return handlers[args.cmd](args, logger)
    except PlatformError as exc:
        logger.log_error(exc)
        print(json.dumps(exc.to_dict(), indent=2, sort_keys=True))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
