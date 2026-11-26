from __future__ import annotations

import json
import sqlite3
from decimal import Decimal, InvalidOperation, ROUND_FLOOR
from pathlib import Path
from typing import Any

from .errors import fail
from .models import Directory, Rule, V1Snapshot, V2Snapshot, WriteEvent
from .util import (
    decode_content,
    read_json,
    required_str,
    sha256_bytes,
    sha256_file,
    strict_int,
)


def _parse_directory(entry: Any, index: int) -> Directory:
    if not isinstance(entry, dict):
        raise fail(
            "E_SCHEMA",
            "directory entry must be an object",
            index=index,
            field="dirs",
        )
    dir_id = required_str(entry.get("dir_id"), f"dirs[{index}].dir_id")
    parent_raw = entry.get("parent_id")
    parent_id = None
    if parent_raw is not None:
        parent_id = required_str(parent_raw, f"dirs[{index}].parent_id")

    base_raw = entry.get("base_quota")
    base_quota: int | None
    if base_raw is None:
        base_quota = None
    else:
        if isinstance(base_raw, bool) or not isinstance(base_raw, int) or base_raw <= 0:
            raise fail(
                "E_DIR_QUOTA_INVALID",
                "base_quota must be a positive integer or null",
                index=index,
                dir_id=dir_id,
                base_quota=base_raw,
            )
        base_quota = base_raw

    ext_raw = entry.get("ext_quotas", {})
    if not isinstance(ext_raw, dict):
        raise fail(
            "E_DIR_QUOTA_INVALID",
            "ext_quotas must be an object",
            index=index,
            dir_id=dir_id,
            ext_quotas=ext_raw,
        )
    ext_quotas: dict[str, int] = {}
    for key, value in ext_raw.items():
        if not isinstance(key, str) or not key:
            raise fail(
                "E_DIR_QUOTA_INVALID",
                "extended quota file key must be a non-empty string",
                index=index,
                dir_id=dir_id,
                file_key=key,
            )
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise fail(
                "E_DIR_QUOTA_INVALID",
                "extended quota must be a positive integer",
                index=index,
                dir_id=dir_id,
                file_key=key,
                quota=value,
            )
        ext_quotas[key] = value
    if base_quota is None and not ext_quotas:
        raise fail(
            "E_DIR_QUOTA_INVALID",
            "directory must declare base_quota or at least one extended quota",
            index=index,
            dir_id=dir_id,
        )

    name_raw = entry.get("name")
    name = None
    if name_raw is not None:
        name = required_str(name_raw, f"dirs[{index}].name")
    return Directory(
        dir_id=dir_id,
        parent_id=parent_id,
        base_quota=base_quota,
        ext_quotas=ext_quotas,
        name=name or dir_id,
    )


def _validate_directory_graph(dirs: list[Directory]) -> None:
    by_id: dict[str, Directory] = {}
    for index, directory in enumerate(dirs):
        if directory.dir_id in by_id:
            raise fail(
                "E_DIR_ID_DUP",
                "directory ID must be unique",
                index=index,
                dir_id=directory.dir_id,
            )
        by_id[directory.dir_id] = directory
    for directory in dirs:
        if directory.parent_id is not None and directory.parent_id not in by_id:
            raise fail(
                "E_DIR_PARENT_MISSING",
                "parent directory does not exist",
                dir_id=directory.dir_id,
                parent_id=directory.parent_id,
            )
    state: dict[str, int] = {}
    for directory in dirs:
        chain: list[str] = []
        current: str | None = directory.dir_id
        while current is not None:
            if state.get(current) == 2:
                break
            if state.get(current) == 1:
                start = chain.index(current) if current in chain else 0
                cycle = chain[start:] + [current]
                raise fail(
                    "E_DIR_CYCLE",
                    "directory parent chain contains a cycle",
                    dir_id=directory.dir_id,
                    chain=cycle,
                )
            state[current] = 1
            chain.append(current)
            current = by_id[current].parent_id
        for item in chain:
            state[item] = 2


