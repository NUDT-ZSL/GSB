"""Strict OSL parser with nested expansion and deterministic diagnostics."""

from __future__ import annotations

import hashlib
import hmac as hmac_mod
import struct
import time
from dataclasses import dataclass, field
from typing import Any

from .canonical import UINT64_MAX, segment_key
from .codes import Code
from .errors import BoundsError, BudgetTimeout, HeaderError, OslError, QuotaExceeded
from .models import Diagnostic, ParseResult, SegmentInfo
from .policy import Policy


_MIN_RECORD_SIZE_PREFIX = 23
_MIN_RECORD_SIZE_FIXED = 21
_HASH_CHUNK = 64 * 1024


@dataclass
class _ParseState:
    file_id: str
    policy: Policy
    start: float
    timeout_seconds: float | None
    diagnostics: list[Diagnostic] = field(default_factory=list)
    segments: list[SegmentInfo] = field(default_factory=list)
    edges: list[tuple[str, str]] = field(default_factory=list)
    expanded_bytes: int = 0
    max_depth_reached: int = 0

    def time_left_expired(self, offset: int | None = None) -> None:
        if self.timeout_seconds is None:
            return
        deadline = self.start + self.timeout_seconds
        if time.perf_counter() >= deadline:
            raise BudgetTimeout(
                Code.E_TIMEOUT,
                f"wall-clock budget {self.timeout_seconds!r}s exceeded",
                offset,
                timeout_seconds=self.timeout_seconds,
                elapsed_seconds=time.perf_counter() - self.start,
            )


def parse_osl(
    data: bytes | bytearray | memoryview,
    file_id: str = "inline",
    policy: Policy | None = None,
    timeout_seconds: float | None = None,
) -> ParseResult:
    """Parse one top-level OSL document.

    The function never lets a structural error escape.  It returns a
    :class:`ParseResult` whose primary diagnostic is stable for marker checks.
    """

    raw = bytes(data)
    used_policy = policy or Policy()
    start = time.perf_counter()
    state = _ParseState(
        file_id=file_id,
        policy=used_policy,
        start=start,
        timeout_seconds=timeout_seconds,
    )
    result = ParseResult(
        file_id=file_id,
        ok=False,
        policy_fingerprint=used_policy.fingerprint(),
        header_layout=used_policy.header_layout.name,
        header_size=used_policy.header_layout.size,
    )

    try:
        _parse_document(
            raw,
            depth=0,
            base_offset=0,
            path_prefix=(),
            ancestors=(),
            state=state,
        )
        state.time_left_expired(0)
    except OslError as exc:
        state.diagnostics.append(
            Diagnostic(exc.code, exc.message, exc.offset, dict(exc.details))
        )
    except struct.error as exc:  # defensive: our bounds checks normally prevent this
        state.diagnostics.append(
            Diagnostic(Code.E_BOUNDS, f"binary decode failed: {exc}", len(raw))
        )

    elapsed = time.perf_counter() - start
    if (
        timeout_seconds is not None
        and elapsed > timeout_seconds
        and not any(d.code == Code.E_TIMEOUT for d in state.diagnostics)
    ):
        state.diagnostics.append(
            Diagnostic(
                Code.E_TIMEOUT,
                f"measured wall-clock {elapsed:.6f}s exceeds {timeout_seconds!r}s",
                0,
                {"elapsed_seconds": elapsed},
            )
        )

    result.diagnostics = state.diagnostics
    result.segments = state.segments
    result.edges = state.edges
    result.expanded_bytes = state.expanded_bytes
    result.max_depth_reached = state.max_depth_reached
    result.elapsed_seconds = elapsed
    result.ok = not state.diagnostics
    return result


