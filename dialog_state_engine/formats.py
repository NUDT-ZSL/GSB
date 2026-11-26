from __future__ import annotations

import os
import shutil
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .canonical import (
    atomic_write_json,
    content_hash,
    read_json,
    sha256_text,
)
from .config import Rules
from .errors import E_FORMAT_CORRUPT, E_VERSION_UNSUPPORTED, fail
from .models import (
    Candidate,
    InternalState,
    ReadResult,
    SlotState,
    completion,
)
from .validation import (
    require_int,
    require_list,
    require_mapping,
    validate_confidence,
    validate_json_value,
    validate_session_id,
    validate_slot_key,
)


V1 = "v1"
V2 = "v2"
V3 = "v3"
SUPPORTED_VERSIONS = (V1, V2, V3)


def _normalize_version(value: Any) -> str | None:
    if isinstance(value, int) and not isinstance(value, bool):
        value = f"v{value}"
    if not isinstance(value, str):
        return None
    normalized = value.strip().lower()
    if normalized.startswith("slot-state-"):
        normalized = normalized.removeprefix("slot-state-")
    if normalized in SUPPORTED_VERSIONS:
        return normalized
    return None


def detect_version(data: dict[str, Any]) -> str:
    header = data.get("format", data.get("version"))
    if header is not None:
        normalized = _normalize_version(header)
        if normalized is None:
            fail(
                E_VERSION_UNSUPPORTED,
                "state format version is not supported",
                field="format",
                details={"value": header, "supported": list(SUPPORTED_VERSIONS)},
            )
        return normalized
    if "shards" in data or "sqlite" in data:
        return V2
    slots = data.get("slots")
    if isinstance(slots, dict):
        for value in slots.values():
            if isinstance(value, dict) and (
                "winner" in value or "change_history" in value
            ):
                return V3
    if isinstance(slots, list) and "updated_at_relative_seconds" in data:
        return V1
    fail(
        E_VERSION_UNSUPPORTED,
        "cannot determine state format from version header or field set",
        field="format",
    )


def _parse_pending(value: Any, rules: Rules, field: str) -> tuple[str, ...]:
    if value is None:
        return ()
    items = require_list(value, field)
    result: list[str] = []
    for index, item in enumerate(items):
        result.append(validate_slot_key(item, rules, f"{field}[{index}]"))
    return tuple(sorted(set(result)))


def _candidate_from_flat(
    record: dict[str, Any],
    *,
    rules: Rules,
    field: str,
    source_turn_field: str = "source_turn",
    completions: list[dict[str, Any]],
) -> Candidate:
    for required in ("slot_key", "value", "confidence", source_turn_field):
        if required not in record:
            fail(
                E_FORMAT_CORRUPT,
                "slot record is missing a required field",
                field=f"{field}.{required}",
            )
    slot_key = validate_slot_key(record["slot_key"], rules, f"{field}.slot_key")
    value = validate_json_value(record["value"], f"{field}.value")
    confidence = validate_confidence(record["confidence"], f"{field}.confidence")
    source_turn = require_int(record[source_turn_field], f"{field}.{source_turn_field}", 1)
    supplied_key = record.get("candidate_key")
    if supplied_key is not None and not isinstance(supplied_key, str):
        fail(
            E_FORMAT_CORRUPT,
            "candidate_key must be a string",
            field=f"{field}.candidate_key",
        )
    try:
        candidate = Candidate.build(slot_key, value, confidence, source_turn, supplied_key)
    except ValueError as exc:
        fail(E_FORMAT_CORRUPT, str(exc), field=f"{field}.candidate_key")
    if supplied_key is None:
        completions.append(
            completion(
                f"{field}.candidate_key",
                "computed:candidate_content_key",
                candidate.candidate_key,
                reason="candidate_key is derived from slot, value, confidence and source turn",
            )
        )
    return candidate


