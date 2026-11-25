"""Command line interface for deterministic sharded checkpoints."""

import argparse
import json
import sys
from typing import Any, Dict, Optional, Sequence

from .crash import CrashInjector
from .errors import CheckpointError, UsageError
from .jsonio import atomic_write_json, read_json
from .manager import CheckpointManager
from .model import load_plan
from .verification import validate_manifest_semantics


def _print_json(value: Any, stream: Any = None) -> None:
    target = stream or sys.stdout
    target.write(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))
    target.write("\n")
    target.flush()


def _add_plan_options(parser: argparse.ArgumentParser, required: bool = True) -> None:
    parser.add_argument(
        "--plan",
        "--training-plan",
        dest="plan",
        required=required,
        help="training plan JSON file",
    )
    parser.add_argument(
        "--strategy",
        "--shard-strategy",
        dest="strategy",
        help="optional shard strategy JSON override",
    )
    parser.add_argument(
        "--save-plan",
        "--checkpoint-manifest",
        dest="save_plan",
        help="optional checkpoint/save manifest override",
    )


def _add_crash_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--crash-manifest",
        "--crash-injection",
        dest="crash_manifest",
        help="one-shot crash injection JSON",
    )
    parser.add_argument(
        "--crash-stage",
        choices=("write", "commit"),
        help="crash stage override (SIGKILL)",
    )
    parser.add_argument(
        "--crash-after",
        type=int,
        help="for write-stage crashes, SIGKILL after this many committed writes",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ckptmgr",
        description="Deterministic sharded checkpoint manager with crash recovery",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate = subparsers.add_parser(
        "validate", help="validate plan, strategy, save manifest or checkpoint manifest"
    )
    validate.add_argument("--plan", "--training-plan", dest="plan", help="training plan JSON file")
    validate.add_argument(
        "--strategy",
        "--shard-strategy",
        dest="strategy",
        help="optional shard strategy JSON override",
    )
    validate.add_argument(
        "--save-plan",
        "--checkpoint-manifest",
        dest="save_plan",
        help="optional save manifest override",
    )
    validate.add_argument("--manifest", help="checkpoint manifest JSON to validate")

    save = subparsers.add_parser("save", help="save a checkpoint version")
    save.add_argument(
        "--root", "--workspace", dest="root", required=True, help="checkpoint workspace directory"
    )
    _add_plan_options(save)
    save.add_argument("--version", type=int, help="checkpoint version")
    save.add_argument(
        "--mode", choices=("auto", "full", "incremental"), default="auto"
    )
    save.add_argument("--base-version", type=int, help="explicit incremental base")
    save.add_argument(
        "--clock-ms",
        "--logical-clock-ms",
        dest="clock_ms",
        type=int,
        help="injected logical clock in milliseconds",
    )
    _add_crash_options(save)

    restore = subparsers.add_parser("restore", help="verify and restore a checkpoint")
    restore.add_argument(
        "--root", "--workspace", dest="root", required=True, help="checkpoint workspace directory"
    )
    restore.add_argument("--checkpoint", "--name", dest="checkpoint", help="checkpoint name")
    restore.add_argument("--version", type=int, help="checkpoint version")
    restore.add_argument(
        "--clock-ms",
        "--logical-clock-ms",
        dest="clock_ms",
        type=int,
        help="injected logical clock for regression checks",
    )
    restore.add_argument(
        "--output", help="atomically write the verified restore report to this file"
    )
    restore.add_argument(
        "--materialize",
        "--restore-dir",
        dest="materialize",
        help="atomically materialize verified layer payloads into this new directory",
    )

    resume = subparsers.add_parser(
        "resume", help="finish an interrupted or budget-exhausted round"
    )
    resume.add_argument(
        "--root", "--workspace", dest="root", required=True, help="checkpoint workspace directory"
    )
    _add_plan_options(resume, required=False)
    resume.add_argument("--checkpoint", "--name", dest="checkpoint", help="checkpoint name")
    resume.add_argument("--version", type=int, help="checkpoint version")
    resume.add_argument(
        "--clock-ms",
        "--logical-clock-ms",
        dest="clock_ms",
        type=int,
        help="injected logical clock in milliseconds",
    )
    _add_crash_options(resume)

    status = subparsers.add_parser("status", help="show ledger and round state")
    status.add_argument(
        "--root", "--workspace", dest="root", required=True, help="checkpoint workspace directory"
    )
    status.add_argument("--checkpoint", help="filter by checkpoint name")

    verify = subparsers.add_parser(
        "verify", help="regenerate and compare every live checkpoint"
    )
    verify.add_argument(
        "--root", "--workspace", dest="root", required=True, help="checkpoint workspace directory"
    )
    verify.add_argument("--checkpoint", help="filter by checkpoint name")
    return parser


def _load_crash(args: argparse.Namespace) -> CrashInjector:
    return CrashInjector.load(
        args.crash_manifest,
        getattr(args, "crash_stage", None),
        getattr(args, "crash_after", None),
    )


def _run(args: argparse.Namespace) -> Dict[str, Any]:
    if args.command == "validate":
        result: Dict[str, Any] = {"ok": True}
        if args.plan:
            plan = load_plan(args.plan, args.strategy, args.save_plan)
            result["plan"] = plan.summary()
        elif args.strategy or args.save_plan:
            raise UsageError("--strategy and --save-plan require --plan")
        if args.manifest:
            manifest = read_json(args.manifest)
            validate_manifest_semantics(manifest)
            result["manifest"] = {
                "checkpoint": manifest["checkpoint"],
                "version": manifest["version"],
                "plan_digest": manifest["plan_digest"],
                "parameter_digest": manifest["parameter_digest"],
                "layer_count": len(manifest["layers"]),
                "shard_count": len(manifest["shards"]),
            }
        if "plan" not in result and "manifest" not in result:
            raise UsageError("validate requires --plan and/or --manifest")
        return result

    if args.command == "save":
        plan = load_plan(args.plan, args.strategy, args.save_plan)
        injector = _load_crash(args)
        manager = CheckpointManager(args.root)
        return manager.save(
            plan,
            version=args.version,
            mode=args.mode,
            clock_ms=args.clock_ms,
            base_version=args.base_version,
            crash_injector=injector,
        )

    if args.command == "resume":
        if args.plan:
            plan = load_plan(args.plan, args.strategy, args.save_plan)
        else:
            if args.strategy or args.save_plan:
                raise UsageError("--strategy and --save-plan require --plan")
            plan = None
        injector = _load_crash(args)
        manager = CheckpointManager(args.root)
        return manager.resume(
            plan,
            version=args.version,
            clock_ms=args.clock_ms,
            crash_injector=injector,
            checkpoint=args.checkpoint,
        )

    if args.command == "restore":
        manager = CheckpointManager(args.root)
        result = manager.restore(
            checkpoint=args.checkpoint,
            version=args.version,
            clock_ms=args.clock_ms,
            materialize=args.materialize,
        )
        if args.output:
            atomic_write_json(args.output, result)
        return result

    if args.command == "status":
        manager = CheckpointManager(args.root)
        return manager.status(checkpoint=args.checkpoint)

    if args.command == "verify":
        manager = CheckpointManager(args.root)
        return manager.verify(checkpoint=args.checkpoint)

    raise UsageError("unknown command", command=args.command)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        result = _run(args)
    except CheckpointError as exc:
        _print_json(exc.to_dict(), sys.stderr)
        return exc.exit_code
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        _print_json(
            {
                "ok": False,
                "error": {
                    "code": "E_INTERNAL",
                    "message": str(exc),
                    "details": {"type": type(exc).__name__},
                },
            },
            sys.stderr,
        )
        return 70
    _print_json(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
