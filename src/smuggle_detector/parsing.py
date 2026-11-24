"""Strict parsing of deterministic message streams, rule snapshots and plans."""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from typing import Any, Iterable

from .constants import (
    CONNECTION_ID_RE,
    E_BAD_CONN,
    E_BAD_DIRECTION,
    E_BAD_DIRECTION_RULE,
    E_BAD_EFFECTIVE,
    E_BAD_ENABLED,
    E_BAD_GENERATION,
    E_BAD_HEX,
    E_BAD_PATTERNS,
    E_BAD_PLAN,
    E_BAD_RECORD,
    E_BAD_RULE_NAME,
    E_BAD_SEQ,
    E_BAD_TOMBSTONE,
    E_BAD_TS,
    E_DUPLICATE_RECORD_INDEX,
    E_RULE_PATTERN,
    E_SHARD_INVALID,
    E_SHARD_MISMATCH,
    E_SHARD_MISSING,
    RULE_DIRECTIONS,
    RULE_NAME_RE,
    VALID_DIRECTIONS,
)
from .errors import InputError
from .io_utils import digest_value, read_json
from .models import MessageRecord, Plan, Rejection, RuleRecord, SampleInput
from .patterns import normalize_pattern

_CONNECTION_RE = re.compile(CONNECTION_ID_RE)
_RULE_NAME_COMPILED = re.compile(RULE_NAME_RE)
_HEX_RE = re.compile(r"^(?:[0-9A-Fa-f]{2})*$")


def _is_int(value: Any) -> bool:
    return type(value) is int


def _reject(
    rejections: list[Rejection],
    source: str,
    index: int,
    code: str,
    detail: str,
) -> None:
    rejections.append(Rejection(source, index, code, detail))


def _message_files(root: Path) -> list[Path]:
    jsonl = root / "messages.jsonl"
    if jsonl.is_file():
        return [jsonl]
    single = root / "messages.json"
    if single.is_file():
        return [single]
    files = sorted(
        list(root.glob("messages*.jsonl")) + list(root.glob("messages*.json"))
    )
    if not files:
        raise InputError(f"no message stream file found under {root}")
    return files


def _iter_message_docs(path: Path) -> Iterable[tuple[int, Any]]:
    if path.suffix.lower() == ".jsonl":
        with path.open("r", encoding="utf-8-sig") as handle:
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                yield line_number, line
        return

    doc = read_json(path)
    if isinstance(doc, list):
        records = doc
    elif isinstance(doc, dict) and isinstance(doc.get("messages"), list):
        records = doc["messages"]
    else:
        raise InputError(f"{path}: expected a message array or {{'messages': []}}")
    for index, record in enumerate(records, start=1):
        yield index, record


def _parse_message_record(
    raw: Any,
    *,
    source: str,
    index: int,
    rejections: list[Rejection],
) -> MessageRecord | None:
    if not isinstance(raw, dict):
        _reject(rejections, source, index, E_BAD_RECORD, "record is not an object")
        return None

    connection_id = raw.get("connection_id")
    if not isinstance(connection_id, str) or not _CONNECTION_RE.fullmatch(
        connection_id
    ):
        _reject(
            rejections,
            source,
            index,
            E_BAD_CONN,
            f"invalid connection_id: {connection_id!r}",
        )
        return None

    direction = raw.get("direction")
    if direction not in VALID_DIRECTIONS:
        _reject(
            rejections,
            source,
            index,
            E_BAD_DIRECTION,
            f"direction must be one of {VALID_DIRECTIONS}: {direction!r}",
        )
        return None

    seq = raw.get("seq")
    if not _is_int(seq) or seq < 0:
        _reject(
            rejections,
            source,
            index,
            E_BAD_SEQ,
            f"seq must be a non-negative integer: {seq!r}",
        )
        return None

    ts_ms = raw.get("ts_ms")
    if not _is_int(ts_ms) or ts_ms < 0:
        _reject(
            rejections,
            source,
            index,
            E_BAD_TS,
            f"ts_ms must be a non-negative integer: {ts_ms!r}",
        )
        return None

    payload_hex = raw.get("payload_hex")
    if (
        not isinstance(payload_hex, str)
        or len(payload_hex) % 2 != 0
        or not _HEX_RE.fullmatch(payload_hex)
    ):
        _reject(
            rejections,
            source,
            index,
            E_BAD_HEX,
            "payload_hex must contain an even number of hexadecimal characters",
        )
        return None

    try:
        payload = bytes.fromhex(payload_hex)
    except ValueError as exc:
        _reject(rejections, source, index, E_BAD_HEX, str(exc))
        return None

    return MessageRecord(
        connection_id=connection_id,
        direction=direction,
        seq=seq,
        ts_ms=ts_ms,
        payload=payload,
        source_category=source,
        record_index=index,
    )