def _slot_map_from_flat_list(
    records: Any,
    *,
    rules: Rules,
    field: str,
    timestamp_resolver: Any,
    completions: list[dict[str, Any]],
) -> dict[str, SlotState]:
    items = require_list(records, field)
    winners: dict[str, tuple[Candidate, int, bool]] = {}
    for index, item in enumerate(items):
        item_field = f"{field}[{index}]"
        record = require_mapping(item, item_field)
        candidate = _candidate_from_flat(
            record, rules=rules, field=item_field, completions=completions
        )
        if candidate.slot_key in winners:
            fail(
                E_FORMAT_CORRUPT,
                "duplicate slot_key in slot records",
                field=f"{item_field}.slot_key",
                details={"slot_key": candidate.slot_key},
            )
        timestamp = timestamp_resolver(record, item_field)
        conflict = record.get("conflict", False)
        if not isinstance(conflict, bool):
            fail(E_FORMAT_CORRUPT, "conflict must be boolean", field=f"{item_field}.conflict")
        winners[candidate.slot_key] = (candidate, timestamp, conflict)

    return {
        key: SlotState(
            winner=winner,
            overwritten=(),
            conflict=conflict,
            updated_at_ms=timestamp,
        )
        for key, (winner, timestamp, conflict) in winners.items()
    }


def _read_overwritten_list(
    value: Any,
    *,
    rules: Rules,
    field: str,
    completions: list[dict[str, Any]],
) -> dict[str, list[Candidate]]:
    if value is None:
        return {}
    records = require_list(value, field)
    grouped: dict[str, list[Candidate]] = {}
    for index, item in enumerate(records):
        item_field = f"{field}[{index}]"
        record = require_mapping(item, item_field)
        candidate = _candidate_from_flat(
            record, rules=rules, field=item_field, completions=completions
        )
        grouped.setdefault(candidate.slot_key, []).append(candidate)
    return grouped


def _attach_overwritten(
    slots: dict[str, SlotState],
    overwritten: dict[str, list[Candidate]],
    *,
    field: str,
) -> dict[str, SlotState]:
    for key in overwritten:
        if key not in slots:
            fail(
                E_FORMAT_CORRUPT,
                "overwritten candidate refers to a missing winner slot",
                field=field,
                details={"slot_key": key},
            )
    result: dict[str, SlotState] = {}
    for key, slot in slots.items():
        items = [
            item
            for item in overwritten.get(key, [])
            if item.candidate_key != slot.winner.candidate_key
        ]
        result[key] = SlotState(
            winner=slot.winner,
            overwritten=tuple(
                sorted(
                    items,
                    key=lambda candidate: (
                        candidate.source_turn,
                        -candidate.confidence,
                        candidate.value_key,
                        candidate.candidate_key,
                    ),
                )
            ),
            conflict=slot.conflict,
            updated_at_ms=slot.updated_at_ms,
        )
    return result


