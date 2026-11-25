"""Reader and writer for sharded JSON plus SQLite metadata v2 transcripts."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Iterable

from .codes import Code, Diagnostic
from .hashing import digest_json
from .models import Utterance
from .results import LoadResult
from .validation import V2_REQUIRED_FIELDS, build_validated_utterances


V2_VERSION = "2.0"
MANIFEST_NAME = "manifest.json"
DB_NAME = "metadata.sqlite3"


def write_v2_dataset(
    utterances: Iterable[Utterance],
    directory: str | Path,
    *,
    shard_size: int = 2,
    pcm_sha256: str | None = None,
    extra_manifest: dict[str, Any] | None = None,
) -> Path:
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    for old_shard in directory.glob("shard-*.json"):
        old_shard.unlink()
    db_path = directory / DB_NAME
    if db_path.exists():
        db_path.unlink()

    items = sorted(utterances, key=lambda item: (item.start_ms, item.utterance_id))
    shard_files: list[str] = []
    for shard_index, start in enumerate(range(0, len(items), shard_size)):
        shard_id = f"shard-{shard_index:03d}.json"
        payload = {
            "format": "transcript-v2-shard",
            "format_version": V2_VERSION,
            "shard_id": shard_index,
            "utterances": [item.to_dict() for item in items[start : start + shard_size]],
        }
        (directory / shard_id).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        shard_files.append(shard_id)

    connection = sqlite3.connect(db_path)
    try:
        connection.execute("CREATE TABLE schema_meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        connection.executemany(
            "INSERT INTO schema_meta(key, value) VALUES (?, ?)",
            [
                ("format", "transcript-v2"),
                ("format_version", V2_VERSION),
                ("absolute_time_unit", "ms"),
            ],
        )
        connection.execute(
            """
            CREATE TABLE utterance_meta (
                utterance_id TEXT PRIMARY KEY,
                start_ms INTEGER NOT NULL,
                end_ms INTEGER NOT NULL,
                speaker TEXT,
                confidence REAL NOT NULL,
                revision INTEGER NOT NULL,
                shard_file TEXT NOT NULL,
                checksum TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE word_timing (
                utterance_id TEXT NOT NULL,
                ordinal INTEGER NOT NULL,
                text TEXT NOT NULL,
                start_ms INTEGER NOT NULL,
                end_ms INTEGER NOT NULL,
                confidence REAL NOT NULL,
                PRIMARY KEY (utterance_id, ordinal)
            )
            """
        )
        for item_index, item in enumerate(items):
            shard_index = item_index // shard_size if shard_size > 0 else 0
            shard_file = f"shard-{shard_index:03d}.json"
            connection.execute(
                "INSERT INTO utterance_meta VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    item.utterance_id,
                    item.start_ms,
                    item.end_ms,
                    item.speaker,
                    item.confidence,
                    item.revision,
                    shard_file,
                    digest_json(item.to_dict()),
                ),
            )
            for ordinal, word in enumerate(item.words):
                connection.execute(
                    "INSERT INTO word_timing VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        item.utterance_id,
                        ordinal,
                        word.text,
                        word.start_ms,
                        word.end_ms,
                        word.confidence,
                    ),
                )
        connection.commit()
    finally:
        connection.close()

    manifest: dict[str, Any] = {
        "format": "transcript-v2",
        "format_version": V2_VERSION,
        "absolute_time": True,
        "time_unit": "ms",
        "shard_files": shard_files,
        "metadata_db": DB_NAME,
        "utterance_count": len(items),
        "pcm_sha256": pcm_sha256,
    }
    if extra_manifest:
        manifest.update(extra_manifest)
    (directory / MANIFEST_NAME).write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return directory / MANIFEST_NAME


def _load_json(path: Path) -> tuple[Any | None, Diagnostic | None]:
    try:
        return json.loads(path.read_text(encoding="utf-8")), None
    except (OSError, json.JSONDecodeError) as exc:
        return None, Diagnostic(Code.E_VALIDATION, f"invalid v2 JSON: {exc}", source=str(path))


def _read_sqlite_metadata(
    db_path: Path,
) -> tuple[dict[str, dict[str, Any]], list[Diagnostic]]:
    diagnostics: list[Diagnostic] = []
    metadata: dict[str, dict[str, Any]] = {}
    if not db_path.exists():
        diagnostics.append(
            Diagnostic(
                Code.E_NOT_FOUND,
                "v2 SQLite metadata database is missing",
                source=str(db_path),
            )
        )
        return metadata, diagnostics

    try:
        connection = sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
    except sqlite3.Error as exc:
        diagnostics.append(Diagnostic(Code.E_VALIDATION, f"cannot open SQLite metadata: {exc}", source=str(db_path)))
        return metadata, diagnostics

    try:
        try:
            meta_rows = connection.execute("SELECT key, value FROM schema_meta").fetchall()
            schema_meta = {row["key"]: row["value"] for row in meta_rows}
            if schema_meta.get("format") != "transcript-v2":
                diagnostics.append(
                    Diagnostic(
                        Code.E_FORMAT_HEADER,
                        "SQLite schema_meta does not identify transcript-v2",
                        source=str(db_path),
                    )
                )
            utterance_rows = connection.execute(
                "SELECT rowid AS rowid, * FROM utterance_meta ORDER BY rowid"
            ).fetchall()
        except sqlite3.Error as exc:
            diagnostics.append(Diagnostic(Code.E_FIELD_MISSING, f"SQLite metadata schema is invalid: {exc}", source=str(db_path)))
            return metadata, diagnostics

        previous_start: int | None = None
        for row in utterance_rows:
            data = dict(row)
            utterance_id = data["utterance_id"]
            start_ms = int(data["start_ms"])
            if previous_start is not None and start_ms < previous_start:
                diagnostics.append(
                    Diagnostic(
                        Code.E_TIME_REGRESSION,
                        "SQLite utterance absolute start_ms regressed in row order",
                        source=str(db_path),
                        utterance_id=utterance_id,
                        details={"start_ms": start_ms, "previous_start_ms": previous_start},
                    )
                )
            previous_start = start_ms
            word_rows = connection.execute(
                "SELECT text, start_ms, end_ms, confidence FROM word_timing WHERE utterance_id = ? ORDER BY ordinal",
                (utterance_id,),
            ).fetchall()
            words = [dict(row) for row in word_rows]
            previous_word_start: int | None = None
            for word in words:
                word_start = int(word["start_ms"])
                if previous_word_start is not None and word_start < previous_word_start:
                    diagnostics.append(
                        Diagnostic(
                            Code.E_TIME_REGRESSION,
                            "SQLite word timing regressed",
                            source=str(db_path),
                            utterance_id=utterance_id,
                        )
                    )
                previous_word_start = word_start
            data["words"] = words
            metadata[utterance_id] = data
    finally:
        connection.close()
    return metadata, diagnostics


def _read_shards(
    path: Path,
) -> tuple[list[dict[str, Any]], str | None, list[Diagnostic], Path | None, dict[str, Any]]:
    diagnostics: list[Diagnostic] = []
    manifest: dict[str, Any] = {}
    manifest_path: Path | None = None
    version: str | None = None

    if path.is_dir():
        manifest_path = path / MANIFEST_NAME
        if manifest_path.exists():
            loaded, error = _load_json(manifest_path)
            if error:
                diagnostics.append(error)
                return [], None, diagnostics, manifest_path, manifest
            manifest = loaded or {}
            version = str(manifest.get("format_version") or "2.0")
            shard_names = list(manifest.get("shard_files", []))
            shard_paths = [path / name for name in shard_names]
            db_path = path / str(manifest.get("metadata_db", DB_NAME))
        else:
            shard_paths = sorted(path.glob("shard-*.json"))
            db_path = path / DB_NAME
    else:
        loaded, error = _load_json(path)
        if error:
            diagnostics.append(error)
            return [], None, diagnostics, path, manifest
        payload = loaded or {}
        if isinstance(payload, dict) and "utterances" in payload:
            version = str(payload.get("format_version") or "2.0")
            payload["_path"] = str(path)
            return [payload], version, diagnostics, path, {"direct_shard": True, "db_path": None}
        manifest = payload if isinstance(payload, dict) else {}
        manifest_path = path
        path = path.parent
        version = str(manifest.get("format_version") or "2.0")
        shard_paths = [path / name for name in manifest.get("shard_files", [])]
        db_path = path / str(manifest.get("metadata_db", DB_NAME))

    rows: list[dict[str, Any]] = []
    for shard_path in shard_paths:
        payload, error = _load_json(shard_path)
        if error:
            diagnostics.append(error)
            continue
        if not isinstance(payload, dict) or not isinstance(payload.get("utterances"), list):
            diagnostics.append(
                Diagnostic(
                    Code.E_FIELD_MISSING,
                    "v2 shard must contain an utterances array",
                    source=str(shard_path),
                )
            )
            continue
        if payload.get("format") and payload.get("format") != "transcript-v2-shard":
            diagnostics.append(
                Diagnostic(Code.E_FORMAT_HEADER, "unsupported v2 shard format header", source=str(shard_path))
            )
        for index, raw in enumerate(payload["utterances"], start=1):
            if not isinstance(raw, dict):
                diagnostics.append(
                    Diagnostic(
                        Code.E_FIELD_TYPE,
                        "v2 utterance must be an object",
                        source=str(shard_path),
                        line=index,
                    )
                )
                continue
            row = dict(raw)
            row["_line"] = index
            row["_source"] = f"{shard_path}:{index}"
            rows.append(row)

    return rows, version, diagnostics, manifest_path, {"db_path": db_path, "manifest": manifest}


def read_v2_dataset(path: str | Path) -> LoadResult:
    path = Path(path)
    rows, version, diagnostics, manifest_path, context = _read_shards(path)
    if context.get("db_path") is None and context.get("direct_shard"):
        result = LoadResult("v2", version or V2_VERSION, [], diagnostics, str(path))
        utterances, row_diagnostics = build_validated_utterances(
            rows[0].get("utterances", []) if rows else [],
            format_name="v2",
            required_fields=V2_REQUIRED_FIELDS,
            confidence_required=False,
            source=str(path),
        )
        result.utterances = utterances
        result.diagnostics.extend(row_diagnostics)
        return result

    db_path = context.get("db_path")
    metadata: dict[str, dict[str, Any]] = {}
    if isinstance(db_path, Path):
        metadata, db_diagnostics = _read_sqlite_metadata(db_path)
        diagnostics.extend(db_diagnostics)

    utterances, row_diagnostics = build_validated_utterances(
        rows,
        format_name="v2",
        required_fields=V2_REQUIRED_FIELDS,
        confidence_required=False,
        source=str(manifest_path or path),
    )
    diagnostics.extend(row_diagnostics)

    by_id = {item.utterance_id: item for item in utterances}
    for utterance_id, db_row in metadata.items():
        json_item = by_id.get(utterance_id)
        if json_item is None:
            diagnostics.append(
                Diagnostic(
                    Code.E_METADATA_MISMATCH,
                    "utterance exists in SQLite but not in valid JSON shards",
                    source=str(db_path),
                    utterance_id=utterance_id,
                )
            )
            continue
        mismatches: dict[str, Any] = {}
        for field_name in ("start_ms", "end_ms", "speaker", "revision"):
            db_value = db_row.get(field_name)
            json_value = getattr(json_item, field_name)
            if field_name in {"start_ms", "end_ms", "revision"} and db_value is not None:
                db_value = int(db_value)
            if db_value != json_value:
                mismatches[field_name] = {"json": json_value, "sqlite": db_value}
        if abs(float(db_row["confidence"]) - json_item.confidence) > 1e-9:
            mismatches["confidence"] = {"json": json_item.confidence, "sqlite": db_row["confidence"]}
        db_words = db_row.get("words", [])
        if len(db_words) != len(json_item.words):
            mismatches["word_count"] = {"json": len(json_item.words), "sqlite": len(db_words)}
        else:
            for index, (word, db_word) in enumerate(zip(json_item.words, db_words)):
                if (
                    word.text != db_word["text"]
                    or word.start_ms != int(db_word["start_ms"])
                    or word.end_ms != int(db_word["end_ms"])
                ):
                    mismatches[f"word_{index}"] = {"json": word.to_dict(), "sqlite": db_word}
        if mismatches:
            diagnostics.append(
                Diagnostic(
                    Code.E_METADATA_MISMATCH,
                    "JSON shard and SQLite metadata disagree",
                    source=str(db_path),
                    utterance_id=utterance_id,
                    details=mismatches,
                )
            )

    for utterance_id in by_id.keys() - metadata.keys():
        diagnostics.append(
            Diagnostic(
                Code.E_METADATA_MISMATCH,
                "utterance exists in JSON shards but not SQLite metadata",
                source=str(db_path),
                utterance_id=utterance_id,
            )
        )

    manifest = context.get("manifest", {})
    return LoadResult(
        format_name="v2",
        format_version=version or V2_VERSION,
        utterances=utterances,
        diagnostics=diagnostics,
        source=str(manifest_path or path),
        details={"manifest": manifest, "sqlite_metadata_count": len(metadata)},
    )


def read_v2_sqlite_only(path: str | Path) -> LoadResult:
    """Read metadata database when JSON shards are intentionally unavailable."""

    db_path = Path(path)
    metadata, diagnostics = _read_sqlite_metadata(db_path)
    rows = [
        {
            "utterance_id": item["utterance_id"],
            "start_ms": item["start_ms"],
            "end_ms": item["end_ms"],
            "text": "",
            "confidence": item["confidence"],
            "speaker": item["speaker"],
            "revision": item["revision"],
            "words": item.get("words", []),
            "_source": str(db_path),
        }
        for item in metadata.values()
    ]
    utterances, validation_diagnostics = build_validated_utterances(
        rows,
        format_name="v2-sqlite",
        required_fields=("utterance_id", "start_ms", "end_ms"),
        confidence_required=False,
        source=str(db_path),
    )
    diagnostics.extend(validation_diagnostics)
    return LoadResult("v2", V2_VERSION, utterances, diagnostics, str(db_path))
