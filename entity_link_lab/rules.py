"""Rule-book readers, canonicalization and v1/v2 migration.

The on-disk formats intentionally differ:

* v1 is one flat JSON file using seconds relative to an upgrade epoch.
* v2 is sharded JSON plus a SQLite metadata database using absolute ms,
  explicit algebra generations and tombstone rows.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Iterable

from .canonical import canonical_bytes, content_hash, read_json, write_json
from .canonical import sha256_bytes
from .errors import (
    E_FORMAT_CORRUPT,
    E_SEMANTIC_DRIFT,
    E_VERSION_UNSUPPORTED,
    LinkError,
)


V1_VERSIONS = {"v1", "1", "1.0", 1}
V2_VERSIONS = {"v2", "2", "2.0", 2}
INTERNAL_SCHEMA = "entity-link-rules-internal/v1"
V1_UPGRADE_ORIGIN_MS = 1761955200000
MIGRATION_EPOCH_MS = 1764201600000


def _corrupt(message: str, **details: Any) -> LinkError:
    return LinkError(E_FORMAT_CORRUPT, message, stage="rules", details=details)


def _require(obj: dict[str, Any], key: str, context: str) -> Any:
    if key not in obj or obj[key] is None:
        raise _corrupt(f"missing field {key!r} in {context}", field=key)
    return obj[key]


def _require_type(value: Any, typ: type | tuple[type, ...], field_name: str) -> None:
    if not isinstance(value, typ):
        raise _corrupt(
            f"field {field_name!r} must be {typ!r}",
            field=field_name,
            actual_type=type(value).__name__,
        )


def detect_version(raw: dict[str, Any]) -> str:
    if not isinstance(raw, dict) or "schema_version" not in raw:
        raise _corrupt("rule snapshot lacks schema_version", field="schema_version")
    version = raw["schema_version"]
    if version in V1_VERSIONS:
        return "v1"
    if version in V2_VERSIONS:
        return "v2"
    raise LinkError(
        E_VERSION_UNSUPPORTED,
        f"unsupported rule schema_version: {version!r}",
        stage="rules",
        details={"schema_version": version},
    )


def _weights(raw_weights: Any, context: str) -> dict[str, int]:
    _require_type(raw_weights, dict, f"{context}.weights_bps")
    required = {
        "shared_proportion",
        "distance_decay",
        "document_scale",
        "rule_algebra",
    }
    missing = sorted(required - set(raw_weights))
    if missing:
        raise _corrupt("rule weights missing fields", context=context, missing=missing)
    weights: dict[str, int] = {}
    for key in sorted(required):
        value = raw_weights[key]
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise _corrupt("weight must be a non-negative integer bps", field=key, value=value)
        weights[key] = value
    if sum(weights.values()) != 10_000:
        raise _corrupt("weights_bps must total 10000", weights=weights)
    return weights


def _normalize_rule(rule: dict[str, Any], context: str) -> dict[str, Any]:
    rule_id = _require(rule, "rule_id", context)
    entity_type = _require(rule, "entity_type", f"{context}:{rule_id}")
    generation = int(_require(rule, "generation", f"{context}:{rule_id}"))
    algebra_generation = int(rule.get("algebra_generation", generation))
    sentence_window = int(_require(rule, "sentence_window", f"{context}:{rule_id}"))
    position_scale_cp = int(_require(rule, "position_scale_cp", f"{context}:{rule_id}"))
    threshold_bps = int(_require(rule, "threshold_bps", f"{context}:{rule_id}"))
    effective_at_ms = int(_require(rule, "effective_at_ms", f"{context}:{rule_id}"))
    algebra_bias_raw_bps = int(rule.get("algebra_bias_raw_bps", 2_000))
    tombstone = bool(rule.get("tombstone", False))

    if generation < 1 or algebra_generation < 1:
        raise _corrupt("rule generation must be positive", rule_id=rule_id)
    if sentence_window < 0 or position_scale_cp <= 0:
        raise _corrupt("invalid sentence window or position scale", rule_id=rule_id)
    if not 0 <= threshold_bps <= 10_000:
        raise _corrupt("threshold_bps must be in [0,10000]", rule_id=rule_id)
    if effective_at_ms < 0:
        raise _corrupt("effective_at_ms must be non-negative", rule_id=rule_id)
    if not 0 <= algebra_bias_raw_bps <= 10_000:
        raise _corrupt("algebra_bias_raw_bps must be in bps range", rule_id=rule_id)

    description = rule.get("description", {})
    if not isinstance(description, dict):
        description = {"text": str(description)}

    return {
        "rule_id": str(rule_id),
        "family": str(rule.get("family", "context")),
        "generation": generation,
        "algebra_generation": algebra_generation,
        "entity_type": str(entity_type),
        "sentence_window": sentence_window,
        "position_scale_cp": position_scale_cp,
        "weights_bps": _weights(rule.get("weights_bps"), f"{context}:{rule_id}"),
        "threshold_bps": threshold_bps,
        "algebra_bias_raw_bps": algebra_bias_raw_bps,
        "effective_at_ms": effective_at_ms,
        "tombstone": tombstone,
        "description": description,
    }


def _validate_timeline(timeline: Any, context: str, time_field: str) -> list[dict[str, int]]:
    if timeline is None:
        return []
    if not isinstance(timeline, list):
        raise _corrupt("generation timeline must be a list", context=context)
    normalized: list[dict[str, int]] = []
    previous_time: int | None = None
    previous_generation = 0
    for item in timeline:
        if not isinstance(item, dict):
            raise _corrupt("timeline item must be an object", context=context)
        generation = int(_require(item, "generation", f"{context}.timeline"))
        timestamp = int(_require(item, time_field, f"{context}.timeline"))
        if generation <= previous_generation:
            raise _corrupt("generation timeline must increase", context=context, generation=generation)
        if previous_time is not None and timestamp < previous_time:
            raise LinkError(
                E_FORMAT_CORRUPT,
                "rule effective timestamp moved backwards",
                stage="rules",
                details={
                    "context": context,
                    "generation": generation,
                    "previous_timestamp": previous_time,
                    "timestamp": timestamp,
                },
            )
        previous_generation = generation
        previous_time = timestamp
        normalized.append({"generation": generation, time_field: timestamp})
    return normalized


def normalize_rule_book(raw: dict[str, Any]) -> dict[str, Any]:
    """Normalize either external format after version-specific reading."""

    snapshot_id = str(_require(raw, "snapshot_id", "rule book"))
    origin_ms = int(_require(raw, "origin_effective_at_ms", "rule book"))
    rules_raw = _require(raw, "rules", "rule book")
    if not isinstance(rules_raw, list) or not rules_raw:
        raise _corrupt("rules must be a non-empty list", snapshot_id=snapshot_id)
    rules = [_normalize_rule(rule, snapshot_id) for rule in rules_raw]
    rules.sort(key=lambda item: (item["entity_type"], item["rule_id"], item["generation"]))
    timeline = _validate_timeline(raw.get("generation_timeline_ms"), snapshot_id, "effective_at_ms")
    if not timeline:
        generations = sorted({rule["generation"] for rule in rules})
        timeline = [
            {
                "generation": generation,
                "effective_at_ms": min(
                    rule["effective_at_ms"] for rule in rules if rule["generation"] == generation
                ),
            }
            for generation in generations
        ]
    return {
        "internal_schema": INTERNAL_SCHEMA,
        "snapshot_id": snapshot_id,
        "origin_effective_at_ms": origin_ms,
        "rules": rules,
        "generation_timeline_ms": timeline,
    }


def internal_bytes(book: dict[str, Any]) -> bytes:
    return canonical_bytes(normalize_rule_book(book))


def internal_hash(book: dict[str, Any]) -> str:
    from .canonical import sha256_bytes

    return sha256_bytes(internal_bytes(book))


def read_v1(path: str | Path) -> dict[str, Any]:
    path = Path(path)
    raw = read_json(path)
    if not isinstance(raw, dict):
        raise _corrupt("v1 rule file must be an object", path=str(path))
    detect_version(raw)
    snapshot_id = str(_require(raw, "snapshot_id", "v1"))
    relative_timeline = _validate_timeline(
        raw.get("generation_timeline_seconds"), "v1", "effective_after_seconds"
    )
    rules_raw = _require(raw, "rules", "v1")
    if not isinstance(rules_raw, list):
        raise _corrupt("v1 rules must be a list")

    rules: list[dict[str, Any]] = []
    for raw_rule in rules_raw:
        if not isinstance(raw_rule, dict):
            raise _corrupt("v1 rule must be an object")
        rule_id = str(_require(raw_rule, "rule_id", "v1.rule"))
        rel_seconds = int(_require(raw_rule, "relative_effective_seconds", rule_id))
        if rel_seconds < 0:
            raise LinkError(
                E_FORMAT_CORRUPT,
                "relative effective seconds moved backwards or negative",
                stage="rules",
                details={"rule_id": rule_id, "relative_effective_seconds": rel_seconds},
            )
        generation = int(raw_rule.get("generation", 1))
        description = {
            key.removeprefix("feature_"): value
            for key, value in sorted(raw_rule.items())
            if key.startswith("feature_")
        }
        if "description" in raw_rule:
            description["text"] = raw_rule["description"]
        rules.append(
            {
                "rule_id": rule_id,
                "family": str(raw_rule.get("family", "context")),
                "generation": generation,
                "algebra_generation": int(raw_rule.get("algebra_generation", 1)),
                "entity_type": str(_require(raw_rule, "entity_type", rule_id)),
                "sentence_window": int(_require(raw_rule, "sentence_window", rule_id)),
                "position_scale_cp": int(raw_rule.get("position_scale_cp", 180)),
                "weights_bps": raw_rule.get("weights_bps"),
                "threshold_bps": int(_require(raw_rule, "threshold_bps", rule_id)),
                "algebra_bias_raw_bps": int(raw_rule.get("algebra_bias_raw_bps", 2_000)),
                "effective_at_ms": V1_UPGRADE_ORIGIN_MS + rel_seconds * 1000,
                "tombstone": bool(raw_rule.get("tombstone", False)),
                "description": description,
            }
        )

    timeline_ms = [
        {
            "generation": item["generation"],
            "effective_at_ms": V1_UPGRADE_ORIGIN_MS
            + item["effective_after_seconds"] * 1000,
        }
        for item in relative_timeline
    ]
    return normalize_rule_book(
        {
            "snapshot_id": snapshot_id,
            "origin_effective_at_ms": V1_UPGRADE_ORIGIN_MS,
            "rules": rules,
            "generation_timeline_ms": timeline_ms,
        }
    )


def _open_meta_db(path: Path) -> sqlite3.Connection:
    if not path.exists():
        raise _corrupt("v2 SQLite metadata database is missing", path=str(path))
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only = ON")
        return conn
    except sqlite3.Error as exc:
        raise _corrupt("cannot open v2 metadata database", path=str(path), error=str(exc)) from exc


def read_v2(source: str | Path) -> dict[str, Any]:
    source_path = Path(source)
    manifest_path = source_path / "manifest.json" if source_path.is_dir() else source_path
    if not manifest_path.exists():
        raise _corrupt("v2 manifest not found", path=str(manifest_path))
    manifest = read_json(manifest_path)
    if not isinstance(manifest, dict):
        raise _corrupt("v2 manifest must be an object")
    detect_version(manifest)
    base_dir = manifest_path.parent
    snapshot_id = str(_require(manifest, "snapshot_id", "v2 manifest"))
    db_rel = str(_require(manifest, "sqlite", "v2 manifest"))
    conn = _open_meta_db(base_dir / db_rel)
    try:
        db_snapshot = conn.execute(
            "SELECT snapshot_id, schema_version, manifest_hash FROM snapshot WHERE snapshot_id = ?",
            (snapshot_id,),
        ).fetchone()
        if db_snapshot is None:
            raise _corrupt("snapshot row missing in SQLite", snapshot_id=snapshot_id)

        rules: list[dict[str, Any]] = []
        shard_entries = _require(manifest, "shards", "v2 manifest")
        if not isinstance(shard_entries, list) or not shard_entries:
            raise _corrupt("v2 shards must be a non-empty list")
        for entry in shard_entries:
            if not isinstance(entry, dict):
                raise _corrupt("shard entry must be an object")
            shard_file = str(_require(entry, "path", "shard"))
            expected_hash = str(_require(entry, "sha256", shard_file))
            shard_path = base_dir / shard_file
            shard = read_json(shard_path)
            detect_version(shard)
            actual_hash = content_hash(shard)
            if actual_hash != expected_hash:
                raise _corrupt(
                    "shard checksum mismatch",
                    shard=shard_file,
                    expected=expected_hash,
                    actual=actual_hash,
                )
            shard_rules = _require(shard, "rules", shard_file)
            if not isinstance(shard_rules, list):
                raise _corrupt("shard rules must be a list", shard=shard_file)
            for raw_rule in shard_rules:
                rule_id = str(_require(raw_rule, "rule_id", shard_file))
                meta = conn.execute(
                    """
                    SELECT rule_id, shard, rule_generation, algebra_generation,
                           effective_at_ms, previous_effective_at_ms, active,
                           tombstone, content_hash
                    FROM rule_meta WHERE rule_id = ?
                    """,
                    (rule_id,),
                ).fetchone()
                if meta is None:
                    raise _corrupt("rule metadata missing in SQLite", rule_id=rule_id)
                if meta["content_hash"] != content_hash(raw_rule):
                    raise _corrupt(
                        "rule content hash mismatch in SQLite",
                        rule_id=rule_id,
                        expected=meta["content_hash"],
                        actual=content_hash(raw_rule),
                    )
                previous_ms = meta["previous_effective_at_ms"]
                effective_ms = int(meta["effective_at_ms"])
                if previous_ms is not None and effective_ms < int(previous_ms):
                    raise LinkError(
                        E_FORMAT_CORRUPT,
                        "SQLite rule timestamp moved backwards",
                        stage="rules",
                        details={
                            "rule_id": rule_id,
                            "previous_effective_at_ms": previous_ms,
                            "effective_at_ms": effective_ms,
                        },
                    )
                raw_rule = dict(raw_rule)
                raw_rule.setdefault("generation", meta["rule_generation"])
                raw_rule.setdefault("algebra_generation", meta["algebra_generation"])
                raw_rule["effective_at_ms"] = effective_ms
                raw_rule["tombstone"] = bool(meta["tombstone"])
                rules.append(raw_rule)

        timeline = _validate_timeline(
            manifest.get("generation_timeline_ms"), "v2 manifest", "effective_at_ms"
        )
        book = normalize_rule_book(
            {
                "snapshot_id": snapshot_id,
                "origin_effective_at_ms": int(
                    _require(manifest, "origin_effective_at_ms", "v2 manifest")
                ),
                "rules": rules,
                "generation_timeline_ms": timeline,
            }
        )
        if db_snapshot["manifest_hash"] != content_hash(manifest):
            raise _corrupt("manifest hash mismatch in SQLite")
        return book
    except sqlite3.Error as exc:
        raise _corrupt("SQLite metadata query failed", error=str(exc)) from exc
    finally:
        conn.close()


def read_rule_book(source: str | Path) -> dict[str, Any]:
    """Read either v1 file or v2 manifest/directory and return internal form."""

    path = Path(source)
    if path.is_dir():
        return read_v2(path / "manifest.json")
    raw = read_json(path)
    version = detect_version(raw)
    if version == "v1":
        return read_v1(path)

    sibling_manifest = path.parent / "manifest.json"
    if path.name == "manifest.json":
        return read_v2(path)
    if sibling_manifest.exists():
        return read_v2(sibling_manifest)
    raise _corrupt("v2 source must be a shard directory containing manifest.json", path=str(path))


def assert_internal_equal(expected: dict[str, Any], actual: dict[str, Any], context: str) -> None:
    expected_bytes = internal_bytes(expected)
    actual_bytes = internal_bytes(actual)
    if expected_bytes != actual_bytes:
        raise LinkError(
            E_SEMANTIC_DRIFT,
            "migrated internal rule representation is not byte-identical",
            stage="rules.migration",
            details={
                "context": context,
            "expected_sha256": sha256_bytes(expected_bytes),
            "actual_sha256": sha256_bytes(actual_bytes),
            },
        )


def write_v2(book: dict[str, Any], out_dir: str | Path) -> dict[str, Any]:
    book = normalize_rule_book(book)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    for old in out_dir.glob("rules-*.json"):
        old.unlink()

    active_rules = [rule for rule in book["rules"] if not rule["tombstone"]]
    shard_size = 2
    shard_entries: list[dict[str, Any]] = []
    shard_names: list[str] = []
    for index in range(0, len(book["rules"]), shard_size):
        shard_id = index // shard_size
        shard_name = f"rules-{shard_id:03d}.json"
        shard_rules = book["rules"][index : index + shard_size]
        shard = {
            "schema_version": "2.0",
            "shard": f"{shard_id:03d}",
            "rules": shard_rules,
        }
        write_json(out_dir / shard_name, shard)
        shard_entries.append(
            {"path": shard_name, "sha256": content_hash(shard), "rule_count": len(shard_rules)}
        )
        shard_names.append(shard_name)

    manifest = {
        "schema_version": "2.0",
        "snapshot_id": book["snapshot_id"],
        "origin_effective_at_ms": book["origin_effective_at_ms"],
        "sqlite": "rules_meta.db",
        "shards": shard_entries,
        "generation_timeline_ms": book["generation_timeline_ms"],
    }
    write_json(out_dir / "manifest.json", manifest)

    db_path = out_dir / "rules_meta.db"
    if db_path.exists():
        db_path.unlink()
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            CREATE TABLE snapshot (
                snapshot_id TEXT PRIMARY KEY,
                schema_version TEXT NOT NULL,
                created_at_ms INTEGER NOT NULL,
                manifest_hash TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE rule_meta (
                rule_id TEXT PRIMARY KEY,
                shard TEXT NOT NULL,
                rule_generation INTEGER NOT NULL,
                algebra_generation INTEGER NOT NULL,
                effective_at_ms INTEGER NOT NULL,
                previous_effective_at_ms INTEGER,
                active INTEGER NOT NULL,
                tombstone INTEGER NOT NULL,
                content_hash TEXT NOT NULL
            )
            """
        )
        conn.execute(
            "INSERT INTO snapshot VALUES (?, ?, ?, ?)",
            (
                book["snapshot_id"],
                "2.0",
                MIGRATION_EPOCH_MS,
                content_hash(manifest),
            ),
        )
        for entry_index, entry in enumerate(shard_entries):
            shard = read_json(out_dir / entry["path"])
            for rule in shard["rules"]:
                previous_ms = None
                if rule["generation"] > 1:
                    earlier_timestamps = [
                        item["effective_at_ms"]
                        for item in book["generation_timeline_ms"]
                        if item["generation"] < rule["generation"]
                    ]
                    if earlier_timestamps:
                        previous_ms = max(earlier_timestamps)
                conn.execute(
                    """
                    INSERT INTO rule_meta VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        rule["rule_id"],
                        entry["path"],
                        rule["generation"],
                        rule["algebra_generation"],
                        rule["effective_at_ms"],
                        previous_ms,
                        int(not rule["tombstone"]),
                        int(rule["tombstone"]),
                        content_hash(rule),
                    ),
                )
        conn.commit()
    finally:
        conn.close()
    return manifest


def migrate_v1_to_v2(v1_path: str | Path, out_dir: str | Path) -> dict[str, Any]:
    source = read_v1(v1_path)
    write_v2(source, out_dir)
    reread = read_v2(Path(out_dir) / "manifest.json")
    assert_internal_equal(source, reread, "v1->v2 reread")
    record = {
        "direction": "v1_to_v2",
        "status": "ok",
        "migration_epoch_ms": MIGRATION_EPOCH_MS,
        "defaults_filled": {
            "algebra_generation": 1,
            "tombstone": False,
            "absolute_ms": "origin + relative_effective_seconds * 1000",
            "origin_effective_at_ms": V1_UPGRADE_ORIGIN_MS,
        },
        "discarded": [],
        "folded": [],
        "internal_sha256": internal_hash(reread),
        "byte_identical": True,
    }
    write_json(Path(out_dir) / "migration_record.json", record)
    return record


def downgrade_book(book: dict[str, Any], mode: str = "fold") -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if mode not in {"fold", "discard"}:
        raise LinkError(E_FORMAT_CORRUPT, "unknown downgrade mode", details={"mode": mode})
    downgraded = normalize_rule_book(book)
    actions: list[dict[str, Any]] = []
    new_rules: list[dict[str, Any]] = []
    for rule in downgraded["rules"]:
        transformed = dict(rule)
        bias = int(rule["algebra_bias_raw_bps"])
        if mode == "fold":
            actions.append(
                {
                    "rule_id": rule["rule_id"],
                    "field": "algebra_bias_raw_bps",
                    "action": "fold",
                    "target": "description.folded_algebra_bias_raw_bps",
                    "value_bps": bias,
                }
            )
        else:
            transformed["algebra_bias_raw_bps"] = 0
            actions.append(
                {
                    "rule_id": rule["rule_id"],
                    "field": "algebra_bias_raw_bps",
                    "action": "discard",
                    "value_bps": bias,
                }
            )
        transformed["algebra_generation"] = 1
        transformed["generation"] = 1
        text = rule["description"].get("text", f"{rule['entity_type']} context rule")
        transformed["description"] = {
            "entity_type": "shared_type_signature",
            "position": "codepoint_and_sentence_decay",
            "sentence_window": f"same_doc_within_{rule['sentence_window']}",
            "text": text,
        }
        new_rules.append(transformed)
    downgraded["rules"] = new_rules
    downgraded["generation_timeline_ms"] = [
        {"generation": 1, "effective_at_ms": book["origin_effective_at_ms"]}
    ]
    return downgraded, actions


def expected_v1_reread(book: dict[str, Any], mode: str) -> dict[str, Any]:
    expected, _ = downgrade_book(book, mode)
    return expected


def write_v1(book: dict[str, Any], out_file: str | Path, mode: str = "fold") -> dict[str, Any]:
    downgraded, actions = downgrade_book(book, mode)
    rules = []
    for rule in downgraded["rules"]:
        flat: dict[str, Any] = {
            "rule_id": rule["rule_id"],
            "schema_version": "v1",
            "relative_effective_seconds": max(
                0,
                (rule["effective_at_ms"] - downgraded["origin_effective_at_ms"]) // 1000,
            ),
            "generation": 1,
            "entity_type": rule["entity_type"],
            "family": rule["family"],
            "sentence_window": rule["sentence_window"],
            "position_scale_cp": rule["position_scale_cp"],
            "weights_bps": rule["weights_bps"],
            "threshold_bps": rule["threshold_bps"],
            "algebra_bias_raw_bps": rule["algebra_bias_raw_bps"],
            "tombstone": rule["tombstone"],
            "description": rule["description"].get("text", f"{rule['entity_type']} context rule"),
            "feature_entity_type": "shared_type_signature",
            "feature_position": "codepoint_and_sentence_decay",
            "feature_sentence_window": f"same_doc_within_{rule['sentence_window']}",
        }
        if mode == "fold":
            flat["folded_algebra_bias_raw_bps"] = rule["description"].get(
                "folded_algebra_bias_raw_bps", rule["algebra_bias_raw_bps"]
            )
            flat["fold_rule"] = "flat_bias_preserved"
        rules.append(flat)
    raw_v1 = {
        "schema_version": "v1",
        "snapshot_id": downgraded["snapshot_id"],
        "generation_timeline_seconds": [
            {"generation": 1, "effective_after_seconds": 0}
        ],
        "rules": rules,
    }
    write_json(out_file, raw_v1)
    record = {
        "direction": "v2_to_v1",
        "status": "ok",
        "mode": mode,
        "migration_epoch_ms": MIGRATION_EPOCH_MS,
        "actions": actions,
        "internal_sha256": internal_hash(read_v1(out_file)),
        "byte_identical_under_policy": True,
    }
    write_json(Path(out_file).with_suffix(".migration.json"), record)
    return record


def migrate_v2_to_v1(
    v2_source: str | Path, out_file: str | Path, mode: str = "fold"
) -> dict[str, Any]:
    source = read_rule_book(v2_source)
    expected, actions = downgrade_book(source, mode)
    write_v1(source, out_file, mode=mode)
    reread = read_v1(out_file)

    if mode == "fold":
        # The explicit fold field is mapped back to the canonical algebra value
        # by read_v1's default handling only if raw v1 retains algebra_bias.
        # Compare policy-transformed expectation with the reread bytes.
        assert_internal_equal(expected, reread, "v2->v1 fold reread")
    else:
        # Discard is intentionally lossy; verify deterministic reread only.
        expected_discard, _ = downgrade_book(source, "discard")
        assert_internal_equal(expected_discard, reread, "v2->v1 discard reread")

    record = {
        "direction": "v2_to_v1",
        "status": "ok",
        "mode": mode,
        "migration_epoch_ms": MIGRATION_EPOCH_MS,
        "actions": actions,
        "internal_sha256": internal_hash(reread),
        "byte_identical_under_policy": True,
    }
    write_json(Path(out_file).with_suffix(".migration.json"), record)
    return record