def read_v1(path: Path, rules: Rules) -> ReadResult:
    try:
        data = read_json(path)
    except FileNotFoundError:
        fail(E_FORMAT_CORRUPT, "v1 snapshot file does not exist", field=str(path))
    except (OSError, ValueError) as exc:
        fail(E_FORMAT_CORRUPT, f"v1 snapshot cannot be parsed: {exc}", field=str(path))
    if not isinstance(data, dict):
        fail(E_FORMAT_CORRUPT, "v1 snapshot must be a JSON object", field="$")
    for required in ("session_id", "updated_at_relative_seconds", "slots"):
        if required not in data:
            fail(
                E_FORMAT_CORRUPT,
                "v1 snapshot is missing a required field",
                field=required,
            )
    session_id = validate_session_id(data["session_id"])
    completions: list[dict[str, Any]] = []
    if "state_generation" in data:
        generation = require_int(data["state_generation"], "state_generation", 0)
    else:
        generation = rules.default_state_generation
        completions.append(
            completion(
                "state_generation",
                "format_default:v1.state_generation",
                generation,
                reason="v1 does not require state_generation",
            )
        )
    updated_at_ms = rules.milliseconds_from_relative(
        data["updated_at_relative_seconds"], "updated_at_relative_seconds"
    )
    completions.append(
        completion(
            "updated_at_ms",
            "derived:v1.relative_seconds_plus_config_time_origin",
            updated_at_ms,
            reason="v1 timestamps are relative seconds",
        )
    )

    def timestamp_resolver(record: dict[str, Any], field: str) -> int:
        if "updated_at_relative_seconds" not in record:
            fail(
                E_FORMAT_CORRUPT,
                "v1 slot record is missing updated_at_relative_seconds",
                field=f"{field}.updated_at_relative_seconds",
            )
        return rules.milliseconds_from_relative(
            record["updated_at_relative_seconds"],
            f"{field}.updated_at_relative_seconds",
        )

    slots = _slot_map_from_flat_list(
        data["slots"],
        rules=rules,
        field="slots",
        timestamp_resolver=timestamp_resolver,
        completions=completions,
    )
    overwritten = _read_overwritten_list(
        data.get("superseded_candidates", []),
        rules=rules,
        field="superseded_candidates",
        completions=completions,
    )
    slots = _attach_overwritten(slots, overwritten, field="superseded_candidates")
    pending = _parse_pending(
        data.get("pending_clarifications", []),
        rules,
        "pending_clarifications",
    )
    completions.append(
        completion(
            "change_history",
            "format_default:v1.has_no_change_history",
            [],
            reason="v1 is a flat snapshot and does not carry nested history",
        )
    )
    state = InternalState.build(
        session_id=session_id,
        state_generation=generation,
        updated_at_ms=updated_at_ms,
        slots=slots,
        pending_clarifications=pending,
    )
    completions.append(
        completion(
            "state_checksum",
            "computed:canonical_semantic_state",
            state.state_checksum,
            reason="v1 has no embedded checksum",
        )
    )
    return ReadResult(
        state=state,
        audit_history=(),
        completions=tuple(completions),
        checksum_source="computed:v1_has_no_checksum",
    )


def _safe_child(base: Path, value: Any, field: str) -> Path:
    if not isinstance(value, str) or not value:
        fail(E_FORMAT_CORRUPT, "path field must be a non-empty string", field=field)
    candidate = (base / value).resolve()
    base_resolved = base.resolve()
    if candidate != base_resolved and base_resolved not in candidate.parents:
        fail(
            E_FORMAT_CORRUPT,
            "path escapes the v2 snapshot directory",
            field=field,
            details={"value": value},
        )
    return candidate