def _parse_document(
    data: bytes,
    *,
    depth: int,
    base_offset: int,
    path_prefix: tuple[str, ...],
    ancestors: tuple[str, ...],
    state: _ParseState,
) -> list[SegmentInfo]:
    state.max_depth_reached = max(state.max_depth_reached, depth)
    local_size = len(data)

    def absolute(local_offset: int) -> int:
        return base_offset + local_offset

    state.time_left_expired(absolute(0))

    state.expanded_bytes += local_size
    if state.expanded_bytes > state.policy.max_expanded_bytes:
        raise QuotaExceeded(
            Code.E_QUOTA,
            "recursive expansion byte quota exceeded",
            absolute(0),
            expanded_bytes=state.expanded_bytes,
            quota=state.policy.max_expanded_bytes,
        )

    layout = state.policy.header_layout
    header_size = layout.size
    if local_size < header_size:
        raise HeaderError(
            Code.E_HEADER,
            f"truncated header: need {header_size}, have {local_size}",
            absolute(local_size),
            header_size=header_size,
            actual_size=local_size,
        )

    if data[0:4] != b"OSL1":
        raise HeaderError(
            Code.E_HEADER, "bad OSL magic", absolute(0), expected="OSL1", actual=data[0:4].hex()
        )

    version_offset = 4
    version = struct.unpack_from("<H", data, version_offset)[0]
    if version not in state.policy.allowed_format_versions:
        raise HeaderError(
            Code.E_HEADER,
            f"unsupported format version {version}",
            absolute(version_offset),
            version=version,
            allowed=list(state.policy.allowed_format_versions),
        )

    count_offset = 6
    count = struct.unpack_from("<I", data, count_offset)[0]
    table_offset_field = 10
    table_offset = struct.unpack_from("<Q", data, table_offset_field)[0]
    total_length_field = 18
    total_length = struct.unpack_from("<Q", data, total_length_field)[0]

    if total_length != local_size:
        raise HeaderError(
            Code.E_HEADER,
            "declared total length does not match actual document size",
            absolute(total_length_field),
            declared_total_length=total_length,
            actual_size=local_size,
        )

    if table_offset < header_size or table_offset > total_length:
        raise HeaderError(
            Code.E_HEADER,
            "segment table offset is outside the fixed header boundary",
            absolute(table_offset_field),
            table_offset=table_offset,
            header_size=header_size,
            total_length=total_length,
        )

    min_record = (
        _MIN_RECORD_SIZE_PREFIX
        if state.policy.digest_length_prefix
        else _MIN_RECORD_SIZE_FIXED
    )
    maximum_plausible_count = (total_length - table_offset) // min_record
    if count > maximum_plausible_count:
        raise HeaderError(
            Code.E_HEADER,
            "segment count is not self-consistent with table bounds",
            absolute(count_offset),
            segment_count=count,
            maximum_plausible_count=maximum_plausible_count,
            table_offset=table_offset,
            total_length=total_length,
        )

    records: list[SegmentInfo] = []
    cursor = table_offset
    for _index in range(count):
        state.time_left_expired(absolute(cursor))
        record = _parse_record(
            data,
            cursor,
            total_length=total_length,
            depth=depth,
            base_offset=base_offset,
            path_prefix=path_prefix,
            state=state,
        )
        records.append(record)
        state.segments.append(record)
        cursor = _record_end(record, state.policy.digest_length_prefix)

    table_end = cursor
    _check_duplicate_names(records, state)
    _check_self_reference(records, table_offset, table_end, state)
    _check_payload_overlaps(records, state)
    _process_payloads_and_refs(data, records, ancestors, state)
    _expand_nested(
        data,
        records,
        depth=depth,
        base_offset=base_offset,
        ancestors=ancestors,
        state=state,
    )
    return records