def load_dirs(path: str | Path) -> list[Directory]:
    payload = read_json(path)
    if not isinstance(payload, dict) or not isinstance(payload.get("dirs"), list):
        raise fail(
            "E_SCHEMA",
            "directory manifest must contain a dirs array",
            path=str(path),
            field="dirs",
        )
    dirs = [_parse_directory(entry, i) for i, entry in enumerate(payload["dirs"])]
    _validate_directory_graph(dirs)
    return dirs


def _validate_digest(value: Any, line: int, seq: int | None) -> str:
    if not isinstance(value, str):
        raise fail(
            "E_EVENT_DIGEST_FORMAT",
            "digest must be a SHA-256 hex string",
            line=line,
            seq=seq,
        )
    normalized = value.lower()
    if len(normalized) != 64 or any(c not in "0123456789abcdef" for c in normalized):
        raise fail(
            "E_EVENT_DIGEST_FORMAT",
            "digest must be a 64-character SHA-256 hex string",
            line=line,
            seq=seq,
            digest=value,
        )
    return normalized


def load_events(path: str | Path, dir_ids: set[str]) -> list[WriteEvent]:
    p = Path(path)
    try:
        fh = p.open("r", encoding="utf-8")
    except FileNotFoundError:
        raise fail("E_FILE_NOT_FOUND", "write stream does not exist", path=str(p))

    events: list[WriteEvent] = []
    previous_seq: int | None = None
    previous_clock: int | None = None
    with fh:
        for line_no, line in enumerate(fh, 1):
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError as exc:
                raise fail(
                    "E_JSON_PARSE",
                    "invalid JSON line in write stream",
                    path=str(p),
                    line=line_no,
                    column=exc.colno,
                    position=exc.pos,
                )
            if not isinstance(entry, dict):
                raise fail(
                    "E_SCHEMA",
                    "write event must be an object",
                    path=str(p),
                    line=line_no,
                )
            seq = strict_int(entry.get("seq"), "seq", minimum=1)
            if previous_seq is not None and seq <= previous_seq:
                raise fail(
                    "E_EVENT_SEQ_ORDER",
                    "event sequence must be strictly increasing",
                    line=line_no,
                    previous_seq=previous_seq,
                    seq=seq,
                )
            clock_ms = strict_int(entry.get("clock_ms"), "clock_ms", minimum=0)
            if previous_clock is not None and clock_ms < previous_clock:
                raise fail(
                    "E_EVENT_CLOCK_ORDER",
                    "injected logical clock must be monotonically non-decreasing",
                    line=line_no,
                    seq=seq,
                    previous_clock_ms=previous_clock,
                    clock_ms=clock_ms,
                )
            dir_id = required_str(entry.get("dir_id"), "dir_id")
            if dir_id not in dir_ids:
                raise fail(
                    "E_EVENT_UNKNOWN_DIR",
                    "write event references an unknown directory",
                    line=line_no,
                    seq=seq,
                    dir_id=dir_id,
                )
            file_key = required_str(entry.get("file_key"), "file_key")
            size = strict_int(entry.get("bytes"), "bytes", minimum=1)
            digest = _validate_digest(entry.get("digest"), line_no, seq)
            content = decode_content(entry, line=line_no)
            if content is not None:
                if len(content) != size:
                    raise fail(
                        "E_EVENT_CONTENT_LENGTH",
                        "content byte length does not match bytes",
                        line=line_no,
                        seq=seq,
                        declared_bytes=size,
                        content_bytes=len(content),
                    )
                actual_digest = sha256_bytes(content)
                if actual_digest != digest:
                    raise fail(
                        "E_EVENT_DIGEST_MISMATCH",
                        "content digest does not match declared digest",
                        line=line_no,
                        seq=seq,
                        expected_digest=digest,
                        actual_digest=actual_digest,
                    )
            content_b64 = entry.get("content_b64")
            events.append(
                WriteEvent(
                    seq=seq,
                    dir_id=dir_id,
                    file_key=file_key,
                    bytes=size,
                    digest=digest,
                    clock_ms=clock_ms,
                    content_b64=content_b64 if isinstance(content_b64, str) else None,
                )
            )
            previous_seq = seq
            previous_clock = clock_ms
    if not events:
        raise fail("E_SCHEMA", "write stream is empty", path=str(p))
    return events


