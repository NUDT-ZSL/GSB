"""Atomic SQLite persistence for committed logical steps."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .engine import Engine
from .errors import QosError
from .models import Sample
from .sample import sample_from_package
from .serialization import canonical_json, digest_value


SCHEMA_VERSION = 1
STEP_VERSION = 1


class Store:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).resolve()

    def _connect(self, *, write: bool) -> sqlite3.Connection:
        if not write and not self.path.exists():
            raise QosError(
                "E_DB_NOT_LOADED",
                "database file does not exist",
                {"path": str(self.path)},
            )
        if write:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            if write:
                connection = sqlite3.connect(
                    str(self.path), timeout=30.0, isolation_level=None
                )
            else:
                uri = f"{self.path.as_uri()}?mode=ro"
                connection = sqlite3.connect(
                    uri, uri=True, timeout=30.0, isolation_level=None
                )
        except sqlite3.Error as exc:
            raise QosError(
                "E_DB_SCHEMA",
                "cannot open SQLite database",
                {"path": str(self.path), "reason": str(exc)},
            ) from exc
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=30000")
        if write:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=FULL")
        return connection

    @contextmanager
    def _reader(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect(write=False)
        try:
            yield connection
        finally:
            connection.close()

    def _ensure_schema(self, connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS metadata (
                key TEXT PRIMARY KEY NOT NULL,
                value TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS steps (
                step_index INTEGER NOT NULL,
                version INTEGER NOT NULL,
                logical_time_ms INTEGER NOT NULL,
                prev_digest TEXT NOT NULL,
                step_digest TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                state_json TEXT NOT NULL,
                PRIMARY KEY (step_index, version)
            )
            """
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS steps_by_index ON steps(step_index, version)"
        )

    def initialize(self, sample: Sample) -> dict[str, Any]:
        initial_state = Engine(sample).initial_state()
        initial_state_json = canonical_json(initial_state)
        initial_digest = digest_value(initial_state)
        sample_package_json = canonical_json(sample.raw_package)
        connection = self._connect(write=True)
        try:
            connection.execute("BEGIN IMMEDIATE")
            self._ensure_schema(connection)
            row = connection.execute(
                "SELECT value FROM metadata WHERE key='sample_digest'"
            ).fetchone()
            if row is not None:
                if row["value"] != sample.sample_digest:
                    raise QosError(
                        "E_DB_SAMPLE_MISMATCH",
                        "database contains a different sample",
                        {
                            "database_sample_digest": row["value"],
                            "requested_sample_digest": sample.sample_digest,
                        },
                    )
                connection.execute("COMMIT")
                return {"loaded": False, "already_loaded": True}
            step_row = connection.execute("SELECT COUNT(*) AS count FROM steps").fetchone()
            if step_row["count"]:
                raise QosError(
                    "E_DB_SCHEMA",
                    "steps exist without sample metadata",
                    {"path": str(self.path)},
                )
            metadata = {
                "schema_version": str(SCHEMA_VERSION),
                "sample_digest": sample.sample_digest,
                "sample_id": sample.sample_id,
                "injection_id": sample.injection_id,
                "config_digest": sample.config_digest,
                "arrivals_digest": sample.arrivals_digest,
                "sample_package": sample_package_json,
                "initial_state": initial_state_json,
                "initial_digest": initial_digest,
            }
            connection.executemany(
                "INSERT INTO metadata(key, value) VALUES(?, ?)",
                sorted(metadata.items()),
            )
            connection.execute("COMMIT")
            return {"loaded": True, "already_loaded": False}
        except QosError:
            connection.execute("ROLLBACK")
            raise
        except sqlite3.Error as exc:
            connection.execute("ROLLBACK")
            raise QosError(
                "E_DB_SCHEMA",
                "failed to initialize SQLite database",
                {"path": str(self.path), "reason": str(exc)},
            ) from exc
        finally:
            connection.close()

    def _read_metadata(self, connection: sqlite3.Connection) -> dict[str, str]:
        rows = connection.execute("SELECT key, value FROM metadata").fetchall()
        if not rows:
            raise QosError(
                "E_DB_NOT_LOADED",
                "database has no pipeline metadata",
                {"path": str(self.path)},
            )
        metadata = {row["key"]: row["value"] for row in rows}
        if metadata.get("schema_version") != str(SCHEMA_VERSION):
            raise QosError(
                "E_DB_SCHEMA",
                "unsupported database schema version",
                {
                    "expected": SCHEMA_VERSION,
                    "actual": metadata.get("schema_version"),
                },
            )
        required = {
            "sample_digest",
            "sample_package",
            "initial_state",
            "initial_digest",
        }
        missing = sorted(required - set(metadata))
        if missing:
            raise QosError(
                "E_DB_SCHEMA",
                "database metadata is incomplete",
                {"missing": missing},
            )
        return metadata

    def metadata(self) -> dict[str, str]:
        with self._reader() as connection:
            return self._read_metadata(connection)

    def sample(self) -> Sample:
        metadata = self.metadata()
        try:
            import json

            package = json.loads(metadata["sample_package"])
        except (ValueError, TypeError) as exc:
            raise QosError(
                "E_DB_SCHEMA", "stored sample package is not valid JSON"
            ) from exc
        sample = sample_from_package(package)
        if sample.sample_digest != metadata["sample_digest"]:
            raise QosError(
                "E_DB_SCHEMA",
                "stored sample package digest does not match metadata",
                {
                    "expected": metadata["sample_digest"],
                    "actual": sample.sample_digest,
                },
            )
        return sample

    def ensure_sample(self, sample: Sample) -> None:
        metadata = self.metadata()
        if metadata["sample_digest"] != sample.sample_digest:
            raise QosError(
                "E_DB_SAMPLE_MISMATCH",
                "requested sample differs from the database sample",
                {
                    "database_sample_digest": metadata["sample_digest"],
                    "requested_sample_digest": sample.sample_digest,
                },
            )

    @staticmethod
    def _row_to_record(row: sqlite3.Row) -> dict[str, Any]:
        import json

        return {
            "step_index": row["step_index"],
            "version": row["version"],
            "logical_time_ms": row["logical_time_ms"],
            "prev_digest": row["prev_digest"],
            "step_digest": row["step_digest"],
            "payload": json.loads(row["payload_json"]),
            "payload_json": row["payload_json"],
            "state": json.loads(row["state_json"]),
            "state_json": row["state_json"],
        }

    def snapshot(self) -> dict[str, Any]:
        with self._reader() as connection:
            metadata = self._read_metadata(connection)
            row = connection.execute(
                "SELECT * FROM steps ORDER BY step_index DESC, version DESC LIMIT 1"
            ).fetchone()
            if row is None:
                import json

                return {
                    "last_step": -1,
                    "logical_time_ms": 0,
                    "step_digest": metadata["initial_digest"],
                    "prev_digest": metadata["initial_digest"],
                    "state": json.loads(metadata["initial_state"]),
                    "version": STEP_VERSION,
                    "empty": True,
                }
            import json

            return {
                "last_step": row["step_index"],
                "logical_time_ms": row["logical_time_ms"],
                "step_digest": row["step_digest"],
                "prev_digest": row["prev_digest"],
                "state": json.loads(row["state_json"]),
                "version": row["version"],
                "empty": False,
            }

    def commit_step(
        self,
        *,
        step_index: int,
        logical_time_ms: int,
        payload: dict[str, Any],
        state: dict[str, Any],
        prev_digest: str,
        version: int = STEP_VERSION,
    ) -> dict[str, Any]:
        payload_json = canonical_json(payload)
        state_json = canonical_json(state)
        step_digest = digest_value(
            {"prev_digest": prev_digest, "payload": payload, "state": state}
        )
        connection = self._connect(write=True)
        try:
            connection.execute("BEGIN IMMEDIATE")
            metadata = self._read_metadata(connection)
            existing = connection.execute(
                "SELECT * FROM steps WHERE step_index=? AND version=?",
                (step_index, version),
            ).fetchone()
            if existing is not None:
                connection.execute("COMMIT")
                record = self._row_to_record(existing)
                record["already_committed"] = True
                return record
            last = connection.execute(
                "SELECT step_index, version FROM steps ORDER BY step_index DESC, version DESC LIMIT 1"
            ).fetchone()
            expected_previous = -1 if last is None else last["step_index"]
            if expected_previous != step_index - 1:
                actual = expected_previous
                connection.execute("ROLLBACK")
                raise QosError(
                    "E_STALE_COMMIT",
                    "another writer advanced or changed the step watermark",
                    {
                        "expected_previous": step_index - 1,
                        "actual_previous": actual,
                        "requested_step": step_index,
                    },
                )
            connection.execute(
                """
                INSERT INTO steps(
                    step_index, version, logical_time_ms, prev_digest,
                    step_digest, payload_json, state_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    step_index,
                    version,
                    logical_time_ms,
                    prev_digest,
                    step_digest,
                    payload_json,
                    state_json,
                ),
            )
            connection.execute("COMMIT")
            return {
                "step_index": step_index,
                "version": version,
                "logical_time_ms": logical_time_ms,
                "prev_digest": prev_digest,
                "step_digest": step_digest,
                "payload": payload,
                "payload_json": payload_json,
                "state": state,
                "state_json": state_json,
                "already_committed": False,
                "sample_digest": metadata["sample_digest"],
            }
        except QosError:
            try:
                connection.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        except sqlite3.IntegrityError as exc:
            try:
                connection.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise QosError(
                "E_STALE_COMMIT",
                "step/version was committed concurrently",
                {"step_index": step_index, "version": version},
            ) from exc
        except sqlite3.Error as exc:
            try:
                connection.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise QosError(
                "E_DB_SCHEMA",
                "failed to commit a logical step",
                {"step_index": step_index, "reason": str(exc)},
            ) from exc
        finally:
            connection.close()

    def steps(self, *, until_step: int | None = None) -> list[dict[str, Any]]:
        with self._reader() as connection:
            self._read_metadata(connection)
            if until_step is None:
                rows = connection.execute(
                    "SELECT * FROM steps ORDER BY step_index, version"
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM steps WHERE step_index < ? ORDER BY step_index, version",
                    (until_step,),
                ).fetchall()
            return [self._row_to_record(row) for row in rows]

    def status(self) -> dict[str, Any]:
        with self._reader() as connection:
            metadata = self._read_metadata(connection)
            row = connection.execute(
                "SELECT * FROM steps ORDER BY step_index DESC, version DESC LIMIT 1"
            ).fetchone()
            if row is None:
                import json

                state = json.loads(metadata["initial_state"])
                last_step = -1
                logical_time_ms = 0
                digest = metadata["initial_digest"]
                version = STEP_VERSION
            else:
                import json

                state = json.loads(row["state_json"])
                last_step = row["step_index"]
                logical_time_ms = row["logical_time_ms"]
                digest = row["step_digest"]
                version = row["version"]
            sample = sample_from_package(__import__("json").loads(metadata["sample_package"]))
            queue_summary: dict[str, Any] = {}
            buckets: dict[str, Any] = {}
            for class_config in sample.classes:
                queue_items = state["queues"][class_config.name]
                queue_summary[class_config.name] = {
                    "events": len(queue_items),
                    "bytes": sum(item["bytes"] for item in queue_items),
                    "digest": digest_value(queue_items),
                }
                bucket = state["buckets"][class_config.name]
                buckets[class_config.name] = {
                    "committed_tokens_units": bucket["committed_tokens_units"],
                    "peak_tokens_units": bucket["peak_tokens_units"],
                    "committed_remainder": bucket["committed_remainder"],
                    "peak_remainder": bucket["peak_remainder"],
                }
            return {
                "ok": True,
                "command": "status",
                "database": str(self.path),
                "sample_id": metadata["sample_id"],
                "sample_digest": metadata["sample_digest"],
                "injection_id": metadata["injection_id"],
                "last_step": last_step,
                "next_step": last_step + 1,
                "logical_time_ms": logical_time_ms,
                "version": version,
                "step_digest": digest,
                "state_digest": digest_value(state),
                "complete": last_step + 1 >= sample.total_steps,
                "total_steps": sample.total_steps,
                "buckets": buckets,
                "queues": queue_summary,
                "counters": state["counters"],
                "arrival_cursor": state["arrival_cursor"],
                "scheduler": state["schedule"],
            }