def read_v2(base_or_manifest: Path, rules: Rules) -> ReadResult:
    base = base_or_manifest if base_or_manifest.is_dir() else base_or_manifest.parent
    manifest_path = (
        base / "manifest.json" if base_or_manifest.is_dir() else base_or_manifest
    )
    try:
        manifest = read_json(manifest_path)
    except FileNotFoundError:
        fail(E_FORMAT_CORRUPT, "v2 manifest does not exist", field=str(manifest_path))
    except (OSError, ValueError) as exc:
        fail(E_FORMAT_CORRUPT, f"v2 manifest cannot be parsed: {exc}", field=str(manifest_path))
    manifest = require_mapping(manifest, "manifest")
    for required in (
        "format",
        "session_id",
        "state_generation",
        "updated_at_ms",
        "shards",
        "sqlite",
    ):
        if required not in manifest:
            fail(
                E_FORMAT_CORRUPT,
                "v2 manifest is missing a required field",
                field=required,
            )
    session_id = validate_session_id(manifest["session_id"])
    generation = require_int(manifest["state_generation"], "state_generation", 0)
    updated_at_ms = require_int(manifest["updated_at_ms"], "updated_at_ms", 0)
    shard_paths = require_list(manifest["shards"], "shards")
    completions: list[dict[str, Any]] = []

    winners: dict[str, Candidate] = {}
    conflicts: dict[str, bool] = {}
    for shard_index, shard_value in enumerate(shard_paths):
        shard_path = _safe_child(base, shard_value, f"shards[{shard_index}]")
        try:
            shard = read_json(shard_path)
        except (OSError, ValueError) as exc:
            fail(
                E_FORMAT_CORRUPT,
                f"v2 shard cannot be parsed: {exc}",
                field=f"shards[{shard_index}]",
            )
        shard = require_mapping(shard, f"shards[{shard_index}]")
        if shard.get("format") != "slot-state-v2-shard":
            fail(
                E_FORMAT_CORRUPT,
                "v2 shard format header is invalid",
                field=f"shards[{shard_index}].format",
            )
        records = require_list(shard.get("records"), f"shards[{shard_index}].records")
        for record_index, record_value in enumerate(records):
            field = f"shards[{shard_index}].records[{record_index}]"
            record = require_mapping(record_value, field)
            candidate = _candidate_from_flat(
                record, rules=rules, field=field, completions=completions
            )
            if candidate.slot_key in winners:
                fail(
                    E_FORMAT_CORRUPT,
                    "duplicate slot_key across v2 shards",
                    field=f"{field}.slot_key",
                    details={"slot_key": candidate.slot_key},
                )
            conflict = record.get("conflict", False)
            if not isinstance(conflict, bool):
                fail(E_FORMAT_CORRUPT, "conflict must be boolean", field=f"{field}.conflict")
            winners[candidate.slot_key] = candidate
            conflicts[candidate.slot_key] = conflict

    sqlite_path = _safe_child(base, manifest["sqlite"], "sqlite")
    if not sqlite_path.exists():
        fail(E_FORMAT_CORRUPT, "v2 SQLite file does not exist", field="sqlite")
    try:
        connection = sqlite3.connect(
            f"file:{sqlite_path}?mode=ro", uri=True
        )
        connection.row_factory = sqlite3.Row
        meta_rows = connection.execute("SELECT key, value FROM meta").fetchall()
        timing_rows = connection.execute(
            "SELECT slot_key, updated_at_ms, state_generation FROM slot_timing"
        ).fetchall()
    except sqlite3.Error as exc:
        fail(
            E_FORMAT_CORRUPT,
            f"v2 SQLite records cannot be read: {exc}",
            field="sqlite",
        )
    finally:
        if "connection" in locals():
            connection.close()
    meta = {row["key"]: row["value"] for row in meta_rows}
    expected_meta = {
        "format": "slot-state-v2",
        "session_id": session_id,
        "state_generation": str(generation),
        "updated_at_ms": str(updated_at_ms),
    }
    for key, expected in expected_meta.items():
        if meta.get(key) != expected:
            fail(
                E_FORMAT_CORRUPT,
                "v2 SQLite metadata disagrees with the manifest",
                field=f"sqlite.meta.{key}",
                details={"expected": expected, "actual": meta.get(key)},
            )
    timings: dict[str, tuple[int, int]] = {}
    for row in timing_rows:
        slot_key = validate_slot_key(
            row["slot_key"], rules, f"sqlite.slot_timing.{row['slot_key']}.slot_key"
        )
        if slot_key in timings:
            fail(
                E_FORMAT_CORRUPT,
                "duplicate slot_key in SQLite slot_timing",
                field=f"sqlite.slot_timing.{slot_key}",
            )
        timings[slot_key] = (
            require_int(row["updated_at_ms"], f"sqlite.slot_timing.{slot_key}.updated_at_ms", 0),
            require_int(
                row["state_generation"],
                f"sqlite.slot_timing.{slot_key}.state_generation",
                0,
            ),
        )
    if set(winners) != set(timings):
        fail(
            E_FORMAT_CORRUPT,
            "v2 shard slot keys and SQLite timing slot keys differ",
            field="sqlite.slot_timing",
            details={
                "missing_in_sqlite": sorted(set(winners) - set(timings)),
                "missing_in_shards": sorted(set(timings) - set(winners)),
            },
        )
    for slot_key, (_, row_generation) in timings.items():
        if row_generation != generation:
            fail(
                E_FORMAT_CORRUPT,
                "v2 slot generation does not match manifest generation",
                field=f"sqlite.slot_timing.{slot_key}.state_generation",
            )

    slots = {
        key: SlotState(
            winner=winner,
            overwritten=(),
            conflict=conflicts[key],
            updated_at_ms=timings[key][0],
        )
        for key, winner in winners.items()
    }
    overwritten = _read_overwritten_list(
        manifest.get("superseded_candidates", []),
        rules=rules,
        field="superseded_candidates",
        completions=completions,
    )
    slots = _attach_overwritten(slots, overwritten, field="superseded_candidates")
    pending = _parse_pending(
        manifest.get("pending_clarifications", []),
        rules,
        "pending_clarifications",
    )
    completions.append(
        completion(
            "change_history",
            "format_default:v2.has_no_change_history",
            [],
            reason="v2 shards have no nested change history field",
        )
    )
    state = InternalState.build(
        session_id=session_id,
        state_generation=generation,
        updated_at_ms=updated_at_ms,
        slots=slots,
        pending_clarifications=pending,
    )
    completions.append(
        completion(
            "state_checksum",
            "computed:canonical_semantic_state",
            state.state_checksum,
            reason="v2 has no embedded semantic checksum",
        )
    )
    return ReadResult(
        state=state,
        audit_history=(),
        completions=tuple(completions),
        checksum_source="computed:v2_has_no_checksum",
    )


