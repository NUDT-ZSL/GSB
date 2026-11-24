"""Connection-local fragment reassembly.

The preferred wire format is a small deterministic envelope (``TLSF``).  The
envelope carries the logical message id, declared message length, byte offset,
and a connection-specific stream tag.  A plain length-prefixed/JSON fallback is
also accepted so that generated and hand-authored fixtures can use either form.
"""

from __future__ import annotations

import hashlib
import json
import struct
from collections import OrderedDict
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from . import codes
from .budget import Deadline, ExpandedBudgetExceeded, FragmentLimitExceeded
from .models import Diagnostic, FragmentEnvelope, RawRecord, ReassembledMessage, ReassemblyResult


MAGIC = b"TLSF"
ENVELOPE_VERSION = 1
KIND_CERTIFICATE = 1
KIND_HANDSHAKE = 2
HEADER = struct.Struct(">4sBBHIII16s")
STREAM_TAG_BYTES = 16


def stream_tag_for(connection_id: str) -> bytes:
    return hashlib.blake2s(connection_id.encode("utf-8"), digest_size=STREAM_TAG_BYTES).digest()


def make_envelope(
    *,
    connection_id: str,
    message_id: int,
    kind: int,
    declared_length: int,
    offset: int,
    data: bytes,
) -> bytes:
    return HEADER.pack(
        MAGIC,
        ENVELOPE_VERSION,
        kind,
        0,
        message_id,
        declared_length,
        offset,
        stream_tag_for(connection_id),
    ) + data


def _compress_ranges(values: Iterable[int]) -> List[Tuple[int, int]]:
    ordered = sorted(set(values))
    if not ordered:
        return []
    ranges: List[Tuple[int, int]] = []
    start = prev = ordered[0]
    for value in ordered[1:]:
        if value == prev + 1:
            prev = value
            continue
        ranges.append((start, prev))
        start = prev = value
    ranges.append((start, prev))
    return ranges


def _missing_sequence_ranges(seqs: Iterable[int]) -> List[Tuple[int, int]]:
    ordered = sorted(set(seqs))
    if not ordered:
        return []
    missing: List[int] = []
    if ordered[0] > 0:
        missing.extend(range(0, ordered[0]))
    cursor = ordered[0]
    for value in ordered[1:]:
        if value > cursor + 1:
            missing.extend(range(cursor + 1, value))
        cursor = value
    return _compress_ranges(missing)


def _parse_envelope(record: RawRecord) -> Tuple[Optional[FragmentEnvelope], Optional[Diagnostic]]:
    if len(record.payload) < HEADER.size:
        return None, Diagnostic(
            code=codes.E_FRAG_FORMAT,
            message="fragment is shorter than the TLSF envelope header",
            source_file=record.source_file,
            record_index=record.line_no,
            connection_id=record.connection_id,
            message_seq=record.record_seq,
            detail={"payload_length": len(record.payload), "header_length": HEADER.size},
        )
    magic, version, kind, _flags, message_id, declared, offset, stream_tag = HEADER.unpack(
        record.payload[: HEADER.size]
    )
    if magic != MAGIC or version != ENVELOPE_VERSION:
        return None, Diagnostic(
            code=codes.E_FRAG_FORMAT,
            message="unsupported fragment envelope magic/version",
            source_file=record.source_file,
            record_index=record.line_no,
            connection_id=record.connection_id,
            message_seq=record.record_seq,
            detail={"magic": magic.hex(), "version": version},
        )
    data = record.payload[HEADER.size :]
    envelope = FragmentEnvelope(
        record=record,
        message_id=message_id,
        kind=kind,
        declared_length=declared,
        offset=offset,
        stream_tag=stream_tag.hex(),
        data=data,
    )
    return envelope, None


def _decode_message_payload(data: bytes) -> Tuple[Optional[Dict[str, Any]], Optional[Diagnostic], str]:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        return (
            None,
            Diagnostic(
                code=codes.E_MSG_JSON,
                message="message payload is not valid UTF-8",
                detail={"error": str(exc)},
            ),
            "",
        )
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        return (
            None,
            Diagnostic(
                code=codes.E_MSG_JSON,
                message="message payload is not valid JSON",
                detail={"error": exc.msg, "line": exc.lineno, "column": exc.colno},
            ),
            "",
        )
    if not isinstance(parsed, dict):
        return (
            None,
            Diagnostic(
                code=codes.E_MSG_JSON,
                message="message payload must be a JSON object",
            ),
            "",
        )
    kind = parsed.get("kind") or parsed.get("message_type") or ""
    return parsed, None, str(kind).lower()


