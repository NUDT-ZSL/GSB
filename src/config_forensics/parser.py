"""Tolerant evidence-file parsing and digest validation."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .canonical import PathNormalizationError, load_json_bytes, normalize_config_path
from .codes import Code
from .digests import normalize_algorithm, verify_observation_digest
from .issues import Issue
from .models import FileResult, RawEvidence

_MISSING = object()


@dataclass
class ParseContext:
    allowed_algorithms: tuple[str, ...]
    hmac_keys: dict[str, str | bytes] = field(default_factory=dict)


def _first(record: dict[str, Any], *names: str, default: Any = _MISSING) -> Any:
    for name in names:
        if name in record:
            return record[name]
    return default


def _record_issue(
    code: str,
    *,
    file_id: str,
    file_index: int,
    record_index: int | None,
    host_id: str | None = None,
    sequence: int | None = None,
    path: str | None = None,
    details: dict[str, Any] | None = None,
) -> Issue:
    return Issue(
        code=code,
        file_id=file_id,
        file_index=file_index,
        record_index=record_index,
        host_id=host_id,
        sequence=sequence,
        path=path,
        details=details or {},
    )


def _extract_records(payload: Any) -> tuple[list[Any] | None, str | None]:
    if isinstance(payload, list):
        return payload, None
    if not isinstance(payload, dict):
        return None, "top-level JSON must be an object or array"
    if isinstance(payload.get("records"), list):
        return payload["records"], None
    if isinstance(payload.get("evidence"), list):
        return payload["evidence"], None
    if isinstance(payload.get("items"), list):
        return payload["items"], None
    if any(key in payload for key in ("host_id", "host", "sequence", "seq")):
        return [payload], None
    return None, "no evidence record array or single record found"


def _file_hmac_material(
    payload: Any,
) -> tuple[dict[str, str | bytes], str | bytes | None, str | None]:
    if not isinstance(payload, dict):
        return {}, None, None
    keys: dict[str, str | bytes] = {}
    raw_keys = payload.get("hmac_keys")
    if raw_keys is not None and not isinstance(raw_keys, dict):
        return {}, None, "hmac_keys must be an object"
    if isinstance(raw_keys, dict):
        for key_id, key in raw_keys.items():
            if not isinstance(key_id, str) or not isinstance(key, (str, bytes)):
                return {}, None, "hmac_keys entries must map strings to local keys"
            keys[key_id] = key
    default_key = payload.get("hmac_key")
    if default_key is not None and not isinstance(default_key, (str, bytes)):
        return keys, None, "hmac_key must be a string or bytes"
    return keys, default_key, None


def _resolve_hmac_key(
    *,
    record: dict[str, Any],
    file_keys: dict[str, str | bytes],
    file_default: str | bytes | None,
    context: ParseContext,
) -> tuple[str | bytes | None, str | None]:
    key_id = _first(record, "hmac_key_id", "key_id", default=None)
    direct_key = record.get("hmac_key")
    if direct_key is not None:
        if not isinstance(direct_key, (str, bytes)):
            return None, "hmac_key must be a string or bytes"
        return direct_key, None
    if key_id is not None:
        if not isinstance(key_id, str) or not key_id:
            return None, "hmac_key_id must be a non-empty string"
        if key_id in file_keys:
            return file_keys[key_id], None
        if key_id in context.hmac_keys:
            return context.hmac_keys[key_id], None
        return None, f"hmac key id {key_id!r} was not supplied"
    if file_default is not None:
        return file_default, None
    if len(file_keys) == 1:
        return next(iter(file_keys.values())), None
    if len(context.hmac_keys) == 1:
        return next(iter(context.hmac_keys.values())), None
    return None, None


def _parse_record(
    item: Any,
    *,
    file_id: str,
    file_index: int,
    record_index: int,
    file_keys: dict[str, str | bytes],
    file_default_key: str | bytes | None,
    context: ParseContext,
) -> tuple[RawEvidence | None, Issue | None]:
    if not isinstance(item, dict):
        return None, _record_issue(
            Code.SCHEMA_INVALID,
            file_id=file_id,
            file_index=file_index,
            record_index=record_index,
            details={"reason": "record must be an object"},
        )

    host_id = _first(item, "host_id", "host")
    if not isinstance(host_id, str) or not host_id or host_id != host_id.strip():
        return None, _record_issue(
            Code.HOST_INVALID,
            file_id=file_id,
            file_index=file_index,
            record_index=record_index,
        )

    sequence = _first(item, "sequence", "seq")
    if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 0:
        return None, _record_issue(
            Code.SEQUENCE_INVALID,
            file_id=file_id,
            file_index=file_index,
            record_index=record_index,
            host_id=host_id,
        )

    raw_path = _first(item, "path", "config_path", "config_key")
    try:
        path = normalize_config_path(raw_path)
    except PathNormalizationError as exc:
        return None, _record_issue(
            Code.PATH_INVALID,
            file_id=file_id,
            file_index=file_index,
            record_index=record_index,
            host_id=host_id,
            sequence=sequence,
            details={"reason": str(exc), "input": raw_path},
        )

    value = _first(item, "value", "observed_value")
    if value is _MISSING:
        return None, _record_issue(
            Code.VALUE_INVALID,
            file_id=file_id,
            file_index=file_index,
            record_index=record_index,
            host_id=host_id,
            sequence=sequence,
            path=path,
        )

    evidence_id = _first(item, "evidence_id", "id", "record_id")
    if evidence_id is None:
        evidence_id = f"{file_id}:{record_index}"
    elif not isinstance(evidence_id, str) or not evidence_id or evidence_id != evidence_id.strip():
        return None, _record_issue(
            Code.EVIDENCE_ID_MISSING,
            file_id=file_id,
            file_index=file_index,
            record_index=record_index,
            host_id=host_id,
            sequence=sequence,
            path=path,
        )

    previous = _first(
        item,
        "previous_evidence_id",
        "prev_id",
        "previous_id",
        "link_id",
        "links_to",
        default=None,
    )
    if previous is not None and (
        not isinstance(previous, str) or not previous or previous != previous.strip()
    ):
        return None, _record_issue(
            Code.LINK_INVALID,
            file_id=file_id,
            file_index=file_index,
            record_index=record_index,
            host_id=host_id,
            sequence=sequence,
            path=path,
        )

    algorithm = _first(item, "algorithm", "digest_algorithm", default="sha256")
    digest = item.get("digest")
    if digest is None and item.get("sha256") is not None:
        digest = item["sha256"]
        algorithm = "sha256"
    digest_bits = _first(item, "digest_bits", "bit_width", "bits", default=None)
    key_id = _first(item, "hmac_key_id", "key_id", default=None)
    hmac_key, key_error = _resolve_hmac_key(
        record=item,
        file_keys=file_keys,
        file_default=file_default_key,
        context=context,
    )

    check = verify_observation_digest(
        normalized_path=path,
        value=value,
        algorithm=algorithm,
        digest=digest,
        declared_bits=digest_bits,
        allowed_algorithms=context.allowed_algorithms,
        hmac_key=hmac_key,
    )
    normalized_algorithm = normalize_algorithm(algorithm)
    if not check.ok and check.code != Code.TAMPERED:
        details: dict[str, Any] = {"reason": check.detail}
        if key_error:
            details["reason"] = key_error
        return None, _record_issue(
            check.code or Code.DIGEST_INVALID,
            file_id=file_id,
            file_index=file_index,
            record_index=record_index,
            host_id=host_id,
            sequence=sequence,
            path=path,
            details=details,
        )

    has_sender_value = any(
        name in item for name in ("sender_observed_value", "sender_value")
    )
    record = RawEvidence(
        file_id=file_id,
        file_index=file_index,
        record_index=record_index,
        evidence_id=evidence_id,
        aliases=[evidence_id],
        host_id=host_id,
        sequence=sequence,
        path=path,
        value=value,
        algorithm=normalized_algorithm or str(algorithm),
        digest=check.digest or str(digest).split(":")[-1].lower(),
        digest_bits=check.bits if check.bits is not None else digest_bits,
        previous_evidence_id=previous,
        hmac_key_id=key_id if isinstance(key_id, str) else None,
        sync_marker=_first(item, "sync_marker", "sync_token", default=None),
        sender_observed_value=_first(
            item, "sender_observed_value", "sender_value", default=None
        ),
        has_sender_observed_value=has_sender_value,
        tampered=not check.ok,
    )
    if record.tampered:
        return record, _record_issue(
            Code.TAMPERED,
            file_id=file_id,
            file_index=file_index,
            record_index=record_index,
            host_id=host_id,
            sequence=sequence,
            path=path,
            details={"digest": record.digest},
        )
    return record, None


def process_evidence_bytes(
    *,
    data: bytes,
    source_id: str,
    source_name: str,
    file_index: int,
    sha256: str,
    context: ParseContext,
) -> FileResult:
    """Parse one file without raising for record-level or file-level errors."""
    issues: list[Issue] = []
    records: list[RawEvidence] = []

    try:
        payload = load_json_bytes(data)
    except (UnicodeDecodeError, ValueError) as exc:
        return FileResult(
            source_id=source_id,
            source_name=source_name,
            file_index=file_index,
            file_id=source_name,
            sha256=sha256,
            status="rejected",
            issues=[
                Issue(
                    Code.JSON_INVALID,
                    file_id=source_name,
                    file_index=file_index,
                    details={"reason": str(exc)},
                )
            ],
        )

    payload_file_id = payload.get("file_id") if isinstance(payload, dict) else None
    if isinstance(payload_file_id, str) and payload_file_id.strip():
        file_id = payload_file_id
    else:
        file_id = source_name

    if isinstance(payload, dict) and "schema_version" in payload:
        version = payload["schema_version"]
        if isinstance(version, bool) or not isinstance(version, int) or version != 1:
            issues.append(
                Issue(
                    Code.SCHEMA_INVALID,
                    file_id=file_id,
                    file_index=file_index,
                    details={"schema_version": version},
                )
            )

    items, extraction_error = _extract_records(payload)
    if items is None:
        issues.append(
            Issue(
                Code.SCHEMA_INVALID,
                file_id=file_id,
                file_index=file_index,
                details={"reason": extraction_error},
            )
        )
        return FileResult(
            source_id=source_id,
            source_name=source_name,
            file_index=file_index,
            file_id=file_id,
            sha256=sha256,
            status="rejected",
            issues=issues,
        )
    if not items:
        issues.append(
            Issue(
                Code.SCHEMA_INVALID,
                file_id=file_id,
                file_index=file_index,
                details={"reason": "record array is empty"},
            )
        )

    file_keys, file_default_key, key_error = _file_hmac_material(payload)
    if key_error:
        issues.append(
            Issue(
                Code.SCHEMA_INVALID,
                file_id=file_id,
                file_index=file_index,
                details={"reason": key_error},
            )
        )

    for record_index, item in enumerate(items):
        record, issue = _parse_record(
            item,
            file_id=file_id,
            file_index=file_index,
            record_index=record_index,
            file_keys=file_keys,
            file_default_key=file_default_key,
            context=context,
        )
        if record is not None:
            records.append(record)
        if issue is not None:
            issues.append(issue)

    if records and issues:
        status = "partial"
    elif records:
        status = "accepted"
    else:
        status = "rejected"
    return FileResult(
        source_id=source_id,
        source_name=source_name,
        file_index=file_index,
        file_id=file_id,
        sha256=sha256,
        status=status,
        records=records,
        issues=issues,
        payload_record_count=len(items),
    )


def process_evidence_file(
    path: Path,
    *,
    source_id: str,
    source_name: str,
    file_index: int,
    context: ParseContext,
) -> FileResult:
    try:
        data = path.read_bytes()
    except OSError as exc:
        return FileResult(
            source_id=source_id,
            source_name=source_name,
            file_index=file_index,
            file_id=source_name,
            sha256="",
            status="rejected",
            issues=[
                Issue(
                    Code.FILE_READ,
                    file_id=source_name,
                    file_index=file_index,
                    details={"reason": str(exc)},
                )
            ],
        )
    return process_evidence_bytes(
        data=data,
        source_id=source_id,
        source_name=source_name,
        file_index=file_index,
        sha256=hashlib.sha256(data).hexdigest(),
        context=context,
    )
