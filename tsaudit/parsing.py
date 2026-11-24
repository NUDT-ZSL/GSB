"""Strict parsing and deterministic normalization for audit inputs."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable

from .canon import canonical_issuer_id, strict_json_loads
from .codes import Code


DIGEST_LENGTHS = {
    "sha1": 40,
    "sha224": 56,
    "sha256": 64,
    "sha384": 96,
    "sha512": 128,
}

_ARTIFACT_LIST_KEYS = (
    "artifacts",
    "signature_records",
    "signatures",
    "signed_artifacts",
    "records",
    "items",
)
_TOKEN_LIST_KEYS = (
    "tokens",
    "timestamp_tokens",
    "tokens_claims",
    "claims",
    "records",
    "items",
)


def rejection(
    source_category: str,
    record_seq: int,
    code: str,
    message: str,
    *,
    source_file: str | None = None,
    **details: Any,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "source_category": source_category,
        "source_file": source_file,
        "record_seq": record_seq,
        "code": code,
        "message": message,
    }
    if details:
        result["details"] = details
    return result


def is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def non_negative_int(value: Any) -> bool:
    return is_int(value) and value >= 0


def _first(dataset: dict[str, Any], keys: Iterable[str], default: Any = None) -> Any:
    for key in keys:
        if key in dataset and dataset[key] is not None:
            return dataset[key]
    return default


def _as_list(payload: Any, keys: tuple[str, ...]) -> list[Any] | None:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in keys:
            value = payload.get(key)
            if isinstance(value, list):
                return value
    return None


def load_manifest(path: str | Path) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    target = Path(path)
    value, error = _read_json_file(target)
    if error is not None:
        return None, [
            rejection(
                "manifest",
                0,
                Code.E_PARSE_JSON,
                f"manifest is not parseable JSON: {error}",
                source_file=str(target),
            )
        ]
    if not isinstance(value, dict):
        return None, [
            rejection(
                "manifest",
                0,
                Code.E_SCHEMA,
                "manifest must be a JSON object",
                source_file=str(target),
            )
        ]
    clock = value.get("observation_clock_ms")
    if not is_int(clock) or clock < 0:
        return None, [
            rejection(
                "manifest",
                0,
                Code.E_OBSERVATION_CLOCK,
                "observation_clock_ms must be a non-negative integer millisecond value",
                source_file=str(target),
            )
        ]
    value["observation_clock_ms"] = int(clock)
    value.setdefault("digest_algorithm", "sha256")
    if value["digest_algorithm"] not in DIGEST_LENGTHS:
        return None, [
            rejection(
                "manifest",
                0,
                Code.E_DIGEST_ALGORITHM,
                f"unsupported digest algorithm: {value['digest_algorithm']}",
                source_file=str(target),
            )
        ]
    return value, []


def _read_json_file(path: Path) -> tuple[Any | None, str | None]:
    try:
        text = path.read_text(encoding="utf-8")
        return strict_json_loads(text), None
    except FileNotFoundError:
        return None, "file not found"
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        return None, f"{type(exc).__name__}: {exc}"


def read_records_source(
    path: str | Path,
    list_keys: tuple[str, ...],
    source_category: str,
) -> tuple[list[Any], list[dict[str, Any]]]:
    target = Path(path)
    if target.suffix.lower() == ".jsonl":
        records: list[Any] = []
        rejections: list[dict[str, Any]] = []
        try:
            lines = target.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            return [], [
                rejection(
                    source_category,
                    0,
                    Code.E_SOURCE_NOT_FOUND,
                    str(exc),
                    source_file=str(target),
                )
            ]
        for line_number, line in enumerate(lines, start=1):
            if not line.strip():
                continue
            try:
                records.append(strict_json_loads(line))
            except Exception as exc:
                rejections.append(
                    rejection(
                        source_category,
                        line_number,
                        Code.E_PARSE_JSON,
                        f"JSONL line is not valid JSON: {exc}",
                        source_file=str(target),
                    )
                )
        return records, rejections

    payload, error = _read_json_file(target)
    if error is not None:
        code = Code.E_SOURCE_NOT_FOUND if error == "file not found" else Code.E_PARSE_JSON
        return [], [
            rejection(
                source_category,
                0,
                code,
                f"input source cannot be read: {error}",
                source_file=str(target),
            )
        ]
    records = _as_list(payload, list_keys)
    if records is None:
        return [], [
            rejection(
                source_category,
                0,
                Code.E_SCHEMA,
                "input must be a JSON array or an object containing a records array",
                source_file=str(target),
            )
        ]
    return records, []


def normalize_digest(
    value: Any,
    declared_algorithm: str = "sha256",
) -> tuple[dict[str, str] | None, str | None, str | None]:
    """Return digest tuple, stable error code and message."""
    algorithm = declared_algorithm
    raw_hex = value
    if isinstance(value, str) and ":" in value:
        prefix, candidate = value.split(":", 1)
        if prefix.lower().replace("-", "") in DIGEST_LENGTHS:
            algorithm = prefix.lower().replace("-", "")
            raw_hex = candidate
    if algorithm not in DIGEST_LENGTHS:
        return None, Code.E_DIGEST_ALGORITHM, f"unsupported digest algorithm: {algorithm}"
    if not isinstance(raw_hex, str) or not raw_hex:
        return None, Code.E_DIGEST_HEX, "digest must be a non-empty hexadecimal string"
    cleaned = raw_hex.strip().lower()
    if any(char not in "0123456789abcdef" for char in cleaned):
        return None, Code.E_DIGEST_HEX, "digest contains non-hexadecimal characters"
    expected_length = DIGEST_LENGTHS[algorithm]
    if len(cleaned) != expected_length:
        return (
            None,
            Code.E_DIGEST_LENGTH,
            f"{algorithm} digest must contain {expected_length} hex characters",
        )
    return (
        {
            "algorithm": algorithm,
            "hex": cleaned,
            "ref": f"{algorithm}:{cleaned}",
        },
        None,
        None,
    )


def _validate_id(
    record: dict[str, Any],
    keys: tuple[str, ...],
    seq: int,
    category: str,
    source_file: str,
    rejections: list[dict[str, Any]],
    code: str = Code.E_ID_EMPTY,
) -> str | None:
    value = _first(record, keys)
    if not isinstance(value, str) or not value.strip():
        rejections.append(
            rejection(
                category,
                seq,
                code,
                "identifier must be a non-empty string",
                source_file=source_file,
            )
        )
        return None
    return value.strip()


def parse_signature_record(
    raw: Any,
    seq: int,
    source_file: str,
    *,
    default_algorithm: str = "sha256",
    seen_artifact_ids: set[str] | None = None,
    source_category: str = "signature_record",
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    rejections: list[dict[str, Any]] = []
    if not isinstance(raw, dict):
        return None, [
            rejection(
                source_category,
                seq,
                Code.E_RECORD_TYPE,
                "signature record must be a JSON object",
                source_file=source_file,
            )
        ]

    artifact_id = _validate_id(
        raw,
        ("artifact_id", "component_id", "id"),
        seq,
        source_category,
        source_file,
        rejections,
    )
    digest_value = _first(
        raw,
        ("signature_digest", "signature_value_digest", "signed_digest", "digest"),
    )
    algorithm = raw.get("digest_algorithm", raw.get("algorithm", default_algorithm))
    digest, digest_code, digest_message = normalize_digest(digest_value, algorithm)
    if digest is None:
        rejections.append(
            rejection(
                source_category,
                seq,
                digest_code or Code.E_DIGEST_HEX,
                digest_message or "invalid digest",
                source_file=source_file,
            )
        )

    signed_at = _first(
        raw,
        ("local_signed_at_ms", "signed_at_ms", "local_sign_time_ms", "sign_time_ms"),
    )
    if not is_int(signed_at):
        rejections.append(
            rejection(
                source_category,
                seq,
                Code.E_TIME_INTEGER,
                "local signed time must be an integer millisecond value",
                source_file=source_file,
            )
        )

    if artifact_id and seen_artifact_ids is not None and artifact_id in seen_artifact_ids:
        rejections.append(
            rejection(
                source_category,
                seq,
                Code.E_DUP_ARTIFACT_ID,
                f"duplicate artifact_id: {artifact_id}",
                source_file=source_file,
                artifact_id=artifact_id,
            )
        )

    if rejections:
        marker = raw.get("sync_marker") if isinstance(raw, dict) else None
        sender_time = raw.get("sender_time_repr") or raw.get("sender_original_time") if isinstance(raw, dict) else None
        for item in rejections:
            if marker is not None:
                item.setdefault("sync_marker", marker)
            if sender_time is not None:
                item.setdefault("sender_time_repr", sender_time)
        return None, rejections

    assert artifact_id is not None and digest is not None and is_int(signed_at)
    if seen_artifact_ids is not None:
        seen_artifact_ids.add(artifact_id)
    bound_token_id = _first(raw, ("bound_token_id", "token_id"))
    if bound_token_id is not None and (
        not isinstance(bound_token_id, str) or not bound_token_id.strip()
    ):
        rejections.append(
            rejection(
                source_category,
                seq,
                Code.E_FIELD_TYPE,
                "bound_token_id must be a non-empty string when supplied",
                source_file=source_file,
            )
        )
        return None, rejections

    return {
        "artifact_id": artifact_id,
        "signature_digest": digest["hex"],
        "digest_algorithm": digest["algorithm"],
        "digest_ref": digest["ref"],
        "local_signed_at_ms": int(signed_at),
        "bound_token_id": bound_token_id.strip() if isinstance(bound_token_id, str) else None,
        "sender_time_repr": raw.get("sender_time_repr") or raw.get("sender_original_time"),
        "sync_marker": raw.get("sync_marker"),
        "source_category": source_category,
        "source_file": source_file,
        "record_seq": seq,
    }, []


def parse_token_record(
    raw: Any,
    seq: int,
    source_file: str,
    *,
    default_algorithm: str = "sha256",
    seen_token_ids: set[str] | None = None,
    source_category: str = "timestamp_token",
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    rejections: list[dict[str, Any]] = []
    if not isinstance(raw, dict):
        return None, [
            rejection(
                source_category,
                seq,
                Code.E_RECORD_TYPE,
                "timestamp token claim must be a JSON object",
                source_file=source_file,
            )
        ]

    token_id = _validate_id(
        raw,
        ("token_id", "timestamp_token_id", "id"),
        seq,
        source_category,
        source_file,
        rejections,
    )
    issuer_id = _validate_id(
        raw,
        ("issuer_id", "tsa_issuer_id", "issuer"),
        seq,
        source_category,
        source_file,
        rejections,
    )
    digest_value = _first(
        raw,
        (
            "covered_signature_digest",
            "signature_digest",
            "signature_value_digest",
            "signed_digest",
        ),
    )
    algorithm = raw.get("digest_algorithm", raw.get("algorithm", default_algorithm))
    digest, digest_code, digest_message = normalize_digest(digest_value, algorithm)
    if digest is None:
        rejections.append(
            rejection(
                source_category,
                seq,
                digest_code or Code.E_DIGEST_HEX,
                digest_message or "invalid digest",
                source_file=source_file,
            )
        )

    issued_at = _first(raw, ("issued_at_ms", "issue_time_ms", "issued_ms", "issuance_ms"))
    if not is_int(issued_at):
        rejections.append(
            rejection(
                source_category,
                seq,
                Code.E_TIME_INTEGER,
                "token issued time must be an integer millisecond value",
                source_file=source_file,
            )
        )

    validity = raw.get("validity") if isinstance(raw.get("validity"), dict) else {}
    valid_from = _first(
        raw,
        ("valid_from_ms", "not_before_ms", "validity_start_ms", "start_ms"),
        _first(validity, ("start_ms", "valid_from_ms", "not_before_ms")),
    )
    valid_to = _first(
        raw,
        ("valid_to_ms", "not_after_ms", "validity_end_ms", "end_ms"),
        _first(validity, ("end_ms", "valid_to_ms", "not_after_ms")),
    )
    if not is_int(valid_from) or not is_int(valid_to):
        rejections.append(
            rejection(
                source_category,
                seq,
                Code.E_TIME_INTEGER,
                "validity start and end must be integer millisecond values",
                source_file=source_file,
            )
        )
    elif int(valid_from) >= int(valid_to):
        rejections.append(
            rejection(
                source_category,
                seq,
                Code.E_TIME_WINDOW,
                "validity start must be strictly earlier than validity end",
                source_file=source_file,
                valid_from_ms=valid_from,
                valid_to_ms=valid_to,
            )
        )

    token_generation = _first(raw, ("token_generation", "generation", "token_gen"), 0)
    if not non_negative_int(token_generation):
        rejections.append(
            rejection(
                source_category,
                seq,
                Code.E_GENERATION,
                "token_generation must be a non-negative integer",
                source_file=source_file,
            )
        )

    if token_id and seen_token_ids is not None and token_id in seen_token_ids:
        rejections.append(
            rejection(
                source_category,
                seq,
                Code.E_DUP_TOKEN_ID,
                f"duplicate token_id: {token_id}",
                source_file=source_file,
                token_id=token_id,
            )
        )

    if rejections:
        marker = raw.get("sync_marker") if isinstance(raw, dict) else None
        sender_time = raw.get("sender_time_repr") or raw.get("sender_original_time") if isinstance(raw, dict) else None
        for item in rejections:
            if marker is not None:
                item.setdefault("sync_marker", marker)
            if sender_time is not None:
                item.setdefault("sender_time_repr", sender_time)
        return None, rejections

    assert token_id and issuer_id and digest
    assert is_int(issued_at) and is_int(valid_from) and is_int(valid_to)
    assert non_negative_int(token_generation)
    if seen_token_ids is not None:
        seen_token_ids.add(token_id)

    return {
        "token_id": token_id,
        "issuer_id": issuer_id,
        "issuer_canonical_id": canonical_issuer_id(issuer_id),
        "signature_digest": digest["hex"],
        "digest_algorithm": digest["algorithm"],
        "digest_ref": digest["ref"],
        "issued_at_ms": int(issued_at),
        "valid_from_ms": int(valid_from),
        "valid_to_ms": int(valid_to),
        "token_generation": int(token_generation),
        "sender_time_repr": raw.get("sender_time_repr") or raw.get("sender_original_time"),
        "sync_marker": raw.get("sync_marker"),
        "source_category": source_category,
        "source_file": source_file,
        "record_seq": seq,
    }, []


def parse_signatures_file(
    path: str | Path,
    *,
    default_algorithm: str = "sha256",
    seen_artifact_ids: set[str] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    target = str(path)
    if seen_artifact_ids is None:
        seen_artifact_ids = set()
    raw_records, file_rejections = read_records_source(
        path, _ARTIFACT_LIST_KEYS, "signature_record"
    )
    records: list[dict[str, Any]] = []
    rejections = list(file_rejections)
    for index, raw in enumerate(raw_records, start=1):
        record, errors = parse_signature_record(
            raw,
            index,
            target,
            default_algorithm=default_algorithm,
            seen_artifact_ids=seen_artifact_ids,
        )
        rejections.extend(errors)
        if record is not None:
            records.append(record)
    return records, rejections


def parse_tokens_file(
    path: str | Path,
    *,
    default_algorithm: str = "sha256",
    seen_token_ids: set[str] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    target = str(path)
    if seen_token_ids is None:
        seen_token_ids = set()
    raw_records, file_rejections = read_records_source(
        path, _TOKEN_LIST_KEYS, "timestamp_token"
    )
    records: list[dict[str, Any]] = []
    rejections = list(file_rejections)
    for index, raw in enumerate(raw_records, start=1):
        record, errors = parse_token_record(
            raw,
            index,
            target,
            default_algorithm=default_algorithm,
            seen_token_ids=seen_token_ids,
        )
        rejections.extend(errors)
        if record is not None:
            records.append(record)
    return records, rejections
