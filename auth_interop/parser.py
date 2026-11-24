from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from .codes import (
    E_DUPLICATE_CLIENT,
    E_DUPLICATE_CODE,
    E_DUPLICATE_SESSION,
    E_INVALID_JSON,
    E_INVALID_RECORD,
    E_METADATA_MISMATCH,
    E_NON_MONOTONIC_TIMESTAMP,
)
from .io_utils import read_json_file, sha256_file
from .model import (
    NormalizedRecord,
    Rejection,
    SourceRef,
    normalize_identifier,
    normalize_scope,
)


@dataclass
class InputBatch:
    sequence: int
    batch_id: str
    records: list[NormalizedRecord] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "sequence": self.sequence,
            "batch_id": self.batch_id,
            "records": [record.to_dict() for record in self.records],
        }


@dataclass
class SampleInput:
    sample_dir: Path
    clock_ms: int
    batches: list[InputBatch]
    rejections: list[Rejection]
    clock_path: Path


class RecordParseError(ValueError):
    def __init__(self, message: str, code: str = E_INVALID_RECORD) -> None:
        super().__init__(message)
        self.code = code


def _get(raw: dict[str, Any], *names: str, default: Any = None) -> Any:
    for name in names:
        if name in raw and raw[name] is not None:
            return raw[name]
    return default


def _as_int(value: Any, field_name: str) -> int:
    if isinstance(value, bool) or value is None:
        raise RecordParseError(f"{field_name} must be an integer")
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise RecordParseError(f"{field_name} must be an integer") from exc


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "t", "tombstone"}
    return bool(value)


def load_clock(path: str | Path) -> tuple[int, dict[str, Any]]:
    clock_path = Path(path)
    data = read_json_file(clock_path)
    if not isinstance(data, dict):
        raise ValueError("clock file must contain a JSON object")
    observed = _as_int(data.get("observed_at_ms"), "observed_at_ms")
    if observed <= 0:
        raise ValueError("observed_at_ms must be a positive epoch millisecond value")
    payload = dict(data)
    payload["observed_at_ms"] = observed
    payload["source_path"] = str(clock_path).replace("\\", "/")
    return observed, payload


def _entries_from_payload(payload: Any) -> list[tuple[Optional[str], int, dict[str, Any]]]:
    entries: list[tuple[Optional[str], int, dict[str, Any]]] = []
    if isinstance(payload, list):
        for index, raw in enumerate(payload, start=1):
            if isinstance(raw, dict):
                entries.append((raw.get("type") or raw.get("record_type"), index, raw))
        return entries

    if not isinstance(payload, dict):
        return entries

    if isinstance(payload.get("records"), list):
        for index, raw in enumerate(payload["records"], start=1):
            if isinstance(raw, dict):
                entries.append((raw.get("type") or raw.get("record_type"), index, raw))
        return entries

    collection_map = {
        "clients": "client",
        "sessions": "session",
        "authorization_codes": "authorization_code",
        "codes": "authorization_code",
        "access_tokens": "access_token",
        "refresh_tokens": "refresh_token",
        "redemptions": "redemption",
        "revocations": "revocation",
    }
    for collection, record_type in collection_map.items():
        values = payload.get(collection)
        if not isinstance(values, list):
            continue
        for index, raw in enumerate(values, start=1):
            if isinstance(raw, dict):
                entries.append((record_type, index, raw))
    return entries


