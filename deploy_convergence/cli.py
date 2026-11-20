"""Command-line entry point for local deterministic verification."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .engine import ConvergenceEngine
from .errors import EngineError
from .store import Store


DEFAULT_CONFIG = Path("samples/config.json")
DEFAULT_STATE = Path("artifacts/state.sqlite")


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _resolve(base: Path, value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else (base / path)


def _load_config(path: Path) -> tuple[dict[str, Any], dict[str, Path]]:
    config = _load_json(path)
    base = path.resolve().parent
    paths = {
        key: _resolve(base, value) for key, value in config["paths"].items()
    }
    return config, paths


def _engine(
    *,
    config_path: Path,
    state_path: Path,
    v2_meta_override: str | None,
    v2_shards_override: str | None,
    injection_override: str | None,
    rollback_policy_override: str | None,
) -> tuple[ConvergenceEngine, dict[str, Any], dict[str, Path]]:
    config, paths = _load_config(config_path)
    v2_meta = (
        Path(v2_meta_override).resolve()
        if v2_meta_override is not None
        else paths["v2_meta"]
    )
    v2_shards = (
        Path(v2_shards_override).resolve()
        if v2_shards_override is not None
        else paths["v2_shards"]
    )
    injection_path = (
        Path(injection_override).resolve()
        if injection_override is not None
        else paths["injections"]
    )
    engine = ConvergenceEngine(
        v1_path=paths["v1"],
        v2_meta_path=v2_meta,
        v2_shards_dir=v2_shards,
        state_path=state_path,
        environment_order=config["environment_order"],
        tolerances=config["tolerances"],
        clock_tolerance_ms=config["clock_tolerance_ms"],
        rollback_policy=(
            rollback_policy_override
            if rollback_policy_override is not None
            else config["rollback_policy"]
        ),
        rollback_window_ms=config["rollback_window_ms"],
        injection_path=injection_path,
    )
    return engine, config, paths


def _clock_from_args(args: argparse.Namespace, config: dict[str, Any], paths: dict[str, Path]) -> int:
    if args.now_ms is not None:
        return args.now_ms
    clock_payload = _load_json(paths["clock_steps"])
    if args.clock_step is not None:
        for step in clock_payload["steps"]:
            if step["name"] == args.clock_step:
                return int(step["now_ms"])
        raise EngineError(
            "E_BAD_MANIFEST",
            "requested clock step does not exist",
            stage="parse",
            details={"clock_step": args.clock_step},
        )
    return int(clock_payload["initial_clock_ms"])


def _print(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def command_verify(args: argparse.Namespace) -> int:
    config_path = Path(args.config).resolve()
    engine, config, paths = _engine(
        config_path=config_path,
        state_path=Path(args.state).resolve(),
        v2_meta_override=args.v2_meta,
        v2_shards_override=args.v2_shards,
        injection_override=args.injections,
        rollback_policy_override=args.rollback_policy,
    )
    clock_ms = _clock_from_args(args, config, paths)
    result = engine.run(
        clock_ms=clock_ms,
        crash_after_step=args.crash_after,
        recover=args.recover,
    )
    payload = result.to_dict()
    if args.json_out is not None:
        output = Path(args.json_out)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    _print(payload)
    return 0


def command_status(args: argparse.Namespace) -> int:
    state_path = Path(args.state).resolve()
    if not state_path.exists():
        raise EngineError(
            "E_IO",
            "state database does not exist",
            stage="persist",
            details={"path": str(state_path)},
        )
    _print(Store(state_path).status())
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="deploy-convergence",
        description="Deterministic local deployment manifest convergence engine",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    verify = subparsers.add_parser("verify", help="load, merge, evaluate and commit")
    verify.add_argument("--config", default=str(DEFAULT_CONFIG))
    verify.add_argument("--state", default=str(DEFAULT_STATE))
    verify.add_argument("--now-ms", type=int)
    verify.add_argument("--clock-step")
    verify.add_argument("--v2-meta")
    verify.add_argument("--v2-shards")
    verify.add_argument("--injections")
    verify.add_argument("--rollback-policy", choices=("reject", "degrade"))
    verify.add_argument("--crash-after", choices=(
        "parse",
        "migrate",
        "merge",
        "evaluate",
        "stage",
        "switch",
    ))
    verify.add_argument("--recover", action="store_true")
    verify.add_argument("--json-out")
    verify.set_defaults(func=command_verify)

    status = subparsers.add_parser("status", help="show persisted state")
    status.add_argument("--state", default=str(DEFAULT_STATE))
    status.set_defaults(func=command_status)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except EngineError as error:
        print(
            json.dumps(
                error.to_dict(),
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
