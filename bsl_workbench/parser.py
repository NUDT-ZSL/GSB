from __future__ import annotations

import hashlib
import hmac as hmac_module
import json
import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from .codes import (
    DEFAULT_HMAC_KEY,
    DEFAULT_MAX_DEPTH,
    DEFAULT_MAX_EXPANDED_BYTES,
    DOCUMENTED_HEADER_SIZE,
    E_BOUNDS,
    E_CHECKSUM,
    E_COMPRESSION,
    E_CYCLE,
    E_DEPTH,
    E_DIGEST_WIDTH,
    E_DUP_NAME,
    E_EXPAND_LIMIT,
    E_HEADER,
    E_OVERLAP,
    E_SELFREF,
    KNOWN_SEGMENT_TYPES,
    MAGIC,
    RECORD_FIXED_FORMAT,
    RECORD_FIXED_SIZE,
    SUPPORTED_VERSIONS,
    UINT64_MAX,
)
from .util import join_key, leaf_name, safe_file_id, sha256_hex


REFS_MAGIC = b"BSLREFS1\n"


@dataclass
class ParseOptions:
    max_depth: int = DEFAULT_MAX_DEPTH
    max_expanded_bytes: int = DEFAULT_MAX_EXPANDED_BYTES
    hmac_key: bytes = DEFAULT_HMAC_KEY


@dataclass
class _Collector:
    options: ParseOptions
    diagnostics: list[dict[str, Any]] = field(default_factory=list)
    segments: list[dict[str, Any]] = field(default_factory=list)
    segment_by_key: dict[str, dict[str, Any]] = field(default_factory=dict)
    edges: list[dict[str, str]] = field(default_factory=list)
    edge_set: set[tuple[str, str, str]] = field(default_factory=set)
    node_keys: set[str] = field(default_factory=set)
    pending_references: list[dict[str, Any]] = field(default_factory=list)
    expanded_bytes: int = 0
    preflight_failed: bool = False

    def add_edge(self, source: str, target: str, kind: str) -> None:
        identity = (source, target, kind)
        if identity not in self.edge_set:
            self.edge_set.add(identity)
            self.edges.append({"from": source, "to": target, "kind": kind})
            self.node_keys.add(source)
            self.node_keys.add(target)


class _FatalParse(RuntimeError):
    def __init__(self, diagnostic: dict[str, Any]):
        super().__init__(diagnostic["message"])
        self.diagnostic = diagnostic


def _checked_end(offset: int, length: int) -> Optional[int]:
    end = offset + length
    if offset > UINT64_MAX or length > UINT64_MAX or end > UINT64_MAX:
        return None
    return end


def _diagnostic(
    code: str,
    message: str,
    *,
    offset: Optional[int] = None,
    severity: str = "error",
    depth: int = 0,
    nested_path: Optional[str] = None,
    **details: Any,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "code": code,
        "severity": severity,
        "message": message,
        "offset": offset,
        "depth": depth,
    }
    if nested_path is not None:
        result["nested_path"] = nested_path
    if details:
        result["details"] = details
    return result


def _parse_ref_payload(payload: bytes) -> list[str]:
    if payload.startswith(REFS_MAGIC):
        text = payload[len(REFS_MAGIC) :].decode("utf-8")
        refs: list[str] = []
        for line in text.splitlines():
            token = line.strip()
            if token and not token.startswith("#"):
                refs.append(token)
        return refs

    value = json.loads(payload.decode("utf-8"))
    if isinstance(value, list):
        raw_refs = value
    elif isinstance(value, dict) and isinstance(value.get("refs"), list):
        raw_refs = value["refs"]
    else:
        raise ValueError("reference list must be a JSON array or {'refs': [...]}")
    return [str(item).strip() for item in raw_refs if str(item).strip()]


def _resolve_reference(
    reference: str,
    parent_key: str,
    ancestors: tuple[tuple[str, str], ...],
    collector: _Collector,
) -> str:
    if reference.startswith("/"):
        return reference

    sibling_key = join_key(parent_key, reference) if parent_key else "/" + reference
    if sibling_key in collector.segment_by_key:
        return sibling_key

    for ancestor_key, ancestor_name in reversed(ancestors):
        if reference == ancestor_name or reference == leaf_name(ancestor_key):
            return ancestor_key

    for key, segment in collector.segment_by_key.items():
        if segment.get("name") == reference:
            return key

    return "external:" + reference