def _parse_rule(
    entry: Any,
    *,
    source: str,
    source_version: str,
    index: int,
    default_generation: int | None = None,
) -> Rule:
    if not isinstance(entry, dict):
        raise fail(
            "E_RULE_SCHEMA",
            "rule entry must be an object",
            source=source,
            source_version=source_version,
            index=index,
        )
    rule_key = required_str(entry.get("rule_key"), "rule_key")
    quota_bytes = strict_int(entry.get("quota_bytes"), "quota_bytes", minimum=1)
    effective_at_ms = strict_int(
        entry.get("effective_at_ms"), "effective_at_ms", minimum=0
    )
    generation_raw = entry.get("generation", default_generation)
    generation = strict_int(generation_raw, "generation", minimum=1)
    return Rule(
        rule_key=rule_key,
        quota_bytes=quota_bytes,
        effective_at_ms=effective_at_ms,
        generation=generation,
        source=source,
        source_version=source_version,
    )


def _decimal_number(value: Any, field: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (int, float, str, Decimal)):
        raise fail("E_RULE_SCHEMA", f"{field} must be a number", field=field, value=value)
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        raise fail("E_RULE_SCHEMA", f"{field} is not a number", field=field, value=value)
    if not number.is_finite():
        raise fail("E_RULE_SCHEMA", f"{field} must be finite", field=field, value=value)
    return number


def load_v1(path: str | Path) -> V1Snapshot:
    payload = read_json(path)
    if not isinstance(payload, dict):
        raise fail("E_RULE_SCHEMA", "v1 rule snapshot must be an object", path=str(path))
    fmt = payload.get("format", "v1")
    if fmt != "v1":
        raise fail(
            "E_RULE_SCHEMA",
            "v1 snapshot format must be 'v1'",
            path=str(path),
            format=fmt,
        )
    manifest_version = required_str(payload.get("manifest_version"), "manifest_version")
    base_clock_ms = strict_int(payload.get("base_clock_ms", 0), "base_clock_ms", minimum=0)
    raw_rules = payload.get("rules")
    if not isinstance(raw_rules, list):
        raise fail("E_RULE_SCHEMA", "v1 rules must be an array", path=str(path))

    rules: list[Rule] = []
    seen: set[str] = set()
    for index, entry in enumerate(raw_rules):
        if not isinstance(entry, dict):
            raise fail(
                "E_RULE_SCHEMA",
                "v1 rule entry must be an object",
                index=index,
            )
        rule_key = required_str(entry.get("rule_key"), f"rules[{index}].rule_key")
        if rule_key in seen:
            raise fail(
                "E_RULE_KEY_DUP",
                "duplicate rule key in v1 snapshot",
                source="v1",
                source_version=manifest_version,
                rule_key=rule_key,
                index=index,
            )
        seen.add(rule_key)
        quota_bytes = strict_int(entry.get("quota_bytes"), "quota_bytes", minimum=1)
        seconds = _decimal_number(entry.get("effective_in_seconds"), "effective_in_seconds")
        if seconds < 0:
            raise fail(
                "E_RULE_SCHEMA",
                "effective_in_seconds must be non-negative",
                rule_key=rule_key,
                effective_in_seconds=str(seconds),
            )
        raw_ms = Decimal(base_clock_ms) + seconds * Decimal(1000)
        effective_at_ms = int(raw_ms.to_integral_value(rounding=ROUND_FLOOR))
        remainder = raw_ms - Decimal(effective_at_ms)
        if remainder == 0:
            rounding = "exact"
            remainder_value: int | float = 0
        else:
            rounding = "floor"
            remainder_value = float(remainder)
        seconds_value: int | float
        if seconds == seconds.to_integral_value():
            seconds_value = int(seconds)
        else:
            seconds_value = float(seconds)
        conversion = {
            "base_clock_ms": base_clock_ms,
            "effective_at_ms": effective_at_ms,
            "effective_in_seconds": seconds_value,
            "remainder_ms": remainder_value,
            "rounding": rounding,
            "scale": 1000,
        }
        generation = strict_int(entry.get("generation", 1), "generation", minimum=1)
        rules.append(
            Rule(
                rule_key=rule_key,
                quota_bytes=quota_bytes,
                effective_at_ms=effective_at_ms,
                generation=generation,
                source="v1",
                source_version=manifest_version,
                conversion=conversion,
            )
        )
    return V1Snapshot(
        manifest_version=manifest_version,
        base_clock_ms=base_clock_ms,
        rules=tuple(rules),
    )


