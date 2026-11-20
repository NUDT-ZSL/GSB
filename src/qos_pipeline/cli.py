"""Command-line interface for load, shape, resume, status, and verify."""

from __future__ import annotations

import argparse
import sys
from typing import Any, Mapping

from .engine import Engine
from .errors import QosError
from .metrics import derive_metrics
from .runner import run_steps
from .sample import load_sample
from .serialization import digest_value, pretty_json
from .store import Store


class JsonArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise QosError("E_USAGE", message, {"parser": self.prog})


def _parser() -> argparse.ArgumentParser:
    parser = JsonArgumentParser(prog="qos-pipeline")
    subparsers = parser.add_subparsers(dest="command", required=True)

    load = subparsers.add_parser("load", help="validate and load a fixed sample")
    load.add_argument("--manifest", required=True)
    load.add_argument("--db", required=True)

    for name, help_text in (
        ("shape", "advance the committed logical clock"),
        ("resume", "resume after the last committed step"),
    ):
        command = subparsers.add_parser(name, help=help_text)
        command.add_argument("--db", required=True)
        command.add_argument("--sample", default=None)
        command.add_argument("--max-steps", type=int, default=None)
        command.add_argument("--no-steps", action="store_true")
        command.add_argument("--crash-at-step", type=int, default=None)
        command.add_argument(
            "--crash-point",
            choices=("after_refill", "after_schedule", "before_commit"),
            default=None,
        )

    status = subparsers.add_parser("status", help="show committed state")
    status.add_argument("--db", required=True)

    verify = subparsers.add_parser("verify", help="replay and verify committed steps")
    verify.add_argument("--db", required=True)
    verify.add_argument("--sample", default=None)
    verify.add_argument("--no-steps", action="store_true")
    return parser


def _resolve_sample(store: Store, sample_path: str | None) -> Any:
    if sample_path is not None:
        sample = load_sample(sample_path)
        store.initialize(sample)
        return sample
    return store.sample()