def _parse_record(
    data: bytes,
    cursor: int,
    *,
    total_length: int,
    depth: int,
    base_offset: int,
    path_prefix: tuple[str, ...],
    state: _ParseState,
) -> SegmentInfo:
    prefix = state.policy.digest_length_prefix

    def abs_offset(local_offset: int) -> int:
        return base_offset + local_offset

    if cursor + 2 > total_length:
        raise BoundsError(
            Code.E_BOUNDS,
            "segment name length exceeds document bounds",
            abs_offset(cursor),
            record_offset=cursor,
        )
    name_length_offset = cursor
    name_length = struct.unpack_from("<H", data, cursor)[0]
    name_offset = cursor + 2
    name_end = name_offset + name_length
    if name_end > total_length:
        raise BoundsError(
            Code.E_BOUNDS,
            "segment name bytes exceed document bounds",
            abs_offset(name_offset),
            name_length=name_length,
            name_end=name_end,
            total_length=total_length,
        )
    try:
        name = data[name_offset:name_end].decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        bad = name_offset + exc.start
        raise HeaderError(
            Code.E_HEADER,
            "segment name is not valid UTF-8",
            abs_offset(bad),
            name_offset=name_offset,
            unicode_error=str(exc),
        ) from exc

    type_offset = name_end
    compression_offset = type_offset + 1
    payload_field = compression_offset + 1
    length_field = payload_field + 8
    algo_offset = length_field + 8
    if algo_offset + 1 > total_length:
        raise BoundsError(
            Code.E_BOUNDS, "segment fixed record fields are truncated", abs_offset(type_offset)
        )

    segment_type = data[type_offset]
    compression = data[compression_offset]
    if segment_type not in state.policy.allowed_segment_types:
        raise HeaderError(
            Code.E_HEADER,
            f"segment type {segment_type} is not whitelisted",
            abs_offset(type_offset),
            segment_type=segment_type,
            allowed=list(state.policy.allowed_segment_types),
        )
    if compression not in state.policy.allowed_compressions:
        raise HeaderError(
            Code.E_HEADER,
            f"compression/dispatch method {compression} is not whitelisted",
            abs_offset(compression_offset),
            compression=compression,
            allowed=list(state.policy.allowed_compressions),
        )

    if payload_field + 8 > total_length or length_field + 8 > total_length:
        raise BoundsError(
            Code.E_BOUNDS,
            "payload offset/length fields are truncated",
            abs_offset(payload_field),
        )
    payload_offset = struct.unpack_from("<Q", data, payload_field)[0]
    declared_length = struct.unpack_from("<Q", data, length_field)[0]

    if algo_offset + 1 > total_length:
        raise BoundsError(Code.E_BOUNDS, "checksum algorithm field is truncated", abs_offset(algo_offset))
    algo = data[algo_offset]

    if prefix:
        digest_length_offset = algo_offset + 1
        digest_offset = digest_length_offset + 2
        if digest_length_offset + 2 > total_length:
            raise BoundsError(
                Code.E_BOUNDS,
                "digest length field is truncated",
                abs_offset(digest_length_offset),
            )
        digest_length = struct.unpack_from("<H", data, digest_length_offset)[0]
    else:
        digest_length_offset = None
        digest_length = state.policy.algorithm_bits.get(algo, 256) // 8
        digest_offset = algo_offset + 1

    digest_end = digest_offset + digest_length
    if digest_end > total_length:
        raise BoundsError(
            Code.E_BOUNDS,
            "declared digest bytes exceed document bounds",
            abs_offset(digest_offset),
            digest_length=digest_length,
            digest_end=digest_end,
            total_length=total_length,
        )

    if payload_offset > UINT64_MAX - declared_length:
        raise BoundsError(
            Code.E_BOUNDS,
            "payload offset plus length overflows uint64",
            abs_offset(payload_field),
            payload_offset=payload_offset,
            declared_length=declared_length,
        )
    payload_end = payload_offset + declared_length
    if payload_offset < state.policy.header_layout.size or payload_end > total_length:
        raise BoundsError(
            Code.E_BOUNDS,
            "declared payload interval is outside the OSL document",
            abs_offset(payload_field),
            payload_offset=payload_offset,
            payload_end=payload_end,
            declared_length=declared_length,
            total_length=total_length,
        )

    path_names = list(path_prefix) + [name]
    key = segment_key(path_names)
    return SegmentInfo(
        key=key,
        name=name,
        path_names=path_names,
        depth=depth,
        record_offset=abs_offset(name_length_offset),
        name_offset=abs_offset(name_offset),
        type_offset=abs_offset(type_offset),
        compression_offset=abs_offset(compression_offset),
        payload_offset_field_offset=abs_offset(payload_field),
        declared_length_field_offset=abs_offset(length_field),
        algo_offset=abs_offset(algo_offset),
        digest_length_offset=abs_offset(digest_length_offset) if digest_length_offset is not None else None,
        digest_offset=abs_offset(digest_offset),
        name_length=name_length,
        segment_type=segment_type,
        compression=compression,
        payload_offset=payload_offset,
        declared_length=declared_length,
        algo=algo,
        digest_length=digest_length,
        declared_digest=data[digest_offset:digest_end].hex(),
        base_offset=base_offset,
        absolute_payload_offset=abs_offset(payload_offset),
        payload_length_available=declared_length,
    )


def _record_end(record: SegmentInfo, prefix: bool) -> int:
    local_digest_offset = record.digest_offset - record.base_offset
    return local_digest_offset + record.digest_length


