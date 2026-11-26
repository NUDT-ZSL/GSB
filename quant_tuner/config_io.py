from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Iterable

from .canonical import canonical_dumps, content_digest
from .errors import E_FORMAT_CORRUPT, E_NOT_FOUND, E_VERSION_UNSUPPORTED, TuningError


UNIFIED_VERSION = "unified/v1"
V1_VERSIONS = {"v1", 1, "1"}
V2_VERSIONS = {"v2", 2, "2"}


def _read_json(path: Path) -> Any:
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError as exc:
        raise TuningError(E_NOT_FOUND, f"config not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise TuningError(E_FORMAT_CORRUPT, f"invalid JSON {path}: {exc}") from exc


def _write_json_atomic(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _require(obj: dict[str, Any], fields: Iterable[str], where: str) -> None:
    missing = [field for field in fields if field not in obj or obj[field] is None]
    if missing:
        raise TuningError(
            E_FORMAT_CORRUPT,
            f"{where} missing fields: {', '.join(missing)}",
            {"missing": missing},
        )


def _check_timestamps(records: list[dict[str, Any]], where: str) -> None:
    last_ms: dict[str, int] = {}
    for index, record in enumerate(records):
        tuning_id = str(record.get("tuning_id", ""))
        absolute_ms = record.get("absolute_ms")
        if not isinstance(absolute_ms, int) or isinstance(absolute_ms, bool):
            raise TuningError(
                E_FORMAT_CORRUPT,
                f"{where}[{index}] absolute_ms must be an integer",
                {"index": index},
            )
        previous = last_ms.get(tuning_id)
        if previous is not None and absolute_ms < previous:
            raise TuningError(
                E_FORMAT_CORRUPT,
                f"{where}[{index}] timestamp moved backwards for {tuning_id}",
                {"tuning_id": tuning_id, "previous_ms": previous, "absolute_ms": absolute_ms},
            )
        last_ms[tuning_id] = absolute_ms


def _flat_schedule(record: dict[str, Any]) -> dict[str, Any]:
    schedule: dict[str, Any] = {}
    for key, value in record.items():
        if key.startswith("schedule_"):
            schedule[key[len("schedule_") :]] = value
    if "schedule_id" in record:
        schedule["schedule_id"] = record["schedule_id"]
    schedule.setdefault("schedule_id", record.get("schedule_id"))
    if not schedule.get("schedule_id"):
        raise TuningError(E_FORMAT_CORRUPT, "v1 record missing schedule_id")
    schedule.setdefault("schedule_revision", int(record.get("schedule_revision", 1)))
    return schedule


def load_v1(path: str | Path) -> dict[str, Any]:
    path = Path(path)
    raw = _read_json(path)
    if not isinstance(raw, dict):
        raise TuningError(E_FORMAT_CORRUPT, "v1 config root must be an object")
    if "version" not in raw:
        raise TuningError(E_FORMAT_CORRUPT, "config missing version")
    if raw["version"] not in V1_VERSIONS:
        if raw["version"] in V2_VERSIONS:
            raise TuningError(E_VERSION_UNSUPPORTED, "use v2 loader for this config")
        raise TuningError(E_VERSION_UNSUPPORTED, f"unsupported config version: {raw['version']}")
    _require(raw, ("origin_absolute_ms", "revisions"), "v1 config")
    if not isinstance(raw["revisions"], list):
        raise TuningError(E_FORMAT_CORRUPT, "v1 revisions must be a list")

    origin = int(raw["origin_absolute_ms"])
    unified_records: list[dict[str, Any]] = []
    relative_last: dict[str, float] = {}
    for index, item in enumerate(raw["revisions"]):
        if not isinstance(item, dict):
            raise TuningError(E_FORMAT_CORRUPT, f"v1 revision {index} must be an object")
        _require(
            item,
            (
                "record_id",
                "tuning_id",
                "effective_second",
                "generation",
                "quant_scale",
                "quant_zero_point",
                "schedule_id",
            ),
            f"v1.revisions[{index}]",
        )
        effective_second = float(item["effective_second"])
        previous = relative_last.get(str(item["tuning_id"]))
        if previous is not None and effective_second + 1e-12 < previous:
            raise TuningError(
                E_FORMAT_CORRUPT,
                "v1 effective_second moved backwards",
                {"tuning_id": item["tuning_id"]},
            )
        relative_last[str(item["tuning_id"])] = effective_second
        quant = {
            "scale": float(item["quant_scale"]),
            "zero_point": int(item["quant_zero_point"]),
        }
        if "quant_bits" in item:
            quant["bits"] = int(item["quant_bits"])
        for key in ("quant_method", "rounding"):
            if f"quant_{key}" in item:
                quant[key] = item[f"quant_{key}"]
        schedule = _flat_schedule(item)
        record = {
            "record_id": str(item["record_id"]),
            "tuning_id": str(item["tuning_id"]),
            "absolute_ms": origin + int(round(effective_second * 1000.0)),
            "generation": int(item["generation"]),
            "quant_config": quant,
            "schedule": schedule,
            "source_version": "v1",
        }
        record["digest"] = content_digest(
            {k: v for k, v in record.items() if k != "digest"}
        )
        unified_records.append(record)

    _check_timestamps(unified_records, "v1.migrated")
    return {
        "format": UNIFIED_VERSION,
        "source_version": "v1",
        "origin_absolute_ms": origin,
        "records": unified_records,
    }


def _verify_v2_record(record: dict[str, Any], where: str) -> dict[str, Any]:
    _require(
        record,
        (
            "record_id",
            "tuning_id",
            "absolute_ms",
            "generation",
            "quant_config",
            "schedule",
            "digest",
        ),
        where,
    )
    expected = record["digest"]
    body = {key: value for key, value in record.items() if key != "digest"}
    actual = content_digest(body)
    if expected != actual:
        raise TuningError(
            E_FORMAT_CORRUPT,
            f"{where} content digest mismatch",
            {"expected": expected, "actual": actual},
        )
    return record


def load_v2(path: str | Path) -> dict[str, Any]:
    root = Path(path)
    if root.is_file():
        root = root.parent
    db_path = root / "meta.sqlite3"
    shard_dir = root / "shards"
    if not db_path.exists():
        raise TuningError(E_NOT_FOUND, f"v2 SQLite metadata not found: {db_path}")
    if not shard_dir.exists():
        raise TuningError(E_NOT_FOUND, f"v2 shard directory not found: {shard_dir}")

    uri = f"file:{db_path.resolve().as_posix()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as conn:
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                """
                SELECT record_id, tuning_id, shard_path, absolute_ms,
                       generation, digest, rowid AS seq
                FROM config_meta ORDER BY rowid
                """
            ).fetchall()
        except sqlite3.DatabaseError as exc:
            raise TuningError(E_FORMAT_CORRUPT, f"cannot read v2 metadata: {exc}") from exc

    if not rows:
        raise TuningError(E_FORMAT_CORRUPT, "v2 metadata contains no config records")

    shard_cache: dict[str, dict[str, Any]] = {}
    records: list[dict[str, Any]] = []
    for row in rows:
        shard_rel = str(row["shard_path"])
        shard_path = root / shard_rel
        if shard_rel not in shard_cache:
            shard = _read_json(shard_path)
            if not isinstance(shard, dict):
                raise TuningError(E_FORMAT_CORRUPT, f"invalid v2 shard: {shard_path}")
            if "version" not in shard:
                raise TuningError(E_FORMAT_CORRUPT, f"v2 shard missing version: {shard_path}")
            if shard["version"] not in V2_VERSIONS:
                raise TuningError(
                    E_VERSION_UNSUPPORTED,
                    f"unsupported v2 shard version: {shard['version']}",
                )
            shard_cache[shard_rel] = shard
        shard = shard_cache[shard_rel]
        shard_records = shard.get("records")
        if not isinstance(shard_records, list):
            raise TuningError(E_FORMAT_CORRUPT, f"v2 shard records must be a list: {shard_path}")
        match = next(
            (item for item in shard_records if item.get("record_id") == row["record_id"]),
            None,
        )
        if match is None:
            raise TuningError(
                E_FORMAT_CORRUPT,
                f"metadata record missing from shard {shard_rel}: {row['record_id']}",
            )
        record = _verify_v2_record(match, f"v2.{row['record_id']}")
        for field in ("tuning_id", "absolute_ms", "generation", "digest"):
            if record.get(field) != row[field]:
                raise TuningError(
                    E_FORMAT_CORRUPT,
                    f"SQLite/shard mismatch for {row['record_id']}.{field}",
                    {"field": field},
                )
        unified = dict(record)
        unified["source_version"] = "v2"
        records.append(unified)

    _check_timestamps(records, "v2")
    return {
        "format": UNIFIED_VERSION,
        "source_version": "v2",
        "origin_absolute_ms": 0,
        "records": records,
    }


def load_unified(path: str | Path) -> dict[str, Any]:
    raw = _read_json(Path(path))
    if not isinstance(raw, dict) or raw.get("format") != UNIFIED_VERSION:
        raise TuningError(E_VERSION_UNSUPPORTED, "not a unified config bundle")
    if "records" not in raw or not isinstance(raw["records"], list):
        raise TuningError(E_FORMAT_CORRUPT, "unified config missing records list")
    _check_timestamps(raw["records"], "unified")
    return raw


def detect_and_load(path: str | Path) -> dict[str, Any]:
    path = Path(path)
    if path.is_dir():
        return load_v2(path)
    raw = _read_json(path)
    version = raw.get("version") if isinstance(raw, dict) else None
    if version in V1_VERSIONS:
        return load_v1(path)
    if version in V2_VERSIONS:
        return load_v2(path.parent if path.name != "meta.sqlite3" else path.parent)
    if isinstance(raw, dict) and raw.get("format") == UNIFIED_VERSION:
        return load_unified(path)
    if "version" not in raw if isinstance(raw, dict) else True:
        raise TuningError(E_FORMAT_CORRUPT, "config missing version")
    raise TuningError(E_VERSION_UNSUPPORTED, f"unsupported config version: {version}")


def to_v1(bundle: dict[str, Any], origin_absolute_ms: int | None = None) -> dict[str, Any]:
    records = bundle["records"]
    origin = int(
        origin_absolute_ms
        if origin_absolute_ms is not None
        else bundle.get("origin_absolute_ms")
        or min((record["absolute_ms"] for record in records), default=0)
    )
    revisions: list[dict[str, Any]] = []
    for record in records:
        quant = record["quant_config"]
        schedule = record["schedule"]
        flat: dict[str, Any] = {
            "record_id": record["record_id"],
            "tuning_id": record["tuning_id"],
            "effective_second": (int(record["absolute_ms"]) - origin) / 1000.0,
            "generation": record["generation"],
            "quant_scale": quant["scale"],
            "quant_zero_point": quant["zero_point"],
            "schedule_id": schedule["schedule_id"],
        }
        if "bits" in quant:
            flat["quant_bits"] = quant["bits"]
        for key, value in quant.items():
            if key not in ("scale", "zero_point", "bits"):
                flat[f"quant_{key}"] = value
        for key, value in schedule.items():
            flat[f"schedule_{key}"] = value
        revisions.append(flat)
    return {
        "version": "v1",
        "origin_absolute_ms": origin,
        "migrated_from": bundle.get("source_version", UNIFIED_VERSION),
        "revisions": revisions,
    }


def write_v1(path: str | Path, bundle: dict[str, Any], origin_ms: int | None = None) -> Path:
    path = Path(path)
    _write_json_atomic(path, to_v1(bundle, origin_ms))
    return path


def write_v2(
    output_dir: str | Path,
    bundle: dict[str, Any],
    shard_size: int = 4,
) -> Path:
    root = Path(output_dir)
    shard_dir = root / "shards"
    shard_dir.mkdir(parents=True, exist_ok=True)
    db_path = root / "meta.sqlite3"
    if db_path.exists():
        db_path.unlink()

    records = bundle["records"]
    shard_size = max(1, shard_size)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE config_meta (
                record_id TEXT PRIMARY KEY,
                tuning_id TEXT NOT NULL,
                shard_id INTEGER NOT NULL,
                shard_path TEXT NOT NULL,
                absolute_ms INTEGER NOT NULL,
                generation INTEGER NOT NULL,
                digest TEXT NOT NULL,
                quant_digest TEXT,
                schedule_digest TEXT
            )
            """
        )
        for shard_id, start in enumerate(range(0, len(records), shard_size)):
            chunk = records[start : start + shard_size]
            shard_records = []
            shard_name = f"shard-{shard_id:03d}.json"
            shard_rel = f"shards/{shard_name}"
            for source in chunk:
                record = {
                    key: value
                    for key, value in source.items()
                    if key not in ("digest", "source_version")
                }
                record["digest"] = content_digest(record)
                shard_records.append(record)
                conn.execute(
                    """
                    INSERT INTO config_meta VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        record["record_id"],
                        record["tuning_id"],
                        shard_id,
                        shard_rel,
                        int(record["absolute_ms"]),
                        int(record["generation"]),
                        record["digest"],
                        content_digest(record["quant_config"]),
                        content_digest(record["schedule"]),
                    ),
                )
            _write_json_atomic(
                shard_dir / f"shard-{shard_id:03d}.json",
                {"version": "v2", "shard_id": shard_id, "records": shard_records},
            )
        conn.commit()
    return root


def write_unified(path: str | Path, bundle: dict[str, Any]) -> Path:
    path = Path(path)
    _write_json_atomic(path, bundle)
    return path


def migrate_config(
    input_path: str | Path,
    target: str,
    output_path: str | Path,
    origin_ms: int | None = None,
) -> dict[str, Any]:
    bundle = detect_and_load(input_path)
    target = target.lower()
    if target == "unified":
        write_unified(output_path, bundle)
    elif target == "v1":
        write_v1(output_path, bundle, origin_ms)
    elif target == "v2":
        write_v2(output_path, bundle)
    else:
        raise TuningError(E_FORMAT_CORRUPT, f"unknown migration target: {target}")
    return bundle