def parse_messages(root: Path) -> tuple[list[MessageRecord], list[Rejection]]:
    records: list[MessageRecord] = []
    rejections: list[Rejection] = []
    global_index = 0
    for path in _message_files(root):
        try:
            iterator = _iter_message_docs(path)
            for local_index, raw in iterator:
                global_index += 1
                if isinstance(raw, str):
                    try:
                        raw = json.loads(raw)
                    except json.JSONDecodeError as exc:
                        _reject(
                            rejections,
                            "messages",
                            global_index,
                            E_BAD_RECORD,
                            f"{path.name}:{local_index}: invalid JSON: {exc}",
                        )
                        continue
                record = _parse_message_record(
                    raw,
                    source="messages",
                    index=global_index,
                    rejections=rejections,
                )
                if record is not None:
                    records.append(record)
        except (OSError, json.JSONDecodeError) as exc:
            raise InputError(f"cannot parse message file {path}: {exc}") from exc
    return records, rejections


def _parse_patterns(
    raw: Any,
    *,
    source: str,
    index: int,
    rejections: list[Rejection],
    allow_empty: bool,
) -> tuple[str, ...] | None:
    if not isinstance(raw, list):
        _reject(rejections, source, index, E_BAD_PATTERNS, "patterns must be a list")
        return None
    if not raw and not allow_empty:
        _reject(rejections, source, index, E_BAD_PATTERNS, "patterns must not be empty")
        return None
    normalized: list[str] = []
    for item in raw:
        try:
            normalized.append(normalize_pattern(item))
        except ValueError as exc:
            _reject(rejections, source, index, E_RULE_PATTERN, str(exc))
            return None
    return tuple(sorted(set(normalized)))


def parse_rules_v1(
    root: Path,
) -> tuple[list[RuleRecord], list[Rejection]]:
    path = root / "rules_v1.json"
    if not path.is_file():
        raise InputError(f"missing v1 rule snapshot: {path}")
    try:
        doc = read_json(path)
    except (OSError, json.JSONDecodeError) as exc:
        raise InputError(f"cannot parse {path}: {exc}") from exc

    epoch_ms = 0
    if isinstance(doc, list):
        raw_rules = doc
    elif isinstance(doc, dict) and isinstance(doc.get("rules"), list):
        epoch_ms = doc.get("relative_epoch_ms", 0)
        if not _is_int(epoch_ms) or epoch_ms < 0:
            raise InputError(f"{path}: relative_epoch_ms must be a non-negative int")
        raw_rules = doc["rules"]
    else:
        raise InputError(f"{path}: expected a rule array or {{'rules': []}}")

    records: list[RuleRecord] = []
    rejections: list[Rejection] = []
    for index, raw in enumerate(raw_rules, start=1):
        if not isinstance(raw, dict):
            _reject(rejections, "rules_v1", index, E_BAD_RECORD, "not an object")
            continue
        name = raw.get("name")
        if not isinstance(name, str) or not _RULE_NAME_COMPILED.fullmatch(name):
            _reject(
                rejections,
                "rules_v1",
                index,
                E_BAD_RULE_NAME,
                f"invalid rule name: {name!r}",
            )
            continue
        seconds = raw.get("relative_effective_seconds")
        if not _is_int(seconds) or seconds < 0:
            _reject(
                rejections,
                "rules_v1",
                index,
                E_BAD_EFFECTIVE,
                f"relative_effective_seconds invalid: {seconds!r}",
            )
            continue
        enabled = raw.get("enabled")
        if type(enabled) is not bool:
            _reject(
                rejections,
                "rules_v1",
                index,
                E_BAD_ENABLED,
                f"enabled must be boolean: {enabled!r}",
            )
            continue
        patterns = _parse_patterns(
            raw.get("patterns"),
            source="rules_v1",
            index=index,
            rejections=rejections,
            allow_empty=not enabled,
        )
        if patterns is None:
            continue
        records.append(
            RuleRecord(
                canonical_name=name,
                generation=1,
                effective_ms=epoch_ms + seconds * 1000,
                direction="both",
                patterns=patterns,
                tombstone=not enabled,
                enabled=enabled,
                source_category="rules_v1",
                record_index=index,
            )
        )
    return records, rejections