def _resolve_meta_path(path: str | Path) -> Path:
    p = Path(path)
    if p.is_dir():
        return p / "meta.db"
    return p


def load_v2(path: str | Path) -> V2Snapshot:
    meta_path = _resolve_meta_path(path)
    package_dir = meta_path.parent
    if not meta_path.exists():
        raise fail("E_FILE_NOT_FOUND", "v2 meta database does not exist", path=str(meta_path))
    try:
        conn = sqlite3.connect(f"file:{meta_path}?mode=ro", uri=True)
        try:
            manifest_rows = conn.execute(
                "SELECT version, min_generation, key_digest FROM manifest"
            ).fetchall()
            shard_rows = conn.execute("SELECT name, sha256 FROM shards").fetchall()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        raise fail(
            "E_V2_META",
            "cannot read v2 metadata database",
            path=str(meta_path),
            detail=str(exc),
        )
    if len(manifest_rows) != 1:
        raise fail(
            "E_V2_META",
            "v2 manifest must contain exactly one row",
            path=str(meta_path),
            rows=len(manifest_rows),
        )
    version, min_generation_raw, key_digest = manifest_rows[0]
    version = required_str(version, "version")
    min_generation = strict_int(min_generation_raw, "min_generation", minimum=1)
    if not isinstance(key_digest, str) or len(key_digest) != 64:
        raise fail(
            "E_V2_META",
            "key_digest must be a SHA-256 hex string",
            path=str(meta_path),
            key_digest=key_digest,
        )

    shard_hashes: dict[str, str] = {}
    rules: list[Rule] = []
    seen: set[str] = set()
    for name, declared_hash in shard_rows:
        name = required_str(name, "shard.name")
        if Path(name).name != name:
            raise fail(
                "E_V2_SHARD_PATH",
                "shard name must not contain a path",
                name=name,
            )
        declared_hash = required_str(declared_hash, "shard.sha256").lower()
        shard_path = package_dir / name
        if not shard_path.exists():
            raise fail(
                "E_V2_SHARD_MISSING",
                "declared shard file does not exist",
                name=name,
                path=str(shard_path),
            )
        actual_hash = sha256_file(shard_path)
        if actual_hash != declared_hash:
            raise fail(
                "E_V2_SHARD_HASH",
                "shard content hash mismatch",
                name=name,
                path=str(shard_path),
                expected_sha256=declared_hash,
                actual_sha256=actual_hash,
            )
        shard_hashes[name] = actual_hash
        payload = read_json(shard_path)
        if not isinstance(payload, dict) or not isinstance(payload.get("rules"), list):
            raise fail(
                "E_RULE_SCHEMA",
                "shard must contain a rules array",
                path=str(shard_path),
                name=name,
            )
        for index, entry in enumerate(payload["rules"]):
            rule = _parse_rule(
                entry,
                source="v2",
                source_version=version,
                index=index,
            )
            if rule.rule_key in seen:
                raise fail(
                    "E_RULE_KEY_DUP",
                    "duplicate rule key across v2 shards",
                    source="v2",
                    source_version=version,
                    rule_key=rule.rule_key,
                    shard=name,
                )
            seen.add(rule.rule_key)
            rules.append(rule)
    keys = tuple(sorted(seen))
    actual_key_digest = sha256_bytes("\n".join(keys).encode("utf-8"))
    if actual_key_digest != key_digest:
        raise fail(
            "E_V2_KEY_DIGEST",
            "v2 rule key set digest mismatch",
            source_version=version,
            expected_key_digest=key_digest,
            actual_key_digest=actual_key_digest,
            keys=list(keys),
        )
    return V2Snapshot(
        version=version,
        min_generation=min_generation,
        key_digest=key_digest,
        shard_hashes=shard_hashes,
        keys=keys,
        rules=tuple(rules),
    )