def normalize_record(
    raw: dict[str, Any],
    *,
    source_version: str,
    path: str,
    record_no: int,
    collection: Optional[str],
    clock_ms: Optional[int],
) -> NormalizedRecord:
    record_type = normalize_identifier(
        _get(raw, "type", "record_type", "kind", default=collection)
    ).lower()
    if not record_type:
        raise RecordParseError("missing record type")

    record_id = normalize_identifier(_get(raw, "id", "record_id", "token_id"))
    client_id = normalize_identifier(_get(raw, "client_id", "clientId", "client")) or None
    code = normalize_identifier(_get(raw, "code", "authorization_code", "authorizationCode")) or None
    session_id = normalize_identifier(_get(raw, "session_id", "sessionId", "sid")) or None
    token_id = normalize_identifier(_get(raw, "token_id", "tokenId")) or None

    if not record_id:
        record_id = token_id or code or session_id or f"{record_type}-{record_no}"

    generation = int(_as_int(_get(raw, "generation", "token_generation", default=0), "generation"))
    issued_at_ms = _optional_int(_get(raw, "issued_at_ms", "issuedAt", "iat_ms"), "issued_at_ms")
    expires_at_ms = _optional_int(_get(raw, "expires_at_ms", "expiresAt", "expiry_ms"), "expires_at_ms")
    not_before_ms = _optional_int(_get(raw, "not_before_ms", "nbf_ms"), "not_before_ms")
    used_at_ms = _optional_int(_get(raw, "used_at_ms", "usedAt"), "used_at_ms")

    if source_version == "v1":
        if clock_ms is None:
            raise RecordParseError("v1 relative expiry requires the declared observation clock")
        offset_seconds = _optional_int(
            _get(raw, "issued_at_offset_seconds", "issued_offset_seconds", "iat_offset_seconds", default=0),
            "issued_at_offset_seconds",
        ) or 0
        issued_at_ms = clock_ms + offset_seconds * 1000
        expires_in = _optional_int(
            _get(raw, "expires_in_seconds", "expires_in", "ttl_seconds"),
            "expires_in_seconds",
        )
        expiry_offset = _optional_int(
            _get(raw, "expires_at_offset_seconds", "expiry_offset_seconds"),
            "expires_at_offset_seconds",
        )
        if expires_in is not None:
            expires_at_ms = issued_at_ms + expires_in * 1000
        elif expiry_offset is not None:
            expires_at_ms = clock_ms + expiry_offset * 1000

    time_scoped_types = {
        "authorization_code",
        "session",
        "access_token",
        "refresh_token",
    }
    if record_type in time_scoped_types and (
        issued_at_ms is None or expires_at_ms is None
    ):
        raise RecordParseError("record requires issued and expiry timestamps")
    if (
        issued_at_ms is not None
        and expires_at_ms is not None
        and expires_at_ms < issued_at_ms
    ):
        raise RecordParseError(
            "expires_at_ms precedes issued_at_ms",
            E_NON_MONOTONIC_TIMESTAMP,
        )

    if record_type == "authorization_code":
        if not code:
            raise RecordParseError("authorization_code requires code")
        if not client_id:
            raise RecordParseError("authorization_code requires client_id")

    source = SourceRef(
        source_version=source_version,
        path=path.replace("\\", "/"),
        record_no=record_no,
        record_id=record_id,
        generation=generation,
        collection=collection or record_type,
    )

    return NormalizedRecord(
        record_type=record_type,
        record_id=record_id,
        source=source,
        client_id=client_id,
        code=code,
        session_id=session_id,
        redirect_uri=normalize_identifier(_get(raw, "redirect_uri", "redirectUri", "redirect")) or None,
        scope=normalize_scope(_get(raw, "scope", "scopes", default=[])),
        issued_at_ms=issued_at_ms,
        expires_at_ms=expires_at_ms,
        not_before_ms=not_before_ms,
        used=_as_bool(_get(raw, "used", "consumed", default=False)),
        used_at_ms=used_at_ms,
        generation=generation,
        tombstoned=_as_bool(_get(raw, "tombstoned", "tombstone", "deleted", default=False)),
        pkce_challenge=_get(raw, "code_challenge", "codeChallenge", "challenge"),
        pkce_method=_get(raw, "code_challenge_method", "codeChallengeMethod", "method"),
        code_verifier=_get(raw, "code_verifier", "codeVerifier", "verifier"),
        chain_id=normalize_identifier(_get(raw, "chain_id", "chainId")) or None,
        token_id=token_id,
        access_token_id=normalize_identifier(_get(raw, "access_token_id", "accessTokenId")) or None,
        refresh_token_id=normalize_identifier(_get(raw, "refresh_token_id", "refreshTokenId")) or None,
        rotates_from_id=normalize_identifier(_get(raw, "rotates_from_id", "rotatesFrom")) or None,
        successor_id=normalize_identifier(_get(raw, "successor_id", "successorId", "next_token_id")) or None,
        batch_id=normalize_identifier(_get(raw, "batch_id", "batchId")) or None,
        arrival_order=int(_as_int(_get(raw, "arrival_order", "order", default=0), "arrival_order")),
        raw=raw,
    )


def _optional_int(value: Any, field_name: str) -> Optional[int]:
    if value is None:
        return None
    return _as_int(value, field_name)


