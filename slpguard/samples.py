"""Deterministic local SLP sample corpus."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any, Iterable

from .errors import ErrorCode, mask_for
from .io_utils import atomic_write_bytes, atomic_write_json
from .protocol import (
    Extension,
    Phase,
    SLPRecord,
    encode_file,
)


class RecordBuilder:
    def __init__(self, first_sync: int = 1000) -> None:
        self.next_sync = first_sync

    def record(
        self,
        session_id: int,
        sequence: int,
        phase: Phase,
        *,
        direction: int = 0,
        extensions: int = 0,
        truth_mask: int = 0,
        payload: bytes | None = None,
    ) -> SLPRecord:
        record = SLPRecord(
            sync_id=self.next_sync,
            session_id=session_id,
            sequence=sequence,
            phase=int(phase),
            direction=direction,
            extensions=extensions,
            truth_mask=truth_mask,
            payload=payload
            if payload is not None
            else f"session={session_id};seq={sequence};phase={phase.name}".encode("ascii"),
        )
        self.next_sync += 1
        return record

    def session(
        self,
        session_id: int,
        start_sequence: int,
        *,
        direction: int = 0,
        close_extensions: int = 0,
    ) -> list[SLPRecord]:
        phases = (Phase.HELLO, Phase.AUTH, Phase.NEGOTIATE, Phase.DATA, Phase.CLOSE)
        records = [
            self.record(
                session_id,
                start_sequence + index,
                phase,
                direction=direction,
                extensions=close_extensions if phase is Phase.CLOSE else 0,
            )
            for index, phase in enumerate(phases)
        ]
        return records


def _write(path: Path, records: Iterable[SLPRecord], *, version: int = 1) -> bytes:
    data = encode_file(records, version=version)
    atomic_write_bytes(path, data)
    return data


def _mark(records: list[SLPRecord], sync_id: int, *codes: ErrorCode) -> list[SLPRecord]:
    result: list[SLPRecord] = []
    for record in records:
        if record.sync_id == sync_id:
            record = replace(record, truth_mask=record.truth_mask | mask_for(*codes))
        result.append(record)
    return result


def _record_with_sequence(records: list[SLPRecord], sequence: int) -> SLPRecord:
    matches = [record for record in records if record.sequence == sequence]
    if len(matches) != 1:
        raise ValueError(f"expected one record at sequence {sequence}, got {len(matches)}")
    return matches[0]


def generate_samples(output_dir: str | Path) -> dict[str, Any]:
    target = Path(output_dir)
    target.mkdir(parents=True, exist_ok=True)

    manifest: dict[str, Any] = {
        "schema": "slp-sample-manifest-v1",
        "note": (
            "Truth is embedded in each record as sync_id + truth_mask. "
            "The manifest is documentation only and is not used for verification."
        ),
        "samples": [],
    }

    def add(
        name: str,
        purpose: str,
        data: bytes,
        *,
        expected: tuple[ErrorCode, ...] = (),
    ) -> None:
        path = target / name
        atomic_write_bytes(path, data)
        manifest["samples"].append(
            {
                "file": name,
                "purpose": purpose,
                "bytes": len(data),
                "expected_codes": [code.name for code in expected],
            }
        )

    builder = RecordBuilder()
    valid_records = builder.session(1, 1) + builder.session(2, 10)
    add(
        "valid_slp.slp",
        "Two valid sessions with globally unique sequence numbers",
        _write(target / "valid_slp.slp", valid_records),
    )

    builder = RecordBuilder()
    physical = builder.session(7, 1)
    physical = [physical[2], physical[0], physical[4], physical[1], physical[3]]
    add(
        "physical_reorder.slp",
        "Valid packet order after sequence-normalized reconstruction",
        _write(target / "physical_reorder.slp", physical),
    )

    builder = RecordBuilder()
    duplicate = builder.session(8, 1)
    original_data = _record_with_sequence(duplicate, 4)
    duplicate_clone = builder.record(
        8,
        4,
        Phase.DATA,
        payload=b"duplicate-data",
    )
    duplicate = [
        replace(record, truth_mask=record.truth_mask | mask_for(ErrorCode.E_TRANSITION))
        if record.sync_id == original_data.sync_id
        else record
        for record in duplicate
    ]
    duplicate.append(
        replace(duplicate_clone, truth_mask=mask_for(ErrorCode.E_TRANSITION))
    )
    add(
        "duplicate.slp",
        "Duplicate sequence and phase in one session",
        _write(target / "duplicate.slp", duplicate),
        expected=(ErrorCode.E_TRANSITION,),
    )

    builder = RecordBuilder()
    logical = builder.session(9, 1)
    hello = _record_with_sequence(logical, 1)
    auth = _record_with_sequence(logical, 2)
    negotiate = _record_with_sequence(logical, 3)
    logical = [
        replace(record, sequence=auth.sequence)
        if record.sync_id == hello.sync_id
        else replace(record, sequence=hello.sequence)
        if record.sync_id == auth.sync_id
        else record
        for record in logical
    ]
    for record in (hello, auth, negotiate):
        logical = _mark(logical, record.sync_id, ErrorCode.E_TRANSITION)
    add(
        "logical_reorder.slp",
        "Sequence swap makes semantics out of order after normalization",
        _write(target / "logical_reorder.slp", logical),
        expected=(ErrorCode.E_TRANSITION,),
    )

    builder = RecordBuilder()
    jump = builder.session(10, 1)
    auth = _record_with_sequence(jump, 2)
    jump = [record for record in jump if record.sync_id != auth.sync_id]
    jump = _mark(
        jump,
        _record_with_sequence(jump, 3).sync_id,
        ErrorCode.E_TRANSITION,
    )
    jump = _mark(
        jump,
        _record_with_sequence(jump, 5).sync_id,
        ErrorCode.E_INCOMPLETE,
    )
    add(
        "stage_jump.slp",
        "AUTH missing so HELLO jumps directly to NEGOTIATE",
        _write(target / "stage_jump.slp", jump),
        expected=(ErrorCode.E_TRANSITION, ErrorCode.E_INCOMPLETE),
    )

    builder = RecordBuilder()
    early_data = builder.session(11, 1)
    early_data = [
        record
        for record in early_data
        if record.phase not in (int(Phase.AUTH), int(Phase.NEGOTIATE))
    ]
    early_data = _mark(
        early_data,
        _record_with_sequence(early_data, 4).sync_id,
        ErrorCode.E_TRANSITION,
    )
    early_data = _mark(
        early_data,
        _record_with_sequence(early_data, 5).sync_id,
        ErrorCode.E_INCOMPLETE,
    )
    add(
        "auth_before_data.slp",
        "DATA appears before AUTH/NEGOTIATE",
        _write(target / "auth_before_data.slp", early_data),
        expected=(ErrorCode.E_TRANSITION, ErrorCode.E_INCOMPLETE),
    )

    builder = RecordBuilder()
    source = builder.session(1, 200)
    target_base = [
        builder.record(2, 100, Phase.HELLO),
        builder.record(2, 101, Phase.AUTH),
        builder.record(2, 303, Phase.DATA),
        builder.record(2, 304, Phase.CLOSE),
    ]
    source_negotiate = _record_with_sequence(source, 202)
    source = _mark(source, source_negotiate.sync_id, ErrorCode.E_MIXUP)
    clone = builder.record(
        2,
        202,
        Phase.NEGOTIATE,
        truth_mask=mask_for(ErrorCode.E_MIXUP),
        payload=b"mixin-from-session-1",
    )
    mixed = target_base + source + [clone]
    add(
        "mixup_cross_session.slp",
        "Session 2 imports session 1 packet at shared sequence 202",
        _write(target / "mixup_cross_session.slp", mixed),
        expected=(ErrorCode.E_MIXUP,),
    )

    builder = RecordBuilder()
    direction = builder.session(12, 1)
    direction_data = _record_with_sequence(direction, 4)
    direction = [
        replace(
            record,
            direction=1,
            truth_mask=record.truth_mask | mask_for(ErrorCode.E_MIXUP),
        )
        if record.sync_id == direction_data.sync_id
        else record
        for record in direction
    ]
    add(
        "mixup_direction.slp",
        "One packet has a contradictory direction marker",
        _write(target / "mixup_direction.slp", direction),
        expected=(ErrorCode.E_MIXUP,),
    )

    builder = RecordBuilder()
    no_base = [
        builder.record(13, 1, Phase.HELLO),
        builder.record(13, 2, Phase.AUTH),
        builder.record(
            13,
            3,
            Phase.NEGOTIATE,
            extensions=int(Extension.RENEGOTIATE),
            truth_mask=mask_for(ErrorCode.E_MIXUP),
        ),
        builder.record(13, 4, Phase.DATA),
        builder.record(13, 5, Phase.CLOSE),
    ]
    add(
        "reneg_without_base.slp",
        "RENEGOTIATE declaration lacks an earlier base NEGOTIATE",
        _write(target / "reneg_without_base.slp", no_base),
        expected=(ErrorCode.E_MIXUP,),
    )

    builder = RecordBuilder()
    missing_decl = [
        builder.record(14, 1, Phase.HELLO),
        builder.record(14, 2, Phase.AUTH),
        builder.record(14, 3, Phase.NEGOTIATE),
        builder.record(14, 4, Phase.DATA),
        builder.record(
            14,
            5,
            Phase.NEGOTIATE,
            truth_mask=mask_for(ErrorCode.E_MIXUP, ErrorCode.E_TRANSITION),
        ),
        builder.record(14, 6, Phase.DATA),
        builder.record(14, 7, Phase.CLOSE),
    ]
    add(
        "reneg_missing_declaration.slp",
        "Repeated NEGOTIATE does not declare the renegotiation extension",
        _write(target / "reneg_missing_declaration.slp", missing_decl),
        expected=(ErrorCode.E_MIXUP, ErrorCode.E_TRANSITION),
    )

    builder = RecordBuilder()
    storm_records = [
        builder.record(15, 1, Phase.HELLO),
        builder.record(15, 2, Phase.AUTH),
        builder.record(15, 3, Phase.NEGOTIATE),
        builder.record(15, 4, Phase.DATA),
    ]
    for cycle in range(5):
        sequence = 5 + cycle * 2
        renegotiation = builder.record(
            15,
            sequence,
            Phase.NEGOTIATE,
            extensions=int(Extension.RENEGOTIATE),
            truth_mask=(
                mask_for(ErrorCode.E_STORM) if cycle >= 3 else 0
            ),
        )
        storm_records.extend(
            [
                renegotiation,
                builder.record(15, sequence + 1, Phase.DATA),
            ]
        )
    storm_records.append(builder.record(15, 15, Phase.CLOSE))
    storm_records += builder.session(16, 100)
    add(
        "reneg_storm.slp",
        "Five renegotiations exceed the default limit 3; session 16 stays valid",
        _write(target / "reneg_storm.slp", storm_records),
        expected=(ErrorCode.E_STORM,),
    )

    builder = RecordBuilder()
    half_closed = builder.session(17, 1, close_extensions=int(Extension.HALF_CLOSE))
    half_closed.append(
        builder.record(
            17,
            6,
            Phase.DATA,
            truth_mask=mask_for(ErrorCode.E_AFTER_CLOSE),
            payload=b"after-half-close",
        )
    )
    add(
        "half_close_violation.slp",
        "DATA is sent after a HALF_CLOSE CLOSE packet",
        _write(target / "half_close_violation.slp", half_closed),
        expected=(ErrorCode.E_AFTER_CLOSE,),
    )

    builder = RecordBuilder()
    incomplete = builder.session(18, 1)
    incomplete = [record for record in incomplete if record.phase != int(Phase.CLOSE)]
    incomplete = _mark(
        incomplete,
        incomplete[-1].sync_id,
        ErrorCode.E_INCOMPLETE,
    )
    add(
        "incomplete.slp",
        "CLOSE is absent",
        _write(target / "incomplete.slp", incomplete),
        expected=(ErrorCode.E_INCOMPLETE,),
    )

    builder = RecordBuilder()
    version_two = builder.session(19, 1)
    add(
        "valid_v2.slp",
        "A valid version-2 session",
        _write(target / "valid_v2.slp", version_two, version=2),
    )

    builder = RecordBuilder()
    malformed = builder.session(20, 1)
    bad_phase = replace(
        malformed[0],
        phase=99,
        truth_mask=mask_for(ErrorCode.E_PHASE),
    )
    add(
        "invalid_phase.slp",
        "Unknown phase code is rejected by the parser",
        _write(target / "invalid_phase.slp", [bad_phase] + malformed[1:]),
        expected=(ErrorCode.E_PHASE,),
    )

    builder = RecordBuilder()
    malformed = builder.session(21, 1)
    bad_ext = replace(
        malformed[3],
        extensions=0x80,
        truth_mask=mask_for(ErrorCode.E_EXT),
    )
    add(
        "invalid_extension.slp",
        "Unknown extension bit is rejected by the parser",
        _write(target / "invalid_extension.slp", malformed[:3] + [bad_ext] + malformed[4:]),
        expected=(ErrorCode.E_EXT,),
    )

    builder = RecordBuilder()
    malformed = builder.session(22, 1)
    bad_combo = replace(
        malformed[3],
        extensions=int(Extension.HALF_CLOSE),
        truth_mask=mask_for(ErrorCode.E_EXT),
    )
    add(
        "invalid_ext_combination.slp",
        "HALF_CLOSE appears on DATA",
        _write(target / "invalid_ext_combination.slp", malformed[:3] + [bad_combo] + malformed[4:]),
        expected=(ErrorCode.E_EXT,),
    )

    builder = RecordBuilder()
    bad_magic_data = bytearray(encode_file(builder.session(23, 1)))
    bad_magic_data[0:4] = b"BAD!"
    add(
        "invalid_magic.slp",
        "Bad file magic",
        bytes(bad_magic_data),
        expected=(ErrorCode.E_MAGIC,),
    )

    builder = RecordBuilder()
    add(
        "invalid_version.slp",
        "Unsupported file version",
        _write(target / "invalid_version.slp", builder.session(24, 1), version=3),
        expected=(ErrorCode.E_VERSION,),
    )

    builder = RecordBuilder()
    truncated = bytearray(encode_file(builder.session(25, 1)))
    payload_length_offset = 12 + 24
    truncated[payload_length_offset : payload_length_offset + 4] = (0xFFFF).to_bytes(4, "little")
    add(
        "bounds_payload.slp",
        "Payload length extends beyond actual file length",
        bytes(truncated),
        expected=(ErrorCode.E_BOUNDS,),
    )

    builder = RecordBuilder()
    bad_total = bytearray(encode_file(builder.session(26, 1)))
    declared = int.from_bytes(bad_total[8:12], "little")
    bad_total[8:12] = (declared + 1).to_bytes(4, "little")
    add(
        "bounds_total_length.slp",
        "Declared total length exceeds actual file length",
        bytes(bad_total),
        expected=(ErrorCode.E_BOUNDS,),
    )

    atomic_write_json(target / "manifest.json", manifest, kind="slp-samples")
    return manifest
