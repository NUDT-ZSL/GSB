"""Emotion annotation ledger readers and deterministic writers."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Dict, Iterable, List, Sequence

from .canonical import atomic_write_json, read_json, sha256_hex
from .config import EngineConfig
from .errors import DiagnosticError, ErrorCode


V1_FORMATS = {"emotion_ledger_v1", "emotion_annotation_v1", "v1"}
V2_FORMATS = {"emotion_ledger_v2", "emotion_annotation_v2", "v2"}
SUPPORTED_V1_VERSIONS = {"1", "1.0", 1, 1.0}
SUPPORTED_V2_VERSIONS = {"2", "2.0", 2, 2.0}


def record_checksum(record: Dict[str, Any]) -> str:
    payload = {
        key: value
        for key, value in record.items()
        if key not in {"checksum", "source_shard"}
    }
    return sha256_hex(payload)


def _missing(field: str, location: str) -> DiagnosticError:
    return DiagnosticError(
        ErrorCode.FORMAT_CORRUPT,
        "required ledger field is missing",
        {"field": field, "location": location},
    )


def _parse_number(value: Any, field: str, location: str) -> float:
    if isinstance(value, bool):
        raise DiagnosticError(
            ErrorCode.PARSE_ERROR,
            "numeric field is not parseable",
            {"field": field, "value": value, "location": location},
        )
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError as exc:
            raise DiagnosticError(
                ErrorCode.PARSE_ERROR,
                "numeric field is not parseable",
                {"field": field, "value": value, "location": location},
            ) from exc
    raise DiagnosticError(
        ErrorCode.PARSE_ERROR,
        "numeric field is not parseable",
        {"field": field, "value": repr(value), "location": location},
    )


def _first_key(record: Dict[str, Any], keys: Sequence[str]) -> Any:
    for key in keys:
        if key in record and record[key] is not None:
            return record[key]
    return None


def _check_version(header: Dict[str, Any] | None, path: Path) -> str:
    header = header or {}
    format_name = str(header.get("format", header.get("format_header", "")))
    version = header.get("version", header.get("schema_version"))
    text = f"{format_name} {version}"
    if "v9" in text or str(version).split(".")[0] not in {"1", "2", "None"}:
        if version is not None and str(version).split(".")[0] not in {"1", "2"}:
            raise DiagnosticError(
                ErrorCode.VERSION_UNSUPPORTED,
                "annotation ledger version is not supported",
                {"path": str(path), "format": format_name, "version": version},
            )
    if format_name in V1_FORMATS or version in SUPPORTED_V1_VERSIONS:
        return "v1"
    if format_name in V2_FORMATS or version in SUPPORTED_V2_VERSIONS:
        return "v2"
    if format_name.endswith("_v1"):
        return "v1"
    if format_name.endswith("_v2"):
        return "v2"
    if "v1" in format_name:
        return "v1"
    if "v2" in format_name:
        return "v2"
    if version is None and isinstance(header.get("records"), list):
        first = header["records"][0] if header["records"] else {}
        if isinstance(first, dict) and ("time_s" in first or "relative_seconds" in first):
            return "v1"
    raise DiagnosticError(
        ErrorCode.VERSION_UNSUPPORTED,
        "could not dispatch annotation ledger format",
        {"path": str(path), "format": format_name, "version": version},
    )


def read_v1(path: str | Path) -> Dict[str, Any]:
    path = Path(path)
    try:
        payload = read_json(path)
    except json.JSONDecodeError as exc:
        raise DiagnosticError(
            ErrorCode.PARSE_ERROR, "v1 ledger JSON is not parseable", {"path": str(path)}
        ) from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("records"), list):
        raise DiagnosticError(
            ErrorCode.FORMAT_CORRUPT,
            "v1 ledger must contain a records array",
            {"path": str(path)},
        )
    version = _check_version(payload, path)
    if version != "v1":
        raise DiagnosticError(
            ErrorCode.VERSION_UNSUPPORTED, "not a v1 ledger", {"path": str(path)}
        )

    normalized: List[Dict[str, Any]] = []
    previous_time: float | None = None
    for index, raw in enumerate(payload["records"]):
        location = f"records[{index}]"
        if not isinstance(raw, dict):
            raise _missing("object", location)
        time_value = _first_key(raw, ["time_s", "relative_seconds", "time_sec", "time"])
        emotion = _first_key(raw, ["emotion_tier", "emotion", "label"])
        if time_value is None:
            raise _missing("time_s", location)
        if emotion is None:
            raise _missing("emotion_tier", location)
        time_s = _parse_number(time_value, "time_s", location)
        if time_s < 0:
            raise DiagnosticError(
                ErrorCode.FORMAT_CORRUPT,
                "relative timestamp cannot be negative",
                {"location": location, "time_s": time_s},
            )
        if previous_time is not None and time_s + 1e-12 < previous_time:
            raise DiagnosticError(
                ErrorCode.FORMAT_CORRUPT,
                "relative timestamp regressed",
                {"location": location, "time_s": time_s, "previous_time_s": previous_time},
            )
        previous_time = time_s
        normalized.append(
            {
                "source_index": index,
                "time_s": time_s,
                "emotion_tier": str(emotion),
                "speaker_id": raw.get("speaker_id"),
                "generation": raw.get("generation"),
                "raw": raw,
            }
        )
    return {
        "version": "v1",
        "path": str(path),
        "format": payload.get("format", "emotion_ledger_v1"),
        "dialogue_id": payload.get("dialogue_id"),
        "metadata": {k: v for k, v in payload.items() if k != "records"},
        "records": normalized,
    }


def _open_metadata_db(path: Path) -> sqlite3.Connection:
    if not path.exists():
        raise DiagnosticError(
            ErrorCode.FORMAT_CORRUPT, "v2 SQLite metadata database is missing", {"path": str(path)}
        )
    try:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        connection.execute("SELECT 1 FROM records LIMIT 1").fetchall()
        return connection
    except sqlite3.DatabaseError as exc:
        raise DiagnosticError(
            ErrorCode.FORMAT_CORRUPT,
            "v2 metadata database is corrupt",
            {"path": str(path)},
        ) from exc


def read_v2(path: str | Path) -> Dict[str, Any]:
    root = Path(path)
    if root.is_file():
        if root.suffix == ".json":
            return _read_v2_from_shard(root)
        raise DiagnosticError(ErrorCode.FORMAT_CORRUPT, "unsupported v2 path", {"path": str(root)})
    manifest_path = root / "manifest.json"
    if not manifest_path.exists():
        raise DiagnosticError(
            ErrorCode.FORMAT_CORRUPT, "v2 manifest is missing", {"path": str(manifest_path)}
        )
    try:
        manifest = read_json(manifest_path)
    except json.JSONDecodeError as exc:
        raise DiagnosticError(ErrorCode.PARSE_ERROR, "v2 manifest is not parseable") from exc
    version = _check_version(manifest, manifest_path)
    if version != "v2":
        raise DiagnosticError(
            ErrorCode.VERSION_UNSUPPORTED, "not a v2 ledger", {"path": str(root)}
        )

    db_path = root / str(manifest.get("database", "metadata.sqlite3"))
    connection = _open_metadata_db(db_path)
    try:
        db_rows = [dict(row) for row in connection.execute(
            "SELECT * FROM records ORDER BY rowid ASC"
        ).fetchall()]
    except sqlite3.DatabaseError as exc:
        connection.close()
        raise DiagnosticError(ErrorCode.FORMAT_CORRUPT, "cannot query v2 records") from exc
    finally:
        connection.close()

    shard_records: List[Dict[str, Any]] = []
    shard_entries = manifest.get("shards")
    if not isinstance(shard_entries, list) or not shard_entries:
        shard_dir = root / str(manifest.get("shard_dir", "shards"))
        shard_paths = sorted(shard_dir.glob("shard-*.json"))
    else:
        shard_paths = [root / str(item["path"]) if isinstance(item, dict) else root / str(item) for item in shard_entries]

    for shard_path in shard_paths:
        result = _read_v2_from_shard(shard_path, expected_root=root)
        shard_records.extend(result["records"])

    if len(db_rows) != len(shard_records):
        raise DiagnosticError(
            ErrorCode.CHECKSUM_MISMATCH,
            "v2 SQLite and shard record counts differ",
            {"sqlite_count": len(db_rows), "shard_count": len(shard_records)},
        )

    normalized: List[Dict[str, Any]] = []
    previous_abs_ms: int | float | None = None
    for index, raw in enumerate(shard_records):
        location = f"shard_records[{index}]"
        if _first_key(
            raw, ["abs_ms", "abs_start_ms", "absolute_ms", "timestamp_ms"]
        ) is None:
            raise _missing("abs_ms", location)
        for field in ["emotion_tier", "generation", "checksum", "speaker_id"]:
            if _first_key(raw, [field]) is None:
                raise _missing(field, location)
        abs_value = _first_key(raw, ["abs_ms", "abs_start_ms", "absolute_ms", "timestamp_ms"])
        abs_ms = _parse_number(abs_value, "abs_ms", location)
        if abs_ms < 0:
            raise DiagnosticError(
                ErrorCode.FORMAT_CORRUPT, "absolute millisecond timestamp is negative", {"location": location}
            )
        if previous_abs_ms is not None and abs_ms + 1e-9 < previous_abs_ms:
            raise DiagnosticError(
                ErrorCode.FORMAT_CORRUPT,
                "absolute millisecond timestamp regressed",
                {
                    "location": location,
                    "abs_ms": abs_ms,
                    "previous_abs_ms": previous_abs_ms,
                    "record_id": raw.get("record_id"),
                },
            )
        previous_abs_ms = abs_ms
        provided_checksum = str(raw.get("checksum", ""))
        actual_checksum = record_checksum(raw)
        if provided_checksum != actual_checksum:
            raise DiagnosticError(
                ErrorCode.CHECKSUM_MISMATCH,
                "v2 record content checksum mismatch",
                {
                    "location": location,
                    "record_id": raw.get("record_id"),
                    "expected": actual_checksum,
                    "provided": provided_checksum,
                },
            )
        db_row = db_rows[index]
        if str(db_row.get("checksum")) != provided_checksum:
            raise DiagnosticError(
                ErrorCode.CHECKSUM_MISMATCH,
                "SQLite checksum does not match shard checksum",
                {"record_id": raw.get("record_id")},
            )
        generation = int(_parse_number(raw.get("generation"), "generation", location))
        revision_raw = raw.get("revision", 0)
        revision = int(_parse_number(revision_raw, "revision", location))
        normalized.append(
            {
                "record_id": raw.get("record_id"),
                "frame_index": raw.get("frame_index"),
                "frame_start": int(_parse_number(raw.get("frame_start", raw.get("frame_index", index)), "frame_start", location)),
                "frame_end": raw.get("frame_end"),
                "abs_ms": abs_ms,
                "abs_start_ms": abs_ms,
                "abs_end_ms": raw.get("abs_end_ms"),
                "speaker_id": int(_parse_number(raw.get("speaker_id", 0), "speaker_id", location)),
                "emotion_tier": str(raw["emotion_tier"]),
                "generation": generation,
                "revision": revision,
                "checksum": provided_checksum,
                "source_shard": raw.get("source_shard"),
                "raw": raw,
            }
        )
    return {
        "version": "v2",
        "path": str(root),
        "format": manifest.get("format", "emotion_ledger_v2"),
        "dialogue_id": manifest.get("dialogue_id"),
        "metadata": manifest,
        "records": normalized,
        "sqlite_rows": db_rows,
    }


def _read_v2_from_shard(path: Path, expected_root: Path | None = None) -> Dict[str, Any]:
    try:
        payload = read_json(path)
    except json.JSONDecodeError as exc:
        raise DiagnosticError(
            ErrorCode.PARSE_ERROR, "v2 shard JSON is not parseable", {"path": str(path)}
        ) from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("records"), list):
        raise DiagnosticError(
            ErrorCode.FORMAT_CORRUPT, "v2 shard must contain records array", {"path": str(path)}
        )
    version = _check_version(payload, path)
    if version != "v2":
        raise DiagnosticError(ErrorCode.VERSION_UNSUPPORTED, "shard is not v2", {"path": str(path)})
    _validate_v2_shard_records(payload["records"], path)
    records = []
    for raw in payload["records"]:
        if not isinstance(raw, dict):
            raise DiagnosticError(ErrorCode.FORMAT_CORRUPT, "shard record must be an object")
        raw = dict(raw)
        raw["source_shard"] = path.name
        records.append(raw)
    return {"version": "v2", "records": records, "path": str(path)}


def _validate_v2_shard_records(records: Sequence[Dict[str, Any]], path: Path) -> None:
    previous_abs_ms: float | None = None
    for index, raw in enumerate(records):
        location = f"{path}:records[{index}]"
        if not isinstance(raw, dict):
            raise DiagnosticError(ErrorCode.FORMAT_CORRUPT, "shard record must be an object")
        if _first_key(raw, ["abs_ms", "abs_start_ms", "absolute_ms", "timestamp_ms"]) is None:
            raise _missing("abs_ms", location)
        abs_ms = _parse_number(
            _first_key(raw, ["abs_ms", "abs_start_ms", "absolute_ms", "timestamp_ms"]),
            "abs_ms",
            location,
        )
        if abs_ms < 0:
            raise DiagnosticError(
                ErrorCode.FORMAT_CORRUPT,
                "absolute millisecond timestamp is negative",
                {"location": location},
            )
        if previous_abs_ms is not None and abs_ms + 1e-9 < previous_abs_ms:
            raise DiagnosticError(
                ErrorCode.FORMAT_CORRUPT,
                "absolute millisecond timestamp regressed",
                {"location": location, "abs_ms": abs_ms, "previous_abs_ms": previous_abs_ms},
            )
        previous_abs_ms = abs_ms
        for field in ["emotion_tier", "generation", "speaker_id", "checksum"]:
            if _first_key(raw, [field]) is None:
                raise _missing(field, location)
        _parse_number(raw["generation"], "generation", location)
        _parse_number(raw["speaker_id"], "speaker_id", location)
        provided = str(raw["checksum"])
        actual = record_checksum(raw)
        if provided != actual:
            raise DiagnosticError(
                ErrorCode.CHECKSUM_MISMATCH,
                "v2 shard record content checksum mismatch",
                {"location": location, "expected": actual, "provided": provided},
            )


def read_ledger(path: str | Path) -> Dict[str, Any]:
    path = Path(path)
    if path.is_dir():
        return read_v2(path)
    payload = read_json_safe(path)
    version = _check_version(payload if isinstance(payload, dict) else {}, path)
    if version == "v1":
        return read_v1(path)
    sibling_v2 = path.parent / "emotion_annotations_v2"
    if sibling_v2.exists():
        return read_v2(sibling_v2)
    return read_v2(path)


def read_json_safe(path: Path) -> Any:
    try:
        return read_json(path)
    except json.JSONDecodeError as exc:
        raise DiagnosticError(
            ErrorCode.PARSE_ERROR, "ledger JSON is not parseable", {"path": str(path)}
        ) from exc


def write_v1_ledger(
    path: str | Path,
    truth: Sequence[Dict[str, Any]],
    labels: Sequence[str],
    config: EngineConfig,
) -> Dict[str, Any]:
    records = [
        {
            "time_s": round(int(row["frame_index"]) * int(config["hop_ms"]) / 1000.0, 3),
            "emotion_tier": labels[index],
        }
        for index, row in enumerate(truth)
    ]
    payload = {
        "format": "emotion_ledger_v1",
        "version": "1.0",
        "dialogue_id": config["dialogue_id"],
        "time_unit": "seconds",
        "seed": int(config["seed"]),
        "record_count": len(records),
        "records": records,
    }
    checksum = atomic_write_json(path, payload)
    return {"path": str(path), "record_count": len(records), "file_sha256": checksum}


def write_v2_ledger(
    output_dir: str | Path,
    truth: Sequence[Dict[str, Any]],
    labels: Sequence[str],
    config: EngineConfig,
    *,
    generation: int = 1,
    revision: int = 0,
    abs_ms_override: Sequence[int] | None = None,
    generation_values: Sequence[int] | None = None,
    revision_values: Sequence[int] | None = None,
) -> Dict[str, Any]:
    root = Path(output_dir)
    shard_dir = root / "shards"
    shard_dir.mkdir(parents=True, exist_ok=True)
    shard_size = int(config["v2_shard_size"])
    dialogue_id = str(config["dialogue_id"])
    hop_ms = int(config["hop_ms"])

    records: List[Dict[str, Any]] = []
    for index, row in enumerate(truth):
        frame_index = int(row["frame_index"])
        abs_ms = (
            int(abs_ms_override[index])
            if abs_ms_override is not None
            else frame_index * hop_ms
        )
        record_generation = (
            int(generation_values[index])
            if generation_values is not None
            else int(generation)
        )
        record_revision = (
            int(revision_values[index])
            if revision_values is not None
            else int(revision)
        )
        record = {
            "record_id": (
                f"{dialogue_id}-F{frame_index:05d}"
                f"-G{record_generation}-R{record_revision}"
            ),
            "frame_index": frame_index,
            "frame_start": frame_index,
            "frame_end": frame_index + 1,
            "abs_ms": abs_ms,
            "abs_start_ms": abs_ms,
            "abs_end_ms": abs_ms + hop_ms,
            "frame_window_end_ms": abs_ms + int(config["frame_ms"]),
            "speaker_id": int(row.get("speaker_id", 0)),
            "emotion_tier": labels[index],
            "generation": record_generation,
            "revision": record_revision,
        }
        record["checksum"] = record_checksum(record)
        records.append(record)

    shard_manifests = []
    for shard_index, start in enumerate(range(0, len(records), shard_size)):
        shard_records = records[start : start + shard_size]
        shard_path = shard_dir / f"shard-{shard_index:03d}.json"
        shard_payload = {
            "format": "emotion_ledger_v2_shard",
            "version": "2.0",
            "shard_index": shard_index,
            "record_count": len(shard_records),
            "records": shard_records,
        }
        shard_hash = atomic_write_json(shard_path, shard_payload)
        shard_manifests.append(
            {
                "path": str(Path("shards") / shard_path.name),
                "shard_index": shard_index,
                "record_count": len(shard_records),
                "sha256": shard_hash,
            }
        )

    db_path = root / "metadata.sqlite3"
    tmp_db = root / ".metadata.sqlite3.tmp"
    if tmp_db.exists():
        tmp_db.unlink()
    connection = sqlite3.connect(str(tmp_db))
    try:
        connection.execute(
            """
            CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)
            """
        )
        connection.execute(
            """
            CREATE TABLE records (
                record_id TEXT PRIMARY KEY,
                frame_index INTEGER,
                frame_start INTEGER,
                frame_end INTEGER,
                abs_ms INTEGER,
                abs_start_ms INTEGER,
                abs_end_ms INTEGER,
                speaker_id INTEGER,
                emotion_tier TEXT,
                generation INTEGER,
                revision INTEGER,
                shard_path TEXT,
                checksum TEXT NOT NULL
            )
            """
        )
        for record in records:
            connection.execute(
                """
                INSERT INTO records VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    record["record_id"],
                    record["frame_index"],
                    record["frame_start"],
                    record["frame_end"],
                    record["abs_ms"],
                    record["abs_start_ms"],
                    record["abs_end_ms"],
                    record["speaker_id"],
                    record["emotion_tier"],
                    record["generation"],
                    record["revision"],
                    f"shards/shard-{record['frame_index'] // shard_size:03d}.json",
                    record["checksum"],
                ),
            )
        meta_rows = {
            "format": "emotion_ledger_v2",
            "version": "2.0",
            "dialogue_id": dialogue_id,
            "generation": str(generation),
            "revision": str(revision),
            "record_count": str(len(records)),
        }
        connection.executemany("INSERT INTO meta VALUES (?,?)", list(meta_rows.items()))
        connection.commit()
    finally:
        connection.close()
    tmp_db.replace(db_path)

    manifest = {
        "format": "emotion_ledger_v2",
        "version": "2.0",
        "dialogue_id": dialogue_id,
        "database": db_path.name,
        "shard_dir": "shards",
        "generation": generation,
        "revision": revision,
        "record_count": len(records),
        "shards": shard_manifests,
    }
    manifest_hash = atomic_write_json(root / "manifest.json", manifest)
    return {
        "path": str(root),
        "record_count": len(records),
        "manifest_sha256": manifest_hash,
        "records": records,
    }


def iter_ledger_records(ledger: Dict[str, Any]) -> Iterable[Dict[str, Any]]:
    yield from ledger.get("records", [])
