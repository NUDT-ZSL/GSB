"""Command line entry points with integer exit codes and JSON E_xxx output."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any, Sequence

from .errors import MTStreamError
from .models import DecodeConfig, TranslationCase
from .runtime_checks import process_pool_and_tempdir_check
from .sample_data import (
    make_sample_bundle,
    make_sample_case,
    modification_token_for,
    unaligned_source_indices,
)
from .selfcheck import run_self_check
from .server import serve
from .session import StreamingSession


def _json_print(payload: Any, *, stream: Any = None) -> None:
    destination = stream or sys.stdout
    destination.write(
        json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    )
    destination.flush()


def _load_case(path: str | None, seed: int) -> TranslationCase:
    if path is None:
        return make_sample_case(seed)
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if "case" in raw:
        raw = raw["case"]
    return TranslationCase.from_dict(raw)


def _decode_command(args: argparse.Namespace) -> dict[str, Any]:
    case = _load_case(args.case, args.seed)
    config = DecodeConfig(
        block_size=args.block_size,
        context_window=args.context_window,
        latency_budget_ms=args.latency_budget_ms,
    )
    session = StreamingSession(case, config)
    blocks = [
        session.request_block(index)
        for index in range(len(session.plan.boundaries))
    ]
    full = session.full_decode()
    chunk_tokens = tuple(token for block in blocks for token in block.tokens)
    chunk_confidences = tuple(
        confidence for block in blocks for confidence in block.confidences
    )
    chunk_projections = tuple(
        projection for block in blocks for projection in block.projections
    )
    return {
        "ok": True,
        "case_id": case.case_id,
        "plan": session.plan.to_dict(),
        "blocks": [block.to_dict() for block in blocks],
        "chunk": {
            "tokens": list(chunk_tokens),
            "confidences": list(chunk_confidences),
            "projections": [list(value) for value in chunk_projections],
        },
        "whole": full.to_dict(),
        "consistency": {
            "tokens_equal": chunk_tokens == full.tokens,
            "confidences_equal": chunk_confidences == full.confidences,
            "projections_equal": chunk_projections == full.projections,
        },
        "timings": {
            "first_chunk_observed_ns": (
                blocks[0].timing.observed_ns if blocks else None
            ),
            "whole_decode_observed_ns": full.timing.observed_ns,
            "paths_separate": True,
        },
        "prefix_cache_entries": len(session.cache_entries()),
    }


def _selfcheck_command(args: argparse.Namespace) -> dict[str, Any]:
    case = _load_case(args.case, args.seed)
    config = DecodeConfig(
        block_size=args.block_size,
        latency_budget_ms=args.latency_budget_ms,
    )
    report = run_self_check(case, config, raise_on_failure=False)
    if args.out:
        path = Path(args.out)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
            + "\n",
            encoding="utf-8",
        )
    return report


def _invalidate_command(args: argparse.Namespace) -> dict[str, Any]:
    case = _load_case(args.case, args.seed)
    config = DecodeConfig(
        block_size=args.block_size,
        latency_budget_ms=args.latency_budget_ms,
    )
    session = StreamingSession(case, config)
    for index in range(len(session.plan.boundaries)):
        session.request_block(index)
    if args.mode == "unaligned":
        positions = unaligned_source_indices(case)
        if not positions:
            raise MTStreamError(
                "E_CONFIG_INVALID",
                "sample contains no unaligned source position",
                field="source_tokens",
            )
        position = args.position if args.position is not None else positions[0]
        changed = case.with_source_token(
            position, modification_token_for(case, position)
        )
        new_config = config
    elif args.mode == "aligned":
        position = (
            args.position
            if args.position is not None
            else min(case.aligned_source_indices())
        )
        changed = case.with_source_token(
            position, modification_token_for(case, position)
        )
        new_config = config
    elif args.mode == "align_version":
        changed = case.with_align_version(case.align_version + 1)
        new_config = config
    elif args.mode == "block_size":
        changed = case
        new_config = replace(
            config, block_size=args.new_block_size or config.block_size + 1
        )
    else:  # pragma: no cover - argparse restricts values
        raise MTStreamError(
            "E_CONFIG_INVALID", "unknown invalidation mode", field="mode"
        )
    report = session.analyze_invalidation(changed, new_config)
    return {
        "ok": True,
        "mode": args.mode,
        "report": report.to_dict(),
    }


def _generate_command(args: argparse.Namespace) -> dict[str, Any]:
    bundle = make_sample_bundle(args.seed)
    path = Path(args.out)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(bundle, ensure_ascii=False, indent=2, sort_keys=True)
        + "\n",
        encoding="utf-8",
    )
    return {
        "ok": True,
        "output": str(path),
        "case_id": bundle["case"]["case_id"],
        "seed": args.seed,
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mtstream",
        description="Deterministic streaming machine-translation diagnostics",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    decode = subparsers.add_parser("decode", help="decode all chunks and whole")
    decode.add_argument("--case")
    decode.add_argument("--seed", type=int, default=20261120)
    decode.add_argument("--block-size", type=int, default=4)
    decode.add_argument("--context-window", type=int, default=3)
    decode.add_argument("--latency-budget-ms", type=float, default=250.0)
    decode.set_defaults(func=_decode_command)

    selfcheck = subparsers.add_parser("selfcheck", help="run cross-module checks")
    selfcheck.add_argument("--case")
    selfcheck.add_argument("--seed", type=int, default=20261120)
    selfcheck.add_argument("--block-size", type=int, default=4)
    selfcheck.add_argument("--latency-budget-ms", type=float, default=1000.0)
    selfcheck.add_argument("--out", default="artifacts/selfcheck.json")
    selfcheck.set_defaults(func=_selfcheck_command)

    invalidate = subparsers.add_parser(
        "invalidate", help="analyze dependency invalidation"
    )
    invalidate.add_argument("--case")
    invalidate.add_argument("--seed", type=int, default=20261120)
    invalidate.add_argument("--block-size", type=int, default=4)
    invalidate.add_argument("--latency-budget-ms", type=float, default=1000.0)
    invalidate.add_argument(
        "--mode",
        choices=("unaligned", "aligned", "align_version", "block_size"),
        default="unaligned",
    )
    invalidate.add_argument("--position", type=int)
    invalidate.add_argument("--new-block-size", type=int)
    invalidate.set_defaults(func=_invalidate_command)

    generate = subparsers.add_parser("generate", help="write deterministic sample")
    generate.add_argument("--seed", type=int, default=20261120)
    generate.add_argument("--out", default="data/sample_bundle.json")
    generate.set_defaults(func=_generate_command)

    pool_check = subparsers.add_parser(
        "pool-check", help="verify tempdir and process pool availability"
    )
    pool_check.set_defaults(
        func=lambda _args: process_pool_and_tempdir_check()
    )

    server = subparsers.add_parser("serve", help="start local workbench")
    server.add_argument("--host", default="127.0.0.1")
    server.add_argument("--port", type=int, default=8765)
    server.add_argument("--seed", type=int, default=20261120)
    server.set_defaults(func=_serve_command)
    return parser


def _serve_command(args: argparse.Namespace) -> dict[str, Any]:
    serve(args.host, args.port)
    return {"ok": True}


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        result = args.func(args)
        _json_print(result)
        return 0
    except MTStreamError as error:
        _json_print(error.envelope(), stream=sys.stderr)
        return int(error.exit_code)
    except KeyboardInterrupt:
        return 130
    except Exception as error:  # pragma: no cover - CLI defensive edge
        wrapped = MTStreamError(
            "E_CONFIG_INVALID",
            "unexpected command failure",
            field="command",
            data={"reason": str(error)},
        )
        _json_print(wrapped.envelope(), stream=sys.stderr)
        return int(wrapped.exit_code)


if __name__ == "__main__":
    raise SystemExit(main())