def _read_overwritten_nested(
    value: Any,
    *,
    rules: Rules,
    field: str,
    completions: list[dict[str, Any]],
    expected_slot_key: str,
) -> tuple[Candidate, ...]:
    if value is None:
        return ()
    items = require_list(value, field)
    result: list[Candidate] = []
    for index, item in enumerate(items):
        item_field = f"{field}[{index}]"
        record = require_mapping(item, item_field)
        candidate = _candidate_from_flat(
            record, rules=rules, field=item_field, completions=completions
        )
        if candidate.slot_key != expected_slot_key:
            fail(
                E_FORMAT_CORRUPT,
                "overwritten candidate slot_key disagrees with its slot",
                field=f"{item_field}.slot_key",
            )
        result.append(candidate)
    return tuple(
        sorted(
            result,
            key=lambda candidate: (
                candidate.source_turn,
                -candidate.confidence,
                candidate.value_key,
                candidate.candidate_key,
            ),
        )
    )


def read_v3(path: Path, rules: Rules) -> ReadResult:
    try:
        data = read_json(path)
    except FileNotFoundError:
        fail(E_FORMAT_CORRUPT, "v3 snapshot file does not exist", field=str(path))
    except (OSError, ValueError) as exc:
        fail(E_FORMAT_CORRUPT, f"v3 snapshot cannot be parsed: {exc}", field=str(path))
    data = require_mapping(data, "$")
    for required in (
        "format",
        "session_id",
        "state_generation",
        "updated_at_ms",
        "slots",
        "checksum",
    ):
        if required not in data:
            fail(
                E_FORMAT_CORRUPT,
                "v3 snapshot is missing a required field",
                field=required,
            )
    supplied_checksum = data["checksum"]
    if not isinstance(supplied_checksum, str) or not supplied_checksum.startswith(
        "sha256:"
    ):
        fail(
            E_FORMAT_CORRUPT,
            "v3 checksum must be a sha256 content hash",
            field="checksum",
        )
    checksum_payload = {key: value for key, value in data.items() if key != "checksum"}
    computed_checksum = content_hash(checksum_payload)
    if supplied_checksum != computed_checksum:
        fail(
            E_FORMAT_CORRUPT,
            "v3 checksum verification failed",
            field="checksum",
            details={"expected": computed_checksum, "actual": supplied_checksum},
        )
    session_id = validate_session_id(data["session_id"])
    generation = require_int(data["state_generation"], "state_generation", 0)
    updated_at_ms = require_int(data["updated_at_ms"], "updated_at_ms", 0)
    slots_value = require_mapping(data["slots"], "slots")
    completions: list[dict[str, Any]] = []
    slots: dict[str, SlotState] = {}
    history: list[dict[str, Any]] = []
    for raw_key, slot_value in slots_value.items():
        slot_key = validate_slot_key(raw_key, rules, f"slots.{raw_key}")
        field = f"slots.{slot_key}"
        nested = require_mapping(slot_value, field)
        if "winner" not in nested:
            fail(E_FORMAT_CORRUPT, "v3 slot is missing winner", field=f"{field}.winner")
        winner_record = require_mapping(nested["winner"], f"{field}.winner")
        winner = _candidate_from_flat(
            winner_record,
            rules=rules,
            field=f"{field}.winner",
            completions=completions,
        )
        if winner.slot_key != slot_key:
            fail(
                E_FORMAT_CORRUPT,
                "winner slot_key disagrees with its slot map key",
                field=f"{field}.winner.slot_key",
            )
        if "updated_at_ms" in winner_record:
            slot_updated = require_int(
                winner_record["updated_at_ms"], f"{field}.winner.updated_at_ms", 0
            )
        elif "updated_at_ms" in nested:
            slot_updated = require_int(
                nested["updated_at_ms"], f"{field}.updated_at_ms", 0
            )
        else:
            fail(
                E_FORMAT_CORRUPT,
                "v3 slot is missing updated_at_ms",
                field=f"{field}.updated_at_ms",
            )
        overwritten = _read_overwritten_nested(
            nested.get("overwritten", []),
            rules=rules,
            field=f"{field}.overwritten",
            completions=completions,
            expected_slot_key=slot_key,
        )
        conflict = nested.get("conflict", False)
        if not isinstance(conflict, bool):
            fail(E_FORMAT_CORRUPT, "conflict must be boolean", field=f"{field}.conflict")
        slots[slot_key] = SlotState(
            winner=winner,
            overwritten=overwritten,
            conflict=conflict,
            updated_at_ms=slot_updated,
        )
        raw_history = nested.get("change_history", [])
        for index, event in enumerate(require_list(raw_history, f"{field}.change_history")):
            event_field = f"{field}.change_history[{index}]"
            event = require_mapping(event, event_field)
            validate_json_value(event, event_field)
            if event.get("slot_key", slot_key) != slot_key:
                fail(
                    E_FORMAT_CORRUPT,
                    "history event slot_key disagrees with its slot",
                    field=f"{event_field}.slot_key",
                )
            normalized_event = dict(event)
            normalized_event.setdefault("slot_key", slot_key)
            history.append(normalized_event)
    pending = _parse_pending(
        data.get("pending_clarifications", []),
        rules,
        "pending_clarifications",
    )
    state = InternalState.build(
        session_id=session_id,
        state_generation=generation,
        updated_at_ms=updated_at_ms,
        slots=slots,
        pending_clarifications=pending,
    )
    return ReadResult(
        state=state,
        audit_history=tuple(history),
        completions=tuple(completions),
        checksum_source="provided:v3.checksum",
        file_checksum=supplied_checksum,
    )


