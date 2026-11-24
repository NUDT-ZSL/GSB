"""Parsing and unification of v1 and v2 policy snapshots."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .errors import Codes, Issue, MFAError
from .models import (
    FACTOR_TYPES,
    PolicyMeta,
    PolicyRecord,
    PolicyView,
    TrustedFactor,
)
from .util import (
    canonical_id,
    canonical_policy_key,
    issue,
    parse_bool,
    parse_int,
    parse_nonnegative_int,
    parse_positive_int,
    seconds_to_window_ms,
    sha256_canonical,
)


@dataclass
class PolicyParseResult:
    records: list[PolicyRecord]
    meta: PolicyMeta
    issues: list[Issue]
    source_counts: dict[str, int]
    policy_key: str | None = None


def _load_json_file(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def parse_v1_policy(
    path: str | Path,
    *,
    observation_clock_ms: int,
    fallback_policy_key: str = "mfa-policy",
) -> PolicyParseResult:
    source = "policy_v1"
    issues: list[Issue] = []
    records: list[PolicyRecord] = []
    policy_path = Path(path)
    try:
        document = _load_json_file(policy_path)
    except (OSError, json.JSONDecodeError) as exc:
        return PolicyParseResult(
            records=[],
            meta=PolicyMeta(),
            issues=[
                issue(
                    source,
                    0,
                    Codes.INPUT_JSON,
                    "cannot read v1 policy JSON",
                    error=str(exc),
                )
            ],
            source_counts={"v1": 0},
        )

    if not isinstance(document, dict):
        return PolicyParseResult(
            records=[],
            meta=PolicyMeta(),
            issues=[
                issue(source, 0, Codes.POLICY_STRUCTURE, "v1 policy must be an object")
            ],
            source_counts={"v1": 0},
        )

    policy_key = canonical_policy_key(document.get("policy_key", fallback_policy_key))
    if policy_key is None:
        issues.append(
            issue(source, 1, Codes.POLICY_STRUCTURE, "invalid policy_key")
        )
        policy_key = fallback_policy_key

    declared_clock = document.get("observed_at_ms")
    if declared_clock is not None:
        declared_clock_int = parse_nonnegative_int(declared_clock)
        if declared_clock_int is None:
            issues.append(
                issue(
                    source,
                    1,
                    Codes.POLICY_CLOCK,
                    "observed_at_ms must be a non-negative integer",
                )
            )
        elif observation_clock_ms is None:
            observation_clock_ms = declared_clock_int
        elif declared_clock_int != observation_clock_ms:
            issues.append(
                issue(
                    source,
                    1,
                    Codes.POLICY_CLOCK,
                    "v1 observed_at_ms disagrees with the sample observation clock",
                    document_clock_ms=declared_clock_int,
                    sample_clock_ms=observation_clock_ms,
                )
            )

    if observation_clock_ms is None or observation_clock_ms < 0:
        issues.append(
            issue(
                source,
                1,
                Codes.POLICY_CLOCK,
                "an explicit sample observation clock is required for v1 relative windows",
            )
        )
        return PolicyParseResult(
            records=[],
            meta=PolicyMeta(),
            issues=issues,
            source_counts={"v1": 0},
            policy_key=policy_key,
        )

    window_ms = seconds_to_window_ms(document.get("verification_window_seconds"))
    if window_ms is None:
        issues.append(
            issue(
                source,
                1,
                Codes.POLICY_WINDOW,
                "verification_window_seconds must convert to a positive integral millisecond window",
            )
        )

    stateless_ticket = parse_bool(document.get("stateless_ticket"))
    if stateless_ticket is None:
        issues.append(
            issue(
                source,
                1,
                Codes.POLICY_STRUCTURE,
                "stateless_ticket must be a boolean",
            )
        )

    allowlist = document.get("factor_allowlist")
    if not isinstance(allowlist, list):
        issues.append(
            issue(
                source,
                1,
                Codes.POLICY_STRUCTURE,
                "factor_allowlist must be a flat string array",
            )
        )
        allowlist = []

    seen: set[str] = set()
    for item_index, item in enumerate(allowlist, start=1):
        factor_id = canonical_id(item)
        if factor_id is None:
            issues.append(
                issue(
                    source,
                    1,
                    Codes.ID_INVALID,
                    "invalid factor_allowlist entry",
                    item_index=item_index,
                )
            )
            continue
        if factor_id in seen:
            issues.append(
                issue(
                    source,
                    1,
                    Codes.POLICY_CONFLICT,
                    "duplicate factor in v1 allowlist",
                    factor_id=factor_id,
                )
            )
            continue
        seen.add(factor_id)
        if window_ms is None or stateless_ticket is None:
            continue
        records.append(
            PolicyRecord(
                policy_key=policy_key,
                factor_id=factor_id,
                policy_generation=1,
                effective_at_ms=observation_clock_ms,
                verification_window_ms=window_ms,
                ticket_generation=1,
                ticket_generation_max=1 if stateless_ticket else 1,
                tombstone=False,
                factor_type=None,
                source="v1",
                source_index=item_index,
                source_ref=str(policy_path),
                stateless_ticket=bool(stateless_ticket),
            )
        )

    return PolicyParseResult(
        records=records,
        meta=PolicyMeta(),
        issues=issues,
        source_counts={"v1": len(records)},
        policy_key=policy_key,
    )


def _parse_v2_record(
    raw: Any,
    *,
    source_index: int,
    source: str,
    source_ref: str,
    fallback_policy_key: str,
) -> tuple[PolicyRecord | None, list[Issue]]:
    issues: list[Issue] = []
    if not isinstance(raw, dict):
        return None, [
            issue(source, source_index, Codes.POLICY_STRUCTURE, "v2 record must be an object")
        ]
    policy_key = canonical_policy_key(raw.get("policy_key", fallback_policy_key))
    if policy_key is None:
        issues.append(
            issue(source, source_index, Codes.POLICY_STRUCTURE, "invalid policy_key")
        )
    factor_id = canonical_id(raw.get("factor_id"))
    if factor_id is None:
        issues.append(
            issue(source, source_index, Codes.ID_INVALID, "invalid canonical factor_id")
        )
    policy_generation = parse_positive_int(raw.get("policy_generation"))
    if policy_generation is None:
        issues.append(
            issue(
                source,
                source_index,
                Codes.POLICY_GENERATION,
                "policy_generation must be a positive integer",
            )
        )
    effective_at_ms = parse_nonnegative_int(raw.get("effective_at_ms"))
    if effective_at_ms is None:
        issues.append(
            issue(
                source,
                source_index,
                Codes.TIME_FORMAT,
                "effective_at_ms must be a non-negative integer",
            )
        )
    window_ms = parse_positive_int(raw.get("verification_window_ms"))
    if window_ms is None:
        issues.append(
            issue(
                source,
                source_index,
                Codes.POLICY_WINDOW,
                "verification_window_ms must be a positive integer",
            )
        )
    ticket_generation = parse_nonnegative_int(raw.get("ticket_generation"))
    if ticket_generation is None:
        issues.append(
            issue(
                source,
                source_index,
                Codes.POLICY_STRUCTURE,
                "ticket_generation must be a non-negative integer",
            )
        )
    ticket_max = parse_nonnegative_int(raw.get("ticket_generation_max"))
    if ticket_max is None:
        issues.append(
            issue(
                source,
                source_index,
                Codes.POLICY_STRUCTURE,
                "ticket_generation_max must be a non-negative integer",
            )
        )
    elif ticket_generation is not None and ticket_max < ticket_generation:
        issues.append(
            issue(
                source,
                source_index,
                Codes.POLICY_STRUCTURE,
                "ticket_generation_max must not be below ticket_generation",
            )
        )
    tombstone = parse_bool(raw.get("tombstone"))
    if tombstone is None:
        issues.append(
            issue(
                source,
                source_index,
                Codes.POLICY_TOMBSTONE,
                "tombstone must be a boolean",
            )
        )
    factor_type: str | None = None
    if "factor_type" in raw and raw.get("factor_type") is not None:
        value = raw.get("factor_type")
        if not isinstance(value, str) or value.upper() not in FACTOR_TYPES:
            issues.append(
                issue(
                    source,
                    source_index,
                    Codes.FACTOR_TYPE,
                    "factor_type must be TOTP, WEBAUTHN, or PUSH",
                )
            )
        else:
            factor_type = value.upper()

    if issues:
        return None, issues
    assert policy_key is not None
    assert factor_id is not None
    assert policy_generation is not None
    assert effective_at_ms is not None
    assert window_ms is not None
    assert ticket_generation is not None
    assert ticket_max is not None
    assert tombstone is not None
    return (
        PolicyRecord(
            policy_key=policy_key,
            factor_id=factor_id,
            policy_generation=policy_generation,
            effective_at_ms=effective_at_ms,
            verification_window_ms=window_ms,
            ticket_generation=ticket_generation,
            ticket_generation_max=ticket_max,
            tombstone=tombstone,
            factor_type=factor_type,
            source=source,
            source_index=source_index,
            source_ref=source_ref,
            stateless_ticket=False,
        ),
        [],
    )


def _read_v2_shards(
    shard_paths: Iterable[str | Path],
    fallback_policy_key: str,
) -> tuple[list[PolicyRecord], list[Issue], int]:
    records: list[PolicyRecord] = []
    issues: list[Issue] = []
    count = 0
    for shard_path_value in sorted(Path(item) for item in shard_paths):
        path = Path(shard_path_value)
        try:
            document = _load_json_file(path)
        except (OSError, json.JSONDecodeError) as exc:
            issues.append(
                issue(
                    "policy_v2_shard",
                    0,
                    Codes.INPUT_JSON,
                    "cannot read v2 policy shard",
                    path=str(path),
                    error=str(exc),
                )
            )
            continue
        if isinstance(document, list):
            raw_records = document
        elif isinstance(document, dict) and isinstance(document.get("records"), list):
            raw_records = document["records"]
        else:
            issues.append(
                issue(
                    "policy_v2_shard",
                    0,
                    Codes.POLICY_STRUCTURE,
                    "v2 shard must contain a records array",
                    path=str(path),
                )
            )
            continue
        for index, raw in enumerate(raw_records, start=1):
            count += 1
            record, record_issues = _parse_v2_record(
                raw,
                source_index=index,
                source="policy_v2_shard",
                source_ref=str(path),
                fallback_policy_key=fallback_policy_key,
            )
            issues.extend(record_issues)
            if record is not None:
                records.append(record)
    return records, issues, count


def _read_v2_database(
    db_path: str | Path,
    fallback_policy_key: str,
) -> tuple[list[PolicyRecord], list[Issue], PolicyMeta, str | None, int]:
    path = Path(db_path)
    if not path.exists():
        return (
            [],
            [
                issue(
                    "policy_v2_db",
                    0,
                    Codes.POLICY_DB,
                    "v2 policy SQLite metadata database is missing",
                    path=str(path),
                )
            ],
            PolicyMeta(),
            None,
            0,
        )
    issues: list[Issue] = []
    records: list[PolicyRecord] = []
    meta = PolicyMeta()
    policy_key: str | None = None
    count = 0
    try:
        connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        try:
            table_names = {
                row["name"]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            if "policy_v2_records" in table_names:
                rows = connection.execute(
                    "SELECT * FROM policy_v2_records ORDER BY factor_id, policy_generation, effective_at_ms"
                ).fetchall()
                for index, row in enumerate(rows, start=1):
                    count += 1
                    raw = dict(row)
                    if isinstance(raw.get("tombstone"), int) and not isinstance(
                        raw.get("tombstone"), bool
                    ):
                        if raw["tombstone"] in (0, 1):
                            raw["tombstone"] = bool(raw["tombstone"])
                    record, record_issues = _parse_v2_record(
                        raw,
                        source_index=index,
                        source="policy_v2_db",
                        source_ref=str(path),
                        fallback_policy_key=fallback_policy_key,
                    )
                    issues.extend(record_issues)
                    if record is not None:
                        records.append(record)
            else:
                issues.append(
                    issue(
                        "policy_v2_db",
                        0,
                        Codes.POLICY_DB,
                        "policy_v2_records table is missing",
                        path=str(path),
                    )
                )

            if "policy_v2_meta" in table_names:
                meta_rows = connection.execute(
                    "SELECT * FROM policy_v2_meta ORDER BY policy_key"
                ).fetchall()
                meta_values: dict[str, Any] = {}
                for row in meta_rows:
                    row_dict = dict(row)
                    key = canonical_policy_key(row_dict.get("policy_key"))
                    if key is None:
                        issues.append(
                            issue(
                                "policy_v2_db",
                                0,
                                Codes.POLICY_STRUCTURE,
                                "invalid policy_v2_meta policy_key",
                            )
                        )
                        continue
                    if policy_key is None:
                        policy_key = key
                    elif key != policy_key:
                        issues.append(
                            issue(
                                "policy_v2_db",
                                0,
                                Codes.POLICY_KEY_CONFLICT,
                                "multiple policy keys in v2 metadata",
                                first=policy_key,
                                second=key,
                            )
                        )
                    for field in (
                        "canonical_scale",
                        "ticket_generation_max",
                        "factor_type_allowlist",
                        "policy_generation",
                        "effective_at_ms",
                    ):
                        if field in row_dict:
                            previous = meta_values.get(field)
                            if previous is not None and previous != row_dict[field]:
                                issues.append(
                                    issue(
                                        "policy_v2_db",
                                        0,
                                        Codes.POLICY_CONFLICT,
                                        "conflicting v2 policy metadata",
                                        field=field,
                                    )
                                )
                            meta_values[field] = row_dict[field]

                if "canonical_scale" in meta_values:
                    value = parse_nonnegative_int(meta_values["canonical_scale"])
                    if value is None or value > 6:
                        issues.append(
                            issue(
                                "policy_v2_db",
                                0,
                                Codes.SCALE_RANGE,
                                "canonical_scale must be an integer in [0, 6]",
                            )
                        )
                    else:
                        meta.canonical_scale = value
                        meta.canonical_scale_defined = True
                if "ticket_generation_max" in meta_values:
                    value = parse_nonnegative_int(meta_values["ticket_generation_max"])
                    if value is None:
                        issues.append(
                            issue(
                                "policy_v2_db",
                                0,
                                Codes.POLICY_STRUCTURE,
                                "ticket_generation_max metadata must be non-negative",
                            )
                        )
                    else:
                        meta.ticket_generation_max = value
                if "policy_generation" in meta_values:
                    value = parse_positive_int(meta_values["policy_generation"])
                    if value is None:
                        issues.append(
                            issue(
                                "policy_v2_db",
                                0,
                                Codes.POLICY_GENERATION,
                                "policy_generation metadata must be positive",
                            )
                        )
                    else:
                        meta.policy_generation = value
                if "effective_at_ms" in meta_values:
                    value = parse_nonnegative_int(meta_values["effective_at_ms"])
                    if value is None:
                        issues.append(
                            issue(
                                "policy_v2_db",
                                0,
                                Codes.TIME_FORMAT,
                                "effective_at_ms metadata must be non-negative",
                            )
                        )
                    else:
                        meta.effective_at_ms = value
                if "factor_type_allowlist" in meta_values:
                    raw_types = meta_values["factor_type_allowlist"]
                    if isinstance(raw_types, str):
                        try:
                            raw_types = json.loads(raw_types)
                        except json.JSONDecodeError:
                            issues.append(
                                issue(
                                    "policy_v2_db",
                                    0,
                                    Codes.POLICY_STRUCTURE,
                                    "factor_type_allowlist metadata is not valid JSON",
                                )
                            )
                            raw_types = None
                    if isinstance(raw_types, list) and raw_types:
                        normalized_types: list[str] = []
                        for item in raw_types:
                            if not isinstance(item, str) or item.upper() not in FACTOR_TYPES:
                                issues.append(
                                    issue(
                                        "policy_v2_db",
                                        0,
                                        Codes.FACTOR_TYPE,
                                        "factor_type_allowlist contains an invalid type",
                                    )
                                )
                                continue
                            normalized_types.append(item.upper())
                        if normalized_types:
                            meta.allowed_factor_types = tuple(sorted(set(normalized_types)))
                    elif raw_types is not None:
                        issues.append(
                            issue(
                                "policy_v2_db",
                                0,
                                Codes.POLICY_STRUCTURE,
                                "factor_type_allowlist metadata must be a non-empty array",
                            )
                        )
        finally:
            connection.close()
    except sqlite3.Error as exc:
        issues.append(
            issue(
                "policy_v2_db",
                0,
                Codes.POLICY_DB,
                "cannot read v2 policy SQLite metadata",
                error=str(exc),
            )
        )
    return records, issues, meta, policy_key, count


def load_policy(
    *,
    policy_key: str,
    observation_clock_ms: int,
    v1_path: str | Path | None = None,
    v2_shard_paths: Iterable[str | Path] = (),
    v2_db_path: str | Path | None = None,
    as_of_ms: int,
    factor_types: dict[str, str] | None = None,
    canonical_scale: int = 6,
) -> PolicyView:
    issues: list[Issue] = []
    records: list[PolicyRecord] = []
    source_counts = {"v1": 0, "v2_shard": 0, "v2_db": 0}
    meta = PolicyMeta(canonical_scale=canonical_scale)
    policy_key = canonical_policy_key(policy_key)
    if policy_key is None:
        raise MFAError(Codes.POLICY_STRUCTURE, "policy_key is invalid")

    if v1_path is not None:
        v1 = parse_v1_policy(
            v1_path,
            observation_clock_ms=observation_clock_ms,
            fallback_policy_key=policy_key,
        )
        if v1.policy_key and v1.policy_key != policy_key:
            issues.append(
                issue(
                    "policy_v1",
                    1,
                    Codes.POLICY_KEY_CONFLICT,
                    "v1 policy_key does not match expected policy key",
                    expected=policy_key,
                    actual=v1.policy_key,
                )
            )
        else:
            records.extend(v1.records)
        issues.extend(v1.issues)
        source_counts["v1"] = v1.source_counts.get("v1", 0)

    shard_records, shard_issues, shard_count = _read_v2_shards(
        v2_shard_paths, policy_key
    )
    records.extend(shard_records)
    issues.extend(shard_issues)
    source_counts["v2_shard"] = shard_count

    if v2_db_path is not None:
        db_records, db_issues, db_meta, db_policy_key, db_count = _read_v2_database(
            v2_db_path, policy_key
        )
        if db_policy_key and db_policy_key != policy_key:
            issues.append(
                issue(
                    "policy_v2_db",
                    0,
                    Codes.POLICY_KEY_CONFLICT,
                    "v2 metadata policy_key does not match expected policy key",
                    expected=policy_key,
                    actual=db_policy_key,
                )
            )
        else:
            records.extend(db_records)
        issues.extend(db_issues)
        meta = db_meta
        source_counts["v2_db"] = db_count

    if not meta.canonical_scale_defined:
        meta.canonical_scale = canonical_scale
    if not records and not any(
        item.code in {Codes.POLICY_STRUCTURE, Codes.INPUT_JSON} for item in issues
    ):
        issues.append(
            issue(
                "policy",
                0,
                Codes.POLICY_STRUCTURE,
                "no policy records were parsed",
            )
        )

    return build_policy_view(
        records=records,
        issues=issues,
        policy_key=policy_key,
        as_of_ms=as_of_ms,
        meta=meta,
        source_counts=source_counts,
        factor_types=factor_types or {},
    )


def _record_business_tuple(record: PolicyRecord) -> tuple[Any, ...]:
    return (
        record.policy_key,
        record.factor_id,
        record.policy_generation,
        record.effective_at_ms,
        record.verification_window_ms,
        record.ticket_generation,
        record.ticket_generation_max,
        record.tombstone,
        record.factor_type,
        record.stateless_ticket,
    )


def build_policy_view(
    *,
    records: list[PolicyRecord],
    issues: list[Issue],
    policy_key: str,
    as_of_ms: int,
    meta: PolicyMeta,
    source_counts: dict[str, int],
    factor_types: dict[str, str],
) -> PolicyView:
    deduplicated: dict[tuple[Any, ...], PolicyRecord] = {}
    identity_to_record: dict[tuple[Any, ...], PolicyRecord] = {}
    for record in records:
        if record.policy_key != policy_key:
            issues.append(
                issue(
                    record.source,
                    record.source_index,
                    Codes.POLICY_KEY_CONFLICT,
                    "record belongs to a different policy key",
                    expected=policy_key,
                    actual=record.policy_key,
                )
            )
            continue
        identity = (
            record.policy_key,
            record.factor_id,
            record.policy_generation,
            record.effective_at_ms,
        )
        existing_identity = identity_to_record.get(identity)
        if existing_identity is not None:
            if _record_business_tuple(existing_identity) != _record_business_tuple(record):
                issues.append(
                    issue(
                        record.source,
                        record.source_index,
                        Codes.POLICY_CONFLICT,
                        "conflicting v2 records share the same authoritative identity",
                        factor_id=record.factor_id,
                        policy_generation=record.policy_generation,
                        effective_at_ms=record.effective_at_ms,
                    )
                )
            continue
        identity_to_record[identity] = record
        key = _record_business_tuple(record)
        deduplicated[key] = record

    eligible = [
        record for record in deduplicated.values() if record.effective_at_ms <= as_of_ms
    ]
    by_factor: dict[str, list[PolicyRecord]] = {}
    for record in eligible:
        by_factor.setdefault(record.factor_id, []).append(record)

    trusted: dict[str, TrustedFactor] = {}
    max_generation = meta.policy_generation or 0
    for factor_id, candidates in by_factor.items():
        candidates.sort(
            key=lambda record: (
                record.policy_generation,
                record.effective_at_ms,
                record.tombstone,
                record.source,
            )
        )
        selected = candidates[-1]
        max_generation = max(max_generation, selected.policy_generation)
        if selected.tombstone:
            continue

        resolved_type = selected.factor_type or factor_types.get(factor_id)
        if resolved_type is None:
            issues.append(
                issue(
                    selected.source,
                    selected.source_index,
                    Codes.POLICY_UNKNOWN_FACTOR,
                    "trusted factor has no matching registration",
                    factor_id=factor_id,
                )
            )
            continue
        if resolved_type not in meta.allowed_factor_types:
            issues.append(
                issue(
                    selected.source,
                    selected.source_index,
                    Codes.POLICY_TYPE_DENIED,
                    "trusted factor type is outside the metadata allowlist",
                    factor_id=factor_id,
                    factor_type=resolved_type,
                )
            )
            continue
        ticket_max = selected.ticket_generation_max
        if meta.ticket_generation_max is not None:
            ticket_max = min(ticket_max, meta.ticket_generation_max)
        if ticket_max < selected.ticket_generation:
            issues.append(
                issue(
                    selected.source,
                    selected.source_index,
                    Codes.POLICY_STRUCTURE,
                    "effective ticket generation exceeds ticket generation maximum",
                    factor_id=factor_id,
                )
            )
            continue
        trusted[factor_id] = TrustedFactor(
            factor_id=factor_id,
            factor_type=resolved_type,
            policy_generation=selected.policy_generation,
            effective_at_ms=selected.effective_at_ms,
            verification_window_ms=selected.verification_window_ms,
            ticket_generation=selected.ticket_generation,
            ticket_generation_max=ticket_max,
            source=selected.source,
            window_basis=(
                "v1_relative_seconds_to_absolute_ms"
                if selected.source == "v1"
                else "v2_absolute_ms"
            ),
            stateless_ticket=selected.stateless_ticket,
        )

    version_material = {
        "policy_key": policy_key,
        "trusted_factors": [
            {
                "factor_id": factor.factor_id,
                "factor_type": factor.factor_type,
                "policy_generation": factor.policy_generation,
                "effective_at_ms": factor.effective_at_ms,
                "verification_window_ms": factor.verification_window_ms,
                "ticket_generation": factor.ticket_generation,
                "ticket_generation_max": factor.ticket_generation_max,
                "window_basis": factor.window_basis,
                "stateless_ticket": factor.stateless_ticket,
            }
            for factor in (trusted[key] for key in sorted(trusted))
        ],
        "allowed_factor_types": sorted(meta.allowed_factor_types),
        "canonical_scale": meta.canonical_scale,
        "metadata_policy_generation": meta.policy_generation,
        "metadata_effective_at_ms": meta.effective_at_ms,
    }
    layout_material = {
        "factor_type_allowlist": sorted(meta.allowed_factor_types),
        "canonical_scale": meta.canonical_scale,
        "window_basis_by_factor": {
            key: trusted[key].window_basis for key in sorted(trusted)
        },
        "window_ms_by_factor": {
            key: trusted[key].verification_window_ms for key in sorted(trusted)
        },
        "ticket_generation_max_by_factor": {
            key: trusted[key].ticket_generation_max for key in sorted(trusted)
        },
    }
    return PolicyView(
        policy_key=policy_key,
        as_of_ms=as_of_ms,
        policy_generation=max_generation,
        policy_version=sha256_canonical(version_material),
        layout_version=sha256_canonical(layout_material),
        trusted_factors=trusted,
        allowed_factor_types=tuple(sorted(meta.allowed_factor_types)),
        canonical_scale=meta.canonical_scale,
        source_counts=source_counts,
        issues=issues,
        considered_records=len(records),
    )

