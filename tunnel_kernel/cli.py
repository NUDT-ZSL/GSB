"""Command-line interface for the local tunnel kernel."""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
from pathlib import Path
from typing import Any

from .config import (
    ConfigBundle,
    canonical_json,
    choose_peer,
    load_config,
    synthetic_payload,
    validate_payload_against_peer,
)
from .engine import ForwardEngine
from .errors import (
    E_ABORTED,
    E_ECHO_MISMATCH,
    E_IO,
    E_NOT_FOUND,
    E_STATE_CORRUPT,
    BusinessError,
)
from .planner import build_plan
from .store import Store
from .transport import query_echo


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_INVENTORY = ROOT / "samples" / "peers.json"
DEFAULT_LINKS = ROOT / "samples" / "links.json"
DEFAULT_POLICY = ROOT / "samples" / "policy.json"
DEFAULT_DB = ROOT / "data" / "tunnel_state.sqlite3"


def _print(value: Any) -> None:
    print(canonical_json(value), flush=True)


def _load_bundle(args: argparse.Namespace) -> ConfigBundle:
    return load_config(args.inventory, args.links, args.policy)


def _status_item(state: dict[str, Any]) -> dict[str, Any]:
    return {
        "algorithm": state["algorithm"],
        "confirmed_bytes": state["confirmed_bytes"],
        "confirmed_through": state["confirmed_through"],
        "current_mtu": state["current_mtu"],
        "expected_fragments": state["expected_fragments"],
        "failure_code": state["failure_code"],
        "inflight_ranges": state["inflight_ranges"],
        "logical_clock_ms": state["logical_clock_ms"],
        "message_id": state["message_id"],
        "mtu": state["current_mtu"],
        "peer": state["peer"],
        "pending_ranges": state["pending_ranges"],
        "reassembly_digest": state["reassembly_digest"],
        "recovery_count": state["recovery_count"],
        "retransmit_count": state["retransmit_count"],
        "status": state["status"],
        "tunnel_id": state["tunnel_id"],
    }


def _cmd_load(args: argparse.Namespace) -> int:
    bundle = _load_bundle(args)
    _print(
        {
            "code": "OK",
            "config": bundle.as_dict(),
            "peers": len(bundle.peers),
            "scenarios": len(bundle.scenarios),
        }
    )
    return 0


def _payload_from_args(args: argparse.Namespace, peer: Any) -> bytes:
    if args.payload_file is not None:
        try:
            return Path(args.payload_file).read_bytes()
        except OSError as exc:
            raise BusinessError(
                E_IO,
                f"cannot read payload file: {args.payload_file}",
            ) from exc
    size = args.payload_size
    if size is None:
        size = max(peer.payload_lengths)
    return synthetic_payload(peer.name, size, args.payload_seed)


def _cmd_tunnel(args: argparse.Namespace) -> int:
    bundle = _load_bundle(args)
    peer = choose_peer(bundle.peers, peer_name=args.peer)
    payload = _payload_from_args(args, peer)
    validate_payload_against_peer(payload, peer, bundle.policy)
    plan = build_plan(
        peer=peer,
        payload=payload,
        policy=bundle.policy,
        link=bundle.links[peer.name],
    )
    store = Store(args.db)
    store.initialize()
    state = store.prepare_tunnel(
        peer=peer,
        payload=payload,
        policy=bundle.policy,
        link=bundle.links[peer.name],
        algorithm=plan.decision.algorithm,
        mtu=plan.mtu,
        message_id=plan.message_id,
        fragments=plan.fragments,
        clock_start_ms=bundle.policy.clock_start_ms,
    )
    output = plan.as_dict()
    output["code"] = "OK"
    output["state"] = _status_item(state)
    _print(output)
    return 0


def _run_forward(args: argparse.Namespace, *, resume: bool) -> int:
    bundle = _load_bundle(args)
    store = Store(args.db)
    if not store.exists:
        raise BusinessError(E_NOT_FOUND, "tunnel database does not exist")
    state = store.get_state(args.tunnel)
    try:
        peer = bundle.peers[state["peer"]]
    except KeyError as exc:
        raise BusinessError(
            E_NOT_FOUND,
            "tunnel peer is missing from the configured inventory",
            details={"peer": state["peer"]},
        ) from exc
    scenario = None
    if args.scenario:
        try:
            scenario = bundle.scenarios[args.scenario]
        except KeyError as exc:
            raise BusinessError(
                E_NOT_FOUND,
                "scenario not found",
                details={"scenario": args.scenario},
            ) from exc
    abort_event = threading.Event()
    crash_after = os.environ.get("TUNNEL_KERNEL_CRASH_AFTER_SENDS")
    crash_after_sends = int(crash_after) if crash_after else None
    engine = ForwardEngine(
        store=store,
        peer=peer,
        link=bundle.links[peer.name],
        policy=bundle.policy,
        scenario_changes=scenario.changes if scenario else (),
        abort_event=abort_event,
        crash_after_sends=crash_after_sends,
    )
    result = engine.forward(args.tunnel, resume=resume)
    result["code"] = "OK"
    _print(result)
    return 0