_V2_REQUIRED_COLUMNS = {
    "canonical_name",
    "effective_ms",
    "generation",
    "direction",
    "tombstone",
    "shard_file",
    "record_index",
}


def _safe_shard_path(root: Path, shard_file: str) -> Path | None:
    candidate = Path(shard_file)
    if candidate.is_absolute():
        return None
    base = (root / "rules_v2_shards").resolve()
    resolved = (base / candidate).resolve()
    try:
        resolved.relative_to(base)
    except ValueError:
        return None
    return resolved


def _load_v2_shards(
    root: Path,
    rows: list[sqlite3.Row] | list[tuple],
    *,
    rejections: list[Rejection],
) -> dict[str, tuple[tuple[str, ...], int, str]]:
    shard_paths = sorted(
        {
            str(row[6])
            for row in rows
            if isinstance(row[6], str) and row[6]
        }
    )
    shards: dict[str, tuple[tuple[str, ...], int, str]] = {}
    for shard_file in shard_paths:
        path = _safe_shard_path(root, shard_file)
        if path is None:
            _reject(
                rejections,
                "rules_v2_shards",
                0,
                E_SHARD_INVALID,
                f"unsafe shard path: {shard_file!r}",
            )
            continue
        if not path.is_file():
            continue
        try:
            doc = read_json(path)
        except (OSError, json.JSONDecodeError) as exc:
            _reject(
                rejections,
                "rules_v2_shards",
                0,
                E_SHARD_INVALID,
                f"{shard_file}: {exc}",
            )
            continue
        if not isinstance(doc, dict) or not isinstance(doc.get("rules"), list):
            _reject(
                rejections,
                "rules_v2_shards",
                0,
                E_SHARD_INVALID,
                f"{shard_file}: expected {{'rules': []}}",
            )
            continue
        for index, raw in enumerate(doc["rules"], start=1):
            if not isinstance(raw, dict):
                _reject(
                    rejections,
                    "rules_v2_shards",
                    index,
                    E_BAD_RECORD,
                    f"{shard_file}: record is not an object",
                )
                continue
            name = raw.get("canonical_name")
            if not isinstance(name, str) or not _RULE_NAME_COMPILED.fullmatch(name):
                _reject(
                    rejections,
                    "rules_v2_shards",
                    index,
                    E_BAD_RULE_NAME,
                    f"{shard_file}: invalid canonical_name: {name!r}",
                )
                continue
            patterns = _parse_patterns(
                raw.get("patterns"),
                source="rules_v2_shards",
                index=index,
                rejections=rejections,
                allow_empty=False,
            )
            if patterns is None:
                continue
            if name in shards:
                _reject(
                    rejections,
                    "rules_v2_shards",
                    index,
                    E_DUPLICATE_RECORD_INDEX,
                    f"{shard_file}: duplicate canonical_name {name}",
                )
                continue
            shards[name] = (patterns, index, shard_file)
    return shards


