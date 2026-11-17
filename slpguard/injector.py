"""Deterministic malformed-sample injection with in-band truth metadata."""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

from .errors import ErrorCode, SLPError, mask_for
from .io_utils import atomic_write_bytes, sha256_hex
from .protocol import (
    Extension,
    Phase,
    SLPRecord,
    encode_file,
    parse_bytes,
)


@dataclass(frozen=True)
class InjectionSpec:
    """Explicit injection knobs.

    Physical swaps never change truth because business judgement is normalized
    by sequence.  Stage jumps remove a sequence and stamp the next packet with
    E_TRANSITION plus the final packet with E_INCOMPLETE.  The other operations
    stamp only their directly implicated truth packets; callers can inspect any
    unexpected observation through the normal truth report.
    """

    target_session: int | None = None
    stage_jump_sequence: int | None = None
    physical_swap_positions: tuple[int, int] | None = None
    mixin_source_session: int | None = None
    mixin_target_session: int | None = None
    mixin_sequence: int | None = None
    renegotiation_count: int | None = None
    max_renegotiations: int = 3
    half_close_append: bool = False

    def __post_init__(self) -> None:
        if self.stage_jump_sequence is not None and self.stage_jump_sequence < 1:
            raise ValueError("stage_jump_sequence must be positive")
        if (
            self.physical_swap_positions is not None
            and len(self.physical_swap_positions) != 2
        ):
            raise ValueError("physical_swap_positions must contain exactly two positions")
        if self.renegotiation_count is not None and self.renegotiation_count < 0:
            raise ValueError("renegotiation_count must be non-negative")
        if self.max_renegotiations < 0:
            raise ValueError("max_renegotiations must be non-negative")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class InjectionResult:
    output_path: str
    file_id: str
    record_count: int
    spec: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "output_path": self.output_path,
            "file_id": self.file_id,
            "record_count": self.record_count,
            "spec": self.spec,
        }


def _session_records(records: list[SLPRecord], session_id: int | None) -> list[SLPRecord]:
    if session_id is None:
        session_ids = sorted({record.session_id for record in records})
        if not session_ids:
            raise SLPError(ErrorCode.E_ARG, "cannot choose a session from an empty file")
        session_id = session_ids[0]
    selected = [record for record in records if record.session_id == session_id]
    if not selected:
        raise SLPError(ErrorCode.E_ARG, f"session {session_id} does not exist")
    return sorted(selected, key=lambda record: (record.sequence, record.sync_id))


def _next_sync_id(records: list[SLPRecord]) -> int:
    return max((record.sync_id for record in records), default=0) + 1


def _next_sequence(records: list[SLPRecord]) -> int:
    return max((record.sequence for record in records), default=0) + 1


def _with_truth(record: SLPRecord, *codes: ErrorCode) -> SLPRecord:
    return replace(record, truth_mask=record.truth_mask | mask_for(*codes))


def _stage_jump(records: list[SLPRecord], sequence: int) -> list[SLPRecord]:
    sorted_records = sorted(
        records,
        key=lambda record: (record.session_id, record.sequence, record.sync_id),
    )
    matching = [record for record in sorted_records if record.sequence == sequence]
    if len(matching) != 1:
        raise SLPError(
            ErrorCode.E_ARG,
            f"stage_jump_sequence={sequence} must identify exactly one packet",
        )
    removed = matching[0]
    remaining = [record for record in records if record is not removed]
    session = [
        record
        for record in remaining
        if record.session_id == removed.session_id
    ]
    session.sort(key=lambda record: (record.sequence, record.sync_id))
    if not session:
        raise SLPError(ErrorCode.E_ARG, "cannot stage-jump an empty session")

    next_record = next(
        (record for record in session if record.sequence > removed.sequence),
        None,
    )
    transition_sync = next_record.sync_id if next_record is not None else None
    incomplete_sync = session[-1].sync_id
    result: list[SLPRecord] = []
    for record in remaining:
        if transition_sync is not None and record.sync_id == transition_sync:
            record = _with_truth(record, ErrorCode.E_TRANSITION)
        if record.sync_id == incomplete_sync:
            record = _with_truth(record, ErrorCode.E_INCOMPLETE)
        result.append(record)
    return result


def _physical_swap(
    records: list[SLPRecord],
    positions: tuple[int, int],
) -> list[SLPRecord]:
    left, right = positions
    if left == right or left < 0 or right < 0 or left >= len(records) or right >= len(records):
        raise SLPError(
            ErrorCode.E_ARG,
            f"physical_swap_positions={positions} are outside 0..{len(records) - 1}",
        )
    swapped = list(records)
    swapped[left], swapped[right] = swapped[right], swapped[left]
    return swapped


def _mixin(
    records: list[SLPRecord],
    source_session: int,
    target_session: int,
    sequence: int,
) -> list[SLPRecord]:
    source_matches = [
        record
        for record in records
        if record.session_id == source_session and record.sequence == sequence
    ]
    if len(source_matches) != 1:
        raise SLPError(
            ErrorCode.E_ARG,
            (
                f"mixin source session={source_session}, sequence={sequence} "
                "must identify exactly one packet"
            ),
        )
    if not any(record.session_id == target_session for record in records):
        raise SLPError(ErrorCode.E_ARG, f"target session {target_session} does not exist")
    source = source_matches[0]
    clone = replace(
        source,
        sync_id=_next_sync_id(records),
        session_id=target_session,
        offset=0,
        truth_mask=source.truth_mask | mask_for(ErrorCode.E_MIXUP),
    )
    result: list[SLPRecord] = []
    for record in records:
        if record is source:
            result.append(_with_truth(record, ErrorCode.E_MIXUP))
        else:
            result.append(record)
    result.append(clone)
    return result


