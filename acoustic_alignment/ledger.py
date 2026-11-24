"""SQLite fixed-point ledger with atomic snapshots and digest recovery."""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import tempfile
from contextlib import closing
from pathlib import Path
from typing import Any, Iterable

from .canonical import canonical_digest, canonical_json
from .codes import Code, EngineError
from .results import BandResult, TrackResult


SCHEMA_VERSION = "1.0"


def read_only_sqlite_uri(path: str | Path) -> str:
    return Path(path).resolve().as_uri() + "?mode=ro"


TRACK_COLUMNS = [
    "input_digest",
    "track_id",
    "track_index",
    "status",
    "is_reference",
    "business_codes_json",
    "input_frames",
    "sample_rate",
    "analysis_sample_rate",
    "channels",
    "bit_depth",
    "expected_start_ms",
    "sweep_start_ms",
    "sweep_end_ms",
    "silence_start_sample",
    "silence_end_sample",
    "excitation_start_sample",
    "excitation_end_sample",
    "tail_end_sample",
    "search_window_samples",
    "analysis_samples",
    "noise_rms",
    "excitation_peak",
    "correlation_peak",
    "noise_gate_threshold",
    "delay_samples",
    "delay_float_samples",
    "delay_float_delta_samples",
    "delay_abs_error_samples",
    "delay_fixed_samples",
    "delay_quantization_error",
    "integer_delay_q",
    "float_delay_q",
    "delay_delta_q",
    "delay_tolerance_q",
    "delay_pass",
    "alignment_pass",
    "result_json",
]

BAND_COLUMNS = [
    "input_digest",
    "track_id",
    "band_index",
    "band_id",
    "point_count",
    "slope_rad_per_hz",
    "slope_q",
    "slope_quantization_error",
    "phase_delay_samples",
    "phase_delay_q",
    "phase_delay_fixed_samples",
    "phase_delay_quantization_error",
    "integer_delay_q",
    "phase_error_q",
    "phase_tolerance_q",
    "passed",
]


