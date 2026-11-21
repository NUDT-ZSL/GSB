"""Exact rational reference checks for persisted wait arithmetic."""

from __future__ import annotations

import os
import shutil
import tempfile
from fractions import Fraction
from pathlib import Path
from typing import Any

from .engine import Engine
from .fixed import fraction_text
from .model import Scenario
from .storage import Store


def wait_truth(status: dict[str, Any], scenario: Scenario) -> dict[str, Any]:
    """Recompute each registered wait from the committed logical timestamps.

    The reference is independent of the diagnostic engine's rounding code: it
    uses Fraction arithmetic directly and compares the ordered sequence per
    transaction. The timestamps themselves are the single-transaction logical
    replay trace stored at enqueue/resolve time.
    """

    expected: dict[str, list[dict[str, Any]]] = {}
    for tx in sorted(scenario.transactions, key=lambda item: item.tx_no):
        expected[tx.tx_key] = []
    for tx_status in sorted(status["transactions"], key=lambda item: item["tx_no"]):
        chain = []
        for wait in tx_status["wait_records"]:
            raw = int(wait["complete_tick"]) - int(wait["enqueue_tick"])
            exact = Fraction(raw) * scenario.config.scale
            fixed = exact.numerator // exact.denominator
            chain.append(
                {
                    "registration_seq": wait["registration_seq"],
                    "raw_wait_ms": raw,
                    "exact_units": fraction_text(exact),
                    "fixed_units": fixed,
                    "remainder": fraction_text(exact - fixed),
                }
            )
        expected[tx_status["tx_key"]] = chain
    return expected


def verify_status(status: dict[str, Any], scenario: Scenario) -> dict[str, Any]:
    expected = wait_truth(status, scenario)
    failures: list[dict[str, Any]] = []
    seen_registration: set[int] = set()
    for tx_status in status["transactions"]:
        key = tx_status["tx_key"]
        actual_chain = [
            {
                "registration_seq": wait["registration_seq"],
                "raw_wait_ms": wait["raw_wait_ms"],
                "exact_units": wait["exact_units"],
                "fixed_units": wait["fixed_units"],
                "remainder": wait["discarded_remainder"],
            }
            for wait in tx_status["wait_records"]
        ]
        expected_chain = expected.get(key, [])
        if actual_chain != expected_chain:
            failures.append(
                {
                    "code": "E_TRUTH_MISMATCH",
                    "tx_key": key,
                    "expected": expected_chain,
                    "actual": actual_chain,
                }
            )
        for wait in tx_status["wait_records"]:
            seq = int(wait["registration_seq"])
            if seq in seen_registration:
                failures.append(
                    {
                        "code": "E_DUPLICATE_REGISTRATION",
                        "registration_seq": seq,
                        "tx_key": key,
                    }
                )
            seen_registration.add(seq)
            raw = int(wait["raw_wait_ms"])
            if int(wait["complete_tick"]) - int(wait["enqueue_tick"]) != raw:
                failures.append(
                    {
                        "code": "E_CLOCK_MISMATCH",
                        "tx_key": key,
                        "registration_seq": seq,
                    }
                )
        expected_fixed = sum(int(wait["fixed_units"]) for wait in actual_chain)
        expected_exact = sum(
            (Fraction(wait["exact_units"]) for wait in actual_chain), Fraction(0, 1)
        )
        expected_error = sum(
            (Fraction(wait["remainder"]) for wait in actual_chain), Fraction(0, 1)
        )
        if tx_status["wait_duration"]["fixed_units"] != expected_fixed:
            failures.append(
                {
                    "code": "E_SUM_MISMATCH",
                    "tx_key": key,
                    "field": "fixed_units",
                }
            )
        if Fraction(tx_status["wait_duration"]["exact_units"]) != expected_exact:
            failures.append(
                {
                    "code": "E_SUM_MISMATCH",
                    "tx_key": key,
                    "field": "exact_units",
                }
            )
        if Fraction(tx_status["error_upper_bound"]) != expected_error:
            failures.append(
                {
                    "code": "E_ERROR_BOUND_MISMATCH",
                    "tx_key": key,
                    "expected": fraction_text(expected_error),
                    "actual": tx_status["error_upper_bound"],
                }
            )
        chain_sequences = [
            entry["registration_seq"] for entry in tx_status["error_source_chain"]
        ]
        wait_sequences = [wait["registration_seq"] for wait in actual_chain]
        if chain_sequences != wait_sequences:
            failures.append(
                {
                    "code": "E_SOURCE_CHAIN_MISMATCH",
                    "tx_key": key,
                    "expected": wait_sequences,
                    "actual": chain_sequences,
                }
            )

    if status["run"]["status"] in {"COMPLETE", "HORIZON"}:
        if status["locks"]["holders"]:
            failures.append(
                {
                    "code": "E_LOCKS_NOT_RELEASED",
                    "holders": status["locks"]["holders"],
                }
            )
        waiting = [
            entry
            for entry in status["locks"]["queue"]
            if entry["status"] == "WAITING"
        ]
        if waiting:
            failures.append({"code": "E_QUEUE_NOT_DRAINED", "queue": waiting})

    return {
        "ok": not failures,
        "truth_source": "exact Fraction replay of committed enqueue/resolve ticks",
        "checked_wait_records": len(seen_registration),
        "failures": failures,
    }


def _status_signature(status: dict[str, Any]) -> dict[str, Any]:
    return {
        "run_status": status["run"]["status"],
        "ledger_digest": status["run"]["ledger_digest"],
        "transactions": [
            {
                "tx_key": tx["tx_key"],
                "status": tx["status"],
                "reason": tx["reason"],
                "wait_records": [
                    {
                        "registration_seq": wait["registration_seq"],
                        "raw_wait_ms": wait["raw_wait_ms"],
                        "fixed_units": wait["fixed_units"],
                        "exact_units": wait["exact_units"],
                        "remainder": wait["discarded_remainder"],
                        "outcome": wait["outcome"],
                    }
                    for wait in tx["wait_records"]
                ],
            }
            for tx in status["transactions"]
        ],
        "decisions": [
            {
                "kind": decision["kind"],
                "tick": decision["tick"],
                "tx_key": decision["tx_key"],
                "reason": decision["reason"],
                "cycle": decision["cycle"],
                "basis": decision["basis"],
                "ledger_sensitive": decision["ledger_sensitive"],
                "sensitivity": decision["sensitivity"],
            }
            for decision in status["decisions"]
        ],
    }


def serial_replay_check(
    status: dict[str, Any],
    scenario: Scenario,
    workdir: Path,
) -> dict[str, Any]:
    """Replay the same script from scratch in an isolated temporary ledger."""

    replay_root = workdir / "tmp"
    replay_root.mkdir(parents=True, exist_ok=True)
    replay_dir = Path(
        tempfile.mkdtemp(prefix=f"replay-{os.getpid()}-", dir=replay_root)
    )
    try:
        replay_store = Store(replay_dir)
        replay_store.initialize(
            scenario, f"replay-{scenario.source_sha256[:16]}"
        )
        replayed = Engine(scenario, replay_store).run()
    finally:
        shutil.rmtree(replay_dir, ignore_errors=True)
    expected = _status_signature(replayed)
    actual = _status_signature(status)
    return {
        "check": "serial_replay",
        "ok": expected == actual,
        "expected": expected,
        "actual": actual,
    }