def _check_duplicate_names(records: list[SegmentInfo], state: _ParseState) -> None:
    first_by_name: dict[str, SegmentInfo] = {}
    for record in records:
        first = first_by_name.get(record.name)
        if first is None:
            first_by_name[record.name] = record
            continue
        record.duplicate_of = first.key
        record.status = Code.E_DUP_NAME
        record.error_code = Code.E_DUP_NAME
        record.error_offset = record.name_offset
        state.diagnostics.append(
            Diagnostic(
                Code.E_DUP_NAME,
                f"duplicate segment name {record.name!r}; first occurrence is authoritative",
                record.name_offset,
                {
                    "name": record.name,
                    "first_key": first.key,
                    "first_offset": first.name_offset,
                    "conflict_offset": record.name_offset,
                },
            )
        )


def _check_self_reference(
    records: list[SegmentInfo], table_offset: int, table_end: int, state: _ParseState
) -> None:
    for record in records:
        start = record.payload_offset
        end = start + record.declared_length
        if record.declared_length == 0:
            continue
        if start < table_end and table_offset < end:
            if record.status == Code.OK:
                record.status = Code.E_SELFREF
                record.error_code = Code.E_SELFREF
                record.error_offset = record.payload_offset_field_offset
            state.diagnostics.append(
                Diagnostic(
                    Code.E_SELFREF,
                    "segment payload references the segment table region",
                    record.payload_offset_field_offset,
                    {
                        "segment": record.key,
                        "table_start": table_offset,
                        "table_end": table_end,
                        "payload_start": start,
                        "payload_end": end,
                    },
                )
            )


def _check_payload_overlaps(records: list[SegmentInfo], state: _ParseState) -> None:
    for left_index, left in enumerate(records):
        left_start = left.payload_offset
        left_end = left_start + left.declared_length
        if left.declared_length == 0:
            continue
        for right in records[left_index + 1 :]:
            right_start = right.payload_offset
            right_end = right_start + right.declared_length
            if right.declared_length == 0:
                continue
            if not (left_start < right_end and right_start < left_end):
                continue

            if left_start <= right_start and right_end <= left_end:
                relation = "full_containment"
            elif right_start <= left_start and left_end <= right_end:
                relation = "full_containment"
            else:
                relation = "partial_cross"

            ordered_names = sorted((left.key, right.key))
            offset = min(
                left.payload_offset_field_offset,
                right.payload_offset_field_offset,
            )
            for record in (left, right):
                if record.status == Code.OK:
                    record.status = Code.E_OVERLAP
                    record.error_code = Code.E_OVERLAP
                    record.error_offset = offset
            state.diagnostics.append(
                Diagnostic(
                    Code.E_OVERLAP,
                    "payload intervals overlap",
                    offset,
                    {
                        "segments": ordered_names,
                        "wire_pair": [left.key, right.key],
                        "relation": relation,
                        "intervals": {
                            left.key: [left_start, left_end],
                            right.key: [right_start, right_end],
                        },
                    },
                )
            )


def _process_payloads_and_refs(
    data: bytes,
    records: list[SegmentInfo],
    ancestors: tuple[str, ...],
    state: _ParseState,
) -> None:
    expected_algo_by_compression = {0x00: 0, 0x01: 1, 0x02: 2, 0x10: 0}
    for record in records:
        payload = data[record.payload_offset : record.payload_offset + record.declared_length]
        record.payload_length_available = len(payload)
        record.content_digest = _chunk_digest(
            "sha256", payload, state, record.payload_offset_field_offset
        )

        algo_known = record.algo in state.policy.algorithm_bits
        if not algo_known or record.algo not in state.policy.allowed_algorithms:
            _mark_record_error(
                record,
                Code.E_CHECK_ALGO,
                record.algo_offset,
                state,
                f"unknown checksum algorithm {record.algo}",
                algorithm=record.algo,
                known_algorithms=list(state.policy.allowed_algorithms),
            )
            continue

        expected_bits = int(state.policy.algorithm_bits[record.algo])
        actual_bits = record.digest_length * 8
        if actual_bits != expected_bits:
            width_offset = record.digest_length_offset or record.algo_offset
            _mark_record_error(
                record,
                Code.E_CHECK_ALGO,
                width_offset,
                state,
                "declared digest width does not match algorithm",
                algorithm=record.algo,
                expected_bits=expected_bits,
                declared_bits=actual_bits,
            )
            continue

        expected_algo = expected_algo_by_compression.get(record.compression)
        if expected_algo is not None and record.algo != expected_algo:
            _mark_record_error(
                record,
                Code.E_CHECK_ALGO,
                record.algo_offset,
                state,
                "compression/dispatch method does not select the declared algorithm",
                compression=record.compression,
                expected_algorithm=expected_algo,
                algorithm=record.algo,
            )
            continue

        if record.compression == 0x00 or record.compression == 0x10:
            record.digest_valid = None
        elif record.compression == 0x01:
            actual = _chunk_digest(
                "sha256", payload, state, record.digest_offset
            )
            record.actual_digest = actual
            if not hmac_mod.compare_digest(actual, record.declared_digest):
                _mark_checksum_mismatch(record, state, actual)
            else:
                record.digest_valid = True
        elif record.compression == 0x02:
            actual = hmac_mod.new(
                state.policy.hmac_key, payload, hashlib.sha256
            ).hexdigest()
            record.actual_digest = actual
            if not hmac_mod.compare_digest(actual, record.declared_digest):
                _mark_checksum_mismatch(record, state, actual)
            else:
                record.digest_valid = True

        if record.segment_type == 3 and record.declared_length:
            _extract_refs(payload, record, ancestors, state)