def _cmd_forward(args: argparse.Namespace) -> int:
    return _run_forward(args, resume=False)


def _cmd_resume(args: argparse.Namespace) -> int:
    return _run_forward(args, resume=True)


def _cmd_status(args: argparse.Namespace) -> int:
    store = Store(args.db)
    if not store.exists:
        _print({"code": "OK", "tunnels": []})
        return 0
    if args.tunnel:
        state = store.get_state(args.tunnel)
        _print({"code": "OK", "tunnel": _status_item(state)})
        return 0
    states = store.list_states()
    _print({"code": "OK", "tunnels": [_status_item(state) for state in states]})
    return 0


def _cmd_verify(args: argparse.Namespace) -> int:
    bundle = _load_bundle(args)
    store = Store(args.db)
    local = store.verify_local(args.tunnel)
    state = store.get_state(args.tunnel)
    checks = dict(local["checks"])
    checks["echo_checked"] = not args.no_echo
    if not args.no_echo:
        peer = bundle.peers[state["peer"]]
        response = query_echo(
            peer,
            bundle.policy,
            message_id=state["message_id"],
        )
        if response is None:
            raise BusinessError(
                E_IO,
                "echo service did not answer the verification query",
                details={"message_id": state["message_id"]},
            )
        if response.get("code") != "OK":
            raise BusinessError(
                str(response.get("code", E_NOT_FOUND)),
                str(response.get("message", "echo query failed")),
                details={"response": response},
            )
        echo_digest = response.get("digest")
        checks["echo_complete"] = bool(response.get("complete"))
        checks["echo_digest_matches"] = echo_digest == state["reassembly_digest"]
        if not checks["echo_complete"] or not checks["echo_digest_matches"]:
            fail = BusinessError(
                E_ECHO_MISMATCH,
                "local reassembly summary does not match the echo service",
                details={
                    "echo_digest": echo_digest,
                    "local_digest": state["reassembly_digest"],
                    "message_id": state["message_id"],
                },
            )
            _print({"code": fail.code, "message": fail.message, "checks": checks})
            return 3
    if not all(
        value for key, value in checks.items() if key != "echo_checked"
    ):
        raise BusinessError(
            E_STATE_CORRUPT,
            "verification failed",
            details={"checks": checks},
        )
    _print(
        {
            "checks": dict(sorted(checks.items())),
            "code": "OK",
            "message_id": state["message_id"],
            "payload_digest": state["payload_digest"],
            "status": "OK",
            "tunnel_id": args.tunnel,
        }
    )
    return 0


def _add_config_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--inventory", type=Path, default=DEFAULT_INVENTORY)
    parser.add_argument("--links", type=Path, default=DEFAULT_LINKS)
    parser.add_argument("--policy", type=Path, default=DEFAULT_POLICY)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tunnel-kernel",
        description="Deterministic local tunnel forwarding kernel",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    load_parser = subparsers.add_parser(
        "load", help="validate and print all local sample documents"
    )
    _add_config_options(load_parser)
    load_parser.set_defaults(func=_cmd_load)

    tunnel_parser = subparsers.add_parser(
        "tunnel", help="prepare a deterministic tunnel plan"
    )
    _add_config_options(tunnel_parser)
    tunnel_parser.add_argument("--peer", required=True)
    tunnel_parser.add_argument("--payload-size", type=int)
    tunnel_parser.add_argument("--payload-seed", type=int, default=0)
    tunnel_parser.add_argument("--payload-file", type=Path)
    tunnel_parser.set_defaults(func=_cmd_tunnel)

    for name, function, help_text in (
        ("forward", _cmd_forward, "execute a prepared tunnel"),
        ("resume", _cmd_resume, "recover and execute a prepared/incomplete tunnel"),
    ):
        command = subparsers.add_parser(name, help=help_text)
        _add_config_options(command)
        command.add_argument("--tunnel", required=True)
        command.add_argument("--scenario")
        command.set_defaults(func=function)

    status_parser = subparsers.add_parser(
        "status", help="print stable persisted tunnel state"
    )
    _add_config_options(status_parser)
    status_parser.add_argument("--tunnel")
    status_parser.set_defaults(func=_cmd_status)

    verify_parser = subparsers.add_parser(
        "verify", help="verify persisted state and optionally the echo summary"
    )
    _add_config_options(verify_parser)
    verify_parser.add_argument("--tunnel", required=True)
    verify_parser.add_argument(
        "--no-echo",
        action="store_true",
        help="only verify local persisted state",
    )
    verify_parser.set_defaults(func=_cmd_verify)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except BusinessError as exc:
        print(
            json.dumps(exc.as_dict(), ensure_ascii=False, sort_keys=True),
            file=sys.stderr,
            flush=True,
        )
        return 2
    except KeyboardInterrupt:
        error = BusinessError(E_ABORTED, "forwarding interrupted by user")
        print(
            json.dumps(error.as_dict(), ensure_ascii=False, sort_keys=True),
            file=sys.stderr,
            flush=True,
        )
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