class ParseContext:
    def __init__(self) -> None:
        self.seen: dict[tuple[str, str, str], SourceRef] = {}

    def check_uniqueness(self, record: NormalizedRecord) -> Optional[Rejection]:
        source_version = record.source.source_version
        if record.record_type == "client" and record.client_id:
            kind, value, code = "client", record.client_id, E_DUPLICATE_CLIENT
        elif record.record_type == "session":
            kind, value, code = "session", record.session_id or record.record_id, E_DUPLICATE_SESSION
        elif record.record_type == "authorization_code" and record.code:
            kind, value, code = "code", record.code, E_DUPLICATE_CODE
        else:
            return None

        key = (source_version, kind, normalize_identifier(value))
        if key in self.seen:
            first = self.seen[key]
            return Rejection(
                code=code,
                message=f"duplicate {kind} identifier {value!r}; first seen at {first.path}#{first.record_no}",
                source_version=source_version,
                path=record.source.path,
                record_no=record.source.record_no,
                record_id=record.record_id,
                collection=record.source.collection,
            )
        self.seen[key] = record.source
        return None


def parse_json_source(
    path: str | Path,
    *,
    source_version: str,
    clock_ms: Optional[int],
    context: Optional[ParseContext] = None,
) -> tuple[list[NormalizedRecord], list[Rejection]]:
    source_path = Path(path)
    display_path = str(source_path).replace("\\", "/")
    context = context or ParseContext()
    records: list[NormalizedRecord] = []
    rejections: list[Rejection] = []

    try:
        payload = read_json_file(source_path)
    except FileNotFoundError as exc:
        return [], [
            Rejection(
                E_INVALID_JSON,
                f"file not found: {exc.filename or display_path}",
                source_version,
                display_path,
                0,
            )
        ]
    except (json.JSONDecodeError, ValueError, OSError) as exc:
        return [], [
            Rejection(E_INVALID_JSON, str(exc), source_version, display_path, 0)
        ]

    for suggested_type, index, raw in _entries_from_payload(payload):
        try:
            record = normalize_record(
                raw,
                source_version=source_version,
                path=display_path,
                record_no=index,
                collection=suggested_type,
                clock_ms=clock_ms,
            )
        except RecordParseError as exc:
            rejections.append(
                Rejection(
                    getattr(exc, "code", E_INVALID_RECORD),
                    str(exc),
                    source_version,
                    display_path,
                    index,
                    record_id=_safe_id(raw),
                    collection=suggested_type,
                )
            )
            continue

        duplicate = context.check_uniqueness(record)
        if duplicate:
            rejections.append(duplicate)
            continue
        records.append(record)

    monotonic_rejections, bad_refs = validate_monotonic_chains(records)
    if monotonic_rejections:
        rejections.extend(monotonic_rejections)
        records = [record for record in records if record.source not in bad_refs]

    return records, rejections


def _safe_id(raw: dict[str, Any]) -> Optional[str]:
    value = _get(raw, "id", "record_id", "code", "session_id", "token_id")
    return None if value is None else str(value)


def validate_monotonic_chains(
    records: list[NormalizedRecord],
) -> tuple[list[Rejection], set[SourceRef]]:
    groups: dict[tuple[str, str], list[NormalizedRecord]] = {}
    for record in records:
        if record.record_type not in {"access_token", "refresh_token"}:
            continue
        chain = record.chain_id or record.token_id or record.record_id
        groups.setdefault((record.source.source_version, chain), []).append(record)

    rejections: list[Rejection] = []
    bad: set[SourceRef] = set()
    for chain_records in groups.values():
        ordered = sorted(chain_records, key=lambda item: (item.generation, item.source.record_no))
        previous: Optional[NormalizedRecord] = None
        for record in ordered:
            if previous and record.issued_at_ms is not None and previous.issued_at_ms is not None:
                if record.generation > previous.generation and record.issued_at_ms < previous.issued_at_ms:
                    rejection = Rejection(
                        E_NON_MONOTONIC_TIMESTAMP,
                        (
                            f"generation {record.generation} timestamp "
                            f"{record.issued_at_ms} precedes generation {previous.generation} "
                            f"timestamp {previous.issued_at_ms}"
                        ),
                        record.source.source_version,
                        record.source.path,
                        record.source.record_no,
                        record.record_id,
                        record.source.collection,
                    )
                    rejections.append(rejection)
                    bad.add(record.source)
            previous = record
    return rejections, bad