def parse_rules_v2(
    root: Path,
) -> tuple[list[RuleRecord], list[Rejection]]:
    db_path = root / "rules_v2.sqlite3"
    if not db_path.is_file():
        raise InputError(f"missing v2 metadata database: {db_path}")
    rejections: list[Rejection] = []
    try:
        uri = f"{db_path.resolve().as_uri()}?mode=ro"
        connection = sqlite3.connect(uri, uri=True)
        try:
            connection.row_factory = sqlite3.Row
            columns = {
                row[1]
                for row in connection.execute("PRAGMA table_info(rule_meta)")
            }
            missing = sorted(_V2_REQUIRED_COLUMNS - columns)
            if missing:
                raise InputError(
                    f"{db_path}: missing rule_meta columns: {', '.join(missing)}"
                )
            raw_rows = list(
                connection.execute(
                    "SELECT rowid, canonical_name, effective_ms, generation, "
                    "direction, tombstone, shard_file, record_index "
                    "FROM rule_meta ORDER BY record_index, rowid"
                )
            )
        finally:
            connection.close()
    except sqlite3.DatabaseError as exc:
        raise InputError(f"cannot read {db_path}: {exc}") from exc

    shards = _load_v2_shards(root, raw_rows, rejections=rejections)
    records: list[RuleRecord] = []
    seen_indices: set[int] = set()
    referenced_names: set[str] = set()
    for row in raw_rows:
        index = row[7]
        if not _is_int(index) or index < 0:
            _reject(
                rejections,
                "rules_v2_meta",
                -1,
                E_BAD_RECORD,
                f"invalid record_index: {index!r}",
            )
            continue
        if index in seen_indices:
            _reject(
                rejections,
                "rules_v2_meta",
                index,
                E_DUPLICATE_RECORD_INDEX,
                f"duplicate record_index {index}",
            )
            continue
        seen_indices.add(index)

        name = row[1]
        if not isinstance(name, str) or not _RULE_NAME_COMPILED.fullmatch(name):
            _reject(
                rejections,
                "rules_v2_meta",
                index,
                E_BAD_RULE_NAME,
                f"invalid canonical_name: {name!r}",
            )
            continue
        effective_ms = row[2]
        if not _is_int(effective_ms) or effective_ms < 0:
            _reject(
                rejections,
                "rules_v2_meta",
                index,
                E_BAD_EFFECTIVE,
                f"effective_ms invalid: {effective_ms!r}",
            )
            continue
        generation = row[3]
        if not _is_int(generation) or generation < 1:
            _reject(
                rejections,
                "rules_v2_meta",
                index,
                E_BAD_GENERATION,
                f"generation invalid: {generation!r}",
            )
            continue
        direction = row[4]
        if direction not in RULE_DIRECTIONS:
            _reject(
                rejections,
                "rules_v2_meta",
                index,
                E_BAD_DIRECTION_RULE,
                f"direction invalid: {direction!r}",
            )
            continue
        tombstone = row[5]
        if type(tombstone) is not int or tombstone not in (0, 1):
            _reject(
                rejections,
                "rules_v2_meta",
                index,
                E_BAD_TOMBSTONE,
                f"tombstone must be 0 or 1: {tombstone!r}",
            )
            continue
        shard_file = row[6]
        if shard_file is None:
            shard_file = ""
        if not isinstance(shard_file, str):
            _reject(
                rejections,
                "rules_v2_meta",
                index,
                E_SHARD_INVALID,
                f"shard_file invalid: {shard_file!r}",
            )
            continue

        referenced_names.add(name)
        if tombstone:
            patterns: tuple[str, ...] = ()
        else:
            if not shard_file:
                _reject(
                    rejections,
                    "rules_v2_meta",
                    index,
                    E_SHARD_MISSING,
                    f"live rule {name} has no shard_file",
                )
                continue
            path = _safe_shard_path(root, shard_file)
            if path is None or not path.is_file():
                _reject(
                    rejections,
                    "rules_v2_meta",
                    index,
                    E_SHARD_MISSING,
                    f"shard not found: {shard_file!r}",
                )
                continue
            shard_record = shards.get(name)
            if shard_record is None:
                _reject(
                    rejections,
                    "rules_v2_meta",
                    index,
                    E_SHARD_MISMATCH,
                    f"canonical_name {name} missing from {shard_file}",
                )
                continue
            patterns = shard_record[0]

        records.append(
            RuleRecord(
                canonical_name=name,
                generation=generation,
                effective_ms=effective_ms,
                direction=direction,
                patterns=patterns,
                tombstone=bool(tombstone),
                enabled=not bool(tombstone),
                source_category="rules_v2_meta",
                record_index=index,
            )
        )

    for name in sorted(set(shards) - referenced_names):
        patterns, index, shard_file = shards[name]
        _reject(
            rejections,
            "rules_v2_shards",
            index,
            E_SHARD_MISMATCH,
            f"{shard_file}: {name} has no metadata row (patterns={patterns})",
        )
    return records, rejections


