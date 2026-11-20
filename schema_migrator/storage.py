from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from .errors import error
from .model import Bundle, schema_object_map
from .util import canonical_json, digest_json


CATALOG_FORMAT = "schema-diagnostics-catalog/v1"


CATALOG_DDL = """
CREATE TABLE IF NOT EXISTS catalog_meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS current_pointer (
  singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
  version INTEGER NOT NULL,
  snapshot_path TEXT NOT NULL,
  schema_digest TEXT NOT NULL,
  data_digest TEXT NOT NULL,
  content_digest TEXT NOT NULL,
  migration_id TEXT,
  change_kind TEXT NOT NULL,
  previous_version INTEGER,
  updated_seq INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS versions (
  version INTEGER PRIMARY KEY,
  snapshot_path TEXT NOT NULL,
  schema_digest TEXT NOT NULL,
  data_digest TEXT NOT NULL,
  content_digest TEXT NOT NULL,
  schema_json TEXT NOT NULL,
  migration_id TEXT,
  change_kind TEXT NOT NULL,
  created_seq INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS migrations (
  migration_id TEXT PRIMARY KEY,
  migration_digest TEXT NOT NULL,
  from_version INTEGER NOT NULL,
  to_version INTEGER NOT NULL,
  content_digest TEXT NOT NULL,
  status TEXT NOT NULL,
  report_json TEXT NOT NULL,
  change_kind TEXT NOT NULL,
  created_seq INTEGER NOT NULL,
  UNIQUE (to_version)
);
CREATE TABLE IF NOT EXISTS version_objects (
  version INTEGER NOT NULL,
  object_type TEXT NOT NULL,
  object_name TEXT NOT NULL,
  object_digest TEXT NOT NULL,
  PRIMARY KEY (version, object_type, object_name)
);
CREATE TABLE IF NOT EXISTS journal (
  migration_id TEXT PRIMARY KEY,
  migration_digest TEXT NOT NULL,
  from_version INTEGER NOT NULL,
  to_version INTEGER NOT NULL,
  base_version INTEGER NOT NULL,
  base_content_digest TEXT NOT NULL,
  stage_path TEXT NOT NULL,
  final_path TEXT,
  planned_ops_json TEXT NOT NULL,
  applied_ops_json TEXT NOT NULL,
  conversion_log_json TEXT NOT NULL,
  target_schema_json TEXT NOT NULL,
  schema_digest TEXT,
  data_digest TEXT,
  content_digest TEXT,
  report_json TEXT,
  status TEXT NOT NULL,
  error_json TEXT,
  created_seq INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS diagnostics (
  run_id TEXT PRIMARY KEY,
  migration_id TEXT NOT NULL,
  from_version INTEGER NOT NULL,
  to_version INTEGER NOT NULL,
  report_json TEXT NOT NULL,
  created_seq INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS sequence (
  name TEXT PRIMARY KEY,
  value INTEGER NOT NULL
);
"""


@dataclass(frozen=True)
class WorkspacePaths:
    root: Path

    @property
    def catalog(self) -> Path:
        return self.root / "catalog.sqlite3"

    @property
    def snapshots(self) -> Path:
        return self.root / "snapshots"

    @property
    def lock_file(self) -> Path:
        return self.root / ".writer.lock"

    def resolve(self, relative: str) -> Path:
        return self.root / relative

    def snapshot_relative(self, version: int, content_digest: str) -> str:
        return f"snapshots/v{version:04d}-{content_digest[:16]}.sqlite"

    def stage_relative(self, migration_id: str, migration_digest: str) -> str:
        return f"snapshots/.stage-{migration_id}-{migration_digest[:12]}.sqlite"