def load_patch(path: str | Path, dir_ids: set[str]) -> dict[str, Any]:
    payload = read_json(path)
    if not isinstance(payload, dict):
        raise fail("E_PATCH_SCHEMA", "patch must be an object", path=str(path))
    patch_id = required_str(payload.get("patch_id"), "patch_id")
    ops = payload.get("ops")
    if not isinstance(ops, list):
        raise fail("E_PATCH_SCHEMA", "patch ops must be an array", patch_id=patch_id)
    for index, op in enumerate(ops):
        if not isinstance(op, dict):
            raise fail(
                "E_PATCH_SCHEMA",
                "patch op must be an object",
                patch_id=patch_id,
                index=index,
            )
        kind = op.get("op")
        if kind == "rule_change":
            rule_key = required_str(op.get("rule_key"), "rule_key")
            if rule_key.startswith("dir:") and rule_key[4:] not in dir_ids:
                raise fail(
                    "E_PATCH_UNKNOWN_DIR",
                    "rule_change references an unknown directory",
                    patch_id=patch_id,
                    index=index,
                    rule_key=rule_key,
                )
            strict_int(op.get("quota_bytes"), "quota_bytes", minimum=1)
            strict_int(op.get("effective_at_ms"), "effective_at_ms", minimum=0)
            strict_int(op.get("generation"), "generation", minimum=1)
        elif kind == "rename":
            dir_id = required_str(op.get("dir_id"), "dir_id")
            if dir_id not in dir_ids:
                raise fail(
                    "E_PATCH_UNKNOWN_DIR",
                    "rename references an unknown directory",
                    patch_id=patch_id,
                    index=index,
                    dir_id=dir_id,
                )
            required_str(op.get("new_name"), "new_name")
        elif kind == "tombstone":
            has_rule = op.get("rule_key") is not None
            has_dir = op.get("dir_id") is not None
            if not has_rule and not has_dir:
                raise fail(
                    "E_PATCH_SCHEMA",
                    "tombstone requires rule_key or dir_id",
                    patch_id=patch_id,
                    index=index,
                )
            if has_rule:
                rule_key = required_str(op.get("rule_key"), "rule_key")
                if rule_key.startswith("dir:") and rule_key[4:] not in dir_ids:
                    raise fail(
                        "E_PATCH_UNKNOWN_DIR",
                        "tombstone references an unknown directory rule",
                        patch_id=patch_id,
                        index=index,
                        rule_key=rule_key,
                    )
            if has_dir:
                dir_id = required_str(op.get("dir_id"), "dir_id")
                if dir_id not in dir_ids:
                    raise fail(
                        "E_PATCH_UNKNOWN_DIR",
                        "tombstone references an unknown directory",
                        patch_id=patch_id,
                        index=index,
                        dir_id=dir_id,
                    )
        else:
            raise fail(
                "E_PATCH_SCHEMA",
                "unsupported patch op",
                patch_id=patch_id,
                index=index,
                op=kind,
            )
    return payload
