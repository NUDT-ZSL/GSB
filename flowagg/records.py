from __future__ import annotations

import hashlib
import ipaddress
import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .errors import FlowAggError, fail
from .plan import MAX_I64, AggregationPlan


_LOOPBACK_ID = re.compile(r"^(?:lo-|loopback-)[A-Za-z0-9_.:-]+$")
_PORT_RE = re.compile(r"^\d{1,5}$")
_HOST_PORT_RE = re.compile(r"^(?P<host>\[[^]]+\]|[^:]+):(?P<port>\d{1,5})$")
_LO_PREFIX_RE = re.compile(r"^lo:(?P<rest>.+)$")


@dataclass(frozen=True, slots=True)
class FlowRecord:
    record_seq: int
    clock_ms: int
    src_endpoint: str
    dst_endpoint: str
    protocol: str
    direction: str
    bytes: int
    packets: int

    def canonical(self) -> dict[str, Any]:
        return {
            "bytes": self.bytes,
            "clock_ms": self.clock_ms,
            "direction": self.direction,
            "dst_endpoint": self.dst_endpoint,
            "packets": self.packets,
            "protocol": self.protocol,
            "record_seq": self.record_seq,
            "src_endpoint": self.src_endpoint,
        }


@dataclass(frozen=True, slots=True)
class BatchData:
    batch_id: str
    path: Path
    records: tuple[FlowRecord, ...]
    digest: str


def _require_int(obj: dict[str, Any], name: str, index: int, *, nonnegative: bool) -> int:
    value = obj.get(name)
    if isinstance(value, float) and not math.isfinite(value):
        raise fail(
            "E_NAN_OR_OVERFLOW",
            f"record {index} field {name} is NaN or infinite",
            record_index=index,
            field=name,
            policy="reject_non_finite",
        )
    if type(value) is not int:
        raise fail(
            "E_RECORD_INVALID",
            f"record {index} field {name} must be an integer",
            record_index=index,
            field=name,
        )
    if nonnegative and value < 0:
        raise fail(
            "E_RECORD_INVALID",
            f"record {index} field {name} must be non-negative",
            record_index=index,
            field=name,
            value=value,
        )
    if value > MAX_I64:
        raise fail(
            "E_NAN_OR_OVERFLOW",
            f"record {index} field {name} exceeds signed 64-bit range",
            record_index=index,
            field=name,
            value=value,
            max=MAX_I64,
        )
    return value


def _require_text(obj: dict[str, Any], name: str, index: int) -> str:
    value = obj.get(name)
    if not isinstance(value, str) or not value.strip():
        raise fail(
            "E_RECORD_INVALID",
            f"record {index} field {name} must be a non-empty string",
            record_index=index,
            field=name,
        )
    if len(value) > 256:
        raise fail(
            "E_RECORD_INVALID",
            f"record {index} field {name} is too long",
            record_index=index,
            field=name,
        )
    return value.strip()