def read_state(path: str | Path, rules: Rules) -> ReadResult:
    source = Path(path)
    if source.is_dir():
        manifest_path = source / "manifest.json"
        try:
            data = read_json(manifest_path)
        except FileNotFoundError:
            fail(
                E_FORMAT_CORRUPT,
                "snapshot directory has no manifest.json",
                field=str(source),
            )
        except (OSError, ValueError) as exc:
            fail(E_FORMAT_CORRUPT, f"snapshot manifest cannot be parsed: {exc}", field=str(manifest_path))
        if not isinstance(data, dict):
            fail(E_FORMAT_CORRUPT, "snapshot manifest must be an object", field="$")
        version = detect_version(data)
        if version != V2:
            fail(
                E_FORMAT_CORRUPT,
                "directory snapshot dispatch did not resolve to v2",
                field="format",
            )
        return read_v2(manifest_path, rules)
    try:
        data = read_json(source)
    except FileNotFoundError:
        fail(E_FORMAT_CORRUPT, "snapshot file does not exist", field=str(source))
    except (OSError, ValueError) as exc:
        fail(E_FORMAT_CORRUPT, f"snapshot cannot be parsed: {exc}", field=str(source))
    if not isinstance(data, dict):
        fail(E_FORMAT_CORRUPT, "snapshot must be a JSON object", field="$")
    version = detect_version(data)
    if version == V1:
        return read_v1(source, rules)
    if version == V2:
        return read_v2(source, rules)
    return read_v3(source, rules)