def _resolve_pending_references(collector: _Collector) -> None:
    for pending in collector.pending_references:
        targets: list[str] = []
        for reference in pending["refs"]:
            target = _resolve_reference(
                reference,
                pending["parent_key"],
                pending["ancestors"],
                collector,
            )
            targets.append(target)
            collector.add_edge(pending["key"], target, "reference")
        segment = collector.segment_by_key.get(pending["key"])
        if segment is not None:
            segment["references"] = targets


def parse_bytes(
    data: bytes,
    *,
    file_id: str = "local",
    path: Optional[str] = None,
    options: Optional[ParseOptions] = None,
) -> dict[str, Any]:
    options = options or ParseOptions()
    collector = _Collector(options=options)

    try:
        _parse_level(
            data,
            depth=0,
            parent_key="",
            nested_path="",
            ancestors=(),
            collector=collector,
        )
    except _FatalParse:
        pass

    _resolve_pending_references(collector)
    collector.diagnostics.extend(_detect_cycles(collector))

    diagnostics = sorted(
        collector.diagnostics,
        key=lambda item: (
            item.get("code") or "",
            item.get("depth") if item.get("depth") is not None else -1,
            item.get("offset") if item.get("offset") is not None else -1,
            json.dumps(item.get("details", {}), sort_keys=True, ensure_ascii=False),
            item.get("message") or "",
        ),
    )
    segments = sorted(collector.segments, key=lambda item: item["canonical_key"])
    edges = sorted(collector.edges, key=lambda item: (item["from"], item["to"], item["kind"]))
    nodes = sorted(collector.node_keys)
    error_codes = sorted(
        {item["code"] for item in diagnostics if item.get("severity") == "error"}
    )

    report: dict[str, Any] = {
        "schema_version": "bsl-report-v1",
        "ok": not error_codes,
        "preflight_failed": collector.preflight_failed,
        "file": {
            "file_id": file_id,
            "path": path,
            "size": len(data),
            "sha256": sha256_hex(data),
        },
        "policy": {
            "max_depth": options.max_depth,
            "max_expanded_bytes": options.max_expanded_bytes,
            "segment_types": sorted(KNOWN_SEGMENT_TYPES),
        },
        "summary": {
            "segment_count": len(segments),
            "diagnostic_count": len(diagnostics),
            "error_count": len(error_codes),
            "error_codes": error_codes,
            "expanded_bytes": collector.expanded_bytes,
            "node_count": len(nodes),
            "edge_count": len(edges),
        },
        "diagnostics": diagnostics,
        "segments": segments,
        "dependencies": {"nodes": nodes, "edges": edges},
    }
    return report


def parse_file(
    path: str | Path,
    *,
    file_id: Optional[str] = None,
    options: Optional[ParseOptions] = None,
) -> dict[str, Any]:
    path_obj = Path(path)
    data = path_obj.read_bytes()
    resolved_id = file_id or safe_file_id(path_obj)
    return parse_bytes(data, file_id=resolved_id, path=str(path_obj), options=options)


