"""Strict deterministic parsing for all three local JSON input types."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .errors import E_SCHEMA
from .models import (
    CERT_FIELDS,
    CERT_SCHEMA,
    CHAIN_FIELDS,
    CHAIN_SCHEMA,
    FINGERPRINT_RE,
    ID_RE,
    POLICY_RE,
    REVOCATION_FIELDS,
    SNAPSHOT_FIELDS,
    SNAPSHOT_SCHEMA,
    Certificate,
    ChainDeclaration,
    Rejection,
    RevocationEntry,
    Snapshot,
    is_strict_int,
    snapshot_content_fingerprint,
)


@dataclass
class ParseResult:
    items: list[Any]
    rejections: list[Rejection]
    declared_count: int


def _source_name(path: Path) -> str:
    return path.name


def _reject(
    *,
    scope: str,
    source_file: str,
    entry_index: int,
    reason: str,
    message: str,
    chain_id: str | None = None,
) -> Rejection:
    return Rejection(
        scope=scope,
        source_file=source_file,
        entry_index=entry_index,
        code=E_SCHEMA,
        reason=reason,
        message=message,
        chain_id=chain_id,
    )


def _read_document(
    path: Path, scope: str
) -> tuple[dict[str, Any] | None, Rejection | None]:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        return None, _reject(
            scope=scope,
            source_file=_source_name(path),
            entry_index=-1,
            reason="unreadable_file",
            message=f"cannot read input file: {exc.__class__.__name__}",
        )
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None, _reject(
            scope=scope,
            source_file=_source_name(path),
            entry_index=-1,
            reason="invalid_json",
            message="input file is not valid UTF-8 JSON",
        )
    if not isinstance(value, dict):
        return None, _reject(
            scope=scope,
            source_file=_source_name(path),
            entry_index=-1,
            reason="invalid_document",
            message="input document must be a JSON object",
        )
    return value, None


def _dedupe_paths(paths: Iterable[str | Path]) -> list[Path]:
    seen: set[str] = set()
    result: list[Path] = []
    for raw in paths:
        path = Path(raw)
        key = str(path.resolve())
        if key in seen:
            continue
        seen.add(key)
        result.append(path)
    return result


def _check_unknown_fields(
    value: dict[str, Any], allowed: set[str]
) -> str | None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        return f"unknown field(s): {', '.join(unknown)}"
    return None


def _validate_id(value: Any, field: str) -> str | None:
    if not isinstance(value, str) or not ID_RE.fullmatch(value):
        return f"{field} must match {ID_RE.pattern}"
    return None


def _parse_certificate(
    entry: Any, source_file: str, entry_index: int
) -> tuple[Certificate | None, Rejection | None]:
    if not isinstance(entry, dict):
        return None, _reject(
            scope="certificate",
            source_file=source_file,
            entry_index=entry_index,
            reason="invalid_entry",
            message="certificate entry must be a JSON object",
        )
    unknown = _check_unknown_fields(entry, CERT_FIELDS)
    if unknown:
        return None, _reject(
            scope="certificate",
            source_file=source_file,
            entry_index=entry_index,
            reason="unknown_field",
            message=unknown,
        )
    cert_id = entry.get("id")
    issuer_id = entry.get("issuer_id")
    fingerprint = entry.get("public_key_fingerprint")
    valid_from = entry.get("valid_from_ms")
    valid_to = entry.get("valid_to_ms")
    revoked = entry.get("revoked")

    for value, field in ((cert_id, "id"), (issuer_id, "issuer_id")):
        reason = _validate_id(value, field)
        if reason:
            return None, _reject(
                scope="certificate",
                source_file=source_file,
                entry_index=entry_index,
                reason=f"invalid_{field}",
                message=reason,
            )
    if not isinstance(fingerprint, str) or not FINGERPRINT_RE.fullmatch(
        fingerprint.lower()
    ):
        return None, _reject(
            scope="certificate",
            source_file=source_file,
            entry_index=entry_index,
            reason="invalid_fingerprint",
            message="public_key_fingerprint must be sha256:<64 hex characters>",
        )
    if not is_strict_int(valid_from) or not is_strict_int(valid_to):
        return None, _reject(
            scope="certificate",
            source_file=source_file,
            entry_index=entry_index,
            reason="invalid_validity",
            message="valid_from_ms and valid_to_ms must be integer milliseconds",
        )
    if valid_from >= valid_to:
        return None, _reject(
            scope="certificate",
            source_file=source_file,
            entry_index=entry_index,
            reason="invalid_validity_interval",
            message="valid_from_ms must be less than valid_to_ms",
        )
    if type(revoked) is not bool:
        return None, _reject(
            scope="certificate",
            source_file=source_file,
            entry_index=entry_index,
            reason="invalid_revoked_status",
            message="revoked must be a boolean",
        )
    return (
        Certificate(
            cert_id=cert_id,
            issuer_id=issuer_id,
            public_key_fingerprint=fingerprint.lower(),
            valid_from_ms=valid_from,
            valid_to_ms=valid_to,
            revoked=revoked,
            source_file=source_file,
            entry_index=entry_index,
        ),
        None,
    )


def parse_certificate_files(paths: Iterable[str | Path]) -> ParseResult:
    records: list[Certificate] = []
    rejections: list[Rejection] = []
    declared_count = 0

    for path in _dedupe_paths(paths):
        source_file = _source_name(path)
        document, document_rejection = _read_document(path, "certificate")
        if document_rejection is not None:
            rejections.append(document_rejection)
            continue
        assert document is not None
        if document.get("schema") != CERT_SCHEMA:
            rejections.append(
                _reject(
                    scope="certificate",
                    source_file=source_file,
                    entry_index=-1,
                    reason="invalid_document_schema",
                    message=f"schema must be {CERT_SCHEMA}",
                )
            )
            continue
        entries = document.get("certificates")
        if not isinstance(entries, list):
            rejections.append(
                _reject(
                    scope="certificate",
                    source_file=source_file,
                    entry_index=-1,
                    reason="invalid_certificates",
                    message="certificates must be a JSON array",
                )
            )
            continue
        extra = sorted(set(document) - {"schema", "certificates"})
        if extra:
            rejections.append(
                _reject(
                    scope="certificate",
                    source_file=source_file,
                    entry_index=-1,
                    reason="unknown_field",
                    message=f"unknown top-level field(s): {', '.join(extra)}",
                )
            )
            continue
        declared_count += len(entries)
        for entry_index, entry in enumerate(entries):
            parsed, rejection = _parse_certificate(
                entry, source_file, entry_index
            )
            if rejection is not None:
                rejections.append(rejection)
            else:
                assert parsed is not None
                records.append(parsed)

    by_id: dict[str, list[Certificate]] = {}
    for record in records:
        by_id.setdefault(record.cert_id, []).append(record)

    candidates: list[Certificate] = []
    for cert_id in sorted(by_id):
        group = sorted(
            by_id[cert_id],
            key=lambda item: (item.source_file, item.entry_index),
        )
        if len(group) > 1:
            for duplicate in group:
                rejections.append(
                    _reject(
                        scope="certificate",
                        source_file=duplicate.source_file,
                        entry_index=duplicate.entry_index,
                        reason="duplicate_certificate_id",
                        message=f"certificate id '{cert_id}' is not globally unique",
                    )
                )
            continue
        candidates.append(group[0])

    known_ids = {record.cert_id for record in candidates}
    while True:
        unresolved = [
            record for record in candidates if record.issuer_id not in known_ids
        ]
        if not unresolved:
            break
        unresolved_ids = {record.cert_id for record in unresolved}
        for record in sorted(
            unresolved, key=lambda item: (item.cert_id, item.source_file, item.entry_index)
        ):
            rejections.append(
                _reject(
                    scope="certificate",
                    source_file=record.source_file,
                    entry_index=record.entry_index,
                    reason="issuer_unresolved",
                    message=(
                        f"issuer_id '{record.issuer_id}' does not resolve to a "
                        "valid certificate"
                    ),
                )
            )
        candidates = [
            record for record in candidates if record.cert_id not in unresolved_ids
        ]
        known_ids = {record.cert_id for record in candidates}

    candidates.sort(key=lambda item: item.cert_id)
    rejections.sort(key=lambda item: item.sort_key)
    return ParseResult(candidates, rejections, declared_count)


def _parse_revocation_entry(
    entry: Any,
    source_file: str,
    entry_index: int,
    observed_at_ms: int,
    field_name: str,
) -> tuple[RevocationEntry | None, str | None]:
    if not isinstance(entry, dict):
        return None, f"{field_name} entries must be JSON objects"
    unknown = _check_unknown_fields(entry, REVOCATION_FIELDS)
    if unknown:
        return None, unknown
    cert_id = entry.get("cert_id")
    reason = _validate_id(cert_id, "cert_id")
    if reason:
        return None, reason
    revoked_at = entry.get("revoked_at_ms")
    if not is_strict_int(revoked_at):
        return None, "revoked_at_ms must be an integer"
    if revoked_at > observed_at_ms:
        return None, "revoked_at_ms cannot be later than observed_at_ms"
    reason_text = entry.get("reason", "")
    if not isinstance(reason_text, str) or len(reason_text) > 256:
        return None, "reason must be a string of at most 256 characters"
    return RevocationEntry(cert_id, revoked_at, reason_text), None


def _parse_snapshot(
    entry: Any, source_file: str, entry_index: int
) -> tuple[Snapshot | None, Rejection | None]:
    if not isinstance(entry, dict):
        return None, _reject(
            scope="snapshot",
            source_file=source_file,
            entry_index=entry_index,
            reason="invalid_entry",
            message="snapshot entry must be a JSON object",
        )
    unknown = _check_unknown_fields(entry, SNAPSHOT_FIELDS)
    if unknown:
        return None, _reject(
            scope="snapshot",
            source_file=source_file,
            entry_index=entry_index,
            reason="unknown_field",
            message=unknown,
        )
    version = entry.get("version")
    policy_version = entry.get("policy_version")
    roots = entry.get("trusted_roots")
    revocation_entries = entry.get("revoked_entries")
    observed_at = entry.get("observed_at_ms")

    if not is_strict_int(version) or version < 1:
        return None, _reject(
            scope="snapshot",
            source_file=source_file,
            entry_index=entry_index,
            reason="invalid_version",
            message="version must be an integer greater than or equal to 1",
        )
    if not isinstance(policy_version, str) or not POLICY_RE.fullmatch(policy_version):
        return None, _reject(
            scope="snapshot",
            source_file=source_file,
            entry_index=entry_index,
            reason="invalid_policy_version",
            message="policy_version must be a semantic version such as 1.2.0",
        )
    if not isinstance(roots, list):
        return None, _reject(
            scope="snapshot",
            source_file=source_file,
            entry_index=entry_index,
            reason="invalid_trusted_roots",
            message="trusted_roots must be a JSON array",
        )
    if len(set(roots)) != len(roots):
        return None, _reject(
            scope="snapshot",
            source_file=source_file,
            entry_index=entry_index,
            reason="duplicate_trusted_root",
            message="trusted_roots must not contain duplicates",
        )
    for root in roots:
        reason = _validate_id(root, "trusted_roots")
        if reason:
            return None, _reject(
                scope="snapshot",
                source_file=source_file,
                entry_index=entry_index,
                reason="invalid_trusted_root",
                message=reason,
            )
    if not is_strict_int(observed_at):
        return None, _reject(
            scope="snapshot",
            source_file=source_file,
            entry_index=entry_index,
            reason="invalid_observed_at",
            message="observed_at_ms must be an integer",
        )
    if not isinstance(revocation_entries, list):
        return None, _reject(
            scope="snapshot",
            source_file=source_file,
            entry_index=entry_index,
            reason="invalid_revoked_entries",
            message="revoked_entries must be a JSON array",
        )

    revocations: list[RevocationEntry] = []
    seen_revocations: set[str] = set()
    for revocation_index, revocation_value in enumerate(revocation_entries):
        revocation, reason = _parse_revocation_entry(
            revocation_value,
            source_file,
            entry_index,
            observed_at,
            f"revoked_entries[{revocation_index}]",
        )
        if reason is not None:
            return None, _reject(
                scope="snapshot",
                source_file=source_file,
                entry_index=entry_index,
                reason="invalid_revocation_entry",
                message=reason,
            )
        assert revocation is not None
        if revocation.cert_id in seen_revocations:
            return None, _reject(
                scope="snapshot",
                source_file=source_file,
                entry_index=entry_index,
                reason="duplicate_revocation",
                message=(
                    f"revoked_entries contains duplicate cert_id "
                    f"'{revocation.cert_id}'"
                ),
            )
        seen_revocations.add(revocation.cert_id)
        revocations.append(revocation)

    roots_sorted = tuple(sorted(roots))
    revocations_sorted = tuple(
        sorted(revocations, key=lambda item: item.cert_id)
    )
    fingerprint = snapshot_content_fingerprint(
        version=version,
        policy_version=policy_version,
        trusted_roots=roots_sorted,
        revoked_entries=revocations_sorted,
        observed_at_ms=observed_at,
    )
    return (
        Snapshot(
            version=version,
            policy_version=policy_version,
            trusted_roots=roots_sorted,
            revoked_entries=revocations_sorted,
            observed_at_ms=observed_at,
            content_fingerprint=fingerprint,
            source_file=source_file,
            entry_index=entry_index,
        ),
        None,
    )


def parse_snapshot_files(paths: Iterable[str | Path]) -> ParseResult:
    snapshots: list[Snapshot] = []
    rejections: list[Rejection] = []
    declared_count = 0
    for path in _dedupe_paths(paths):
        source_file = _source_name(path)
        document, document_rejection = _read_document(path, "snapshot")
        if document_rejection is not None:
            rejections.append(document_rejection)
            continue
        assert document is not None
        if document.get("schema") != SNAPSHOT_SCHEMA:
            rejections.append(
                _reject(
                    scope="snapshot",
                    source_file=source_file,
                    entry_index=-1,
                    reason="invalid_document_schema",
                    message=f"schema must be {SNAPSHOT_SCHEMA}",
                )
            )
            continue
        entries = document.get("snapshots")
        if not isinstance(entries, list):
            rejections.append(
                _reject(
                    scope="snapshot",
                    source_file=source_file,
                    entry_index=-1,
                    reason="invalid_snapshots",
                    message="snapshots must be a JSON array",
                )
            )
            continue
        extra = sorted(set(document) - {"schema", "snapshots"})
        if extra:
            rejections.append(
                _reject(
                    scope="snapshot",
                    source_file=source_file,
                    entry_index=-1,
                    reason="unknown_field",
                    message=f"unknown top-level field(s): {', '.join(extra)}",
                )
            )
            continue
        declared_count += len(entries)
        for entry_index, entry in enumerate(entries):
            snapshot, rejection = _parse_snapshot(entry, source_file, entry_index)
            if rejection is not None:
                rejections.append(rejection)
            else:
                assert snapshot is not None
                snapshots.append(snapshot)

    snapshots.sort(key=lambda item: (item.version, item.content_fingerprint))
    rejections.sort(key=lambda item: item.sort_key)
    return ParseResult(snapshots, rejections, declared_count)


def resolve_snapshot_candidates(
    candidates: Iterable[Snapshot],
) -> tuple[list[Snapshot], list[Rejection], list[Snapshot]]:
    """Resolve same-version conflicts with an arrival-order-independent rule.

    The lexicographically smallest content fingerprint wins for each version.
    Exact duplicates are deduplicated. Losing candidates are retained in the
    returned all-candidates list so the store can remain order independent.
    """

    unique: dict[tuple[int, str], Snapshot] = {}
    for candidate in candidates:
        key = (candidate.version, candidate.content_fingerprint)
        unique[key] = candidate

    by_version: dict[int, list[Snapshot]] = {}
    for candidate in unique.values():
        by_version.setdefault(candidate.version, []).append(candidate)

    accepted: list[Snapshot] = []
    rejections: list[Rejection] = []
    for version in sorted(by_version):
        group = sorted(
            by_version[version],
            key=lambda item: (
                item.content_fingerprint,
                item.source_file,
                item.entry_index,
            ),
        )
        winner = group[0]
        accepted.append(winner)
        for loser in group[1:]:
            rejections.append(
                _reject(
                    scope="snapshot",
                    source_file=loser.source_file,
                    entry_index=loser.entry_index,
                    reason="snapshot_version_conflict",
                    message=(
                        f"snapshot version {version} has conflicting content; "
                        f"canonical fingerprint is {winner.content_fingerprint}"
                    ),
                )
            )
    accepted.sort(key=lambda item: (item.version, item.content_fingerprint))
    rejections.sort(key=lambda item: item.sort_key)
    all_candidates = sorted(
        unique.values(),
        key=lambda item: (
            item.version,
            item.content_fingerprint,
            item.source_file,
            item.entry_index,
        ),
    )
    return accepted, rejections, all_candidates


def _parse_chain(
    entry: Any, source_file: str, entry_index: int
) -> tuple[ChainDeclaration | None, Rejection | None]:
    declared_chain_id: str | None = None
    if isinstance(entry, dict) and isinstance(entry.get("chain_id"), str):
        declared_chain_id = entry.get("chain_id")
    if not isinstance(entry, dict):
        return None, _reject(
            scope="chain",
            source_file=source_file,
            entry_index=entry_index,
            reason="invalid_entry",
            message="chain entry must be a JSON object",
        )
    unknown = _check_unknown_fields(entry, CHAIN_FIELDS)
    if unknown:
        return None, _reject(
            scope="chain",
            source_file=source_file,
            entry_index=entry_index,
            reason="unknown_field",
            message=unknown,
            chain_id=declared_chain_id,
        )
    chain_id = entry.get("chain_id")
    reason = _validate_id(chain_id, "chain_id")
    if reason:
        return None, _reject(
            scope="chain",
            source_file=source_file,
            entry_index=entry_index,
            reason="invalid_chain_id",
            message=reason,
            chain_id=declared_chain_id,
        )
    cert_ids = entry.get("certificate_ids")
    if not isinstance(cert_ids, list):
        return None, _reject(
            scope="chain",
            source_file=source_file,
            entry_index=entry_index,
            reason="invalid_certificate_ids",
            message="certificate_ids must be a JSON array",
            chain_id=chain_id,
        )
    for position, cert_id in enumerate(cert_ids):
        reason = _validate_id(cert_id, f"certificate_ids[{position}]")
        if reason:
            return None, _reject(
                scope="chain",
                source_file=source_file,
                entry_index=entry_index,
                reason="invalid_certificate_id",
                message=reason,
                chain_id=chain_id,
            )
    if len(set(cert_ids)) != len(cert_ids):
        return None, _reject(
            scope="chain",
            source_file=source_file,
            entry_index=entry_index,
            reason="duplicate_chain_certificate",
            message="certificate_ids must not contain duplicate identifiers",
            chain_id=chain_id,
        )
    deadline_ms = entry.get("deadline_ms")
    if not is_strict_int(deadline_ms) or deadline_ms <= 0:
        return None, _reject(
            scope="chain",
            source_file=source_file,
            entry_index=entry_index,
            reason="invalid_deadline",
            message="deadline_ms must be a positive integer",
            chain_id=chain_id,
        )
    expected_summary = entry.get("expected_summary")
    if not isinstance(expected_summary, str) or not expected_summary:
        return None, _reject(
            scope="chain",
            source_file=source_file,
            entry_index=entry_index,
            reason="invalid_expected_summary",
            message="expected_summary must be a non-empty string",
            chain_id=chain_id,
        )
    return (
        ChainDeclaration(
            chain_id=chain_id,
            certificate_ids=tuple(cert_ids),
            deadline_ms=deadline_ms,
            expected_summary=expected_summary,
            source_file=source_file,
            entry_index=entry_index,
        ),
        None,
    )


def parse_chain_files(paths: Iterable[str | Path]) -> ParseResult:
    records: list[ChainDeclaration] = []
    rejections: list[Rejection] = []
    declared_count = 0
    for path in _dedupe_paths(paths):
        source_file = _source_name(path)
        document, document_rejection = _read_document(path, "chain")
        if document_rejection is not None:
            rejections.append(document_rejection)
            continue
        assert document is not None
        if document.get("schema") != CHAIN_SCHEMA:
            rejections.append(
                _reject(
                    scope="chain",
                    source_file=source_file,
                    entry_index=-1,
                    reason="invalid_document_schema",
                    message=f"schema must be {CHAIN_SCHEMA}",
                )
            )
            continue
        entries = document.get("chains")
        if not isinstance(entries, list):
            rejections.append(
                _reject(
                    scope="chain",
                    source_file=source_file,
                    entry_index=-1,
                    reason="invalid_chains",
                    message="chains must be a JSON array",
                )
            )
            continue
        extra = sorted(set(document) - {"schema", "chains"})
        if extra:
            rejections.append(
                _reject(
                    scope="chain",
                    source_file=source_file,
                    entry_index=-1,
                    reason="unknown_field",
                    message=f"unknown top-level field(s): {', '.join(extra)}",
                )
            )
            continue
        declared_count += len(entries)
        for entry_index, entry in enumerate(entries):
            chain, rejection = _parse_chain(entry, source_file, entry_index)
            if rejection is not None:
                rejections.append(rejection)
            else:
                assert chain is not None
                records.append(chain)

    by_id: dict[str, list[ChainDeclaration]] = {}
    for record in records:
        by_id.setdefault(record.chain_id, []).append(record)

    accepted: list[ChainDeclaration] = []
    for chain_id in sorted(by_id):
        group = sorted(by_id[chain_id], key=lambda item: item.sort_key)
        if len(group) > 1:
            for duplicate in group:
                rejections.append(
                    _reject(
                        scope="chain",
                        source_file=duplicate.source_file,
                        entry_index=duplicate.entry_index,
                        reason="duplicate_chain_id",
                        message=f"chain_id '{chain_id}' is not globally unique",
                        chain_id=chain_id,
                    )
                )
            continue
        accepted.append(group[0])

    accepted.sort(key=lambda item: item.sort_key)
    rejections.sort(key=lambda item: item.sort_key)
    return ParseResult(accepted, rejections, declared_count)