def parse_plan(path: Path) -> Plan:
    try:
        doc = read_json(path)
    except (OSError, json.JSONDecodeError) as exc:
        raise InputError(f"cannot parse plan {path}: {exc}") from exc
    if not isinstance(doc, dict):
        raise InputError(f"{path}: plan must be an object")

    def integer(name: str, *, minimum: int, maximum: int | None = None) -> int:
        value = doc.get(name)
        if not _is_int(value) or value < minimum:
            raise InputError(f"{path}: {name} must be an integer >= {minimum}")
        if maximum is not None and value > maximum:
            raise InputError(f"{path}: {name} must be <= {maximum}")
        return value

    concurrency = integer("concurrency", minimum=1, maximum=64)
    timeout_ms = integer("connection_timeout_ms", minimum=1, maximum=3_600_000)
    budget_ms = integer("batch_budget_ms", minimum=0, maximum=86_400_000)
    queue_limit = integer("queue_limit", minimum=0, maximum=100_000)
    output_path = doc.get("output_path")
    if not isinstance(output_path, str) or not output_path.strip():
        raise InputError(f"{path}: output_path must be a non-empty string")
    return Plan(
        concurrency=concurrency,
        connection_timeout_ms=timeout_ms,
        batch_budget_ms=budget_ms,
        queue_limit=queue_limit,
        output_path=output_path,
    )


def parse_sample_dir(root: Path) -> SampleInput:
    root = Path(root)
    if not root.is_dir():
        raise InputError(f"sample directory does not exist: {root}")
    messages, message_rejections = parse_messages(root)
    rules_v1, v1_rejections = parse_rules_v1(root)
    rules_v2, v2_rejections = parse_rules_v2(root)
    plan = parse_plan(root / "plan.json")
    source_files = {
        name: str((root / name).resolve())
        for name in (
            "messages.jsonl",
            "messages.json",
            "rules_v1.json",
            "rules_v2.sqlite3",
            "plan.json",
        )
        if (root / name).exists()
    }
    return SampleInput(
        messages=messages,
        rules=rules_v1 + rules_v2,
        plan=plan,
        rejections=sorted(
            message_rejections + v1_rejections + v2_rejections,
            key=lambda item: (
                item.source_category,
                item.record_index,
                item.code,
                item.detail,
            ),
        ),
        source_files=source_files,
    )


def compute_input_digest(sample: SampleInput) -> str:
    message_docs = sorted(
        (
            item.connection_id,
            item.direction,
            item.seq,
            item.ts_ms,
            item.payload.hex(),
        )
        for item in sample.messages
    )
    rule_docs = sorted(
        (
            item.canonical_name,
            item.generation,
            item.effective_ms,
            item.direction,
            item.patterns,
            item.tombstone,
            item.enabled,
        )
        for item in sample.rules
    )
    rejection_docs = sorted(
        (
            item.source_category,
            item.record_index,
            item.code,
            item.detail,
        )
        for item in sample.rejections
    )
    return digest_value(
        {
            "messages": message_docs,
            "rules": rule_docs,
            "rejections": rejection_docs,
        }
    )


def compute_plan_digest(plan: Plan) -> str:
    return digest_value(
        {
            "concurrency": plan.concurrency,
            "connection_timeout_ms": plan.connection_timeout_ms,
            "batch_budget_ms": plan.batch_budget_ms,
            "queue_limit": plan.queue_limit,
        }
    )


def plan_with_overrides(
    plan: Plan,
    *,
    concurrency: int | None = None,
    timeout_ms: int | None = None,
    budget_ms: int | None = None,
    queue_limit: int | None = None,
    output_path: str | None = None,
) -> Plan:
    result = Plan(
        concurrency=plan.concurrency if concurrency is None else concurrency,
        connection_timeout_ms=(
            plan.connection_timeout_ms if timeout_ms is None else timeout_ms
        ),
        batch_budget_ms=plan.batch_budget_ms if budget_ms is None else budget_ms,
        queue_limit=plan.queue_limit if queue_limit is None else queue_limit,
        output_path=plan.output_path if output_path is None else output_path,
    )
    if result.concurrency < 1 or result.concurrency > 64:
        raise InputError(f"{E_BAD_PLAN}: concurrency must be in [1, 64]")
    if result.connection_timeout_ms < 1:
        raise InputError(f"{E_BAD_PLAN}: connection_timeout_ms must be >= 1")
    if result.batch_budget_ms < 0:
        raise InputError(f"{E_BAD_PLAN}: batch_budget_ms must be >= 0")
    if result.queue_limit < 0:
        raise InputError(f"{E_BAD_PLAN}: queue_limit must be >= 0")
    if not result.output_path.strip():
        raise InputError(f"{E_BAD_PLAN}: output_path must not be empty")
    return result