def _parse_level(
    data: bytes,
    *,
    depth: int,
    parent_key: str,
    nested_path: str,
    ancestors: tuple[tuple[str, str], ...],
    collector: _Collector,
) -> list[dict[str, Any]]:
    def fatal(
        code: str,
        message: str,
        *,
        offset: Optional[int],
        **details: Any,
    ) -> None:
        diagnostic = _diagnostic(
            code,
            message,
            offset=offset,
            depth=depth,
            nested_path=nested_path or None,
            fatal=True,
            **details,
        )
        collector.diagnostics.append(diagnostic)
        collector.preflight_failed = True
        raise _FatalParse(diagnostic)

    data_len = len(data)
    if data_len < DOCUMENTED_HEADER_SIZE:
        fatal(
            E_HEADER,
            "BSL fixed header is truncated",
            offset=data_len,
            expected_size=DOCUMENTED_HEADER_SIZE,
            actual_size=data_len,
        )

    try:
        magic, version, count, table_offset, declared_total = struct.unpack_from(
            "<4sHIQQ", data, 0
        )
    except struct.error as exc:
        fatal(E_HEADER, "failed to unpack fixed header fields", offset=0, reason=str(exc))

    if magic != MAGIC:
        fatal(
            E_HEADER,
            "invalid BSL magic",
            offset=0,
            expected=MAGIC.decode("ascii"),
            actual=magic.hex(),
        )

    if version not in SUPPORTED_VERSIONS:
        fatal(
            E_HEADER,
            "unsupported BSL format version",
            offset=4,
            supported=list(SUPPORTED_VERSIONS),
            actual=version,
        )

    if table_offset < DOCUMENTED_HEADER_SIZE:
        fatal(
            E_HEADER,
            "segment table offset overlaps fixed header",
            offset=10,
            minimum=DOCUMENTED_HEADER_SIZE,
            actual=table_offset,
        )

    if declared_total != data_len:
        fatal(
            E_HEADER,
            "declared total length does not match actual BSL length",
            offset=18,
            declared_total=declared_total,
            actual_length=data_len,
        )

    if table_offset > data_len:
        fatal(
            E_BOUNDS,
            "segment table offset is beyond file length",
            offset=10,
            table_offset=table_offset,
            actual_length=data_len,
        )

    minimum_record_size = 2 + RECORD_FIXED_SIZE
    minimum_table_end = _checked_end(table_offset, count * minimum_record_size)
    if minimum_table_end is None:
        fatal(
            E_BOUNDS,
            "table offset plus minimum table size overflows uint64",
            offset=10,
            table_offset=table_offset,
            minimum_table_size=count * minimum_record_size,
        )
    if minimum_table_end > data_len:
        fatal(
            E_BOUNDS,
            "segment count and table offset exceed file length",
            offset=6,
            count=count,
            table_offset=table_offset,
            minimum_table_end=minimum_table_end,
            actual_length=data_len,
        )

    raw_records: list[dict[str, Any]] = []
    position = table_offset
    for index in range(count):
        record_offset = position
        if position + 2 > data_len:
            fatal(
                E_BOUNDS,
                "segment name length field is beyond file length",
                offset=position,
                segment_index=index,
            )

        name_length = struct.unpack_from("<H", data, position)[0]
        record_size = 2 + name_length + RECORD_FIXED_SIZE
        record_end = _checked_end(position, record_size)
        if record_end is None:
            fatal(
                E_BOUNDS,
                "segment record size overflows uint64",
                offset=position,
                segment_index=index,
                record_size=record_size,
            )
        if record_end > data_len:
            fatal(
                E_BOUNDS,
                "segment record extends beyond file length",
                offset=position,
                segment_index=index,
                record_end=record_end,
                actual_length=data_len,
            )

        name_offset = position + 2
        name_bytes = data[name_offset : name_offset + name_length]
        try:
            name = name_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            fatal(
                E_HEADER,
                "segment name is not valid UTF-8",
                offset=name_offset,
                segment_index=index,
                reason=str(exc),
            )

        fixed_start = name_offset + name_length
        seg_type, compression, payload_offset, payload_length, digest_declared = struct.unpack_from(
            RECORD_FIXED_FORMAT, data, fixed_start
        )
        payload_offset_field = fixed_start + 2
        payload_length_field = fixed_start + 10
        digest_offset = fixed_start + 18

        if seg_type not in KNOWN_SEGMENT_TYPES:
            fatal(
                E_HEADER,
                "unknown segment type",
                offset=fixed_start,
                segment_index=index,
                segment_name=name,
                segment_type=seg_type,
                known_types=sorted(KNOWN_SEGMENT_TYPES),
            )

        raw_records.append(
            {
                "index": index,
                "name": name,
                "record_offset": record_offset,
                "record_end": record_end,
                "fixed_start": fixed_start,
                "compression_offset": fixed_start + 1,
                "payload_offset_field": payload_offset_field,
                "digest_offset": digest_offset,
                "seg_type": seg_type,
                "compression": compression,
                "payload_offset": payload_offset,
                "payload_length": payload_length,
                "digest_declared": digest_declared,
                "bounds_ok": False,
                "payload_end": None,
            }
        )
        position = record_end

    table_end = position

    first_by_name: dict[str, dict[str, Any]] = {}
    canonical_raw: list[dict[str, Any]] = []
    for raw in sorted(raw_records, key=lambda item: item["record_offset"]):
        name = raw["name"]
        if name in first_by_name:
            first = first_by_name[name]
            collector.diagnostics.append(
                _diagnostic(
                    E_DUP_NAME,
                    "duplicate segment name; first occurrence is the truth",
                    offset=raw["record_offset"],
                    depth=depth,
                    nested_path=nested_path or None,
                    segment=name,
                    first_offset=first["record_offset"],
                    duplicate_offset=raw["record_offset"],
                )
            )
            continue
        first_by_name[name] = raw
        canonical_raw.append(raw)

    for raw in raw_records:
        payload_end = _checked_end(raw["payload_offset"], raw["payload_length"])
        raw["payload_end"] = payload_end
        if payload_end is None:
            collector.diagnostics.append(
                _diagnostic(
                    E_BOUNDS,
                    "payload offset plus length overflows uint64",
                    offset=raw["payload_offset_field"],
                    depth=depth,
                    nested_path=nested_path or None,
                    segment=raw["name"],
                    payload_offset=raw["payload_offset"],
                    payload_length=raw["payload_length"],
                )
            )
        elif payload_end > data_len:
            collector.diagnostics.append(
                _diagnostic(
                    E_BOUNDS,
                    "payload interval extends beyond actual BSL length",
                    offset=raw["payload_offset_field"],
                    depth=depth,
                    nested_path=nested_path or None,
                    segment=raw["name"],
                    payload_offset=raw["payload_offset"],
                    payload_length=raw["payload_length"],
                    payload_end=payload_end,
                    actual_length=data_len,
                )
            )
        else:
            raw["bounds_ok"] = True

    for raw in raw_records:
        if not raw["bounds_ok"]:
            continue
        start = raw["payload_offset"]
        end = raw["payload_end"]
        if start < table_end and table_offset < end:
            collector.diagnostics.append(
                _diagnostic(
                    E_SELFREF,
                    "payload references the segment table region",
                    offset=raw["payload_offset_field"],
                    depth=depth,
                    nested_path=nested_path or None,
                    segment=raw["name"],
                    segment_pair=[raw["name"], "__segment_table__"],
                    payload_offset=start,
                    payload_end=end,
                    table_offset=table_offset,
                    table_end=table_end,
                )
            )

    bounded_records = [raw for raw in raw_records if raw["bounds_ok"]]
    for left_index in range(len(bounded_records)):
        left = bounded_records[left_index]
        for right_index in range(left_index + 1, len(bounded_records)):
            right = bounded_records[right_index]
            left_start, left_end = left["payload_offset"], left["payload_end"]
            right_start, right_end = right["payload_offset"], right["payload_end"]
            if not (left_start < right_end and right_start < left_end):
                continue

            if left_start == right_start and left_end == right_end:
                overlap_kind = "equal"
            elif left_start <= right_start and left_end >= right_end:
                overlap_kind = "contains"
            elif right_start <= left_start and right_end >= left_end:
                overlap_kind = "contained_by"
            else:
                overlap_kind = "partial"

            pair = sorted([left["name"], right["name"]])
            collector.diagnostics.append(
                _diagnostic(
                    E_OVERLAP,
                    "payload intervals overlap",
                    offset=min(left["payload_offset_field"], right["payload_offset_field"]),
                    depth=depth,
                    nested_path=nested_path or None,
                    segment_pair=pair,
                    overlap_kind=overlap_kind,
                    intervals=sorted(
                        [
                            {"segment": left["name"], "start": left_start, "end": left_end},
                            {"segment": right["name"], "start": right_start, "end": right_end},
                        ],
                        key=lambda item: item["segment"],
                    ),
                )
            )

    local_segments: list[dict[str, Any]] = []
    for raw in sorted(canonical_raw, key=lambda item: item["record_offset"]):
        name = raw["name"]
        key = join_key(parent_key, name)
        collector.node_keys.add(key)

        payload = b""
        content_sha256: Optional[str] = None
        if raw["bounds_ok"]:
            payload = data[raw["payload_offset"] : raw["payload_end"]]
            content_sha256 = sha256_hex(payload)

        state = "valid"
        digest_status = "not_verified"
        actual_digest: Optional[str] = None
        refs: list[str] = []

        if not raw["bounds_ok"]:
            state = "bounds_error"
            digest_status = "unavailable"
        elif raw["compression"] not in (0, 1, 2):
            digest_status = "unsupported_compression"
            state = "compression_error"
            collector.diagnostics.append(
                _diagnostic(
                    E_COMPRESSION,
                    "unknown compression method",
                    offset=raw["compression_offset"],
                    depth=depth,
                    nested_path=nested_path or None,
                    segment=name,
                    canonical_key=key,
                    compression=raw["compression"],
                    known_compression=[0, 1, 2],
                )
            )
        else:
            declared = raw["digest_declared"]
            declared_all_zero = not any(declared)
            if raw["compression"] in (1, 2):
                if declared_all_zero:
                    digest_status = "width_mismatch"
                    state = "digest_width_error"
                    collector.diagnostics.append(
                        _diagnostic(
                            E_DIGEST_WIDTH,
                            "declared digest width does not match compression method",
                            offset=raw["digest_offset"],
                            depth=depth,
                            nested_path=nested_path or None,
                            segment=name,
                            canonical_key=key,
                            expected_bits=256,
                            declared_bits=0,
                            compression=raw["compression"],
                        )
                    )
                else:
                    if raw["compression"] == 1:
                        actual = hashlib.sha256(payload).digest()
                        algorithm = "sha256"
                    else:
                        actual = hmac_module.new(
                            collector.options.hmac_key, payload, hashlib.sha256
                        ).digest()
                        algorithm = "hmac-sha256"
                    actual_digest = actual.hex()
                    if not hmac_module.compare_digest(actual, declared):
                        digest_status = "mismatch"
                        state = "checksum_mismatch"
                        collector.diagnostics.append(
                            _diagnostic(
                                E_CHECKSUM,
                                "content digest mismatch",
                                offset=raw["digest_offset"],
                                depth=depth,
                                nested_path=nested_path or None,
                                segment=name,
                                canonical_key=key,
                                algorithm=algorithm,
                                declared_digest=declared.hex(),
                                actual_digest=actual_digest,
                            )
                        )
                    else:
                        digest_status = "verified"
            else:
                digest_status = "not_verified"

        if raw["bounds_ok"] and raw["seg_type"] == 0x20:
            try:
                refs = _parse_ref_payload(payload)
            except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
                collector.diagnostics.append(
                    _diagnostic(
                        "W_REFERENCE",
                        "reference list payload could not be parsed",
                        severity="warning",
                        offset=raw["payload_offset_field"],
                        depth=depth,
                        nested_path=nested_path or None,
                        segment=name,
                        canonical_key=key,
                        reason=str(exc),
                    )
                )
                refs = []

        watermark = max(raw["record_end"], raw["payload_end"] or raw["record_end"])
        segment_info = {
            "canonical_key": key,
            "name": name,
            "depth": depth,
            "nested_path": nested_path or None,
            "segment_type": raw["seg_type"],
            "segment_type_name": KNOWN_SEGMENT_TYPES.get(raw["seg_type"], "unknown"),
            "compression": raw["compression"],
            "record_offset": raw["record_offset"],
            "record_end": raw["record_end"],
            "payload_offset": raw["payload_offset"],
            "payload_length": raw["payload_length"],
            "payload_end": raw["payload_end"],
            "watermark": watermark,
            "declared_digest": raw["digest_declared"].hex(),
            "actual_digest": actual_digest,
            "content_sha256": content_sha256,
            "digest_status": digest_status,
            "state": state,
            "references": [],
        }
        collector.segments.append(segment_info)
        collector.segment_by_key[key] = segment_info
        local_segments.append(segment_info)
        if refs:
            collector.pending_references.append(
                {
                    "key": key,
                    "parent_key": parent_key,
                    "ancestors": ancestors,
                    "refs": refs,
                }
            )

    for segment in local_segments:
        if segment["segment_type"] != 0x10:
            continue
        key = segment["canonical_key"]
        raw = first_by_name[segment["name"]]
        if not raw["bounds_ok"]:
            continue

        child_depth = depth + 1
        if child_depth > collector.options.max_depth:
            segment["state"] = "depth_exceeded"
            collector.diagnostics.append(
                _diagnostic(
                    E_DEPTH,
                    "nested BSL recursion depth exceeds limit",
                    offset=raw["payload_offset_field"],
                    depth=depth,
                    nested_path=nested_path or None,
                    segment=segment["name"],
                    canonical_key=key,
                    child_depth=child_depth,
                    max_depth=collector.options.max_depth,
                )
            )
            continue

        expansion = raw["payload_length"]
        if collector.expanded_bytes + expansion > collector.options.max_expanded_bytes:
            segment["state"] = "expand_limit"
            collector.diagnostics.append(
                _diagnostic(
                    E_EXPAND_LIMIT,
                    "cumulative nested expansion byte quota exceeded",
                    offset=raw["payload_offset_field"],
                    depth=depth,
                    nested_path=nested_path or None,
                    segment=segment["name"],
                    canonical_key=key,
                    expansion_bytes=expansion,
                    expanded_bytes_before=collector.expanded_bytes,
                    attempted_total=collector.expanded_bytes + expansion,
                    max_expanded_bytes=collector.options.max_expanded_bytes,
                )
            )
            continue

        collector.expanded_bytes += expansion
        child_nested_path = (
            (nested_path + "/" + segment["name"]) if nested_path else "/" + segment["name"]
        )
        child_segments = _parse_level(
            data[raw["payload_offset"] : raw["payload_end"]],
            depth=child_depth,
            parent_key=key,
            nested_path=child_nested_path,
            ancestors=ancestors + ((key, segment["name"]),),
            collector=collector,
        )
        for child in child_segments:
            collector.add_edge(key, child["canonical_key"], "nesting")

    return local_segments


