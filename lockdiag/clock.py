"""Injected logical clock and deterministic fixed-point conversion."""

from __future__ import annotations

import sqlite3
from typing import Any

from .model import ClockSpec
from .util import format_micro, format_units


def _ceil_micro(numerator: int, denominator: int) -> int:
    if numerator <= 0:
        return 0
    return (numerator * 1_000_000 + denominator - 1) // denominator


def next_sample(
    connection: sqlite3.Connection,
    clock: ClockSpec,
    bundle_id: str,
    purpose: str,
    tx_id: str | None = None,
    op_seq: int | None = None,
) -> dict[str, Any]:
    """Consume exactly one injected logical-clock sample inside the caller's transaction."""

    state = connection.execute(
        "SELECT next_sequence, next_raw, last_raw FROM clock_state WHERE id=1"
    ).fetchone()
    if state is None:
        raise RuntimeError("clock_state is missing")
    sequence_index = int(state["next_sequence"])
    if sequence_index < len(clock.samples):
        raw_value = clock.samples[sequence_index]
    else:
        raw_value = int(state["next_raw"])
    cursor = connection.execute(
        """
        INSERT INTO clock_samples(
            sequence_index, raw_value, raw_units_per_ms, purpose,
            tx_id, op_seq, bundle_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            sequence_index,
            raw_value,
            clock.raw_units_per_ms,
            purpose,
            tx_id,
            op_seq,
            bundle_id,
        ),
    )
    sample_id = int(cursor.lastrowid)
    next_sequence = sequence_index + 1
    next_raw = (
        clock.samples[next_sequence]
        if next_sequence < len(clock.samples)
        else raw_value + clock.step_units
    )
    connection.execute(
        """
        UPDATE clock_state
        SET next_sequence=?, next_raw=?, last_sample_id=?, last_raw=?
        WHERE id=1
        """,
        (next_sequence, next_raw, sample_id, raw_value),
    )
    return {
        "sample_id": sample_id,
        "sequence_index": sequence_index,
        "raw_value": raw_value,
        "raw_units_per_ms": clock.raw_units_per_ms,
    }


def sample_by_id(connection: sqlite3.Connection, sample_id: int) -> dict[str, Any]:
    row = connection.execute(
        """
        SELECT sample_id, sequence_index, raw_value, raw_units_per_ms, purpose, tx_id, op_seq
        FROM clock_samples WHERE sample_id=?
        """,
        (sample_id,),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"clock sample {sample_id} is missing")
    return dict(row)


def convert_duration(
    start: dict[str, Any],
    end: dict[str, Any],
    scale: int,
) -> dict[str, Any]:
    """Convert raw sample subtraction to fixed-point milliseconds.

    The raw clock is rational milliseconds.  The rounded value is stored with
    the configured fixed scale; both the exact rational and the conservative
    half-quantum conversion bound are retained.
    """

    if start["raw_units_per_ms"] != end["raw_units_per_ms"]:
        raise RuntimeError("clock sample unit mismatch")
    raw_delta = int(end["raw_value"]) - int(start["raw_value"])
    if raw_delta < 0:
        raise RuntimeError("logical clock moved backwards")
    denominator = int(start["raw_units_per_ms"])
    numerator = raw_delta * scale
    rounded_units = (numerator + denominator // 2) // denominator
    residual_numerator = abs(numerator - rounded_units * denominator)
    if residual_numerator == 0:
        direction = "EXACT"
    elif numerator > rounded_units * denominator:
        direction = "DOWN"
    else:
        direction = "UP"
    actual_residual_micro = _ceil_micro(residual_numerator, denominator * scale)
    half_quantum_micro = (500_000 + scale - 1) // scale
    error_bound_micro = max(actual_residual_micro, half_quantum_micro)
    return {
        "raw_delta": raw_delta,
        "scale": scale,
        "raw_units_per_ms": denominator,
        "rounded_units": rounded_units,
        "exact_numerator": numerator,
        "exact_denominator": denominator,
        "rounding_method": "HALF_UP",
        "rounding_direction": direction,
        "residual_numerator": residual_numerator,
        "actual_residual_micro": actual_residual_micro,
        "error_bound_micro": error_bound_micro,
        "rounded_ms": format_units(rounded_units, scale),
        "actual_residual_ms": format_micro(actual_residual_micro),
        "error_bound_ms": format_micro(error_bound_micro),
    }
