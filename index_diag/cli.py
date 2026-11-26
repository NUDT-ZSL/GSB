"""Command-line and interactive entry points."""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from .compression import CompressionParams, encode_integer_stream, decode_integer_stream
from .config import ConfigLoader, downgrade_v2_to_v1, upgrade_v1_to_v2
from .engine import IndexEngine, InjectedFault
from .errors import DiagnosticError
from .samples import generate_samples


def _dump(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def _error_payload(error: DiagnosticError) -> dict[str, Any]:
    return {"ok": False, **error.to_dict()}


def _config_command(args: argparse.Namespace) -> dict[str, Any]:
    if args.config_action == "inspect":
        loaded = ConfigLoader.load(args.config)
        return {
            "ok": True,
            "config": loaded.config.to_dict(),
            "source_kind": loaded.source_kind,
            "source_path": loaded.source_path,
        }
    if args.config_action == "migrate":
        if args.to_version == 2:
            result = upgrade_v1_to_v2(
                args.source,
                args.target,
                shard_count=args.shard_count,
            )
        else:
            result = downgrade_v2_to_v1(args.source, args.target)
        return {"ok": True, **result.to_dict()}
    raise DiagnosticError("E_VERSION_UNSUPPORTED", "unknown config action")


def _run_command(args: argparse.Namespace) -> dict[str, Any]:
    engine = IndexEngine(args.work, config_path=args.config)
    batches = args.batch or None
    return engine.run(
        batches,
        fault_path=getattr(args, "fault", None),
        recovery_path=args.recovery,
    )


def _replace_command(args: argparse.Namespace) -> dict[str, Any]:
    engine = IndexEngine(args.work, config_path=args.config)
    return engine.replace_batch(
        args.batch_no,
        args.batch_file,
        fault_path=args.fault,
        recovery_path=args.recovery,
    )


def _query_command(args: argparse.Namespace) -> dict[str, Any]:
    engine = IndexEngine(args.work, config_path=args.config)
    result = engine.query_term(args.term)
    return {"ok": True, **result}


def _block_command(args: argparse.Namespace) -> dict[str, Any]:
    engine = IndexEngine(args.work, config_path=args.config)
    result = engine.query_block(args.term, args.index)
    return {"ok": True, **result}


def _status_command(args: argparse.Namespace) -> dict[str, Any]:
    engine = IndexEngine(args.work, config_path=args.config)
    return {"ok": True, **engine.status()}


def _selfcheck_command(args: argparse.Namespace) -> dict[str, Any]:
    engine = IndexEngine(args.work, config_path=args.config)
    return {"ok": True, **engine.self_check()}


def _last_code_command(args: argparse.Namespace) -> dict[str, Any]:
    engine = IndexEngine(args.work, config_path=args.config)
    return {"ok": True, "last_code": engine.last_business_code()}


def _shell(args: argparse.Namespace) -> dict[str, Any]:
    engine = IndexEngine(args.work, config_path=args.config)
    print("index-diag shell; type help for commands")
    while True:
        try:
            line = input("index-diag> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not line:
            continue
        parts = line.split()
        command = parts[0].lower()
        try:
            if command in {"quit", "exit"}:
                break
            if command == "help":
                print(
                    "postings <term> | blocks <term> | block <term> <index> | "
                    "status | selfcheck | last-code | quit"
                )
            elif command == "postings" and len(parts) == 2:
                _dump(engine.query_term(parts[1]))
            elif command == "blocks" and len(parts) == 2:
                _dump({"term": parts[1], "blocks": engine.query_term(parts[1])["blocks"]})
            elif command == "block" and len(parts) == 3:
                _dump(engine.query_block(parts[1], int(parts[2])))
            elif command == "status":
                _dump(engine.status())
            elif command == "selfcheck":
                _dump(engine.self_check())
            elif command in {"last-code", "last_code"}:
                _dump({"last_code": engine.last_business_code()})
            else:
                print("unknown or malformed command; type help")
        except DiagnosticError as error:
            _dump(_error_payload(error))
        except (ValueError, IndexError) as error:
            print(f"input error: {error}")
    return {"ok": True, "shell": "closed"}


def _generate_command(args: argparse.Namespace) -> dict[str, Any]:
    manifest = generate_samples(args.output, seed=args.seed)
    return {"ok": True, "manifest": manifest}


def _codec_command(args: argparse.Namespace) -> dict[str, Any]:
    params = CompressionParams(
        chunk_size=args.chunk_size,
        compression=args.mode,
        delta_step=args.delta_step,
    )
    values = [int(item) for item in args.values]
    payload = encode_integer_stream(values, params)
    decoded = decode_integer_stream(payload, params, expected_length=len(values))
    if decoded != values:
        raise DiagnosticError("E_COMPRESS_PARAM", "codec round-trip failed")
    return {
        "ok": True,
        "decoded": decoded,
        "encoded_hex": payload.hex(),
        "encoded_size": len(payload),
        "params": params.to_dict(),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="index-diag",
        description="Deterministic local inverted-index diagnostics engine",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    generate = subparsers.add_parser("generate", help="generate fixed-seed samples")
    generate.add_argument("--output", required=True)
    generate.add_argument("--seed", type=int, default=20261127)
    generate.set_defaults(handler=_generate_command)

    run = subparsers.add_parser("run", help="run or resume the four-stage pipeline")
    run.add_argument("--work", required=True)
    run.add_argument("--config")
    run.add_argument("--batch", action="append")
    run.add_argument("--fault")
    run.add_argument("--recovery")
    run.set_defaults(handler=_run_command)

    recover = subparsers.add_parser("recover", help="resume from latest committed checkpoint")
    recover.add_argument("--work", required=True)
    recover.add_argument("--config")
    recover.add_argument("--batch", action="append")
    recover.add_argument("--recovery", required=True)
    recover.set_defaults(handler=_run_command)

    query = subparsers.add_parser("query", help="query one term's postings and blocks")
    query.add_argument("--work", required=True)
    query.add_argument("--config")
    query.add_argument("--term", required=True)
    query.set_defaults(handler=_query_command)

    block = subparsers.add_parser("block", help="decode one compressed block")
    block.add_argument("--work", required=True)
    block.add_argument("--config")
    block.add_argument("--term", required=True)
    block.add_argument("--index", type=int, required=True)
    block.set_defaults(handler=_block_command)

    status = subparsers.add_parser("status", help="print checkpoint, cache, and ledger state")
    status.add_argument("--work", required=True)
    status.add_argument("--config")
    status.set_defaults(handler=_status_command)

    selfcheck = subparsers.add_parser("selfcheck", help="run cross-module consistency checks")
    selfcheck.add_argument("--work", required=True)
    selfcheck.add_argument("--config")
    selfcheck.set_defaults(handler=_selfcheck_command)

    last_code = subparsers.add_parser("last-code", help="show the most recent business code")
    last_code.add_argument("--work", required=True)
    last_code.add_argument("--config")
    last_code.set_defaults(handler=_last_code_command)

    replace = subparsers.add_parser("replace", help="atomically replace one batch")
    replace.add_argument("--work", required=True)
    replace.add_argument("--config")
    replace.add_argument("--batch-no", type=int, required=True)
    replace.add_argument("--batch-file", required=True)
    replace.add_argument("--fault")
    replace.add_argument("--recovery")
    replace.set_defaults(handler=_replace_command)

    shell = subparsers.add_parser("shell", help="interactive local query shell")
    shell.add_argument("--work", required=True)
    shell.add_argument("--config")
    shell.set_defaults(handler=_shell)

    codec = subparsers.add_parser("codec", help="exercise a compression codec round-trip")
    codec.add_argument("--mode", default="delta_zlib")
    codec.add_argument("--chunk-size", type=int, default=4)
    codec.add_argument("--delta-step", type=int, default=2)
    codec.add_argument("--values", nargs="+", required=True)
    codec.set_defaults(handler=_codec_command)

    config = subparsers.add_parser("config", help="inspect or migrate v1/v2 configuration")
    config_subparsers = config.add_subparsers(dest="config_action", required=True)
    inspect = config_subparsers.add_parser("inspect")
    inspect.add_argument("--config", required=True)
    inspect.set_defaults(handler=_config_command)
    migrate = config_subparsers.add_parser("migrate")
    migrate.add_argument("--source", required=True)
    migrate.add_argument("--target", required=True)
    migrate.add_argument("--to-version", type=int, choices=(1, 2), required=True)
    migrate.add_argument("--shard-count", type=int, default=2)
    migrate.set_defaults(handler=_config_command)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        response = args.handler(args)
        _dump(response)
        return 0
    except DiagnosticError as error:
        _dump(_error_payload(error))
        return 2
    except InjectedFault as fault:
        _dump(
            {
                "ok": False,
                "code": fault.code,
                "message": str(fault),
                "details": {"phase": fault.phase, "when": fault.when},
            }
        )
        return 3
    except (OSError, ValueError) as error:
        print(json.dumps({"ok": False, "code": "E_IO", "message": str(error)}))
        return 2


if __name__ == "__main__":
    sys.exit(main())
