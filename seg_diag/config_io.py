"""Read v1 JSON and v2 sharded JSON + SQLite configuration snapshots."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Iterable

from .codes import CODES
from .errors import DiagnosticError
from .hashing import content_key
from .models import Category, InstanceMapping, UnifiedConfig


V1_NAME = "config_v1.json"
V2_DB_NAME = "metadata.sqlite3"


def _invalid(message: str, **details: Any) -> DiagnosticError:
    return DiagnosticError(CODES.CONFIG_INVALID, message, details)


def _unsupported(version: Any) -> DiagnosticError:
    return DiagnosticError(
        CODES.VERSION_UNSUPPORTED,
        f"unsupported configuration version: {version!r}",
        {"version": version},
    )


def _read_json(path: Path) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError as exc:
        raise _invalid(f"configuration file not found: {path}", path=str(path)) from exc
    except json.JSONDecodeError as exc:
        raise _invalid(f"invalid JSON configuration: {path}: {exc}", path=str(path)) from exc
    if not isinstance(data, dict):
        raise _invalid("configuration root must be a JSON object", path=str(path))
    return data


def _version(data: dict[str, Any]) -> str:
    value = data.get("version", data.get("schema_version", data.get("config_version")))
    if value is None:
        raise _invalid("missing configuration version", fields=["version"])
    text = str(value).lower()
    if text in {"1", "v1"}:
        return "v1"
    if text in {"2", "v2"}:
        return "v2"
    raise _unsupported(value)


def _parse_categories(value: Any) -> tuple[Category, ...]:
    if not isinstance(value, list) or not value:
        raise _invalid("categories must be a non-empty list", field="categories")
    categories: list[Category] = []
    seen: set[int] = set()
    for index, item in enumerate(value):
        if isinstance(item, int) and not isinstance(item, bool):
            category_id = int(item)
            name = f"category_{category_id}"
        elif isinstance(item, dict):
            raw_id = item.get("id", item.get("category_id"))
            if not isinstance(raw_id, int) or isinstance(raw_id, bool):
                raise _invalid("category id must be an integer", index=index, category=item)
            category_id = int(raw_id)
            name = str(item.get("name", f"category_{category_id}"))
        else:
            raise _invalid("illegal category entry", index=index, category=item)
        if category_id < 0:
            raise _invalid("category id must be non-negative", category_id=category_id)
        if category_id in seen:
            raise _invalid("duplicate category id", category_id=category_id)
        seen.add(category_id)
        categories.append(Category(id=category_id, name=name))
    return tuple(sorted(categories, key=lambda item: item.id))


def _require_number(data: dict[str, Any], *names: str) -> float:
    for name in names:
        if name in data and isinstance(data[name], (int, float)) and not isinstance(data[name], bool):
            return float(data[name])
    raise _invalid(f"missing numeric field: one of {names}", fields=list(names))


def _parse_v1_mappings(
    data: dict[str, Any],
    categories: tuple[Category, ...],
    effective_seconds: float,
    time_origin_ms: int = 0,
) -> tuple[InstanceMapping, ...]:
    category_ids = {item.id for item in categories}
    raw_mappings = data.get("mappings", data.get("instance_to_category", data.get("records", [])))
    mappings: list[InstanceMapping] = []

    if isinstance(raw_mappings, dict):
        iterable: Iterable[Any] = [
            {"instance_id": key, "category_id": value}
            for key, value in raw_mappings.items()
        ]
    else:
        iterable = raw_mappings

    seen: set[int] = set()
    for item in iterable:
        if not isinstance(item, dict):
            raise _invalid("v1 mapping/record must be an object", record=item)
        if item.get("record_type") == "category":
            continue
        instance_raw = item.get("instance_id", item.get("instance"))
        category_raw = item.get("category_id", item.get("category"))
        if instance_raw is None or category_raw is None:
            raise _invalid("v1 record lacks instance_id or category_id", record=item)
        try:
            instance_id = int(instance_raw)
            category_id = int(category_raw)
        except (TypeError, ValueError) as exc:
            raise _invalid("instance/category ids must be integers", record=item) from exc
        if instance_id < 0 or category_id not in category_ids:
            raise _invalid(
                "v1 mapping references illegal category or instance",
                instance_id=instance_id,
                category_id=category_id,
            )
        if instance_id in seen:
            raise _invalid("duplicate v1 instance mapping", instance_id=instance_id)
        seen.add(instance_id)
        seconds = float(item.get("effective_after_seconds", effective_seconds))
        mappings.append(
            InstanceMapping(
                instance_id=instance_id,
                category_id=category_id,
                confidence_threshold=1.0,
                effective_at_ms=time_origin_ms + int(round(seconds * 1000.0)),
                confidence_scope="implicit_one",
            )
        )

    if not mappings:
        # Deterministic fallback for the compact v1 convention:
        # instance_id = category_id * 100 + serial. Instance 0 is background.
        for category in categories:
            if category.id == 0:
                mappings.append(
                    InstanceMapping(
                        0,
                        0,
                        1.0,
                        time_origin_ms + int(effective_seconds * 1000),
                        "implicit_one",
                    )
                )
            else:
                mappings.append(
                    InstanceMapping(
                        category.id * 100 + 1,
                        category.id,
                        1.0,
                        time_origin_ms + int(effective_seconds * 1000),
                        "implicit_one",
                    )
                )
    return tuple(sorted(mappings, key=lambda item: item.instance_id))


def read_v1_config(path: str | Path) -> UnifiedConfig:
    path = Path(path)
    data = _read_json(path)
    if _version(data) != "v1":
        raise _unsupported(data.get("version"))
    effective_seconds = _require_number(
        data, "relative_effective_seconds", "effective_after_seconds", "effective_seconds"
    )
    if effective_seconds < 0:
        raise _invalid("relative_effective_seconds must be non-negative")
    categories = _parse_categories(data.get("categories"))
    time_origin_ms = int(data.get("time_origin_ms", 0))
    mappings = _parse_v1_mappings(
        data, categories, effective_seconds, time_origin_ms=time_origin_ms
    )
    raw_mapping_value = data.get(
        "mappings", data.get("instance_to_category", data.get("records"))
    )
    if isinstance(raw_mapping_value, dict):
        has_explicit_mappings = len(raw_mapping_value) > 0
    elif isinstance(raw_mapping_value, list):
        has_explicit_mappings = any(
            isinstance(item, dict) and ("instance_id" in item or "instance" in item)
            for item in raw_mapping_value
        )
    else:
        has_explicit_mappings = False
    config = UnifiedConfig(
        source_version=1,
        revision=int(data.get("revision", 1)),
        categories=categories,
        mappings=mappings,
        default_confidence_threshold=1.0,
        effective_at_ms=time_origin_ms + int(round(effective_seconds * 1000.0)),
        relative_effective_seconds=effective_seconds,
        time_origin_ms=time_origin_ms,
        downgrade_policy=str(data.get("downgrade_policy", "discard")),
        mapping_source="explicit" if has_explicit_mappings else "instance_id_convention",
        raw_digest=content_key(data),
    )
    config.checksum = content_key(config.canonical_payload())
    return config


def _load_v2_shards(shard_paths: list[Path]) -> tuple[list[Any], list[dict[str, Any]]]:
    categories: list[Any] | None = None
    shard_mappings: list[dict[str, Any]] = []
    for path in sorted(shard_paths):
        data = _read_json(path)
        if _version(data) != "v2":
            raise _unsupported(data.get("version"))
        if "categories" in data:
            categories = data["categories"] if categories is None else categories + data["categories"]
        shard_mappings.extend(data.get("mappings", []))
    if categories is None:
        raise _invalid("v2 shards do not contain categories")
    return categories, shard_mappings


def _connect_sqlite(db_path: Path) -> sqlite3.Connection:
    if not db_path.exists():
        raise _invalid("v2 SQLite metadata database not found", path=str(db_path))
    try:
        connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        return connection
    except sqlite3.Error as exc:
        raise _invalid(f"cannot open SQLite metadata: {exc}", path=str(db_path)) from exc


def read_v2_config(path: str | Path) -> UnifiedConfig:
    root = Path(path)
    if root.is_file():
        db_path = root
        shard_paths = sorted(root.parent.glob("*.shard.json"))
    else:
        db_path = root / V2_DB_NAME
        shard_paths = sorted([*root.glob("*.shard.json"), *root.glob("shard_*.json")])
    if not shard_paths:
        raise _invalid("no v2 JSON shards found", root=str(root))

    categories_raw, shard_mappings = _load_v2_shards(shard_paths)
    categories = _parse_categories(categories_raw)
    category_ids = categories.category_ids() if hasattr(categories, "category_ids") else {c.id for c in categories}

    with _connect_sqlite(db_path) as connection:
        try:
            meta_rows = {
                str(row["key"]): row["value"]
                for row in connection.execute("SELECT key, value FROM config_meta")
            }
            db_mappings = [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT instance_id, category_id, confidence_threshold,
                           effective_at_ms, confidence_scope
                    FROM category_instance_map
                    ORDER BY instance_id
                    """
                )
            ]
        except sqlite3.Error as exc:
            raise _invalid(f"SQLite metadata schema is invalid: {exc}") from exc

    if not db_mappings:
        raise _invalid("SQLite category_instance_map is empty")
    try:
        default_threshold = float(meta_rows["default_confidence_threshold"])
        effective_at_ms = int(meta_rows["effective_at_ms"])
        revision = int(meta_rows.get("revision", "1"))
    except (KeyError, TypeError, ValueError) as exc:
        raise _invalid(
            "SQLite config_meta lacks default_confidence_threshold/effective_at_ms/revision"
        ) from exc

    mappings: list[InstanceMapping] = []
    seen: set[int] = set()
    for row in db_mappings:
        try:
            instance_id = int(row["instance_id"])
            category_id = int(row["category_id"])
            threshold = float(row["confidence_threshold"])
            timestamp = int(row["effective_at_ms"])
        except (KeyError, TypeError, ValueError) as exc:
            raise _invalid("illegal category_instance_map row", row=row) from exc
        if instance_id < 0 or category_id not in category_ids:
            raise _invalid(
                "v2 mapping references illegal category/instance",
                instance_id=instance_id,
                category_id=category_id,
            )
        if not 0.0 <= threshold <= 1.0:
            raise _invalid("confidence_threshold must be in [0,1]", instance_id=instance_id)
        if instance_id in seen:
            raise _invalid("duplicate v2 instance mapping", instance_id=instance_id)
        seen.add(instance_id)
        mappings.append(
            InstanceMapping(
                instance_id=instance_id,
                category_id=category_id,
                confidence_threshold=threshold,
                effective_at_ms=timestamp,
                confidence_scope=str(row.get("confidence_scope") or "explicit"),
            )
        )

    if shard_mappings:
        shard_keys = content_key(
            sorted(
                [(int(m["instance_id"]), int(m["category_id"])) for m in shard_mappings],
                key=lambda x: x[0],
            )
        )
        db_keys = content_key([(m.instance_id, m.category_id) for m in mappings])
        if shard_keys != db_keys:
            raise _invalid(
                "v2 shard mappings do not match SQLite category_instance_map",
                shard_checksum=shard_keys,
                db_checksum=db_keys,
            )

    config = UnifiedConfig(
        source_version=2,
        revision=revision,
        categories=categories,
        mappings=tuple(sorted(mappings, key=lambda item: item.instance_id)),
        default_confidence_threshold=default_threshold,
        effective_at_ms=effective_at_ms,
        relative_effective_seconds=(effective_at_ms - int(meta_rows.get("time_origin_ms", 0))) / 1000.0
        if "time_origin_ms" in meta_rows
        else None,
        time_origin_ms=int(meta_rows.get("time_origin_ms", 0)),
        downgrade_policy=str(meta_rows.get("downgrade_policy", "discard")),
        raw_digest=content_key({"shards": shard_paths, "meta": meta_rows, "mappings": db_mappings}),
    )
    config.checksum = content_key(config.canonical_payload())
    return config


def read_any_config(path: str | Path) -> UnifiedConfig:
    """Read either a v1 JSON file or a v2 snapshot directory."""
    path = Path(path)
    if path.is_dir():
        return read_v2_config(path)
    data = _read_json(path)
    version = _version(data)
    if version == "v1":
        return read_v1_config(path)
    sibling_db = path.parent / V2_DB_NAME
    if sibling_db.exists():
        return read_v2_config(path.parent)
    raise _unsupported(data.get("version"))