def reassemble(
    records: Sequence[RawRecord],
    *,
    max_frags: Optional[int] = None,
    max_expanded_bytes: Optional[int] = None,
    deadline: Optional[Deadline] = None,
) -> ReassemblyResult:
    if deadline is not None:
        deadline.check("reassembly", records[0].record_seq if records else None)

    result = ReassemblyResult(fragment_count=len(records))
    result.expanded_bytes = sum(len(r.payload) for r in records)
    if max_frags is not None and len(records) > max_frags:
        raise FragmentLimitExceeded(
            "connection fragment count exceeds the configured limit",
            fragment_count=len(records),
            max_frags=max_frags,
        )
    if max_expanded_bytes is not None and result.expanded_bytes > max_expanded_bytes:
        raise ExpandedBudgetExceeded(
            "connection expanded byte budget is exhausted",
            expanded_bytes=result.expanded_bytes,
            max_expanded_bytes=max_expanded_bytes,
        )

    # "First occurrence wins" is defined by file arrival order, not by numeric
    # offset.  Sorting the surviving fragments later makes the reassembly itself
    # independent of their arrival order.
    first_by_seq: "OrderedDict[int, RawRecord]" = OrderedDict()
    duplicate_counts: Dict[int, int] = {}
    duplicate_conflicts = 0
    for record in sorted(records, key=lambda r: (r.line_no, r.arrival_index)):
        deadline and deadline.check("deduplicate", record.record_seq)
        previous = first_by_seq.get(record.record_seq)
        if previous is None:
            first_by_seq[record.record_seq] = record
            continue
        duplicate_counts[record.record_seq] = duplicate_counts.get(record.record_seq, 0) + 1
        if previous.payload != record.payload or previous.direction != record.direction:
            duplicate_conflicts += 1
    result.duplicate_count = sum(duplicate_counts.values())
    result.duplicate_sequences = sorted(duplicate_counts)
    result.duplicate_conflicts = duplicate_conflicts
    result.unique_fragment_count = len(first_by_seq)
    if result.duplicate_count:
        result.issues.append(
            Diagnostic(
                code=codes.E_FRAG_DUP,
                message="duplicate record sequence(s); the first occurrence is authoritative",
                severity="warning",
                connection_id=records[0].connection_id if records else None,
                message_seq=min(result.duplicate_sequences),
                detail={
                    "duplicate_count": result.duplicate_count,
                    "duplicate_sequences": list(result.duplicate_sequences),
                    "payload_conflicts": duplicate_conflicts,
                },
            )
        )

    unique_records = sorted(first_by_seq.values(), key=lambda r: r.record_seq)
    if not unique_records:
        result.status = "empty"
        result.complete = False
        return result

    rejected_seqs: set[int] = set()
    for record in unique_records:
        deadline and deadline.check("mixup_check", record.record_seq)
        if (
            record.embedded_connection_id is not None
            and record.embedded_connection_id != record.connection_id
        ):
            rejected_seqs.add(record.record_seq)
            result.mixup_records.append(record.record_seq)
            result.complete = False
            result.issues.append(
                Diagnostic(
                    code=codes.E_MIXUP,
                    message="fragment declares a different origin connection",
                    source_file=record.source_file,
                    record_index=record.line_no,
                    connection_id=record.connection_id,
                    message_seq=record.record_seq,
                    detail={
                        "origin_connection_id": record.embedded_connection_id,
                        "connection_id": record.connection_id,
                    },
                )
            )
    if rejected_seqs:
        unique_records = [r for r in unique_records if r.record_seq not in rejected_seqs]
    if not unique_records:
        result.status = "mixup"
        result.complete = False
        return result

    envelope_flags = [r.payload.startswith(MAGIC) for r in unique_records]
    envelope_mode = any(envelope_flags)
    result.envelope_mode = envelope_mode
    if envelope_mode and not all(envelope_flags):
        result.issues.append(
            Diagnostic(
                code=codes.E_FRAG_FORMAT,
                message="connection mixes TLSF envelope fragments with raw fragments",
                connection_id=unique_records[0].connection_id,
                message_seq=min(r.record_seq for r in unique_records),
            )
        )
        result.status = "error"
        result.complete = False

    if envelope_mode:
        accepted = _reassemble_envelopes(unique_records, result, deadline)
    else:
        accepted = _reassemble_raw(unique_records, result, deadline)

    accepted_seqs = sorted({r.record_seq for r in accepted})
    result.missing_sequence_ranges = _missing_sequence_ranges(accepted_seqs)
    if result.missing_sequence_ranges:
        result.complete = False
        if result.status == "ok":
            result.status = "gap"
        result.issues.append(
            Diagnostic(
                code=codes.E_FRAG_GAP,
                message="record sequence gap detected",
                connection_id=unique_records[0].connection_id,
                message_seq=result.missing_sequence_ranges[0][0],
                detail={
                    "missing_sequence_ranges": [
                        list(x) for x in result.missing_sequence_ranges
                    ]
                },
            )
        )
    elif result.missing_byte_ranges:
        result.complete = False
        if result.status == "ok":
            result.status = "gap"
        result.issues.append(
            Diagnostic(
                code=codes.E_FRAG_GAP,
                message="byte range gap detected inside a logical message",
                connection_id=unique_records[0].connection_id,
                message_seq=min(
                    (m.first_seq for m in result.messages), default=unique_records[0].record_seq
                ),
                detail={
                    "missing_byte_ranges": {
                        str(k): [list(x) for x in v]
                        for k, v in sorted(result.missing_byte_ranges.items())
                    }
                },
            )
        )

    if result.mixup_records and result.status == "ok":
        result.status = "mixup"
    if not result.complete and result.status == "ok":
        result.status = "error"
    return result