def _add_renegotiations(
    records: list[SLPRecord],
    *,
    target_session: int | None,
    desired_total: int,
    max_allowed: int,
) -> list[SLPRecord]:
    if desired_total < 0:
        raise SLPError(ErrorCode.E_ARG, "renegotiation_count must be non-negative")
    selected = _session_records(records, target_session)
    close_records = [record for record in selected if record.phase == int(Phase.CLOSE)]
    if len(close_records) != 1:
        raise SLPError(
            ErrorCode.E_ARG,
            "renegotiation injection requires exactly one CLOSE in the target session",
        )
    close = close_records[0]
    existing = [
        record
        for record in selected
        if record.phase == int(Phase.NEGOTIATE)
        and record.extensions & int(Extension.RENEGOTIATE)
    ]
    if desired_total < len(existing):
        raise SLPError(
            ErrorCode.E_ARG,
            (
                f"desired renegotiation_count={desired_total} is lower than "
                f"existing count {len(existing)}"
            ),
        )
    cycles = desired_total - len(existing)
    if cycles == 0:
        return list(records)

    used_sequences = {record.sequence for record in records}
    next_sequence = _next_sequence(records)
    next_sync = _next_sync_id(records)
    new_records: list[SLPRecord] = []
    renegotiation_records: list[SLPRecord] = []
    direction = close.direction
    payload_prefix = b"injected-renegotiation-"
    for _ in range(cycles):
        while next_sequence in used_sequences:
            next_sequence += 1
        negotiate = SLPRecord(
            sync_id=next_sync,
            session_id=close.session_id,
            sequence=next_sequence,
            phase=int(Phase.NEGOTIATE),
            direction=direction,
            extensions=int(Extension.RENEGOTIATE),
            truth_mask=0,
            payload=payload_prefix + b"negotiate",
        )
        next_sync += 1
        next_sequence += 1
        while next_sequence in used_sequences:
            next_sequence += 1
        data = SLPRecord(
            sync_id=next_sync,
            session_id=close.session_id,
            sequence=next_sequence,
            phase=int(Phase.DATA),
            direction=direction,
            extensions=0,
            truth_mask=0,
            payload=payload_prefix + b"data",
        )
        next_sync += 1
        next_sequence += 1
        new_records.extend((negotiate, data))
        renegotiation_records.append(negotiate)

    all_renegotiations = existing + renegotiation_records
    over_limit_syncs = {
        record.sync_id
        for index, record in enumerate(all_renegotiations)
        if index >= max_allowed
    }
    new_records = [
        _with_truth(record, ErrorCode.E_STORM)
        if record.sync_id in over_limit_syncs
        else record
        for record in new_records
    ]

    close_sequence = next_sequence
    while close_sequence in used_sequences:
        close_sequence += 1
    updated_close = replace(close, sequence=close_sequence)
    result: list[SLPRecord] = []
    for record in records:
        if record is close:
            record = updated_close
        if record.sync_id in over_limit_syncs:
            record = _with_truth(record, ErrorCode.E_STORM)
        result.append(record)
    result.extend(new_records)
    return result


def _half_close_append(
    records: list[SLPRecord],
    target_session: int | None,
) -> list[SLPRecord]:
    selected = _session_records(records, target_session)
    close_records = [record for record in selected if record.phase == int(Phase.CLOSE)]
    if len(close_records) != 1:
        raise SLPError(
            ErrorCode.E_ARG,
            "half-close injection requires exactly one CLOSE in the target session",
        )
    close = close_records[0]
    updated_close = replace(
        close,
        extensions=close.extensions | int(Extension.HALF_CLOSE),
    )
    appended = SLPRecord(
        sync_id=_next_sync_id(records),
        session_id=close.session_id,
        sequence=_next_sequence(records),
        phase=int(Phase.DATA),
        direction=close.direction,
        extensions=0,
        truth_mask=mask_for(ErrorCode.E_AFTER_CLOSE),
        payload=b"injected-half-close-data",
    )
    return [
        updated_close if record is close else record
        for record in records
    ] + [appended]


def inject(
    base_data: bytes,
    spec: InjectionSpec,
    output_path: str | Path,
) -> InjectionResult:
    """Apply one explicit injection spec and atomically emit an SLP sample."""

    parsed = parse_bytes(base_data)
    records = list(parsed.records)

    if spec.stage_jump_sequence is not None:
        records = _stage_jump(records, spec.stage_jump_sequence)
    if spec.mixin_source_session is not None:
        if spec.mixin_target_session is None or spec.mixin_sequence is None:
            raise SLPError(
                ErrorCode.E_ARG,
                "mixin requires source session, target session, and sequence",
            )
        records = _mixin(
            records,
            spec.mixin_source_session,
            spec.mixin_target_session,
            spec.mixin_sequence,
        )
    if spec.renegotiation_count is not None:
        records = _add_renegotiations(
            records,
            target_session=spec.target_session,
            desired_total=spec.renegotiation_count,
            max_allowed=spec.max_renegotiations,
        )
    if spec.half_close_append:
        records = _half_close_append(records, spec.target_session)
    if spec.physical_swap_positions is not None:
        records = _physical_swap(records, spec.physical_swap_positions)

    data = encode_file(records, version=parsed.version, flags=parsed.flags)
    # Structural validation is allowed; state judgement is not used to create
    # or overwrite in-band truth metadata.
    parse_bytes(data)
    target = Path(output_path)
    atomic_write_bytes(target, data)
    return InjectionResult(
        output_path=str(target),
        file_id=sha256_hex(data),
        record_count=len(records),
        spec=spec.to_dict(),
    )