def _is_loopback_host(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def validate_loopback_endpoint(value: str) -> None:
    if _LOOPBACK_ID.fullmatch(value):
        return
    candidate = value
    match = _LO_PREFIX_RE.match(candidate)
    if match:
        candidate = match.group("rest")
    host_port = _HOST_PORT_RE.match(candidate)
    if not host_port:
        raise fail(
            "E_ENDPOINT_INVALID",
            f"endpoint {value!r} is not a valid loopback endpoint",
            endpoint=value,
            accepted=["lo:127.0.0.1:12345", "[::1]:12345", "lo-[id]"],
        )
    host = host_port.group("host")
    port = host_port.group("port")
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    if not _PORT_RE.fullmatch(port) or not 0 <= int(port) <= 65535:
        raise fail(
            "E_ENDPOINT_INVALID",
            f"endpoint {value!r} has an invalid port",
            endpoint=value,
        )
    if not _is_loopback_host(host):
        raise fail(
            "E_ENDPOINT_INVALID",
            f"endpoint {value!r} does not use a loopback host",
            endpoint=value,
        )


def parse_record_line(line: str, plan: AggregationPlan, index: int) -> FlowRecord:
    def reject_constant(value: str) -> None:
        raise fail(
            "E_NAN_OR_OVERFLOW",
            f"record {index} contains non-finite JSON number {value!r}",
            record_index=index,
            policy="reject_non_finite",
            value=value,
        )

    try:
        obj = json.loads(line, parse_constant=reject_constant)
    except FlowAggError:
        raise
    except Exception as exc:
        raise fail(
            "E_RECORD_INVALID",
            f"record {index} is not valid JSON: {exc}",
            record_index=index,
        ) from exc
    if not isinstance(obj, dict):
        raise fail("E_RECORD_INVALID", f"record {index} must be a JSON object", record_index=index)

    record_seq = _require_int(obj, "record_seq", index, nonnegative=True)
    clock_ms = _require_int(obj, "clock_ms", index, nonnegative=True)
    bytes_value = _require_int(obj, "bytes", index, nonnegative=True)
    packets = _require_int(obj, "packets", index, nonnegative=True)
    src = _require_text(obj, "src_endpoint", index)
    dst = _require_text(obj, "dst_endpoint", index)
    protocol = _require_text(obj, "protocol", index)
    direction = _require_text(obj, "direction", index)
    validate_loopback_endpoint(src)
    validate_loopback_endpoint(dst)

    normalized_protocol = (
        protocol.upper() if plan.protocol_case == "upper" else protocol.lower()
    )
    normalized_direction = (
        direction.lower() if plan.direction_case == "lower" else direction.upper()
    )
    if normalized_protocol not in plan.protocols:
        raise fail(
            "E_PROTOCOL_INVALID",
            f"record {index} protocol {protocol!r} is not in the closed enum",
            record_index=index,
            protocol=protocol,
            allowed=list(plan.protocols),
        )
    if normalized_direction not in plan.directions:
        raise fail(
            "E_DIRECTION_INVALID",
            f"record {index} direction {direction!r} is not in the closed enum",
            record_index=index,
            direction=direction,
            allowed=list(plan.directions),
        )

    return FlowRecord(
        record_seq=record_seq,
        clock_ms=clock_ms,
        src_endpoint=src,
        dst_endpoint=dst,
        protocol=normalized_protocol,
        direction=normalized_direction,
        bytes=bytes_value,
        packets=packets,
    )


def compute_batch_digest(batch_id: str, records: Iterable[FlowRecord]) -> str:
    digest = hashlib.sha256()
    digest.update(b"flowagg-batch-v1\x00")
    digest.update(batch_id.encode("utf-8"))
    digest.update(b"\x00")
    for record in records:
        line = json.dumps(
            record.canonical(),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
        digest.update(line.encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


def read_batch(path: str | Path, plan: AggregationPlan, batch_id: str) -> BatchData:
    source = Path(path)
    if not source.exists():
        raise fail("E_SAMPLE_MISSING", f"batch file does not exist: {source}", path=str(source))
    try:
        lines = source.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise fail("E_SAMPLE_MISSING", f"cannot read batch file: {exc}", path=str(source)) from exc

    records: list[FlowRecord] = []
    for index, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        records.append(parse_record_line(line, plan, index))
    if not records:
        raise fail("E_BATCH_EMPTY", f"batch {batch_id!r} contains no records", batch_id=batch_id)
    if len(records) > plan.max_batch_rows:
        raise fail(
            "E_BATCH_TOO_LARGE",
            f"batch {batch_id!r} has {len(records)} rows; max is {plan.max_batch_rows}",
            batch_id=batch_id,
            rows=len(records),
            max_batch_rows=plan.max_batch_rows,
        )
    previous = -1
    for record in records:
        if record.record_seq <= previous:
            raise fail(
                "E_RECORD_SEQ",
                f"record_seq must be strictly increasing within batch {batch_id!r}",
                batch_id=batch_id,
                previous=previous,
                current=record.record_seq,
            )
        previous = record.record_seq

    frozen = tuple(records)
    return BatchData(
        batch_id=batch_id,
        path=source.resolve(),
        records=frozen,
        digest=compute_batch_digest(batch_id, frozen),
    )


def read_batch_manifest(path: str | Path) -> list[dict[str, Any]]:
    source = Path(path)
    if not source.exists():
        raise fail(
            "E_SAMPLE_MISSING",
            f"manifest file does not exist: {source}",
            path=str(source),
        )
    try:
        data = json.loads(source.read_text(encoding="utf-8"))
    except Exception as exc:
        raise fail("E_RECORD_INVALID", f"cannot parse manifest: {exc}", path=str(source)) from exc
    if not isinstance(data, dict) or not isinstance(data.get("batches"), list):
        raise fail("E_RECORD_INVALID", "manifest must contain a batches array", path=str(source))
    result: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for item in data["batches"]:
        if not isinstance(item, dict):
            raise fail("E_RECORD_INVALID", "manifest batch entries must be objects")
        batch_id = item.get("batch_id")
        file_name = item.get("path")
        if not isinstance(batch_id, str) or not batch_id:
            raise fail("E_RECORD_INVALID", "manifest batch_id must be a non-empty string")
        if batch_id in seen_ids:
            raise fail(
                "E_RECORD_INVALID",
                f"manifest contains duplicate batch_id {batch_id!r}",
            )
        seen_ids.add(batch_id)
        if not isinstance(file_name, str) or not file_name:
            raise fail("E_RECORD_INVALID", "manifest path must be a non-empty string")
        batch_path = Path(file_name)
        if not batch_path.is_absolute():
            batch_path = (source.parent / batch_path).resolve()
        result.append({"batch_id": batch_id, "path": batch_path})
    if not result:
        raise fail("E_BATCH_EMPTY", "manifest contains no batches", path=str(source))
    return result