def _reassemble_envelopes(
    records: Sequence[RawRecord],
    result: ReassemblyResult,
    deadline: Optional[Deadline],
) -> List[RawRecord]:
    accepted: List[RawRecord] = []
    by_message: "OrderedDict[int, List[FragmentEnvelope]]" = OrderedDict()
    for record in records:
        deadline and deadline.check("parse_envelope", record.record_seq)
        expected_tag = stream_tag_for(record.connection_id)
        if not record.payload.startswith(MAGIC):
            continue
        envelope, issue = _parse_envelope(record)
        if issue is not None:
            issue.connection_id = record.connection_id
            result.issues.append(issue)
            continue
        assert envelope is not None
        if envelope.stream_tag != expected_tag.hex():
            result.mixup_records.append(record.record_seq)
            result.complete = False
            result.issues.append(
                Diagnostic(
                    code=codes.E_MIXUP,
                    message="fragment stream tag belongs to a different connection",
                    source_file=record.source_file,
                    record_index=record.line_no,
                    connection_id=record.connection_id,
                    message_seq=record.record_seq,
                    detail={
                        "stream_tag": envelope.stream_tag,
                        "expected_stream_tag": expected_tag.hex(),
                    },
                )
            )
            continue
        if envelope.end_offset > envelope.declared_length:
            result.issues.append(
                Diagnostic(
                    code=codes.E_FRAG_LEN,
                    message="fragment extends beyond the declared message length",
                    source_file=record.source_file,
                    record_index=record.line_no,
                    connection_id=record.connection_id,
                    message_seq=record.record_seq,
                    detail={
                        "offset": envelope.offset,
                        "fragment_length": len(envelope.data),
                        "declared_length": envelope.declared_length,
                    },
                )
            )
            result.complete = False
            continue
        accepted.append(record)
        by_message.setdefault(envelope.message_id, []).append(envelope)

    for message_id, fragments in by_message.items():
        deadline and deadline.check("assemble_message", fragments[0].record.record_seq)
        fragments = sorted(
            fragments,
            key=lambda f: (f.offset, f.record.record_seq, f.record.arrival_index),
        )
        declared = fragments[0].declared_length
        kinds = {f.kind for f in fragments}
        directions = {f.record.direction for f in fragments}
        if len(kinds) > 1:
            result.issues.append(
                Diagnostic(
                    code=codes.E_MIXUP,
                    message="logical message mixes fragment kinds",
                    connection_id=fragments[0].record.connection_id,
                    message_seq=fragments[0].record.record_seq,
                    detail={"message_id": message_id, "kinds": sorted(kinds)},
                )
            )
            result.status = "mixup"
            result.complete = False
        if len(directions) > 1:
            result.issues.append(
                Diagnostic(
                    code=codes.E_MIXUP,
                    message="logical message mixes directions",
                    connection_id=fragments[0].record.connection_id,
                    message_seq=fragments[0].record.record_seq,
                    detail={"message_id": message_id, "directions": sorted(directions)},
                )
            )
            result.status = "mixup"
            result.complete = False
        if any(f.declared_length != declared for f in fragments):
            result.issues.append(
                Diagnostic(
                    code=codes.E_FRAG_LEN,
                    message="fragments disagree on the declared message length",
                    connection_id=fragments[0].record.connection_id,
                    message_seq=fragments[0].record.record_seq,
                    detail={"message_id": message_id},
                )
            )
            result.complete = False
        declared = max(declared, max(f.declared_length for f in fragments))

        buffer = bytearray(declared)
        written = bytearray(declared)  # 1 byte per covered offset
        cursor = 0
        gaps: List[Tuple[int, int]] = []
        overlapped = False
        for fragment in fragments:
            deadline and deadline.check("assemble_fragment", fragment.record.record_seq)
            if fragment.offset < cursor:
                overlapped = True
                result.issues.append(
                    Diagnostic(
                        code=codes.E_FRAG_OVERLAP,
                        message="fragment byte ranges overlap",
                        source_file=fragment.record.source_file,
                        record_index=fragment.record.line_no,
                        connection_id=fragment.record.connection_id,
                        message_seq=fragment.record.record_seq,
                        detail={
                            "message_id": message_id,
                            "offset": fragment.offset,
                            "already_covered_until": cursor,
                        },
                    )
                )
            if fragment.offset > cursor:
                gaps.append((cursor, fragment.offset - 1))
            start = fragment.offset
            end = min(fragment.end_offset, declared)
            for index in range(start, end):
                if not written[index]:
                    buffer[index] = fragment.data[index - start]
                    written[index] = 1
            cursor = max(cursor, fragment.end_offset)
        if cursor < declared:
            gaps.append((cursor, declared - 1))
        if gaps:
            merged = _compress_ranges(
                index for start, end in gaps for index in range(start, end + 1)
            )
            result.missing_byte_ranges[message_id] = merged
        covered = sum(written)
        length_ok = covered == declared and not gaps and not overlapped
        if not length_ok and not gaps:
            result.issues.append(
                Diagnostic(
                    code=codes.E_FRAG_LEN,
                    message="reassembled message length differs from the declared length",
                    connection_id=fragments[0].record.connection_id,
                    message_seq=fragments[0].record.record_seq,
                    detail={
                        "message_id": message_id,
                        "declared_length": declared,
                        "covered_length": covered,
                    },
                )
            )
        if gaps:
            result.complete = False
        if overlapped:
            result.complete = False

        kind_name = "certificate" if kinds == {KIND_CERTIFICATE} else (
            "handshake" if kinds == {KIND_HANDSHAKE} else "other"
        )
        payload: Optional[Dict[str, Any]] = None
        if length_ok:
            payload, issue, json_kind = _decode_message_payload(bytes(buffer))
            if issue is not None:
                issue.connection_id = fragments[0].record.connection_id
                issue.message_seq = fragments[0].record.record_seq
                issue.detail.setdefault("message_id", message_id)
                result.issues.append(issue)
                result.complete = False
            else:
                if json_kind and kind_name != "other" and json_kind != kind_name:
                    result.issues.append(
                        Diagnostic(
                            code=codes.E_MSG_KIND,
                            message="envelope kind disagrees with JSON message kind",
                            connection_id=fragments[0].record.connection_id,
                            message_seq=fragments[0].record.record_seq,
                            detail={
                                "message_id": message_id,
                                "envelope_kind": kind_name,
                                "json_kind": json_kind,
                            },
                        )
                    )
                    result.complete = False
                if kind_name == "other" and json_kind in ("certificate", "handshake"):
                    kind_name = json_kind

        result.messages.append(
            ReassembledMessage(
                message_id=message_id,
                kind=kind_name,
                data=bytes(buffer),
                declared_length=declared,
                first_seq=min(f.record.record_seq for f in fragments),
                last_seq=max(f.record.record_seq for f in fragments),
                direction=fragments[0].record.direction,
                record_seqs=[f.record.record_seq for f in fragments],
                payload=payload,
                length_ok=length_ok,
                stream_tag=fragments[0].stream_tag,
            )
        )
    return accepted


