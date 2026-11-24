"""Versioned v1/v2 rule snapshot normalization and deterministic merge."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import unicodedata
from pathlib import Path
from typing import Any, Iterable

from .constants import (
    DIRECTION_BOTH,
    MERGE_POLICY_VERSION,
    SOURCE_V1,
    SOURCE_V2,
    normalize_direction,
)
from .models import Rejection, RuleEntity, RuleRecord, RuleView

_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_SPACE_RE = re.compile(r"[\s_]+")


def canonical_rule_name(value: str) -> str:
    """Canonicalize the entity key without discarding dotted rule families."""

    text = unicodedata.normalize("NFKC", value).strip().casefold()
    text = _SPACE_RE.sub("-", text)
    text = re.sub(r"-{2,}", "-", text)
    return text


def _json_default(value: Any) -> Any:
    if isinstance(value, bytes):
        return value.hex()
    raise TypeError(f"not JSON serializable: {type(value)!r}")


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=_json_default,
    )


def _record_view(record: RuleRecord) -> dict[str, Any]:
    return record.to_dict()


def _validate_rule_name(value: Any) -> tuple[str | None, str | None]:
    if not isinstance(value, str):
        return None, "rule name must be a string"
    if not value.strip():
        return None, "rule name must not be empty"
    if _CONTROL_RE.search(value):
        return None, "rule name contains control characters"
    return canonical_rule_name(value), None


def _nonnegative_int(value: Any, field_name: str) -> tuple[int | None, str | None]:
    if isinstance(value, bool) or not isinstance(value, int):
        return None, f"{field_name} must be an integer"
    if value < 0:
        return None, f"{field_name} must be non-negative"
    return value, None


def _bool_value(value: Any, field_name: str, *, allow_int: bool = False) -> tuple[bool | None, str | None]:
    if isinstance(value, bool):
        return value, None
    if allow_int and value in (0, 1):
        return bool(value), None
    return None, f"{field_name} must be a boolean"


def _first(record: dict[str, Any], names: tuple[str, ...]) -> tuple[str | None, Any]:
    for name in names:
        if name in record:
            return name, record[name]
    return None, None


def _dependencies(value: Any) -> tuple[tuple[str, ...] | None, str | None]:
    if value is None:
        return (), None
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return (), None
        try:
            value = json.loads(stripped)
        except json.JSONDecodeError:
            value = [part.strip() for part in stripped.split(",") if part.strip()]
    if not isinstance(value, (list, tuple)):
        return None, "depends_on must be a list of canonical rule names"
    result: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            return None, "depends_on entries must be non-empty strings"
        if _CONTROL_RE.search(item):
            return None, "depends_on entry contains control characters"
        result.append(canonical_rule_name(item))
    return tuple(sorted(set(result))), None


def _load_v1(
    path: str | Path | None,
    observed_clock_ms: int | None,
) -> tuple[list[RuleRecord], int | None, list[Rejection]]:
    if path is None:
        return [], observed_clock_ms, []
    path = Path(path)
    rejects: list[Rejection] = []
    try:
        with path.open("r", encoding="utf-8") as handle:
            document = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        return (
            [],
            observed_clock_ms,
            [
                Rejection(
                    source=SOURCE_V1,
                    record_index=0,
                    code="E_V1_DOCUMENT",
                    message=f"cannot load v1 snapshot: {exc}",
                    record_ref=str(path),
                )
            ],
        )

    declared_clock: Any = None
    if isinstance(document, dict):
        _, declared_clock = _first(
            document,
            (
                "observed_clock_ms",
                "observed_at_ms",
                "clock_ms",
                "observation_clock_ms",
                "observed_clock",
            ),
        )
        if declared_clock is None and isinstance(document.get("clock"), dict):
            _, declared_clock = _first(
                document["clock"],
                ("observed_clock_ms", "observed_at_ms", "observed_ms", "epoch_ms"),
            )
        if declared_clock is not None:
            declared_clock, error = _nonnegative_int(
                declared_clock, "observed_clock_ms"
            )
            if error:
                rejects.append(
                    Rejection(
                        source=SOURCE_V1,
                        record_index=0,
                        code="E_V1_CLOCK",
                        message=error,
                        record={"observed_clock_ms": declared_clock},
                        record_ref=str(path),
                    )
                )
                declared_clock = None
        rules_container = document.get("rules", [])
    else:
        rules_container = document

    if observed_clock_ms is None:
        observed_clock_ms = declared_clock
    elif declared_clock is not None and observed_clock_ms != declared_clock:
        rejects.append(
            Rejection(
                source=SOURCE_V1,
                record_index=0,
                code="E_V1_CLOCK_CONFLICT",
                message=(
                    "configuration observed clock conflicts with snapshot clock; "
                    "the snapshot declaration is authoritative"
                ),
                record={
                    "configured_observed_clock_ms": observed_clock_ms,
                    "snapshot_observed_clock_ms": declared_clock,
                },
                record_ref=str(path),
            )
        )
        observed_clock_ms = declared_clock

    if not isinstance(rules_container, list):
        rejects.append(
            Rejection(
                source=SOURCE_V1,
                record_index=0,
                code="E_V1_RULES_TYPE",
                message="v1 rules must be an array",
                record_ref=str(path),
            )
        )
        return [], observed_clock_ms, rejects

    if observed_clock_ms is None:
        rejects.append(
            Rejection(
                source=SOURCE_V1,
                record_index=0,
                code="E_V1_MISSING_CLOCK",
                message=(
                    "v1 requires a sample-declared observed_clock_ms; "
                    "system time is never used"
                ),
                record_ref=str(path),
            )
        )
        return [], None, rejects

    records: list[RuleRecord] = []
    seen_names: set[str] = set()
    for index, raw in enumerate(rules_container, start=1):
        if not isinstance(raw, dict):
            rejects.append(
                Rejection(
                    source=SOURCE_V1,
                    record_index=index,
                    code="E_V1_RULE_TYPE",
                    message="v1 rule must be an object",
                    record={"value_repr": repr(raw)},
                    record_ref=f"{path}#rules/{index}",
                )
            )
            continue
        name_key, name_value = _first(raw, ("rule_name", "name", "rule"))
        seconds_key, seconds_value = _first(
            raw,
            (
                "effective_after_seconds",
                "effective_after_sec",
                "after_seconds",
                "relative_effective_seconds",
            ),
        )
        pattern_key, pattern_value = _first(raw, ("match", "pattern", "flat_match"))
        enabled_key, enabled_value = _first(raw, ("enabled", "is_enabled", "active"))
        errors: list[str] = []
        canonical, error = _validate_rule_name(name_value)
        if name_key is None:
            errors.append("missing rule_name")
        elif error:
            errors.append(error)
        seconds, error = _nonnegative_int(seconds_value, "effective_after_seconds")
        if seconds_key is None:
            errors.append("missing effective_after_seconds")
        elif error:
            errors.append(error)
        if pattern_key is None:
            errors.append("missing flat match string")
        elif not isinstance(pattern_value, str) or not pattern_value.strip():
            errors.append("match must be a non-empty string")
        enabled, error = _bool_value(enabled_value, "enabled")
        if enabled_key is None:
            errors.append("missing enabled flag")
        elif error:
            errors.append(error)
        if errors:
            rejects.append(
                Rejection(
                    source=SOURCE_V1,
                    record_index=index,
                    code="E_V1_RULE_FIELD",
                    message="; ".join(errors),
                    record=raw,
                    record_ref=f"{path}#rules/{index}",
                )
            )
            continue
        assert canonical is not None and seconds is not None and enabled is not None
        if canonical in seen_names:
            rejects.append(
                Rejection(
                    source=SOURCE_V1,
                    record_index=index,
                    code="E_V1_DUPLICATE_RULE",
                    message=f"duplicate v1 rule entity {canonical!r}",
                    record=raw,
                    record_ref=f"{path}#rules/{index}",
                )
            )
            continue
        seen_names.add(canonical)
        records.append(
            RuleRecord(
                canonical_name=canonical,
                effective_at_ms=observed_clock_ms + seconds * 1000,
                generation=0,
                direction=DIRECTION_BOTH,
                tombstone=False,
                depends_on=(),
                pattern=pattern_value,
                source=SOURCE_V1,
                source_index=index,
                source_ref=f"{path}#rules/{index}",
                enabled=enabled,
                raw_name=name_value,
            )
        )
    return records, observed_clock_ms, rejects


def _load_json_records(
    shards: Iterable[str | Path],
) -> tuple[list[dict[str, Any]], list[Rejection]]:
    records: list[dict[str, Any]] = []
    rejects: list[Rejection] = []
    global_index = 0
    for shard_path in sorted((Path(item) for item in shards), key=lambda item: str(item)):
        try:
            with shard_path.open("r", encoding="utf-8") as handle:
                document = json.load(handle)
        except (OSError, json.JSONDecodeError) as exc:
            rejects.append(
                Rejection(
                    source=SOURCE_V2,
                    record_index=global_index,
                    code="E_V2_SHARD",
                    message=f"cannot load v2 shard: {exc}",
                    record_ref=str(shard_path),
                )
            )
            continue
        if isinstance(document, dict):
            entries = document.get("rules", document.get("records", []))
        else:
            entries = document
        if not isinstance(entries, list):
            rejects.append(
                Rejection(
                    source=SOURCE_V2,
                    record_index=global_index,
                    code="E_V2_SHARD_TYPE",
                    message="v2 shard must contain a rule array",
                    record_ref=str(shard_path),
                )
            )
            continue
        for local_index, entry in enumerate(entries, start=1):
            global_index += 1
            if not isinstance(entry, dict):
                rejects.append(
                    Rejection(
                        source=SOURCE_V2,
                        record_index=global_index,
                        code="E_V2_RULE_TYPE",
                        message="v2 shard rule must be an object",
                        record={"value_repr": repr(entry)},
                        record_ref=f"{shard_path}#{local_index}",
                    )
                )
                continue
            item = dict(entry)
            item["_source_ref"] = f"{shard_path}#{local_index}"
            item["_source_index"] = global_index
            records.append(item)
    return records, rejects


def _sqlite_tables(connection: sqlite3.Connection) -> list[str]:
    rows = connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    ).fetchall()
    return [str(row[0]) for row in rows]


def _sqlite_readonly_uri(path: Path) -> str:
    return path.resolve().as_uri() + "?mode=ro"


def _load_db_records(
    database: str | Path | None,
) -> tuple[list[dict[str, Any]], list[Rejection]]:
    if database is None:
        return [], []
    database = Path(database)
    if not database.exists():
        return [], [
            Rejection(
                source=SOURCE_V2,
                record_index=0,
                code="E_V2_DB",
                message="v2 SQLite metadata database does not exist",
                record_ref=str(database),
            )
        ]
    records: list[dict[str, Any]] = []
    rejects: list[Rejection] = []
    try:
        connection = sqlite3.connect(_sqlite_readonly_uri(database), uri=True)
    except sqlite3.Error as exc:
        return [], [
            Rejection(
                source=SOURCE_V2,
                record_index=0,
                code="E_V2_DB",
                message=f"cannot open v2 SQLite metadata: {exc}",
                record_ref=str(database),
            )
        ]
    try:
        tables = _sqlite_tables(connection)
        preferred = [
            table
            for table in (
                "v2_rules",
                "v2_rule_metadata",
                "rule_metadata",
                "rules",
            )
            if table in tables
        ]
        if not preferred:
            return [], [
                Rejection(
                    source=SOURCE_V2,
                    record_index=0,
                    code="E_V2_DB_SCHEMA",
                    message="no v2_rules/rules/rule_metadata table found",
                    record={"tables": tables},
                    record_ref=str(database),
                )
            ]
        table = preferred[0]
        # Table selection is allow-listed above; quoting keeps the query valid
        # for punctuation-safe metadata schemas.
        quoted_table = '"' + table.replace('"', '""') + '"'
        cursor = connection.execute(f"SELECT rowid, * FROM {quoted_table} ORDER BY rowid")
        column_names = [description[0] for description in cursor.description]
        for row_number, row in enumerate(cursor, start=1):
            item = dict(zip(column_names, row))
            item.pop("rowid", None)
            item["_source_ref"] = f"{database}#{table}/rowid={row[0]}"
            item["_source_index"] = row_number
            item["_from_db"] = True
            records.append(item)
    except sqlite3.Error as exc:
        rejects.append(
            Rejection(
                source=SOURCE_V2,
                record_index=0,
                code="E_V2_DB_QUERY",
                message=f"cannot read v2 SQLite metadata: {exc}",
                record_ref=str(database),
            )
        )
    finally:
        connection.close()
    return records, rejects


def _merge_sources(
    shard_records: list[dict[str, Any]],
    db_records: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[Rejection]]:
    """Overlay database metadata on shard records by logical rule key."""

    def key_for(raw: dict[str, Any]) -> tuple[str, int | None, int | None]:
        name_value = next(
            (
                raw[key]
                for key in ("canonical_name", "name", "rule_name")
                if key in raw
            ),
            "",
        )
        canonical = canonical_rule_name(name_value) if isinstance(name_value, str) else ""
        generation = next(
            (
                raw[key]
                for key in ("generation", "rule_generation", "generation_no")
                if key in raw
            ),
            None,
        )
        effective = next(
            (
                raw[key]
                for key in (
                    "effective_at_ms",
                    "effective_ms",
                    "effective_at",
                    "effective_time_ms",
                )
                if key in raw
            ),
            None,
        )
        if isinstance(generation, bool) or not isinstance(generation, int):
            generation = None
        if isinstance(effective, bool) or not isinstance(effective, int):
            effective = None
        return canonical, generation, effective

    def public_record(raw: dict[str, Any]) -> dict[str, Any]:
        return {
            key: value
            for key, value in raw.items()
            if not key.startswith("_")
        }

    def authority_reject(
        raw: dict[str, Any], key: tuple[str, int | None, int | None]
    ) -> Rejection:
        return Rejection(
            source=SOURCE_V2,
            record_index=int(raw.get("_source_index", 0)),
            code="E_V2_AUTHORITY_CONFLICT",
            message=(
                "two v2 records claim the same canonical name, generation and "
                "effective time with different content"
            ),
            record={"logical_key": list(key), **public_record(raw)},
            record_ref=raw.get("_source_ref"),
        )

    merged: dict[tuple[str, int | None, int | None], dict[str, Any]] = {}
    rejects: list[Rejection] = []
    db_conflicts: set[tuple[str, int | None, int | None]] = set()
    for db in db_records:
        key = key_for(db)
        if key in merged:
            if canonical_json(public_record(merged[key])) != canonical_json(
                public_record(db)
            ):
                rejects.append(authority_reject(db, key))
                db_conflicts.add(key)
            continue
        merged[key] = dict(db)
    for key in db_conflicts:
        merged.pop(key, None)

    shard_map: dict[tuple[str, int | None, int | None], dict[str, Any]] = {}
    shard_conflicts: set[tuple[str, int | None, int | None]] = set()
    for shard in shard_records:
        key = key_for(shard)
        if key in shard_map:
            if canonical_json(public_record(shard_map[key])) != canonical_json(
                public_record(shard)
            ):
                rejects.append(authority_reject(shard, key))
                shard_conflicts.add(key)
            continue
        shard_map[key] = dict(shard)
    for key in shard_conflicts:
        shard_map.pop(key, None)
        merged.pop(key, None)

    blocked_keys = db_conflicts | shard_conflicts
    for key, shard in sorted(
        shard_map.items(),
        key=lambda item: (
            item[0][0],
            item[0][1] if item[0][1] is not None else -1,
            item[0][2] if item[0][2] is not None else -1,
            int(item[1].get("_source_index", 0)),
        ),
    ):
        if key in blocked_keys:
            continue
        key = key_for(shard)
        candidates = [key]
        if key not in merged:
            # Some metadata stores only canonical_name + generation. Prefer an
            # exact name/generation match before a name-only fallback.
            canonical, generation, _ = key
            candidates.extend(
                db_key
                for db_key in merged
                if db_key[0] == canonical and (generation is None or db_key[1] == generation)
            )
            candidates.extend(
                db_key for db_key in merged if db_key[0] == canonical and db_key not in candidates
            )
        target_key = next((candidate for candidate in candidates if candidate in merged), key)
        target = merged.get(target_key)
        if target is None:
            merged[target_key] = dict(shard)
            continue
        combined = dict(shard)
        combined.update(
            {
                field: value
                for field, value in target.items()
                if value is not None
                and field not in {"_source_ref", "_source_index", "_from_db"}
            }
        )
        if target.get("_from_db"):
            combined["_from_db"] = True
        combined["_source_ref"] = f"{shard['_source_ref']}|{target.get('_source_ref', '')}"
        combined["_source_index"] = int(shard["_source_index"])
        merged[target_key] = combined

    return list(merged.values()), rejects


def _normalize_v2(
    raw: dict[str, Any],
    index: int,
) -> tuple[RuleRecord | None, Rejection | None]:
    name_key, name_value = _first(raw, ("canonical_name", "name", "rule_name"))
    effective_key, effective_value = _first(
        raw,
        (
            "effective_at_ms",
            "effective_ms",
            "effective_at",
            "effective_time_ms",
        ),
    )
    generation_key, generation_value = _first(
        raw, ("generation", "rule_generation", "generation_no")
    )
    direction_key, direction_value = _first(raw, ("direction", "scope"))
    tombstone_key, tombstone_value = _first(raw, ("tombstone", "is_tombstone"))
    depends_key, depends_value = _first(
        raw, ("depends_on", "depends_on_json", "dependencies", "depends")
    )
    pattern_key, pattern_value = _first(raw, ("match", "pattern", "flat_match"))
    enabled_key, enabled_value = _first(raw, ("enabled", "active"))
    source_ref = raw.get("_source_ref") or f"v2#{index}"

    errors: list[str] = []
    canonical, error = _validate_rule_name(name_value)
    if name_key is None:
        errors.append("missing canonical_name")
    elif error:
        errors.append(error)
    effective, error = _nonnegative_int(effective_value, "effective_at_ms")
    if effective_key is None:
        errors.append("missing effective_at_ms")
    elif error:
        errors.append(error)
    generation, error = _nonnegative_int(generation_value, "generation")
    if generation_key is None:
        errors.append("missing generation")
    elif error:
        errors.append(error)
    direction = normalize_direction(direction_value)
    if direction_key is None:
        errors.append("missing direction")
    elif direction is None:
        errors.append("direction must be request/response/both")
    tombstone, error = _bool_value(
        tombstone_value, "tombstone", allow_int=True
    )
    if tombstone_key is None:
        errors.append("missing tombstone")
    elif error:
        errors.append(error)
    dependencies, error = _dependencies(depends_value)
    if error:
        errors.append(error)
    enabled = True
    if enabled_key is not None:
        parsed_enabled, error = _bool_value(
            enabled_value, "enabled", allow_int=True
        )
        if error:
            errors.append(error)
        else:
            enabled = bool(parsed_enabled)
    if tombstone is False:
        if pattern_key is None:
            errors.append("active v2 rule requires a match string")
        elif not isinstance(pattern_value, str) or not pattern_value.strip():
            errors.append("match must be a non-empty string")
    if errors:
        return None, Rejection(
            source=SOURCE_V2,
            record_index=int(raw.get("_source_index", index)),
            code="E_V2_RULE_FIELD",
            message="; ".join(errors),
            record={
                key: value
                for key, value in raw.items()
                if not key.startswith("_")
            },
            record_ref=source_ref,
        )
    assert (
        canonical is not None
        and effective is not None
        and generation is not None
        and direction is not None
        and tombstone is not None
        and dependencies is not None
    )
    return (
        RuleRecord(
            canonical_name=canonical,
            effective_at_ms=effective,
            generation=generation,
            direction=direction,
            tombstone=tombstone,
            depends_on=dependencies,
            pattern=pattern_value if isinstance(pattern_value, str) else None,
            source=SOURCE_V2,
            source_index=int(raw.get("_source_index", index)),
            source_ref=source_ref,
            enabled=enabled,
            raw_name=name_value,
        ),
        None,
    )


def _changed_dimensions(previous: RuleView | None, current: RuleView) -> tuple[str, ...]:
    if previous is None:
        return ("initial_snapshot",)
    dimensions: set[str] = set()
    previous_names = set(previous.entities)
    current_names = set(current.entities)
    if previous_names - current_names:
        dimensions.add("rule_removed")
    if current_names - previous_names:
        dimensions.add("rule_added")
    for name in sorted(previous_names & current_names):
        before = previous.entities[name].winner
        after = current.entities[name].winner
        if before.generation != after.generation:
            dimensions.add("generation_changed")
        if before.direction != after.direction:
            dimensions.add("direction_changed")
        if before.tombstone != after.tombstone:
            dimensions.add("tombstone_changed")
        if before.pattern != after.pattern:
            dimensions.add("pattern_changed")
        if before.depends_on != after.depends_on:
            dimensions.add("dependency_changed")
        if before.effective_at_ms != after.effective_at_ms:
            dimensions.add("effective_time_changed")
    if previous.version != current.version:
        dimensions.add("snapshot_replaced")
    return tuple(sorted(dimensions))


def load_rule_view(
    *,
    v1_path: str | Path | None,
    v2_shards: Iterable[str | Path] = (),
    v2_database: str | Path | None = None,
    v2_db: str | Path | None = None,
    observed_clock_ms: int | None = None,
    previous_view: RuleView | None = None,
) -> RuleView:
    """Load, validate and replace-rebuild a merged rule view."""

    if v2_database is None:
        v2_database = v2_db
    elif v2_db is not None and Path(v2_database) != Path(v2_db):
        raise ValueError("v2_database and v2_db point to different files")
    rejects: list[Rejection] = []
    v1_records, effective_clock, v1_rejects = _load_v1(v1_path, observed_clock_ms)
    rejects.extend(v1_rejects)
    shard_records, shard_rejects = _load_json_records(v2_shards)
    db_records, db_rejects = _load_db_records(v2_database)
    rejects.extend(shard_rejects)
    rejects.extend(db_rejects)
    merged_raw, merge_rejects = _merge_sources(shard_records, db_records)
    rejects.extend(merge_rejects)
    v2_records: list[RuleRecord] = []
    for index, raw in enumerate(
        sorted(
            merged_raw,
            key=lambda item: (
                str(item.get("canonical_name", item.get("name", ""))),
                int(item.get("generation", item.get("rule_generation", 0)) or 0),
                int(
                    item.get(
                        "effective_at_ms",
                        item.get("effective_ms", item.get("effective_at", 0)),
                    )
                    or 0
                ),
                int(item.get("_source_index", 0)),
            ),
        ),
        start=1,
    ):
        record, reject = _normalize_v2(raw, index)
        if reject is not None:
            rejects.append(reject)
        if record is not None:
            v2_records.append(record)

    records = tuple(sorted(v1_records + v2_records, key=lambda item: (
        item.canonical_name,
        item.generation,
        item.effective_at_ms,
        item.source,
        item.source_index,
    )))
    entities: dict[str, RuleEntity] = {}
    for name in sorted({record.canonical_name for record in records}):
        history = tuple(record for record in records if record.canonical_name == name)
        winner = max(history, key=lambda record: record.precedence())
        entities[name] = RuleEntity(
            canonical_name=name,
            winner=winner,
            history=history,
        )

    # Dependency references are validated against the complete entity view,
    # not import order.  Unknown references are reported but still leave the
    # dependent invalid for closure purposes.
    for name, entity in sorted(entities.items()):
        for dependency in entity.winner.depends_on:
            if dependency not in entities:
                rejects.append(
                    Rejection(
                        source=SOURCE_V2,
                        record_index=entity.winner.source_index,
                        code="E_RULE_DEP_UNKNOWN",
                        message=(
                            f"rule {name!r} references unknown dependency "
                            f"{dependency!r}"
                        ),
                        record=entity.winner.to_dict(),
                        record_ref=entity.winner.source_ref,
                    )
                )

    if effective_clock is None:
        for record in records:
            if record.source == SOURCE_V1:
                effective_clock = record.effective_at_ms
                break
    if effective_clock is None:
        effective_clock = 0

    digest_material = {
        "merge_policy_version": MERGE_POLICY_VERSION,
        "observed_clock_ms": effective_clock,
        "records": [_record_view(record) for record in records],
    }
    version = hashlib.sha256(canonical_json(digest_material).encode("utf-8")).hexdigest()
    view = RuleView(
        entities=entities,
        records=records,
        version=version,
        generation=(previous_view.generation + 1) if previous_view is not None else 1,
        observed_clock_ms=effective_clock,
        rejects=tuple(
            sorted(
                rejects,
                key=lambda item: (
                    item.source,
                    item.record_index,
                    item.code,
                    item.message,
                ),
            )
        ),
        previous_version=previous_view.version if previous_view is not None else None,
        merge_policy_version=MERGE_POLICY_VERSION,
    )
    return RuleView(
        entities=view.entities,
        records=view.records,
        version=view.version,
        generation=view.generation,
        observed_clock_ms=view.observed_clock_ms,
        rejects=view.rejects,
        previous_version=view.previous_version,
        merge_policy_version=view.merge_policy_version,
        changed_dimensions=_changed_dimensions(previous_view, view),
    )