@dataclass(frozen=True)
class ExportResult:
    version: str
    path: str
    dropped_audit_history: tuple[dict[str, Any], ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "path": self.path,
            "dropped_audit_history": list(self.dropped_audit_history),
        }


def _drop_history(
    audit_history: Iterable[dict[str, Any]], target_version: str
) -> tuple[dict[str, Any], ...]:
    events = list(audit_history)
    if not events:
        return ()
    return (
        {
            "field": "change_history",
            "action": "drop",
            "reason": f"{target_version} has no nested change-history field",
            "event_count": len(events),
        },
    )


def write_v1(
    path: str | Path,
    state: InternalState,
    rules: Rules,
    *,
    audit_history: Iterable[dict[str, Any]] = (),
    include_state_generation: bool = True,
    overwrite: bool = False,
) -> ExportResult:
    target = Path(path)
    if target.exists() and not overwrite:
        fail(E_FORMAT_CORRUPT, "target v1 file already exists", field=str(target))
    data: dict[str, Any] = {
        "format": "slot-state-v1",
        "session_id": state.session_id,
        "updated_at_relative_seconds": rules.relative_seconds(state.updated_at_ms),
        "slots": [],
        "superseded_candidates": [],
        "pending_clarifications": list(state.pending_clarifications),
    }
    if include_state_generation:
        data["state_generation"] = state.state_generation
    for slot_key, slot in state.slots:
        record = slot.winner.to_dict()
        record["updated_at_relative_seconds"] = rules.relative_seconds(slot.updated_at_ms)
        record["conflict"] = slot.conflict
        data["slots"].append(record)
        data["superseded_candidates"].extend(
            candidate.to_dict() for candidate in slot.overwritten
        )
    dropped = _drop_history(audit_history, V1)
    atomic_write_json(target, data)
    return ExportResult(version=V1, path=str(target), dropped_audit_history=dropped)


