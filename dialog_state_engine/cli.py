from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Sequence

from .canonical import atomic_write_json, canonical_line
from .config import Rules
from .errors import E_FORMAT_CORRUPT, EngineError
from .formats import read_state
from .graph import DiagnosticEngine
from .migration import migrate
from .samples import DEFAULT_SEED, generate_samples
from .turns import group_turns_by_session, load_turns


def _write_output(value: Any, output: str | None) -> None:
    if output is None or output == "-":
        sys.stdout.write(canonical_line(value))
        return
    atomic_write_json(Path(output), value)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dialog-state-engine",
        description="Deterministic LLM dialogue-state diagnostic engine",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    generate = subparsers.add_parser("generate", help="generate deterministic local samples")
    generate.add_argument("--out", default="samples")
    generate.add_argument("--seed", type=int, default=DEFAULT_SEED)

    inspect = subparsers.add_parser("inspect", help="read a v1/v2/v3 state snapshot")
    inspect.add_argument("--state", required=True)
    inspect.add_argument("--config", required=True)
    inspect.add_argument("--out")

    migrate_parser = subparsers.add_parser("migrate", help="migrate a snapshot between formats")
    migrate_parser.add_argument("--source", required=True)
    migrate_parser.add_argument("--target-version", required=True, choices=("v1", "v2", "v3"))
    migrate_parser.add_argument("--out", required=True)
    migrate_parser.add_argument("--config", required=True)
    migrate_parser.add_argument("--overwrite", action="store_true")
    migrate_parser.add_argument("--strict-audit-drops", action="store_true")
    migrate_parser.add_argument("--report")

    run = subparsers.add_parser("run", help="execute the diagnostic DAG for JSONL turns")
    run.add_argument("--turns", required=True)
    run.add_argument("--config", required=True)
    run.add_argument("--cache-dir", required=True)
    run.add_argument("--session")
    run.add_argument("--out")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "generate":
            result = generate_samples(args.out, seed=args.seed)
            _write_output(result, "-")
            return 0
        if args.command == "inspect":
            rules = Rules.from_file(args.config)
            result = read_state(args.state, rules)
            _write_output(result.to_dict(), args.out)
            return 0
        if args.command == "migrate":
            rules = Rules.from_file(args.config)
            result = migrate(
                args.source,
                args.out,
                target_version=args.target_version,
                rules=rules,
                overwrite=args.overwrite,
                strict_audit_drops=args.strict_audit_drops,
            )
            _write_output(result.to_dict(), args.report)
            return 0
        if args.command == "run":
            rules = Rules.from_file(args.config)
            turns = load_turns(args.turns, rules)
            grouped = group_turns_by_session(turns)
            if args.session is not None:
                if args.session not in grouped:
                    raise EngineError(
                        E_FORMAT_CORRUPT,
                        "requested session is absent from the turns file",
                        field="session_id",
                        details={"session_id": args.session},
                    )
                grouped = {args.session: grouped[args.session]}
            engine = DiagnosticEngine(rules, args.cache_dir)
            reports = [
                engine.execute(session_id, session_turns).to_dict()
                for session_id, session_turns in grouped.items()
            ]
            result = {
                "ok": True,
                "config_version": rules.config_version,
                "config_digest": rules.digest,
                "reports": reports,
            }
            _write_output(result, args.out)
            return 0
    except EngineError as exc:
        sys.stderr.write(canonical_line(exc.to_dict()))
        return 2
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        failure = {
            "ok": False,
            "error": {
                "code": E_FORMAT_CORRUPT,
                "message": str(exc),
            },
        }
        sys.stderr.write(canonical_line(failure))
        return 2
    return 1