def _reassemble_raw(
    records: Sequence[RawRecord],
    result: ReassemblyResult,
    deadline: Optional[Deadline],
) -> List[RawRecord]:
    directions = {r.direction for r in records}
    if len(directions) > 1:
        result.issues.append(
            Diagnostic(
                code=codes.E_MIXUP,
                message="raw connection stream mixes directions",
                connection_id=records[0].connection_id,
                message_seq=records[0].record_seq,
                detail={"directions": sorted(directions)},
            )
        )
        result.status = "mixup"
        result.complete = False
    blob = b"".join(r.payload for r in records)
    if not blob:
        result.status = "error"
        result.complete = False
        result.issues.append(
            Diagnostic(
                code=codes.E_FRAG_LEN,
                message="raw connection stream is empty",
                connection_id=records[0].connection_id,
            )
        )
        return list(records)

    # A single JSON object is the simplest valid raw form.
    try:
        parsed = json.loads(blob.decode("utf-8"))
        if isinstance(parsed, dict):
            kind = str(parsed.get("kind", "other")).lower()
            result.messages.append(
                ReassembledMessage(
                    message_id=0,
                    kind=kind if kind in ("certificate", "handshake") else "other",
                    data=blob,
                    declared_length=len(blob),
                    first_seq=records[0].record_seq,
                    last_seq=records[-1].record_seq,
                    direction=records[0].direction,
                    record_seqs=[r.record_seq for r in records],
                    payload=parsed,
                    length_ok=True,
                )
            )
            return list(records)
    except (UnicodeDecodeError, json.JSONDecodeError):
        pass

    # Otherwise use uint32 big-endian length-prefixed JSON frames.
    messages: List[ReassembledMessage] = []
    offset = 0
    frame_index = 0
    while offset < len(blob):
        deadline and deadline.check("parse_raw_frame")
        if offset + 4 > len(blob):
            result.issues.append(
                Diagnostic(
                    code=codes.E_FRAG_LEN,
                    message="raw frame header is truncated",
                    connection_id=records[0].connection_id,
                    detail={"offset": offset},
                )
            )
            result.complete = False
            break
        length = struct.unpack(">I", blob[offset : offset + 4])[0]
        offset += 4
        if offset + length > len(blob):
            result.issues.append(
                Diagnostic(
                    code=codes.E_FRAG_LEN,
                    message="raw frame payload is truncated",
                    connection_id=records[0].connection_id,
                    detail={
                        "frame": frame_index,
                        "declared_length": length,
                        "remaining": len(blob) - offset,
                    },
                )
            )
            result.complete = False
            break
        data = blob[offset : offset + length]
        offset += length
        payload, issue, json_kind = _decode_message_payload(data)
        if issue is not None:
            issue.connection_id = records[0].connection_id
            issue.detail.setdefault("frame", frame_index)
            result.issues.append(issue)
            result.complete = False
        messages.append(
            ReassembledMessage(
                message_id=frame_index,
                kind=json_kind if json_kind in ("certificate", "handshake") else "other",
                data=data,
                declared_length=length,
                first_seq=records[0].record_seq,
                last_seq=records[-1].record_seq,
                direction=records[0].direction,
                record_seqs=[r.record_seq for r in records],
                payload=payload,
                length_ok=len(data) == length,
            )
        )
        frame_index += 1
    if messages:
        result.messages.extend(messages)
    return list(records)


def primary_reassembly_code(result: ReassemblyResult) -> Optional[str]:
    priority = [
        codes.E_MIXUP,
        codes.E_FRAG_GAP,
        codes.E_FRAG_LEN,
        codes.E_FRAG_OVERLAP,
        codes.E_FRAG_FORMAT,
        codes.E_MSG_JSON,
        codes.E_MSG_KIND,
        codes.E_FRAG_DUP,
    ]
    present = {i.code for i in result.issues if i.severity != "warning"}
    for code in priority:
        if code in present:
            return code
    warnings = {i.code for i in result.issues}
    for code in priority:
        if code in warnings:
            return code
    return None