def _cycle_rotation(cycle: list[str]) -> tuple[str, ...]:
    nodes = cycle[:-1]
    if not nodes:
        return tuple(cycle)
    rotations = []
    for index in range(len(nodes)):
        rotations.append(tuple(nodes[index:] + nodes[:index]))
    return min(rotations)


def _detect_cycles(collector: _Collector) -> list[dict[str, Any]]:
    adjacency: dict[str, list[str]] = {}
    for edge in collector.edges:
        adjacency.setdefault(edge["from"], []).append(edge["to"])
        adjacency.setdefault(edge["to"], [])
    for values in adjacency.values():
        values.sort()

    color: dict[str, int] = {key: 0 for key in adjacency}
    stack: list[str] = []
    on_stack: set[str] = set()
    found: dict[tuple[str, ...], list[str]] = {}

    def visit(node: str) -> None:
        color[node] = 1
        stack.append(node)
        on_stack.add(node)
        for target in adjacency.get(node, []):
            if target not in color:
                color[target] = 0
            if color[target] == 0:
                visit(target)
            elif target in on_stack:
                start = stack.index(target)
                cycle = stack[start:] + [target]
                found[_cycle_rotation(cycle)] = cycle
        stack.pop()
        on_stack.remove(node)
        color[node] = 2

    for node in sorted(adjacency):
        if color.get(node, 0) == 0:
            visit(node)

    diagnostics: list[dict[str, Any]] = []
    for signature in sorted(found):
        cycle_keys = found[signature]
        referencing_key = cycle_keys[-2] if len(cycle_keys) >= 2 else cycle_keys[0]
        segment = collector.segment_by_key.get(referencing_key, {})
        cycle_names = [
            leaf_name(key) if not key.startswith("external:") else key
            for key in cycle_keys
        ]
        diagnostics.append(
            _diagnostic(
                E_CYCLE,
                "reference/nesting graph contains a cycle",
                offset=segment.get("record_offset"),
                depth=segment.get("depth", 0),
                segment=segment.get("name"),
                canonical_key=referencing_key,
                cycle_keys=cycle_keys,
                cycle=cycle_names,
            )
        )
    return diagnostics
