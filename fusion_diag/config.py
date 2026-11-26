from __future__ import annotations

import copy
import json
import math
import shutil
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .canonical import (
    atomic_write_json,
    canonical_json,
    digest_json,
    stable_float,
)
from .errors import ConfigurationError


ROUTE_ORDER = ("lexical", "prefix", "category", "vector")
NORMALIZATIONS = ("minmax", "reciprocal_rank")
DEDUP_POLICIES = ("highest_fusion", "first_hit")
TOPK_COMPARISONS = ("strict", "tolerance")
DEFAULT_MIGRATION_EPOCH_MS = 1_700_000_000_000


def default_config_payload(
    *,
    generation: int = 1,
    normalization: str = "minmax",
    lexical_weight: float = 0.35,
    prefix_weight: float = 0.2,
    category_weight: float = 0.2,
    vector_weight: float = 0.25,
    worker_count: int = 2,
    memory_limit_mb: int = 128,
) -> dict[str, Any]:
    return {
        "generation": generation,
        "recalls": {
            "lexical": {
                "enabled": True,
                "limit": 10,
                "weight": lexical_weight,
                "k1": 1.2,
                "b": 0.75,
            },
            "prefix": {
                "enabled": True,
                "limit": 8,
                "weight": prefix_weight,
                "min_prefix": 2,
                "scale": 1.0,
            },
            "category": {
                "enabled": True,
                "limit": 8,
                "weight": category_weight,
                "popularity_scale": 0.05,
            },
            "vector": {
                "enabled": True,
                "limit": 8,
                "weight": vector_weight,
                "dim": 16,
                "seed": 20261127,
                "normalize": True,
            },
        },
        "fusion": {
            "normalization": normalization,
            "dedup_policy": "highest_fusion",
            "topk_compare": "tolerance",
            "tolerance": 1e-10,
            "candidate_budget": 24,
            "worker_count": worker_count,
            "memory_limit_mb": memory_limit_mb,
        },
    }


def semantic_payload(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "generation": payload["generation"],
        "recalls": payload["recalls"],
        "fusion": payload["fusion"],
    }


def semantic_digest(payload: dict[str, Any]) -> str:
    return digest_json(semantic_payload(payload))


def config_version(payload: dict[str, Any]) -> str:
    return f"gen-{payload['generation']}-{semantic_digest(payload)[:12]}"


