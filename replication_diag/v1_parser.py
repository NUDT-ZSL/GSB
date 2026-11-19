"""Parser and validator for the line-delimited v1 position format."""

from __future__ import annotations

from fractions import Fraction
from pathlib import Path
from typing import Any

from .canonical import require_bool, require_int, parse_tx_id
from .config import Config
from .errors import DiagnosticsError
from .models import PositionRecord
from .numeric import round_fraction
from .util import read_jsonl, sha256_bytes, stable_json_bytes


def _fraction_text(value: Fraction) -> str:
    from .canonical import fraction_text

    return fraction_text(value)


def _content_digest(payload: dict[str, Any]) -> str:
    return sha256_bytes(stable_json_bytes(payload))


def parse_v1(path: Path, config: Config) -> list[PositionRecord]:
    rows = read_jsonl(path)
    if not rows:
        raise DiagnosticsError(
            "E_JSON", message="v1 position file is empty", location=str(path)
        )

    records: list[PositionRecord] = []
    previous_tx: int | None = None
    saw_uncommitted = False

    for index, (line_number, raw) in enumerate(rows):
        location = f"{path}:{line_number}"
        if not isinstance(raw, dict):
            raise DiagnosticsError(
                "E_JSON", message="v1 line must be a JSON object", location=location
            )

        tx = parse_tx_id(raw.get("tx_id"), location=f"{location}:tx_id")
        if previous_tx is not None and tx.number <= previous_tx:
            raise DiagnosticsError(
                "E_V1_ORDER",
                location=f"{location}:tx_id",
                details={"previous": previous_tx, "current": tx.number},
            )
        previous_tx = tx.number

        delay = require_int(
            raw.get("relative_delay_ms"),
            code="E_V1_DELAY",
            location=f"{location}:relative_delay_ms",
            minimum=0,
        )
        payload_bytes = require_int(
            raw.get("payload_bytes"),
            code="E_V1_BYTES",
            location=f"{location}:payload_bytes",
            minimum=1,
        )
        committed = require_bool(
            raw.get("committed"), code="E_V1_COMMIT", location=f"{location}:committed"
        )
        if saw_uncommitted and committed:
            raise DiagnosticsError(
                "E_V1_COMMIT",
                location=f"{location}:committed",
                details={"reason": "committed records must form an ordered prefix"},
            )
        if not committed:
            saw_uncommitted = True

        logical_clock = raw.get("logical_clock")
        if logical_clock is None:
            logical_clock = config.v1_base_clock_ms + config.v1_clock_step_ms * index
        logical_clock = require_int(
            logical_clock,
            code="E_CLOCK_ROLLBACK",
            location=f"{location}:logical_clock",
            minimum=0,
        )
        source_node = raw.get("source_node", "v1-primary")
        replica_node = raw.get("replica_node", "v1-replica")
        if not isinstance(source_node, str) or not source_node:
            raise DiagnosticsError(
                "E_V1_NODE",
                message="v1 source_node must be a non-empty string",
                location=f"{location}:source_node",
            )
        if not isinstance(replica_node, str) or not replica_node:
            raise DiagnosticsError(
                "E_V1_NODE",
                message="v1 replica_node must be a non-empty string",
                location=f"{location}:replica_node",
            )

        exact_scaled = Fraction(delay, 1) * config.v1_scale
        rounded_delay, rounding_direction = round_fraction(exact_scaled, config.v1_rounding)
        rounding_error = abs(Fraction(rounded_delay, 1) - exact_scaled)
        error_bound = rounding_error + config.v1_observation_error
        commit_time = logical_clock
        replay_time = commit_time + rounded_delay

        migration = {
            "from_version": "v1",
            "to_version": "v2",
            "relative_delay_ms": delay,
            "injected_logical_clock_ms": logical_clock,
            "scale": f"{config.v1_scale.numerator}/{config.v1_scale.denominator}",
            "rounding_mode": config.v1_rounding,
            "rounding_direction": rounding_direction,
            "exact_delay_ms": _fraction_text(exact_scaled),
            "rounded_delay_ms": rounded_delay,
            "rounding_error_ms": _fraction_text(rounding_error),
            "ordering_preserved": True,
            "replay_generation": {
                "from": None,
                "to": 0,
                "mapping": "unversioned->generation:0",
            },
            "absolute_time_basis": "injected_logical_clock_plus_relative_delay",
        }
        original_time = {
            "kind": "relative_delay_ms",
            "relative_delay_ms": delay,
            "injected_commit_time_ms": commit_time,
            "scale": f"{config.v1_scale.numerator}/{config.v1_scale.denominator}",
            "rounding_mode": config.v1_rounding,
        }
        original_delay = {
            "kind": "v1_relative_delay_ms",
            "value": delay,
            "scale": f"{config.v1_scale.numerator}/{config.v1_scale.denominator}",
        }
        payload = {
            "tx_id": tx.canonical,
            "source_version": "v1",
            "source_priority": 1,
            "logical_clock": logical_clock,
            "replay_generation": 0,
            "payload_bytes": payload_bytes,
            "committed": committed,
            "commit_time_ms": commit_time,
            "replay_time_ms": replay_time,
            "delay_ms": rounded_delay,
            "original": raw,
        }
        records.append(
            PositionRecord(
                tx_id=tx.canonical,
                tx_num=tx.number,
                source_version="v1",
                source_priority=1,
                logical_clock=logical_clock,
                replay_generation=0,
                source_node=source_node,
                replica_node=replica_node,
                committed=committed,
                payload_bytes=payload_bytes,
                commit_time_ms=commit_time,
                replay_time_ms=replay_time,
                delay_ms=rounded_delay,
                tombstone_applied=False,
                original_time=original_time,
                original_delay=original_delay,
                migration=migration,
                error_upper_bound=error_bound,
                error_sources=(
                    "v1.relative_delay_ms: exact integer input",
                    f"v1.logical_clock_ms: injected exact integer ({logical_clock})",
                    f"v1.scale: {config.v1_scale.numerator}/{config.v1_scale.denominator}",
                    f"v1.rounding: {config.v1_rounding} ({rounding_direction})",
                    f"v1.observation_error: {_fraction_text(config.v1_observation_error)} ms",
                ),
                location=location,
                content_digest=_content_digest(payload),
            )
        )

    # A monotone quantizer cannot reorder delays.  This explicit check guards a
    # future rounding implementation from silently violating the migration
    # contract.
    for left, right in zip(records, records[1:]):
        if left.delay_ms <= right.delay_ms and not left.replay_time_ms <= right.replay_time_ms:
            raise DiagnosticsError(
                "E_CLOCK_ROLLBACK",
                message="v1 migration changed transaction replay order",
                location=right.location,
            )
    return records