def _mark_record_error(
    record: SegmentInfo,
    code: str,
    offset: int,
    state: _ParseState,
    message: str,
    **details: Any,
) -> None:
    detail_map = {"segment": record.key, **details}
    state.diagnostics.append(Diagnostic(code, message, offset, detail_map))
    if record.status == Code.OK:
        record.status = code
        record.error_code = code
        record.error_offset = offset


def _mark_checksum_mismatch(
    record: SegmentInfo, state: _ParseState, actual: str
) -> None:
    record.digest_valid = False
    if record.status == Code.OK:
        record.status = Code.E_CHECKSUM
        record.error_code = Code.E_CHECKSUM
        record.error_offset = record.digest_offset
    state.diagnostics.append(
        Diagnostic(
            Code.E_CHECKSUM,
            "declared digest does not match actual payload digest",
            record.digest_offset,
                {
                    "segment": record.key,
                    "expected_declared": record.declared_digest,
                    "actual_digest": actual,
                },
        )
    )


def _extract_refs(
    payload: bytes,
    record: SegmentInfo,
    ancestors: tuple[str, ...],
    state: _ParseState,
) -> None:
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError:
        record.refs = []
        return

    refs: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        refs.append(line)
        state.edges.append((record.key, line))

    record.refs = refs
    ancestor_set = set(ancestors)
    for target in refs:
        if target in ancestor_set:
            start_index = ancestors.index(target)
            cycle_keys = list(ancestors[start_index:]) + [record.key]
            if record.status == Code.OK:
                record.status = Code.E_CYCLE
                record.error_code = Code.E_CYCLE
                record.error_offset = record.absolute_payload_offset
            state.diagnostics.append(
                Diagnostic(
                    Code.E_CYCLE,
                    "segment reference closes a cycle through an ancestor",
                    record.absolute_payload_offset,
                    {
                        "segment": record.key,
                        "target": target,
                        "cycle": cycle_keys,
                    },
                )
            )


def _expand_nested(
    data: bytes,
    records: list[SegmentInfo],
    *,
    depth: int,
    base_offset: int,
    ancestors: tuple[str, ...],
    state: _ParseState,
) -> None:
    for record in records:
        if record.compression != 0x10:
            continue
        if depth >= state.policy.max_depth:
            record.status = Code.E_DEPTH
            record.error_code = Code.E_DEPTH
            record.error_offset = record.compression_offset
            raise HeaderError(
                Code.E_DEPTH,
                f"maximum recursion depth {state.policy.max_depth} exceeded",
                record.compression_offset,
                depth=depth + 1,
                max_depth=state.policy.max_depth,
                segment=record.key,
            )

        child = data[record.payload_offset : record.payload_offset + record.declared_length]
        child_records = _parse_document(
            child,
            depth=depth + 1,
            base_offset=base_offset + record.payload_offset,
            path_prefix=tuple(record.path_names),
            ancestors=ancestors + (record.key,),
            state=state,
        )
        for child_record in child_records:
            state.edges.append((record.key, child_record.key))


def _chunk_digest(
    algorithm: str,
    payload: bytes,
    state: _ParseState,
    offset: int,
) -> str:
    if algorithm == "sha256":
        hasher = hashlib.sha256()
    else:  # pragma: no cover - currently only SHA-256 chunk helper
        raise ValueError(algorithm)
    for start in range(0, len(payload), _HASH_CHUNK):
        state.time_left_expired(offset)
        hasher.update(payload[start : start + _HASH_CHUNK])
    return hasher.hexdigest()
