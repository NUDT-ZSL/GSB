"""SQLite-backed committed row store.

The scheduler stages a transaction's writes in memory.  This class applies the
whole write set atomically at commit time and owns the monotonically increasing
global data version.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Mapping

from .errors import E_STORAGE, LabError
from .models import RowState
from .util import canonical_json


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS lab_rows (
    row_key      TEXT PRIMARY KEY,
    payload      TEXT NOT NULL,
    row_version  INTEGER NOT NULL DEFAULT 0,
    updated_by   TEXT
);

CREATE TABLE IF NOT EXISTS lab_meta (
    meta_key   TEXT PRIMARY KEY,
    meta_value TEXT NOT NULL
);
"""


class SQLiteStore:
    def __init__(self, db_path: str | Path | None) -> None:
        self.path = str(db_path) if db_path is not None else ":memory:"
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        try:
            self.conn = sqlite3.connect(
                self.path,
                timeout=5.0,
                isolation_level=None,
                check_same_thread=False,
            )
            self.conn.row_factory = sqlite3.Row
            self.conn.execute("PRAGMA busy_timeout = 5000")
            if self.path != ":memory:":
                self.conn.execute("PRAGMA journal_mode = WAL")
            self.conn.execute("PRAGMA synchronous = FULL")
            self.initialize_schema()
        except sqlite3.Error as exc:
            raise LabError(
                E_STORAGE,
                f"cannot open SQLite database {self.path}: {exc}",
            ) from exc

    def __enter__(self) -> "SQLiteStore":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()

    def close(self) -> None:
        self.conn.close()

    def initialize_schema(self) -> None:
        try:
            self.conn.executescript(SCHEMA_SQL)
            self.conn.execute(
                "INSERT OR IGNORE INTO lab_meta(meta_key, meta_value) "
                "VALUES('data_version', '0')"
            )
        except sqlite3.Error as exc:
            raise LabError(E_STORAGE, f"cannot initialize SQLite schema: {exc}") from exc

    def reset_from_sql(self, sql_text: str) -> None:
        """Clear application tables and load deterministic initial rows."""

        self.initialize_schema()
        try:
            self.conn.execute("BEGIN IMMEDIATE")
            self.conn.execute("DELETE FROM lab_rows")
            self.conn.execute("DELETE FROM lab_meta")
            self.conn.execute("COMMIT")
            self.conn.executescript(sql_text)
            self.initialize_schema()
        except sqlite3.Error as exc:
            try:
                self.conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise LabError(E_STORAGE, f"cannot apply initial SQL: {exc}") from exc

    def replace_state(
        self,
        rows: Mapping[str, RowState] | list[dict[str, Any]] | dict[str, Any],
        data_version: int,
    ) -> None:
        """Replace all rows and the data version in one SQLite transaction."""

        normalized: dict[str, RowState] = {}
        if isinstance(rows, Mapping):
            for key, value in rows.items():
                if isinstance(value, RowState):
                    normalized[str(key)] = value
                else:
                    normalized[str(key)] = RowState(str(key), value, 0)
        elif isinstance(rows, list):
            for item in rows:
                normalized[item["key"]] = RowState(
                    key=item["key"],
                    payload=item["payload"],
                    row_version=int(item.get("row_version", 0)),
                )
        else:
            raise LabError(E_STORAGE, "invalid rows payload for full-state replacement")

        try:
            self.conn.execute("BEGIN IMMEDIATE")
            self.conn.execute("DELETE FROM lab_rows")
            for key in sorted(normalized):
                row = normalized[key]
                self.conn.execute(
                    "INSERT INTO lab_rows(row_key, payload, row_version, updated_by) "
                    "VALUES(?, ?, ?, ?)",
                    (
                        row.key,
                        canonical_json(row.payload),
                        int(row.row_version),
                        "snapshot-restore",
                    ),
                )
            self.conn.execute(
                "INSERT INTO lab_meta(meta_key, meta_value) VALUES('data_version', ?) "
                "ON CONFLICT(meta_key) DO UPDATE SET meta_value=excluded.meta_value",
                (str(int(data_version)),),
            )
            self.conn.execute("COMMIT")
        except (sqlite3.Error, ValueError) as exc:
            try:
                self.conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise LabError(E_STORAGE, f"cannot replace database state: {exc}") from exc

    def data_version(self) -> int:
        try:
            row = self.conn.execute(
                "SELECT meta_value FROM lab_meta WHERE meta_key='data_version'"
            ).fetchone()
            return int(row["meta_value"]) if row is not None else 0
        except (sqlite3.Error, ValueError) as exc:
            raise LabError(E_STORAGE, f"cannot read data version: {exc}") from exc

    def load_rows(self) -> dict[str, RowState]:
        try:
            rows = self.conn.execute(
                "SELECT row_key, payload, row_version FROM lab_rows ORDER BY row_key"
            ).fetchall()
        except sqlite3.Error as exc:
            raise LabError(E_STORAGE, f"cannot read rows: {exc}") from exc
        result: dict[str, RowState] = {}
        for row in rows:
            key = str(row["row_key"])
            result[key] = RowState(
                key=key,
                payload=canonical_payload_load(str(row["payload"])),
                row_version=int(row["row_version"]),
            )
        return result

    def get_row(self, key: str) -> RowState | None:
        try:
            row = self.conn.execute(
                "SELECT row_key, payload, row_version FROM lab_rows WHERE row_key=?",
                (key,),
            ).fetchone()
        except sqlite3.Error as exc:
            raise LabError(E_STORAGE, f"cannot read row {key!r}: {exc}") from exc
        if row is None:
            return None
        return RowState(
            key=str(row["row_key"]),
            payload=canonical_payload_load(str(row["payload"])),
            row_version=int(row["row_version"]),
        )

    def row_keys(self) -> set[str]:
        try:
            rows = self.conn.execute("SELECT row_key FROM lab_rows").fetchall()
        except sqlite3.Error as exc:
            raise LabError(E_STORAGE, f"cannot list row keys: {exc}") from exc
        return {str(row["row_key"]) for row in rows}

    def commit_writes(
        self,
        session_id: str,
        writes: Mapping[str, Any],
    ) -> int:
        """Atomically apply a staged write set and return the resulting version."""

        try:
            self.conn.execute("BEGIN IMMEDIATE")
            current_row = self.conn.execute(
                "SELECT meta_value FROM lab_meta WHERE meta_key='data_version'"
            ).fetchone()
            current_version = int(current_row["meta_value"]) if current_row else 0
            new_version = current_version + 1 if writes else current_version

            for key in sorted(writes):
                self.conn.execute(
                    "INSERT INTO lab_rows(row_key, payload, row_version, updated_by) "
                    "VALUES(?, ?, ?, ?) "
                    "ON CONFLICT(row_key) DO UPDATE SET "
                    "payload=excluded.payload, row_version=excluded.row_version, "
                    "updated_by=excluded.updated_by",
                    (
                        key,
                        canonical_json(writes[key]),
                        new_version,
                        session_id,
                    ),
                )

            self.conn.execute(
                "INSERT INTO lab_meta(meta_key, meta_value) VALUES('data_version', ?) "
                "ON CONFLICT(meta_key) DO UPDATE SET meta_value=excluded.meta_value",
                (str(new_version),),
            )
            self.conn.execute("COMMIT")
            return new_version
        except (sqlite3.Error, ValueError) as exc:
            try:
                self.conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise LabError(E_STORAGE, f"cannot commit transaction: {exc}") from exc

    def rows_as_list(self) -> list[dict[str, Any]]:
        rows = self.load_rows()
        return [rows[key].to_dict() for key in sorted(rows)]


def canonical_payload_load(payload_text: str) -> Any:
    import json

    try:
        return json.loads(payload_text)
    except json.JSONDecodeError as exc:
        raise LabError(E_STORAGE, f"invalid JSON payload in SQLite: {exc}") from exc