def verify_v2_metadata(v2_dir: str | Path, shard_records: list[NormalizedRecord]) -> list[Rejection]:
    directory = Path(v2_dir)
    db_path = directory / "metadata.sqlite"
    display = str(db_path).replace("\\", "/")
    if not db_path.exists():
        return [
            Rejection(
                E_METADATA_MISMATCH,
                "v2 metadata SQLite database is missing",
                "v2",
                display,
                0,
            )
        ]

    try:
        with sqlite3.connect(str(db_path)) as conn:
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            if "shards" not in tables:
                return [
                    Rejection(
                        E_METADATA_MISMATCH,
                        "metadata database has no shards table",
                        "v2",
                        display,
                        0,
                    )
                ]
            rows = conn.execute("SELECT shard_name, sha256, record_count FROM shards").fetchall()
    except sqlite3.DatabaseError as exc:
        return [Rejection(E_METADATA_MISMATCH, f"cannot read metadata: {exc}", "v2", display, 0)]

    rejections: list[Rejection] = []
    by_name = {path.name: path for path in sorted(directory.glob("shard-*.json"))}
    for shard_name, expected_sha, expected_count in rows:
        path = directory / str(shard_name)
        if not path.exists():
            rejections.append(
                Rejection(E_METADATA_MISMATCH, f"missing shard {shard_name}", "v2", display, 0)
            )
            continue
        actual_sha = sha256_file(path)
        if expected_sha and actual_sha != expected_sha:
            rejections.append(
                Rejection(E_METADATA_MISMATCH, f"checksum mismatch for {shard_name}", "v2", display, 0)
            )
        try:
            payload = read_json_file(path)
            entries = _entries_from_payload(payload)
            actual_count = len(entries)
        except (OSError, ValueError):
            actual_count = -1
        if expected_count is not None and actual_count != int(expected_count):
            rejections.append(
                Rejection(
                    E_METADATA_MISMATCH,
                    f"record count mismatch for {shard_name}: metadata={expected_count} json={actual_count}",
                    "v2",
                    display,
                    0,
                )
            )

    metadata_sources = {
        Path(record.source.path).name
        for record in shard_records
        if record.source.source_version == "v2"
    }
    if not rows and metadata_sources:
        rejections.append(
            Rejection(E_METADATA_MISMATCH, "metadata contains no shard rows", "v2", display, 0)
        )
    return rejections


def load_sample_input(sample_dir: str | Path, clock_path: Optional[str | Path] = None) -> SampleInput:
    root = Path(sample_dir)
    clock_file = Path(clock_path) if clock_path else root / "clock.json"
    clock_ms, _clock = load_clock(clock_file)
    context = ParseContext()
    batches: list[InputBatch] = []
    rejections: list[Rejection] = []
    sequence = 1

    v1_path = root / "v1" / "flat.json"
    if v1_path.exists():
        records, rejected = parse_json_source(
            v1_path, source_version="v1", clock_ms=clock_ms, context=context
        )
        rejections.extend(rejected)
        batches.append(InputBatch(sequence=sequence, batch_id=str(v1_path).replace("\\", "/"), records=records))
        sequence += 1

    v2_dir = root / "v2"
    v2_records: list[NormalizedRecord] = []
    if v2_dir.exists():
        shards = sorted(v2_dir.glob("shard-*.json"))
        for shard in shards:
            records, rejected = parse_json_source(
                shard, source_version="v2", clock_ms=clock_ms, context=context
            )
            rejections.extend(rejected)
            v2_records.extend(records)
            batches.append(
                InputBatch(sequence=sequence, batch_id=str(shard).replace("\\", "/"), records=records)
            )
            sequence += 1
        if shards:
            rejections.extend(verify_v2_metadata(v2_dir, v2_records))

    if not batches:
        rejections.append(
            Rejection(
                E_INVALID_JSON,
                f"sample {root} contains neither v1/flat.json nor v2/shard-*.json",
                "sample",
                str(root).replace("\\", "/"),
                0,
            )
        )

    return SampleInput(
        sample_dir=root,
        clock_ms=clock_ms,
        batches=batches,
        rejections=rejections,
        clock_path=clock_file,
    )