def _require_plain_dict(value: Any, *, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ConfigurationError(f"{field} must be an object", field=field)
    return value


def _number(
    value: Any,
    *,
    field: str,
    minimum: float | None = None,
    maximum: float | None = None,
) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigurationError(f"{field} must be numeric", field=field)
    result = float(value)
    if not math.isfinite(result):
        raise ConfigurationError(f"{field} must be finite", field=field)
    if minimum is not None and result < minimum:
        raise ConfigurationError(
            f"{field} must be >= {minimum}", field=field
        )
    if maximum is not None and result > maximum:
        raise ConfigurationError(
            f"{field} must be <= {maximum}", field=field
        )
    return result


def _integer(
    value: Any,
    *,
    field: str,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigurationError(f"{field} must be an integer", field=field)
    if minimum is not None and value < minimum:
        raise ConfigurationError(f"{field} must be >= {minimum}", field=field)
    if maximum is not None and value > maximum:
        raise ConfigurationError(f"{field} must be <= {maximum}", field=field)
    return value


def _boolean(value: Any, *, field: str) -> bool:
    if not isinstance(value, bool):
        raise ConfigurationError(f"{field} must be a boolean", field=field)
    return value


def validate_config_payload(payload: dict[str, Any]) -> dict[str, Any]:
    root = _require_plain_dict(payload, field="config")
    generation = _integer(
        root.get("generation"), field="generation", minimum=0
    )
    recalls = _require_plain_dict(root.get("recalls"), field="recalls")
    if set(recalls) != set(ROUTE_ORDER):
        raise ConfigurationError(
            f"recalls must contain exactly {list(ROUTE_ORDER)}", field="recalls"
        )
    enabled_weight = 0.0
    normalized_recalls: dict[str, Any] = {}
    for name in ROUTE_ORDER:
        route = _require_plain_dict(recalls[name], field=f"recalls.{name}")
        enabled = _boolean(route.get("enabled"), field=f"recalls.{name}.enabled")
        limit = _integer(
            route.get("limit"),
            field=f"recalls.{name}.limit",
            minimum=1,
            maximum=100_000,
        )
        weight = _number(
            route.get("weight"),
            field=f"recalls.{name}.weight",
            minimum=0.0,
        )
        if enabled:
            enabled_weight += weight
        item: dict[str, Any] = {
            "enabled": enabled,
            "limit": limit,
            "weight": stable_float(weight),
        }
        if name == "lexical":
            item["k1"] = stable_float(
                _number(route.get("k1"), field="recalls.lexical.k1", minimum=0.0)
            )
            item["b"] = stable_float(
                _number(
                    route.get("b"),
                    field="recalls.lexical.b",
                    minimum=0.0,
                    maximum=1.0,
                )
            )
        elif name == "prefix":
            item["min_prefix"] = _integer(
                route.get("min_prefix"),
                field="recalls.prefix.min_prefix",
                minimum=1,
                maximum=64,
            )
            item["scale"] = stable_float(
                _number(
                    route.get("scale"),
                    field="recalls.prefix.scale",
                    minimum=0.0,
                )
            )
        elif name == "category":
            item["popularity_scale"] = stable_float(
                _number(
                    route.get("popularity_scale"),
                    field="recalls.category.popularity_scale",
                    minimum=0.0,
                )
            )
        elif name == "vector":
            item["dim"] = _integer(
                route.get("dim"),
                field="recalls.vector.dim",
                minimum=4,
                maximum=256,
            )
            item["seed"] = _integer(
                route.get("seed"),
                field="recalls.vector.seed",
                minimum=0,
                maximum=2**63 - 1,
            )
            item["normalize"] = _boolean(
                route.get("normalize"), field="recalls.vector.normalize"
            )
        normalized_recalls[name] = item
    if enabled_weight <= 0.0:
        raise ConfigurationError(
            "at least one enabled recall must have a positive weight",
            field="recalls",
        )
    fusion = _require_plain_dict(root.get("fusion"), field="fusion")
    normalization = fusion.get("normalization")
    if normalization not in NORMALIZATIONS:
        raise ConfigurationError(
            f"normalization must be one of {list(NORMALIZATIONS)}",
            field="fusion.normalization",
        )
    dedup_policy = fusion.get("dedup_policy")
    if dedup_policy not in DEDUP_POLICIES:
        raise ConfigurationError(
            f"dedup_policy must be one of {list(DEDUP_POLICIES)}",
            field="fusion.dedup_policy",
        )
    topk_compare = fusion.get("topk_compare")
    if topk_compare not in TOPK_COMPARISONS:
        raise ConfigurationError(
            f"topk_compare must be one of {list(TOPK_COMPARISONS)}",
            field="fusion.topk_compare",
        )
    tolerance = _number(
        fusion.get("tolerance"),
        field="fusion.tolerance",
        minimum=0.0,
    )
    if topk_compare == "tolerance" and tolerance <= 0:
        raise ConfigurationError(
            "tolerance strategy requires tolerance > 0",
            field="fusion.tolerance",
        )
    candidate_budget = _integer(
        fusion.get("candidate_budget"),
        field="fusion.candidate_budget",
        minimum=1,
        maximum=100_000,
    )
    worker_count = _integer(
        fusion.get("worker_count"),
        field="fusion.worker_count",
        minimum=1,
        maximum=128,
    )
    memory_limit_mb = _integer(
        fusion.get("memory_limit_mb"),
        field="fusion.memory_limit_mb",
        minimum=1,
        maximum=1_048_576,
    )
    normalized_fusion = {
        "normalization": normalization,
        "dedup_policy": dedup_policy,
        "topk_compare": topk_compare,
        "tolerance": stable_float(tolerance),
        "candidate_budget": candidate_budget,
        "worker_count": worker_count,
        "memory_limit_mb": memory_limit_mb,
    }
    return {
        "generation": generation,
        "recalls": normalized_recalls,
        "fusion": normalized_fusion,
    }


@dataclass(frozen=True)
class ConfigSnapshot:
    source_schema: str
    config_id: str
    generation: int
    effective_seconds: float | None
    effective_at_ms: int | None
    payload: dict[str, Any]
    digest: str
    tombstones: tuple[dict[str, Any], ...]
    path: str
    migration_log: tuple[dict[str, Any], ...] = ()

    @property
    def recalls(self) -> dict[str, Any]:
        return self.payload["recalls"]

    @property
    def fusion(self) -> dict[str, Any]:
        return self.payload["fusion"]

    @property
    def version(self) -> str:
        return config_version(self.payload)

    @property
    def enabled_routes(self) -> tuple[str, ...]:
        return tuple(
            name for name in ROUTE_ORDER if self.recalls[name]["enabled"]
        )

    def reference(self) -> dict[str, Any]:
        reference: dict[str, Any] = {
            "schema": self.source_schema,
            "version": self.version,
            "generation": self.generation,
            "digest": self.digest,
            "config_id": self.config_id,
        }
        if self.effective_at_ms is not None:
            reference["effective_at_ms"] = self.effective_at_ms
        else:
            reference["effective_seconds"] = stable_float(
                self.effective_seconds or 0.0
            )
        return reference


def make_v1_snapshot(
    payload: dict[str, Any],
    *,
    config_id: str,
    effective_seconds: float = 0.0,
) -> dict[str, Any]:
    normalized = validate_config_payload(payload)
    envelope = {
        "snapshot_schema": "fusion_config.v1",
        "config_id": config_id,
        "effective_seconds": stable_float(effective_seconds),
        "generation": normalized["generation"],
        "recalls": normalized["recalls"],
        "fusion": normalized["fusion"],
    }
    envelope["digest"] = semantic_digest(envelope)
    return envelope


def write_v1(
    path: str | Path,
    payload: dict[str, Any],
    *,
    config_id: str,
    effective_seconds: float = 0.0,
) -> dict[str, Any]:
    envelope = make_v1_snapshot(
        payload, config_id=config_id, effective_seconds=effective_seconds
    )
    atomic_write_json(path, envelope)
    return envelope


def load_v1(path: str | Path) -> ConfigSnapshot:
    source = Path(path)
    try:
        envelope = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigurationError(
            f"cannot parse v1 configuration: {exc}", field="config"
        ) from exc
    if not isinstance(envelope, dict) or envelope.get("snapshot_schema") != "fusion_config.v1":
        raise ConfigurationError(
            "expected fusion_config.v1", field="snapshot_schema"
        )
    payload = validate_config_payload(envelope)
    expected = semantic_digest(envelope)
    if envelope.get("digest") != expected:
        raise ConfigurationError(
            "v1 configuration digest mismatch", field="digest"
        )
    config_id = envelope.get("config_id")
    if not isinstance(config_id, str) or not config_id:
        raise ConfigurationError("config_id must be a string", field="config_id")
    effective_seconds = _number(
        envelope.get("effective_seconds"),
        field="effective_seconds",
        minimum=0.0,
    )
    return ConfigSnapshot(
        source_schema="fusion_config.v1",
        config_id=config_id,
        generation=payload["generation"],
        effective_seconds=stable_float(effective_seconds),
        effective_at_ms=None,
        payload=payload,
        digest=expected,
        tombstones=(),
        path=str(source),
    )


def _write_v2(
    directory: str | Path,
    payload: dict[str, Any],
    *,
    config_id: str,
    effective_at_ms: int,
    tombstones: list[dict[str, Any]],
    migration_log: list[dict[str, Any]],
) -> dict[str, Any]:
    destination = Path(directory)
    if destination.exists():
        shutil.rmtree(destination)
    shards_dir = destination / "shards"
    shards_dir.mkdir(parents=True)
    shard_files: dict[str, str] = {}
    shard_meta: list[dict[str, Any]] = []
    for route in ROUTE_ORDER:
        shard = {
            "snapshot_schema": "fusion_config.v2.shard",
            "generation": payload["generation"],
            "route": route,
            "value": payload["recalls"][route],
        }
        filename = f"recall_{route}.json"
        atomic_write_json(shards_dir / filename, shard)
        shard_files[route] = filename
        shard_meta.append(
            {
                "name": f"recall.{route}",
                "file": f"shards/{filename}",
                "digest": digest_json(shard),
            }
        )
    fusion_shard = {
        "snapshot_schema": "fusion_config.v2.shard",
        "generation": payload["generation"],
        "route": "fusion",
        "value": payload["fusion"],
    }
    atomic_write_json(shards_dir / "fusion.json", fusion_shard)
    shard_meta.append(
        {
            "name": "fusion",
            "file": "shards/fusion.json",
            "digest": digest_json(fusion_shard),
        }
    )
    manifest = {
        "snapshot_schema": "fusion_config.v2",
        "config_id": config_id,
        "generation": payload["generation"],
        "effective_at_ms": effective_at_ms,
        "digest": semantic_digest(payload),
        "shards": shard_meta,
        "metadata_db": "metadata.sqlite",
    }
    atomic_write_json(destination / "manifest.json", manifest)
    database = destination / "metadata.sqlite"
    with sqlite3.connect(database) as connection:
        connection.executescript(
            """
            CREATE TABLE meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE shards (
                name TEXT PRIMARY KEY,
                path TEXT NOT NULL,
                digest TEXT NOT NULL
            );
            CREATE TABLE tombstones (
                tombstone_id TEXT PRIMARY KEY,
                kind TEXT NOT NULL,
                field TEXT NOT NULL,
                reason TEXT NOT NULL,
                source_generation INTEGER NOT NULL
            );
            CREATE TABLE migration_log (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                item TEXT NOT NULL,
                source TEXT NOT NULL,
                target TEXT NOT NULL,
                value_json TEXT NOT NULL
            );
            """
        )
        metadata = {
            "snapshot_schema": "fusion_config.v2",
            "config_id": config_id,
            "generation": str(payload["generation"]),
            "effective_at_ms": str(effective_at_ms),
            "digest": semantic_digest(payload),
        }
        connection.executemany(
            "INSERT INTO meta(key, value) VALUES(?, ?)",
            sorted(metadata.items()),
        )
        connection.executemany(
            "INSERT INTO shards(name, path, digest) VALUES(?, ?, ?)",
            [
                (entry["name"], entry["file"], entry["digest"])
                for entry in shard_meta
            ],
        )
        connection.executemany(
            """
            INSERT INTO tombstones(tombstone_id, kind, field, reason, source_generation)
            VALUES(?, ?, ?, ?, ?)
            """,
            [
                (
                    item["tombstone_id"],
                    item["kind"],
                    item["field"],
                    item["reason"],
                    int(item["source_generation"]),
                )
                for item in tombstones
            ],
        )
        connection.executemany(
            """
            INSERT INTO migration_log(sequence, item, source, target, value_json)
            VALUES(?, ?, ?, ?, ?)
            """,
            [
                (
                    int(item["sequence"]),
                    item["item"],
                    item["source"],
                    item["target"],
                    canonical_json(item["value"]),
                )
                for item in migration_log
            ],
        )
        connection.commit()
    return manifest


def load_v2(directory: str | Path) -> ConfigSnapshot:
    root = Path(directory)
    manifest_path = root / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigurationError(
            f"cannot parse v2 manifest: {exc}", field="manifest"
        ) from exc
    if not isinstance(manifest, dict) or manifest.get("snapshot_schema") != "fusion_config.v2":
        raise ConfigurationError(
            "expected fusion_config.v2", field="snapshot_schema"
        )
    database_path = root / str(manifest.get("metadata_db"))
    if not database_path.is_file():
        raise ConfigurationError(
            "v2 metadata SQLite database is missing", field="metadata_db"
        )
    recalls: dict[str, Any] = {}
    fusion: dict[str, Any] | None = None
    for shard in manifest.get("shards", []):
        if not isinstance(shard, dict):
            raise ConfigurationError("invalid shard entry", field="shards")
        shard_path = root / str(shard.get("file"))
        try:
            value = json.loads(shard_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ConfigurationError(
                f"cannot parse shard {shard.get('name')}: {exc}",
                field="shards",
            ) from exc
        if digest_json(value) != shard.get("digest"):
            raise ConfigurationError(
                f"shard digest mismatch: {shard.get('name')}", field="shards"
            )
        name = shard.get("name")
        if name == "fusion":
            fusion = value.get("value")
        elif isinstance(name, str) and name.startswith("recall."):
            recalls[name.split(".", 1)[1]] = value.get("value")
    if set(recalls) != set(ROUTE_ORDER) or fusion is None:
        raise ConfigurationError("v2 shard set is incomplete", field="shards")
    payload = validate_config_payload(
        {
            "generation": manifest.get("generation"),
            "recalls": recalls,
            "fusion": fusion,
        }
    )
    expected = semantic_digest(payload)
    if manifest.get("digest") != expected:
        raise ConfigurationError(
            "v2 configuration digest mismatch", field="digest"
        )
    config_id = manifest.get("config_id")
    if not isinstance(config_id, str) or not config_id:
        raise ConfigurationError("config_id must be a string", field="config_id")
    effective_at_ms = _integer(
        manifest.get("effective_at_ms"),
        field="effective_at_ms",
        minimum=0,
    )
    with sqlite3.connect(database_path) as connection:
        meta = dict(connection.execute("SELECT key, value FROM meta"))
        if meta.get("digest") != expected:
            raise ConfigurationError(
                "v2 SQLite metadata digest mismatch", field="metadata_db.digest"
            )
        if int(meta.get("generation", "-1")) != payload["generation"]:
            raise ConfigurationError(
                "v2 SQLite generation mismatch", field="metadata_db.generation"
            )
        shard_rows = {
            row[0]: (row[1], row[2])
            for row in connection.execute(
                "SELECT name, path, digest FROM shards"
            )
        }
        manifest_shards = {
            item["name"]: (item["file"], item["digest"])
            for item in manifest.get("shards", [])
        }
        if shard_rows != manifest_shards:
            raise ConfigurationError(
                "v2 SQLite shard metadata mismatch",
                field="metadata_db.shards",
            )
        tombstones = tuple(
            {
                "tombstone_id": row[0],
                "kind": row[1],
                "field": row[2],
                "reason": row[3],
                "source_generation": row[4],
            }
            for row in connection.execute(
                """
                SELECT tombstone_id, kind, field, reason, source_generation
                FROM tombstones ORDER BY tombstone_id
                """
            )
        )
        migration_log = tuple(
            {
                "sequence": row[0],
                "item": row[1],
                "source": row[2],
                "target": row[3],
                "value": json.loads(row[4]),
            }
            for row in connection.execute(
                """
                SELECT sequence, item, source, target, value_json
                FROM migration_log ORDER BY sequence
                """
            )
        )
    return ConfigSnapshot(
        source_schema="fusion_config.v2",
        config_id=config_id,
        generation=payload["generation"],
        effective_seconds=None,
        effective_at_ms=effective_at_ms,
        payload=payload,
        digest=expected,
        tombstones=tombstones,
        path=str(root),
        migration_log=migration_log,
    )


def load_config(path: str | Path) -> ConfigSnapshot:
    source = Path(path)
    if source.is_dir() or source.name.endswith(".sqlite"):
        return load_v2(source if source.is_dir() else source.parent)
    return load_v1(source)


def migrate_v1_to_v2(
    source_file: str | Path,
    destination_dir: str | Path,
    *,
    migration_epoch_ms: int = DEFAULT_MIGRATION_EPOCH_MS,
) -> dict[str, Any]:
    source = load_v1(source_file)
    effective_at_ms = int(
        round((source.effective_seconds or 0.0) * 1000.0)
    ) + migration_epoch_ms
    tombstones = [
        {
            "tombstone_id": "v1.effective_seconds",
            "kind": "config_field",
            "field": "effective_seconds",
            "reason": "replaced by absolute effective_at_ms in v2",
            "source_generation": source.generation,
        }
    ]
    migration_log = [
        {
            "sequence": 1,
            "item": "effective_at_ms",
            "source": "v1.effective_seconds + migration_epoch_ms",
            "target": "manifest.effective_at_ms",
            "value": {
                "effective_seconds": source.effective_seconds,
                "migration_epoch_ms": migration_epoch_ms,
                "effective_at_ms": effective_at_ms,
            },
        },
        {
            "sequence": 2,
            "item": "generation",
            "source": "v1.generation",
            "target": "manifest.generation",
            "value": source.generation,
        },
        {
            "sequence": 3,
            "item": "tombstones",
            "source": "migration.v1_to_v2",
            "target": "metadata.sqlite.tombstones",
            "value": tombstones,
        },
        {
            "sequence": 4,
            "item": "digest",
            "source": "semantic_digest(v1.recalls, v1.fusion, v1.generation)",
            "target": "manifest.digest",
            "value": source.digest,
        },
    ]
    manifest = _write_v2(
        destination_dir,
        source.payload,
        config_id=source.config_id,
        effective_at_ms=effective_at_ms,
        tombstones=tombstones,
        migration_log=migration_log,
    )
    return {
        "direction": "v1_to_v2",
        "source": str(source_file),
        "destination": str(destination_dir),
        "config_id": source.config_id,
        "generation": source.generation,
        "digest": source.digest,
        "completed": migration_log,
        "tombstones": tombstones,
        "manifest_digest": digest_json(manifest),
    }


def migrate_v2_to_v1(
    source_dir: str | Path,
    destination_file: str | Path,
    *,
    migration_epoch_ms: int = DEFAULT_MIGRATION_EPOCH_MS,
) -> dict[str, Any]:
    source = load_v2(source_dir)
    if source.effective_at_ms is None:
        raise ConfigurationError(
            "v2 effective_at_ms is missing", field="effective_at_ms"
        )
    effective_seconds = stable_float(
        (source.effective_at_ms - migration_epoch_ms) / 1000.0
    )
    consumed = [
        {
            "item": "effective_seconds",
            "source": "v2.effective_at_ms - migration_epoch_ms",
            "target": "v1.effective_seconds",
            "value": {
                "effective_at_ms": source.effective_at_ms,
                "migration_epoch_ms": migration_epoch_ms,
                "effective_seconds": effective_seconds,
            },
        },
        {
            "item": "generation",
            "source": "v2.generation",
            "target": "v1.generation",
            "value": source.generation,
        },
    ]
    dropped = [
        {
            "tombstone_id": item["tombstone_id"],
            "field": item["field"],
            "reason": item["reason"],
            "rule": "not_representable_in_v1_explicit_drop",
        }
        for item in source.tombstones
    ]
    envelope = make_v1_snapshot(
        source.payload,
        config_id=source.config_id,
        effective_seconds=effective_seconds,
    )
    atomic_write_json(destination_file, envelope)
    return {
        "direction": "v2_to_v1",
        "source": str(source_dir),
        "destination": str(destination_file),
        "config_id": source.config_id,
        "generation": source.generation,
        "digest": source.digest,
        "completed": consumed,
        "dropped_tombstones": dropped,
        "drop_rule": "not_representable_in_v1_explicit_drop",
    }


def clone_payload(snapshot: ConfigSnapshot) -> dict[str, Any]:
    return copy.deepcopy(snapshot.payload)


def changed_leaf_paths(
    old: dict[str, Any], new: dict[str, Any], prefix: str = ""
) -> list[str]:
    changes: list[str] = []
    keys = sorted(set(old) | set(new))
    for key in keys:
        path = f"{prefix}.{key}" if prefix else key
        left = old.get(key)
        right = new.get(key)
        if isinstance(left, dict) and isinstance(right, dict):
            changes.extend(changed_leaf_paths(left, right, path))
        elif left != right:
            changes.append(path)
    return changes


def classify_config_change(
    old_payload: dict[str, Any], new_payload: dict[str, Any]
) -> dict[str, Any]:
    old_search = {
        "recalls": old_payload["recalls"],
        "fusion": old_payload["fusion"],
    }
    new_search = {
        "recalls": new_payload["recalls"],
        "fusion": new_payload["fusion"],
    }
    paths = changed_leaf_paths(old_search, new_search)
    if not paths:
        return {"kind": "none", "paths": [], "routes": []}
    weight_routes = {
        path.split(".")[1]
        for path in paths
        if path.startswith("recalls.")
        and path.endswith(".weight")
        and path.count(".") == 2
    }
    if len(weight_routes) == len({path.split(".")[1] for path in paths}) and all(
        path.startswith("recalls.")
        and path.endswith(".weight")
        and path.count(".") == 2
        for path in paths
    ):
        return {
            "kind": "route_weight",
            "paths": paths,
            "routes": sorted(weight_routes),
        }
    if all(path == "fusion.normalization" for path in paths):
        return {"kind": "normalization", "paths": paths, "routes": []}
    return {"kind": "other", "paths": paths, "routes": []}


def configs_equal_for_search(
    left: ConfigSnapshot, right: ConfigSnapshot
) -> bool:
    return semantic_digest(left.payload) == semantic_digest(right.payload)