def _connect_writer(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA journal_mode=DELETE")
    connection.execute("PRAGMA synchronous=FULL")
    return connection


def write_v2(
    path: str | Path,
    state: InternalState,
    rules: Rules,
    *,
    audit_history: Iterable[dict[str, Any]] = (),
    shard_count: int = 2,
    overwrite: bool = False,
) -> ExportResult:
    target = Path(path)
    if target.exists():
        if not overwrite:
            fail(E_FORMAT_CORRUPT, "target v2 directory already exists", field=str(target))
        shutil.rmtree(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.parent / f".{target.name}.tmp-{os.getpid()}"
    if temp.exists():
        shutil.rmtree(temp)
    temp.mkdir(parents=True)
    shard_buckets: list[list[dict[str, Any]]] = [[] for _ in range(max(1, shard_count))]
    for slot_key, slot in state.slots:
        bucket = int(sha256_text(slot_key)[:8], 16) % len(shard_buckets)
        record = slot.winner.to_dict()
        record["conflict"] = slot.conflict
        shard_buckets[bucket].append(record)
    shard_names: list[str] = []
    for index, records in enumerate(shard_buckets):
        shard_name = f"shard-{index:03d}.json"
        shard_names.append(shard_name)
        atomic_write_json(
            temp / shard_name,
            {
                "format": "slot-state-v2-shard",
                "shard_id": f"{index:03d}",
                "records": records,
            },
        )
    manifest = {
        "format": "slot-state-v2",
        "session_id": state.session_id,
        "state_generation": state.state_generation,
        "updated_at_ms": state.updated_at_ms,
        "shards": shard_names,
        "sqlite": "state.sqlite",
        "pending_clarifications": list(state.pending_clarifications),
        "superseded_candidates": [
            candidate.to_dict()
            for _, slot in state.slots
            for candidate in slot.overwritten
        ],
    }
    atomic_write_json(temp / "manifest.json", manifest)
    connection = _connect_writer(temp / "state.sqlite")
    try:
        connection.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        connection.execute(
            """
            CREATE TABLE slot_timing (
                slot_key TEXT PRIMARY KEY,
                updated_at_ms INTEGER NOT NULL,
                state_generation INTEGER NOT NULL
            )
            """
        )
        metadata = {
            "format": "slot-state-v2",
            "session_id": state.session_id,
            "state_generation": str(state.state_generation),
            "updated_at_ms": str(state.updated_at_ms),
        }
        connection.executemany(
            "INSERT INTO meta(key, value) VALUES (?, ?)",
            sorted(metadata.items()),
        )
        connection.executemany(
            "INSERT INTO slot_timing(slot_key, updated_at_ms, state_generation) VALUES (?, ?, ?)",
            [
                (slot_key, slot.updated_at_ms, state.state_generation)
                for slot_key, slot in state.slots
            ],
        )
        connection.commit()
        connection.execute("VACUUM")
    finally:
        connection.close()
    dropped = _drop_history(audit_history, V2)
    if target.exists():
        shutil.rmtree(target)
    os.replace(temp, target)
    return ExportResult(version=V2, path=str(target), dropped_audit_history=dropped)


def write_v3(
    path: str | Path,
    state: InternalState,
    rules: Rules,
    *,
    audit_history: Iterable[dict[str, Any]] = (),
    overwrite: bool = False,
) -> ExportResult:
    target = Path(path)
    if target.exists() and not overwrite:
        fail(E_FORMAT_CORRUPT, "target v3 file already exists", field=str(target))
    history_by_slot: dict[str, list[dict[str, Any]]] = {}
    for event in audit_history:
        slot_key = event.get("slot_key")
        if not isinstance(slot_key, str):
            fail(
                E_FORMAT_CORRUPT,
                "v3 history event must carry slot_key",
                field="audit_history.slot_key",
            )
        history_by_slot.setdefault(slot_key, []).append(dict(event))
    slots: dict[str, Any] = {}
    for slot_key, slot in state.slots:
        winner = slot.winner.to_dict()
        winner["updated_at_ms"] = slot.updated_at_ms
        slots[slot_key] = {
            "winner": winner,
            "overwritten": [candidate.to_dict() for candidate in slot.overwritten],
            "conflict": slot.conflict,
            "updated_at_ms": slot.updated_at_ms,
            "change_history": history_by_slot.get(slot_key, []),
        }
    data: dict[str, Any] = {
        "format": "slot-state-v3",
        "session_id": state.session_id,
        "state_generation": state.state_generation,
        "updated_at_ms": state.updated_at_ms,
        "pending_clarifications": list(state.pending_clarifications),
        "slots": slots,
    }
    data["checksum"] = content_hash(data)
    atomic_write_json(target, data)
    return ExportResult(version=V3, path=str(target), dropped_audit_history=())


def export_state(
    path: str | Path,
    state: InternalState,
    rules: Rules,
    *,
    target_version: str,
    audit_history: Iterable[dict[str, Any]] = (),
    overwrite: bool = False,
    include_v1_generation: bool = True,
) -> ExportResult:
    version = _normalize_version(target_version)
    if version is None:
        fail(
            E_VERSION_UNSUPPORTED,
            "target state version is not supported",
            field="target_version",
            details={"value": target_version, "supported": list(SUPPORTED_VERSIONS)},
        )
    if version == V1:
        return write_v1(
            path,
            state,
            rules,
            audit_history=audit_history,
            include_state_generation=include_v1_generation,
            overwrite=overwrite,
        )
    if version == V2:
        return write_v2(
            path,
            state,
            rules,
            audit_history=audit_history,
            overwrite=overwrite,
        )
    return write_v3(
        path,
        state,
        rules,
        audit_history=audit_history,
        overwrite=overwrite,
    )