def _fsync_directory(path: Path) -> None:
    if not hasattr(os, "O_RDONLY"):
        return
    try:
        descriptor = os.open(str(path), os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except (OSError, PermissionError):
        # Some Windows filesystems do not support directory fsync; rename is atomic there.
        pass


def atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
        _fsync_directory(path.parent)
    except Exception:
        try:
            temporary_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def atomic_copy_file(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=str(destination.parent)
    )
    os.close(descriptor)
    temporary_path = Path(temporary_name)
    try:
        shutil.copyfile(source, temporary_path)
        with temporary_path.open("rb") as handle:
            os.fsync(handle.fileno())
        os.replace(temporary_path, destination)
        _fsync_directory(destination.parent)
    except Exception:
        try:
            temporary_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


class LedgerStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.backup_path = self.path.parent / "ledger_versions" / "latest_valid.sqlite"
        self.results: dict[tuple[str, str], TrackResult] = {}
        self.ledger_digest: str | None = None
        self.restored_from_backup = False
        self._load()

    @property
    def exists(self) -> bool:
        return self.path.is_file()

    def _connect_read(self) -> sqlite3.Connection:
        return sqlite3.connect(read_only_sqlite_uri(self.path), uri=True)

    def _load(self) -> None:
        if not self.path.is_file():
            self.results = {}
            self.ledger_digest = None
            return
        try:
            digest = self._read_and_verify()
        except (sqlite3.DatabaseError, EngineError, KeyError, TypeError, ValueError):
            if not self.restore_latest_valid():
                raise EngineError(
                    Code.E_DIGEST,
                    "ledger digest is invalid and no digest-valid backup is available",
                    {"ledger": str(self.path)},
                )
            digest = self._read_and_verify()
        self.ledger_digest = digest

    def _read_and_verify(self) -> str:
        with closing(self._connect_read()) as conn:
            tables = {
                row[0]
                for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            if not {"tracks", "bands", "meta"}.issubset(tables):
                raise EngineError(Code.E_LEDGER, "ledger schema is incomplete")
            meta = dict(conn.execute("SELECT key, value FROM meta").fetchall())
            track_rows = [
                dict(zip(TRACK_COLUMNS, values))
                for values in conn.execute(
                    f"SELECT {', '.join(TRACK_COLUMNS)} FROM tracks ORDER BY input_digest, track_id"
                ).fetchall()
            ]
            band_rows = [
                dict(zip(BAND_COLUMNS, values))
                for values in conn.execute(
                    f"""
                    SELECT {', '.join(BAND_COLUMNS)}
                    FROM bands
                    ORDER BY input_digest, track_id, band_index, band_id
                    """
                ).fetchall()
            ]

        expected_digest = self.digest_from_rows(track_rows, band_rows)
        stored_digest = meta.get("ledger_digest")
        if stored_digest != expected_digest:
            raise EngineError(
                Code.E_DIGEST,
                "ledger digest does not match its records",
                {"stored": stored_digest, "actual": expected_digest},
            )

        results: dict[tuple[str, str], TrackResult] = {}
        for row in track_rows:
            result = TrackResult.from_dict(json.loads(row["result_json"]))
            results[(row["input_digest"], row["track_id"])] = result
        self.results = results
        return expected_digest

    @staticmethod
    def digest_from_rows(track_rows: Iterable[dict[str, Any]], band_rows: Iterable[dict[str, Any]]) -> str:
        track_digest_rows = (
            {key: value for key, value in row.items() if key != "result_json"}
            | {"result_json": row.get("result_json")}
            for row in track_rows
        )
        tracks = sorted(
            track_digest_rows,
            key=lambda row: (
                str(row.get("input_digest")),
                str(row.get("track_id")),
                int(row.get("track_index", 0)),
            ),
        )
        bands = sorted(band_rows, key=lambda row: (str(row.get("input_digest")), str(row.get("track_id")), int(row.get("band_index", 0))))
        return canonical_digest({"tracks": tracks, "bands": bands})

    def restore_latest_valid(self) -> bool:
        if not self.backup_path.is_file():
            return False
        try:
            with closing(
                sqlite3.connect(read_only_sqlite_uri(self.backup_path), uri=True)
            ) as conn:
                tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if not {"tracks", "bands", "meta"}.issubset(tables):
                    return False
            atomic_copy_file(self.backup_path, self.path)
            self.restored_from_backup = True
            return True
        except (sqlite3.DatabaseError, OSError, EngineError):
            return False

    def has_complete_track(self, input_digest: str, track_id: str) -> bool:
        result = self.results.get((input_digest, track_id))
        return bool(result is not None and result.complete)

    def get_track(self, input_digest: str, track_id: str) -> TrackResult | None:
        return self.results.get((input_digest, track_id))

    def current_input_results(self, input_digest: str) -> list[TrackResult]:
        return [result for (digest, _track_id), result in self.results.items() if digest == input_digest]

    def query(
        self,
        track_id: str | None = None,
        band_id: str | None = None,
        input_digest: str | None = None,
    ) -> dict[str, list[dict[str, Any]]]:
        if not self.path.is_file():
            return {"tracks": [], "bands": []}
        with closing(self._connect_read()) as conn:
            track_clauses = []
            band_clauses = []
            params: list[Any] = []
            if input_digest:
                track_clauses.append("input_digest = ?")
                band_clauses.append("input_digest = ?")
                params.append(input_digest)
            if track_id:
                track_clauses.append("track_id = ?")
                band_clauses.append("track_id = ?")
            if band_id:
                band_clauses.append("band_id = ?")
            track_where = " WHERE " + " AND ".join(track_clauses) if track_clauses else ""
            band_where = " WHERE " + " AND ".join(band_clauses) if band_clauses else ""
            track_params = params + ([track_id] if track_id else [])
            band_params = list(params)
            if track_id:
                band_params.append(track_id)
            if band_id:
                band_params.append(band_id)
            tracks = [
                dict(zip(TRACK_COLUMNS, values))
                for values in conn.execute(
                    f"SELECT {', '.join(TRACK_COLUMNS)} FROM tracks{track_where} ORDER BY track_id",
                    track_params,
                ).fetchall()
            ]
            bands = [
                dict(zip(BAND_COLUMNS, values))
                for values in conn.execute(
                    f"""
                    SELECT {', '.join(BAND_COLUMNS)}
                    FROM bands{band_where}
                    ORDER BY track_id, band_index
                    """,
                    band_params,
                ).fetchall()
            ]
        for track in tracks:
            track.pop("result_json", None)
        for band in bands:
            band["passed"] = bool(band["passed"])
        return {"tracks": tracks, "bands": bands}

    def commit_track(self, input_digest: str, result: TrackResult) -> str:
        self.results[(input_digest, result.track_id)] = result
        return self._write_snapshot()

    def _row_for_result(self, input_digest: str, result: TrackResult) -> dict[str, Any]:
        data = result.to_dict()
        row = {column: None for column in TRACK_COLUMNS}
        row.update(
            {
                "input_digest": input_digest,
                "track_id": result.track_id,
                "track_index": int(result.track_index),
                "status": result.status,
                "is_reference": 1 if result.is_reference else 0,
                "business_codes_json": json.dumps(result.sorted_codes(), ensure_ascii=False, separators=(",", ":")),
                "result_json": json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            }
        )
        for key in TRACK_COLUMNS:
            if key in row and row[key] is not None:
                continue
            if key in data:
                value = data[key]
                if isinstance(value, bool):
                    value = 1 if value else 0
                row[key] = value
        return row

    def _band_rows(self, input_digest: str, result: TrackResult) -> list[dict[str, Any]]:
        integer_q = result.integer_delay_q
        rows = []
        for band in result.bands:
            row = {column: None for column in BAND_COLUMNS}
            row.update(
                {
                    "input_digest": input_digest,
                    "track_id": result.track_id,
                    "band_index": int(band.band_index),
                    "band_id": band.band_id,
                    "point_count": int(band.point_count),
                    "slope_rad_per_hz": band.slope_rad_per_hz,
                    "slope_q": band.slope_q,
                    "slope_quantization_error": band.slope_quantization_error,
                    "phase_delay_samples": band.phase_delay_samples,
                    "phase_delay_q": band.phase_delay_q,
                    "phase_delay_fixed_samples": band.phase_delay_fixed_samples,
                    "phase_delay_quantization_error": band.phase_delay_quantization_error,
                    "integer_delay_q": band.integer_delay_q
                    if band.integer_delay_q is not None
                    else integer_q,
                    "phase_error_q": band.phase_error_q,
                    "phase_tolerance_q": band.phase_tolerance_q,
                    "passed": 1 if band.passed else 0,
                }
            )
            rows.append(row)
        return rows

    def _write_snapshot(self) -> str:
        track_rows = [self._row_for_result(digest, result) for (digest, _), result in sorted(self.results.items())]
        band_rows: list[dict[str, Any]] = []
        for (digest, _track_id), result in sorted(self.results.items()):
            band_rows.extend(self._band_rows(digest, result))
        digest = self.digest_from_rows(track_rows, band_rows)

        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{self.path.name}.", suffix=".tmp", dir=str(self.path.parent)
        )
        os.close(descriptor)
        temporary_path = Path(temporary_name)
        try:
            conn = sqlite3.connect(str(temporary_path))
            try:
                conn.execute(
                    """
                    CREATE TABLE meta (
                        key TEXT PRIMARY KEY,
                        value TEXT NOT NULL
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE tracks (
                        input_digest TEXT NOT NULL,
                        track_id TEXT NOT NULL,
                        track_index INTEGER NOT NULL,
                        status TEXT NOT NULL,
                        is_reference INTEGER NOT NULL,
                        business_codes_json TEXT NOT NULL,
                        input_frames INTEGER,
                        sample_rate INTEGER,
                        analysis_sample_rate INTEGER,
                        channels INTEGER,
                        bit_depth INTEGER,
                        expected_start_ms INTEGER,
                        sweep_start_ms REAL,
                        sweep_end_ms REAL,
                        silence_start_sample INTEGER,
                        silence_end_sample INTEGER,
                        excitation_start_sample INTEGER,
                        excitation_end_sample INTEGER,
                        tail_end_sample INTEGER,
                        search_window_samples INTEGER,
                        analysis_samples INTEGER,
                        noise_rms REAL,
                        excitation_peak REAL,
                        correlation_peak REAL,
                        noise_gate_threshold REAL,
                        delay_samples INTEGER,
                        delay_float_samples REAL,
                        delay_float_delta_samples REAL,
                        delay_abs_error_samples REAL,
                        delay_fixed_samples REAL,
                        delay_quantization_error REAL,
                        integer_delay_q INTEGER,
                        float_delay_q INTEGER,
                        delay_delta_q INTEGER,
                        delay_tolerance_q INTEGER,
                        delay_pass INTEGER NOT NULL,
                        alignment_pass INTEGER NOT NULL,
                        result_json TEXT NOT NULL,
                        PRIMARY KEY (input_digest, track_id)
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE bands (
                        input_digest TEXT NOT NULL,
                        track_id TEXT NOT NULL,
                        band_index INTEGER NOT NULL,
                        band_id TEXT NOT NULL,
                        point_count INTEGER NOT NULL,
                        slope_rad_per_hz REAL,
                        slope_q INTEGER,
                        slope_quantization_error REAL,
                        phase_delay_samples REAL,
                        phase_delay_q INTEGER,
                        phase_delay_fixed_samples REAL,
                        phase_delay_quantization_error REAL,
                        integer_delay_q INTEGER,
                        phase_error_q INTEGER,
                        phase_tolerance_q INTEGER,
                        passed INTEGER NOT NULL,
                        PRIMARY KEY (input_digest, track_id, band_id)
                    )
                    """
                )
                conn.execute("CREATE INDEX idx_bands_track ON bands(input_digest, track_id)")
                conn.execute("CREATE INDEX idx_bands_band ON bands(input_digest, band_id)")
                insert_columns = ", ".join(TRACK_COLUMNS)
                placeholders = ", ".join(f":{column}" for column in TRACK_COLUMNS)
                conn.executemany(
                    f"INSERT INTO tracks ({insert_columns}) VALUES ({placeholders})",
                    track_rows,
                )
                band_columns = ", ".join(BAND_COLUMNS)
                band_placeholders = ", ".join(f":{column}" for column in BAND_COLUMNS)
                conn.executemany(
                    f"INSERT INTO bands ({band_columns}) VALUES ({band_placeholders})",
                    band_rows,
                )
                conn.execute("INSERT INTO meta(key, value) VALUES ('schema_version', ?)", (SCHEMA_VERSION,))
                conn.execute("INSERT INTO meta(key, value) VALUES ('record_count', ?)", (str(len(track_rows)),))
                conn.execute("INSERT INTO meta(key, value) VALUES ('ledger_digest', ?)", (digest,))
                conn.commit()
            finally:
                conn.close()

            with temporary_path.open("rb") as handle:
                os.fsync(handle.fileno())
            os.replace(temporary_path, self.path)
            _fsync_directory(self.path.parent)
        finally:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass

        self.ledger_digest = digest
        atomic_copy_file(self.path, self.backup_path)
        versioned_backup = self.path.parent / "ledger_versions" / f"ledger-{digest[:16]}.sqlite"
        if not versioned_backup.exists():
            atomic_copy_file(self.path, versioned_backup)
        return digest

    def canonical_bytes(self) -> bytes:
        track_rows = [self._row_for_result(digest, result) for (digest, _), result in sorted(self.results.items())]
        band_rows: list[dict[str, Any]] = []
        for (digest, _track_id), result in sorted(self.results.items()):
            band_rows.extend(self._band_rows(digest, result))
        return canonical_json({"tracks": track_rows, "bands": band_rows})


def build_report_from_ledger(
    ledger: LedgerStore,
    input_digest: str,
    task: Any,
    recovery: dict[str, Any] | None = None,
) -> dict[str, Any]:
    results = sorted(
        ledger.current_input_results(input_digest),
        key=lambda result: result.track_index,
    )
    business_codes = sorted(
        {code for result in results for code in result.sorted_codes()}
    )
    report: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "task_id": task.task_id if task is not None else None,
        "input_digest": input_digest,
        "ledger_digest": ledger.ledger_digest,
        "fixed_point_scale": task.fixed_point_scale if task is not None else None,
        "track_count": len(results),
        "declared_track_count": len(task.tracks) if task is not None else len(results),
        "business_codes": business_codes,
        "summary": {
            "total": len(results),
            "measured": sum(result.status == "measured" for result in results),
            "passed": sum(result.alignment_pass for result in results),
            "business_errors": sum(bool(result.business_codes) for result in results),
        },
        "tracks": [result.to_dict() for result in results],
    }
    if recovery:
        report["digest_recovery"] = recovery
    report["report_digest"] = canonical_digest(
        {key: value for key, value in report.items() if key != "report_digest"}
    )
    return report


def write_report_atomic(report: dict[str, Any], path: str | Path) -> None:
    path = Path(path)
    data = (json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    atomic_write_bytes(path, data)
