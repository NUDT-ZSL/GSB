from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from .engine import apply_merge, run_eviction_round
from .errors import Code, KvError
from .recovery import recover_workspace
from .reports import build_status, verify_workspace
from .seed import LOGICAL_CLOCK_MS, generate_samples
from .storage import Database
from .util import stable_json_dump


DEFAULT_WORKSPACE = "run"


def _print(payload: Dict[str, Any]) -> None:
    sys.stdout.write(stable_json_dump(payload))


def _add_workspace(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--workspace", default=argparse.SUPPRESS, help="state directory (default: run)")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="kvswap", description="KV-cache tiering ledger and recovery engine")
    parser.add_argument("--workspace", default=DEFAULT_WORKSPACE, help="state directory (default: run)")
    sub = parser.add_subparsers(dest="command", required=True)

    p_seed = sub.add_parser("generate-samples", help="generate deterministic local inputs")
    p_seed.add_argument("--out", default="samples")

    p_load = sub.add_parser("load", help="validate and load requests and memory config")
    _add_workspace(p_load)
    p_load.add_argument("--requests", required=True)
    p_load.add_argument("--config", required=True)
    p_load.add_argument("--clock-ms", type=int, default=LOGICAL_CLOCK_MS)

    p_merge = sub.add_parser("merge", help="validate, merge and atomically activate policy package")
    _add_workspace(p_merge)
    p_merge.add_argument("--package", required=True)
    p_merge.add_argument("--clock-ms", type=int, default=LOGICAL_CLOCK_MS)
    p_merge.add_argument("--sigkill-after-stage", action="store_true", help=argparse.SUPPRESS)

    p_evict = sub.add_parser("evict", help="advance tokens and run one budget/concurrency round")
    _add_workspace(p_evict)
    p_evict.add_argument("--round-id", required=True)
    p_evict.add_argument("--clock-ms", type=int, default=LOGICAL_CLOCK_MS + 600_000)
    p_evict.add_argument("--seq-id", action="append", dest="seq_ids")
    p_evict.add_argument("--shard-count", type=int)
    p_evict.add_argument("--shard-index", type=int)
    p_evict.add_argument("--sigkill-after-claims", type=int, default=None, help=argparse.SUPPRESS)
    p_evict.add_argument("--fail-entry-id", default=None, help=argparse.SUPPRESS)

    p_resume = sub.add_parser("resume", help="recover committed and half-committed evictions")
    _add_workspace(p_resume)
    p_resume.add_argument("--clock-ms", type=int, default=LOGICAL_CLOCK_MS + 601_000)

    p_status = sub.add_parser("status", help="stable sorted JSON status")
    _add_workspace(p_status)

    p_verify = sub.add_parser("verify", help="verify ledger, capacity and NVMe frame invariants")
    _add_workspace(p_verify)

    p_seed_alias = sub.add_parser("seed", help=argparse.SUPPRESS)
    p_seed_alias.add_argument("--out", default="samples")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command in {"generate-samples", "seed"}:
        paths = generate_samples(args.out)
        _print({"ok": True, "command": "generate-samples", "paths": paths})
        return 0

    try:
        with Database(args.workspace) as db:
            if args.command == "load":
                result = _load(db, args)
            elif args.command == "merge":
                result = apply_merge(
                    db,
                    args.package,
                    args.clock_ms,
                    sigkill_after_stage=bool(args.sigkill_after_stage),
                )
            elif args.command == "evict":
                result = run_eviction_round(
                    db,
                    args.round_id,
                    args.clock_ms,
                    seq_ids=args.seq_ids,
                    shard_count=args.shard_count,
                    shard_index=args.shard_index,
                    sigkill_after_claims=args.sigkill_after_claims,
                    fail_entry_id=args.fail_entry_id,
                )
            elif args.command == "resume":
                result = recover_workspace(db, args.clock_ms)
            elif args.command == "status":
                result = build_status(db)
            elif args.command == "verify":
                result = verify_workspace(db)
                _print(result)
                return 0 if result["ok"] else 3
            else:  # pragma: no cover - argparse enforces command
                raise KvError(Code.CLI_USAGE, f"unknown command {args.command}")
        _print(result)
        return 0
    except KvError as exc:
        _print(exc.to_dict())
        return exc.exit_code
    except KeyboardInterrupt:
        _print({"ok": False, "error": {"code": "INTERRUPTED", "message": "keyboard interrupt"}})
        return 130
    except Exception as exc:
        _print(
            {
                "ok": False,
                "error": {
                    "code": Code.INTERNAL,
                    "message": f"{type(exc).__name__}: {exc}",
                },
            }
        )
        return 1


def _load(db: Database, args: argparse.Namespace) -> Dict[str, Any]:
    from .engine import load_inputs

    return load_inputs(db, args.requests, args.config, args.clock_ms)


if __name__ == "__main__":
    raise SystemExit(main())