def open_catalog(
    paths: WorkspacePaths,
    create: bool = False,
    read_only: bool = False,
) -> sqlite3.Connection:
    if read_only:
        if not paths.catalog.exists():
            raise error("E_NOT_LOADED", "workspace catalog does not exist", workspace=str(paths.root))
        uri = paths.catalog.resolve().as_uri() + "?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=30.0)
        conn.execute("PRAGMA query_only=ON")
    else:
        if create:
            paths.root.mkdir(parents=True, exist_ok=True)
            paths.snapshots.mkdir(parents=True, exist_ok=True)
        elif not paths.catalog.exists():
            raise error("E_NOT_LOADED", "workspace catalog does not exist", workspace=str(paths.root))
        conn = sqlite3.connect(str(paths.catalog), timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    if not read_only:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=FULL")
        conn.execute("PRAGMA foreign_keys=ON")
    return conn


def initialize_catalog(paths: WorkspacePaths) -> None:
    conn = open_catalog(paths, create=True)
    try:
        conn.executescript(CATALOG_DDL)
        conn.commit()
    finally:
        conn.close()


def has_bundle(paths: WorkspacePaths) -> bool:
    if not paths.catalog.exists():
        return False
    conn = open_catalog(paths, read_only=True)
    try:
        try:
            row = conn.execute(
                "SELECT value FROM catalog_meta WHERE key='bundle_json'"
            ).fetchone()
            return row is not None
        except sqlite3.OperationalError:
            return False
    finally:
        conn.close()


def bundle_to_dict(bundle: Bundle) -> Dict[str, Any]:
    return {
        "schemas": {str(key): value for key, value in sorted(bundle.schemas.items())},
        "migrations": bundle.migrations,
        "templates": bundle.templates,
        "default_tolerance": bundle.default_tolerance,
        "source_digests": bundle.source_digests or {},
    }


def bundle_from_dict(raw: Dict[str, Any]) -> Bundle:
    schemas = {int(key): value for key, value in raw["schemas"].items()}
    return Bundle(
        schemas=schemas,
        migrations=raw["migrations"],
        templates=raw["templates"],
        default_tolerance=float(raw.get("default_tolerance", 0.000001)),
        source_digests=raw.get("source_digests", {}),
    )


def store_bundle(conn: sqlite3.Connection, bundle: Bundle, bundle_digest: str) -> None:
    values = {
        "format": CATALOG_FORMAT,
        "bundle_json": canonical_json(bundle_to_dict(bundle)),
        "bundle_digest": bundle_digest,
    }
    for key, value in values.items():
        conn.execute(
            "INSERT INTO catalog_meta(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )


def read_bundle(paths: WorkspacePaths, conn: Optional[sqlite3.Connection] = None) -> Bundle:
    own = conn is None
    if conn is None:
        conn = open_catalog(paths, read_only=True)
    try:
        row = conn.execute(
            "SELECT value FROM catalog_meta WHERE key='bundle_json'"
        ).fetchone()
        if row is None:
            raise error("E_NOT_LOADED", "workspace has no loaded sample bundle")
        return bundle_from_dict(json.loads(row[0]))
    finally:
        if own:
            conn.close()


def get_meta(conn: sqlite3.Connection, key: str) -> Optional[str]:
    row = conn.execute("SELECT value FROM catalog_meta WHERE key=?", (key,)).fetchone()
    return None if row is None else row[0]


def current_pointer(conn: sqlite3.Connection) -> Optional[Dict[str, Any]]:
    row = conn.execute("SELECT * FROM current_pointer WHERE singleton=1").fetchone()
    return None if row is None else dict(row)


def next_sequence(conn: sqlite3.Connection, name: str = "global") -> int:
    row = conn.execute("SELECT value FROM sequence WHERE name=?", (name,)).fetchone()
    value = 1 if row is None else int(row[0]) + 1
    conn.execute(
        "INSERT INTO sequence(name, value) VALUES (?, ?) "
        "ON CONFLICT(name) DO UPDATE SET value=excluded.value",
        (name, value),
    )
    return value


def replace_pointer(conn: sqlite3.Connection, pointer: Dict[str, Any]) -> None:
    """Replace the singleton pointer as one table-shaped row set.

    The new pointer is materialized in a TEMP table first, then the singleton
    table is replaced wholesale inside the caller's transaction.
    """

    conn.execute(
        """
        CREATE TEMP TABLE IF NOT EXISTS _next_current_pointer (
          singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
          version INTEGER NOT NULL,
          snapshot_path TEXT NOT NULL,
          schema_digest TEXT NOT NULL,
          data_digest TEXT NOT NULL,
          content_digest TEXT NOT NULL,
          migration_id TEXT,
          change_kind TEXT NOT NULL,
          previous_version INTEGER,
          updated_seq INTEGER NOT NULL
        )
        """
    )
    conn.execute("DELETE FROM _next_current_pointer")
    conn.execute(
        """
        INSERT INTO _next_current_pointer(
          singleton, version, snapshot_path, schema_digest, data_digest,
          content_digest, migration_id, change_kind, previous_version, updated_seq
        ) VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            pointer["version"],
            pointer["snapshot_path"],
            pointer["schema_digest"],
            pointer["data_digest"],
            pointer["content_digest"],
            pointer.get("migration_id"),
            pointer["change_kind"],
            pointer.get("previous_version"),
            pointer["updated_seq"],
        ),
    )
    conn.execute("DELETE FROM current_pointer")
    conn.execute("INSERT INTO current_pointer SELECT * FROM _next_current_pointer")
    conn.execute("DROP TABLE _next_current_pointer")


def write_version_objects(
    conn: sqlite3.Connection,
    version: int,
    schema: Dict[str, Any],
) -> None:
    for node, item in sorted(schema_object_map(schema).items()):
        conn.execute(
            "INSERT INTO version_objects(version, object_type, object_name, object_digest) "
            "VALUES (?, ?, ?, ?)",
            (version, item["type"], item["name"], item["digest"]),
        )


def journal_row(conn: sqlite3.Connection) -> Optional[Dict[str, Any]]:
    row = conn.execute("SELECT * FROM journal ORDER BY created_seq LIMIT 1").fetchone()
    return None if row is None else dict(row)


def diagnostics_row(
    conn: sqlite3.Connection,
    migration_id: Optional[str] = None,
    to_version: Optional[int] = None,
) -> Optional[Dict[str, Any]]:
    if migration_id is not None:
        row = conn.execute(
            "SELECT * FROM diagnostics WHERE migration_id=? ORDER BY created_seq DESC LIMIT 1",
            (migration_id,),
        ).fetchone()
    elif to_version is not None:
        row = conn.execute(
            "SELECT * FROM diagnostics WHERE to_version=? ORDER BY created_seq DESC LIMIT 1",
            (to_version,),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT * FROM diagnostics ORDER BY created_seq DESC LIMIT 1"
        ).fetchone()
    return None if row is None else dict(row)