def _public_steps(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for row in rows:
        item = {
            **row["payload"],
            "version": row["version"],
            "prev_digest": row["prev_digest"],
            "step_digest": row["step_digest"],
        }
        result.append(item)
    return result


def _summary(
    command: str,
    store: Store,
    sample: Any,
    run_result: Mapping[str, Any] | None,
    *,
    include_steps: bool,
) -> dict[str, Any]:
    rows = store.steps()
    payloads = [row["payload"] for row in rows]
    state = rows[-1]["state"] if rows else store.snapshot()["state"]
    derived = derive_metrics(sample, payloads, state)
    result: dict[str, Any] = {
        "ok": True,
        "command": command,
        "database": str(store.path),
        "sample_id": sample.sample_id,
        "sample_digest": sample.sample_digest,
        "injection_id": sample.injection_id,
        "status": run_result["status"] if run_result is not None else store.status(),
        "steps_committed_this_call": (
            run_result["steps_committed_this_call"] if run_result is not None else []
        ),
        "derived": derived,
    }
    if include_steps:
        result["steps"] = _public_steps(rows)
    return result


def _first_diff(expected: Any, actual: Any, path: str = "$") -> dict[str, Any] | None:
    if type(expected) is not type(actual) and not (
        isinstance(expected, (int, float)) and isinstance(actual, (int, float))
    ):
        return {"path": path, "expected": expected, "actual": actual}
    if isinstance(expected, dict):
        expected_keys = sorted(expected)
        actual_keys = sorted(actual)
        if expected_keys != actual_keys:
            return {
                "path": path,
                "expected_keys": expected_keys,
                "actual_keys": actual_keys,
            }
        for key in expected_keys:
            difference = _first_diff(
                expected[key], actual[key], f"{path}.{key}"
            )
            if difference is not None:
                return difference
        return None
    if isinstance(expected, list):
        if len(expected) != len(actual):
            return {
                "path": path,
                "expected_length": len(expected),
                "actual_length": len(actual),
            }
        for index, (left, right) in enumerate(zip(expected, actual)):
            difference = _first_diff(left, right, f"{path}[{index}]")
            if difference is not None:
                return difference
        return None
    if expected != actual:
        return {"path": path, "expected": expected, "actual": actual}
    return None


def _verify(store: Store, sample: Any, include_steps: bool) -> dict[str, Any]:
    metadata = store.metadata()
    if metadata["sample_digest"] != sample.sample_digest:
        raise QosError(
            "E_DB_SAMPLE_MISMATCH",
            "verification sample differs from the database sample",
            {
                "database_sample_digest": metadata["sample_digest"],
                "verification_sample_digest": sample.sample_digest,
            },
        )
    rows = store.steps()
    engine = Engine(sample)
    state = engine.initial_state()
    previous_digest = metadata["initial_digest"]
    expected_payloads: list[dict[str, Any]] = []
    first_mismatch: dict[str, Any] | None = None
    contiguous = True
    stored_steps: list[dict[str, Any]] = []
    if len(rows) > sample.total_steps:
        first_mismatch = {
            "kind": "steps_beyond_horizon",
            "expected_max_steps": sample.total_steps,
            "actual_committed_steps": len(rows),
        }
    for index, row in enumerate(rows):
        if first_mismatch is not None:
            break
        if row["step_index"] != index:
            contiguous = False
            if first_mismatch is None:
                first_mismatch = {
                    "kind": "non_contiguous_steps",
                    "expected_step": index,
                    "actual_step": row["step_index"],
                }
            break
        expected_payload, state = engine.simulate_step(state, index)
        expected_payloads.append(expected_payload)
        payload_diff = _first_diff(expected_payload, row["payload"])
        state_diff = _first_diff(state, row["state"])
        expected_digest = digest_value(
            {
                "prev_digest": previous_digest,
                "payload": expected_payload,
                "state": state,
            }
        )
        if first_mismatch is None and payload_diff is not None:
            first_mismatch = {
                "kind": "payload_mismatch",
                "step_index": index,
                "difference": payload_diff,
            }
        if first_mismatch is None and state_diff is not None:
            first_mismatch = {
                "kind": "state_mismatch",
                "step_index": index,
                "difference": state_diff,
            }
        if first_mismatch is None and row["prev_digest"] != previous_digest:
            first_mismatch = {
                "kind": "prev_digest_mismatch",
                "step_index": index,
                "expected": previous_digest,
                "actual": row["prev_digest"],
            }
        if first_mismatch is None and row["logical_time_ms"] != expected_payload["step_end_ms"]:
            first_mismatch = {
                "kind": "logical_time_mismatch",
                "step_index": index,
                "expected": expected_payload["step_end_ms"],
                "actual": row["logical_time_ms"],
            }
        if first_mismatch is None and row["version"] != 1:
            first_mismatch = {
                "kind": "version_mismatch",
                "step_index": index,
                "expected": 1,
                "actual": row["version"],
            }
        if first_mismatch is None and row["step_digest"] != expected_digest:
            first_mismatch = {
                "kind": "step_digest_mismatch",
                "step_index": index,
                "expected": expected_digest,
                "actual": row["step_digest"],
            }
        if first_mismatch is not None:
            break
        previous_digest = expected_digest
        stored_steps.append({"step_index": index, "step_digest": expected_digest})

    derived = derive_metrics(sample, expected_payloads, state)
    complete = len(rows) == sample.total_steps and contiguous
    result: dict[str, Any] = {
        "ok": (
            contiguous
            and first_mismatch is None
            and derived["estimator_comparison"]["consistent"]
        ),
        "command": "verify",
        "database": str(store.path),
        "sample_id": sample.sample_id,
        "sample_digest": sample.sample_digest,
        "committed_steps": len(rows),
        "verified_steps": len(expected_payloads),
        "total_steps": sample.total_steps,
        "complete": complete,
        "contiguous": contiguous,
        "chain_valid": first_mismatch is None,
        "first_mismatch": first_mismatch,
        "estimator_consistent": derived["estimator_comparison"]["consistent"],
        "estimator_comparison": derived["estimator_comparison"],
        "derived": derived,
    }
    if include_steps:
        result["steps"] = stored_steps
    return result


def dispatch(args: argparse.Namespace) -> dict[str, Any]:
    store = Store(args.db)
    if args.command == "load":
        sample = load_sample(args.manifest)
        load_result = store.initialize(sample)
        return {
            "ok": True,
            "command": "load",
            "database": str(store.path),
            "sample_id": sample.sample_id,
            "sample_digest": sample.sample_digest,
            "injection_id": sample.injection_id,
            "config_digest": sample.config_digest,
            "arrivals_digest": sample.arrivals_digest,
            "classes": [item.name for item in sample.classes],
            "classifiers": [item.rule_id for item in sample.classifiers],
            "events": len(sample.events),
            "total_steps": sample.total_steps,
            "load": load_result,
            "status": store.status(),
        }
    if args.command in ("shape", "resume"):
        sample = _resolve_sample(store, args.sample)
        run_result = run_steps(
            sample,
            store,
            end_step=args.max_steps,
            crash_at_step=args.crash_at_step,
            crash_point=args.crash_point,
        )
        return _summary(
            args.command,
            store,
            sample,
            run_result,
            include_steps=not args.no_steps,
        )
    if args.command == "status":
        return store.status()
    if args.command == "verify":
        if args.sample is not None:
            sample = load_sample(args.sample)
            store.ensure_sample(sample)
        else:
            sample = store.sample()
        return _verify(store, sample, include_steps=not args.no_steps)
    raise QosError("E_USAGE", f"unsupported command {args.command!r}")


def main(argv: list[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
        result = dispatch(args)
        print(pretty_json(result))
        return 0
    except QosError as exc:
        print(pretty_json(exc.as_dict()))
        return exc.exit_code
    except (OSError, ValueError) as exc:
        error = QosError("E_USAGE", str(exc))
        print(pretty_json(error.as_dict()))
        return error.exit_code


if __name__ == "__main__":
    sys.exit(main())

