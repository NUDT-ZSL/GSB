from __future__ import annotations

import copy
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .engine import Computation, FeaturePlan
from .errors import (
    E_ENCODING_ERROR,
    E_RESOURCE_LIMIT,
    E_STAGE_BUSY,
    EngineError,
    encoding_error,
    resource_error,
    stage_busy,
    stale_result,
)
from .util import canonical_json, chunked, sha256_bytes, sha256_json, stable_float


_LOCK_MESSAGES = ("locked", "busy")


def _is_lock_error(error: BaseException) -> bool:
    text = str(error).lower()
    return any(token in text for token in _LOCK_MESSAGES)


class CacheStore:
    def __init__(self, path: Path, max_entries: int, *, initialize: bool = True) -> None:
        self.path = Path(path)
        self.max_entries = int(max_entries)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if initialize:
            self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.path), timeout=0, isolation_level=None)
        connection.execute("PRAGMA busy_timeout=0")
        return connection

    def _connect_reader(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.path), timeout=1.0, isolation_level=None)
        connection.execute("PRAGMA busy_timeout=1000")
        return connection

    def _initialize(self) -> None:
        connection = self._connect()
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=FULL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS encoding_cache (
                    cache_key TEXT PRIMARY KEY,
                    row_id TEXT NOT NULL,
                    feature_id TEXT NOT NULL,
                    value_json TEXT NOT NULL,
                    generation INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS selection_cache (
                    selection_key TEXT PRIMARY KEY,
                    matrix_digest TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    payload_checksum TEXT NOT NULL,
                    generation INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS current_state (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    generation INTEGER NOT NULL,
                    selection_key TEXT NOT NULL,
                    matrix_digest TEXT NOT NULL,
                    config_digest TEXT NOT NULL,
                    input_digest TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS stage_lease (
                    name TEXT PRIMARY KEY,
                    owner TEXT NOT NULL,
                    nonce TEXT NOT NULL
                );
                """
            )
        except sqlite3.OperationalError as error:
            if _is_lock_error(error):
                raise stage_busy("cache initialization is locked by another writer") from None
            raise
        finally:
            connection.close()

    def reset(self) -> None:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            for table in ("encoding_cache", "selection_cache", "current_state", "stage_lease"):
                connection.execute(f"DELETE FROM {table}")
            connection.commit()
        except sqlite3.OperationalError as error:
            connection.rollback()
            if _is_lock_error(error):
                raise stage_busy("cache reset is locked by another writer") from None
            raise
        finally:
            connection.close()

    def entry_count(self) -> int:
        connection = self._connect_reader()
        try:
            return int(connection.execute("SELECT COUNT(*) FROM encoding_cache").fetchone()[0])
        finally:
            connection.close()

    def _fetch_existing_keys(self, keys: list[str]) -> set[str]:
        if not keys:
            return set()
        connection = self._connect_reader()
        found: set[str] = set()
        try:
            for chunk in chunked(keys, 400):
                placeholders = ",".join("?" for _ in chunk)
                rows = connection.execute(
                    f"SELECT cache_key FROM encoding_cache WHERE cache_key IN ({placeholders})",
                    chunk,
                ).fetchall()
                found.update(str(row[0]) for row in rows)
        except sqlite3.OperationalError as error:
            if _is_lock_error(error):
                raise stage_busy("cache lookup is locked by another writer") from None
            raise
        finally:
            connection.close()
        return found

    def fetch_encoding_values(self, keys: list[str]) -> dict[str, float]:
        if not keys:
            return {}
        connection = self._connect_reader()
        result: dict[str, float] = {}
        try:
            for chunk in chunked(keys, 400):
                placeholders = ",".join("?" for _ in chunk)
                rows = connection.execute(
                    f"SELECT cache_key, value_json FROM encoding_cache WHERE cache_key IN ({placeholders})",
                    chunk,
                ).fetchall()
                for cache_key, value_json in rows:
                    try:
                        value = float(json.loads(value_json))
                        stable_float(value)
                    except Exception as error:
                        raise encoding_error(
                            "cached encoding entry cannot be parsed",
                            field="encoding_cache",
                            cache_key=str(cache_key),
                            error=str(error),
                        ) from None
                    result[str(cache_key)] = value
        except sqlite3.OperationalError as error:
            if _is_lock_error(error):
                raise stage_busy("cache read is locked by another writer") from None
            raise
        finally:
            connection.close()
        return result

    def commit(
        self,
        computation: Computation,
        *,
        input_digest: str,
        config_digest: str,
    ) -> dict[str, Any]:
        keys_by_feature: dict[str, str] = {}
        for row in computation.rows:
            row_id = row["row_id"]
            keys = computation.feature_keys[row_id]
            for feature_id, cache_key in zip(computation.plan.ids, keys):
                keys_by_feature[cache_key] = feature_id
        all_keys = sorted(keys_by_feature)
        if len(all_keys) > self.max_entries:
            raise resource_error(
                "encoding cache entry count exceeds max_cache_entries",
                resource="cache",
                limit=self.max_entries,
                needed=len(all_keys),
            )
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            lease = connection.execute("SELECT name FROM stage_lease").fetchall()
            if lease:
                connection.rollback()
                raise stage_busy("cache commit stage is already leased", lease=[row[0] for row in lease])
            existing_count = int(connection.execute("SELECT COUNT(*) FROM encoding_cache").fetchone()[0])
            existing_keys = self._fetch_existing_keys_on(connection, all_keys)
            final_count = existing_count + len(set(all_keys) - existing_keys)
            if final_count > self.max_entries:
                connection.rollback()
                raise resource_error(
                    "cache entry count exceeds max_cache_entries",
                    resource="cache",
                    limit=self.max_entries,
                    existing=existing_count,
                    requested=len(all_keys),
                    needed=final_count,
                )
            row_state = connection.execute("SELECT generation FROM current_state WHERE singleton=1").fetchone()
            generation = 1 if row_state is None else int(row_state[0]) + 1
            payload_json = canonical_json(computation.payload())
            payload_checksum = sha256_bytes(payload_json.encode("utf-8"))
            entries = []
            for row in computation.rows:
                row_id = row["row_id"]
                for feature_id, cache_key in zip(computation.plan.ids, computation.feature_keys[row_id]):
                    entries.append(
                        (
                            cache_key,
                            row_id,
                            feature_id,
                            canonical_json(stable_float(computation.encodings[row_id][feature_id])),
                            generation,
                        )
                    )
            connection.executemany(
                """
                INSERT INTO encoding_cache(cache_key, row_id, feature_id, value_json, generation)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(cache_key) DO UPDATE SET generation=excluded.generation
                """,
                entries,
            )
            connection.execute(
                """
                INSERT INTO selection_cache(selection_key, matrix_digest, payload_json, payload_checksum, generation)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(selection_key) DO UPDATE SET
                    generation=excluded.generation,
                    payload_json=excluded.payload_json,
                    payload_checksum=excluded.payload_checksum
                """,
                (
                    computation.selection_key,
                    computation.matrix_digest,
                    payload_json,
                    payload_checksum,
                    generation,
                ),
            )
            connection.execute(
                """
                INSERT INTO current_state(
                    singleton, generation, selection_key, matrix_digest, config_digest, input_digest
                ) VALUES (1, ?, ?, ?, ?, ?)
                ON CONFLICT(singleton) DO UPDATE SET
                    generation=excluded.generation,
                    selection_key=excluded.selection_key,
                    matrix_digest=excluded.matrix_digest,
                    config_digest=excluded.config_digest,
                    input_digest=excluded.input_digest
                """,
                (
                    generation,
                    computation.selection_key,
                    computation.matrix_digest,
                    config_digest,
                    input_digest,
                ),
            )
            connection.commit()
            return {
                "generation": generation,
                "selection_key": computation.selection_key,
                "matrix_digest": computation.matrix_digest,
                "cache_entries": final_count,
                "selection_payload_checksum": payload_checksum,
            }
        except EngineError:
            raise
        except sqlite3.OperationalError as error:
            try:
                connection.rollback()
            except Exception:
                pass
            if _is_lock_error(error):
                raise stage_busy("another writer is committing to the cache") from None
            raise
        except Exception:
            try:
                connection.rollback()
            except Exception:
                pass
            raise
        finally:
            connection.close()

    def _fetch_existing_keys_on(self, connection: sqlite3.Connection, keys: list[str]) -> set[str]:
        found: set[str] = set()
        for chunk in chunked(keys, 400):
            placeholders = ",".join("?" for _ in chunk)
            rows = connection.execute(
                f"SELECT cache_key FROM encoding_cache WHERE cache_key IN ({placeholders})",
                chunk,
            ).fetchall()
            found.update(str(row[0]) for row in rows)
        return found

    def current_state(self) -> dict[str, Any] | None:
        connection = self._connect_reader()
        try:
            row = connection.execute(
                """
                SELECT generation, selection_key, matrix_digest, config_digest, input_digest
                FROM current_state WHERE singleton=1
                """
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            return None
        return {
            "generation": int(row[0]),
            "selection_key": str(row[1]),
            "matrix_digest": str(row[2]),
            "config_digest": str(row[3]),
            "input_digest": str(row[4]),
        }

    def load_selection(self, selection_key: str | None = None) -> dict[str, Any]:
        state = self.current_state()
        if state is None:
            raise stale_result("no committed selection exists")
        requested = state["selection_key"] if selection_key is None else selection_key
        if requested != state["selection_key"]:
            raise stale_result(
                "selection key does not reference the current committed result",
                requested_selection_key=requested,
                current_selection_key=state["selection_key"],
                current_generation=state["generation"],
            )
        connection = self._connect_reader()
        try:
            row = connection.execute(
                """
                SELECT payload_json, payload_checksum, matrix_digest, generation
                FROM selection_cache WHERE selection_key=?
                """,
                (requested,),
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            raise stale_result(
                "current selection payload is missing",
                selection_key=requested,
                current_generation=state["generation"],
            )
        payload_json, checksum, matrix_digest, generation = row
        actual = sha256_bytes(str(payload_json).encode("utf-8"))
        if actual != checksum:
            raise encoding_error(
                "selection payload checksum mismatch",
                field="selection_cache.payload_json",
                selection_key=requested,
                expected=checksum,
                actual=actual,
            )
        try:
            payload = json.loads(payload_json)
        except Exception as error:
            raise encoding_error(
                "selection payload cannot be parsed",
                field="selection_cache.payload_json",
                selection_key=requested,
                error=str(error),
            ) from None
        if payload.get("selection_key") != requested:
            raise stale_result(
                "selection payload contains a different selection key",
                requested_selection_key=requested,
                payload_selection_key=payload.get("selection_key"),
            )
        if payload.get("matrix_digest") != matrix_digest:
            raise encoding_error(
                "selection payload matrix digest mismatch",
                field="selection_cache.matrix_digest",
            )
        return {
            **payload,
            "generation": int(generation),
            "payload_checksum": str(checksum),
        }

    def assert_current(self, selection_key: str) -> dict[str, Any]:
        return self.load_selection(selection_key)

    @contextmanager
    def held_stage(self, owner: str = "diagnostic-holder") -> Iterator[None]:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "INSERT OR REPLACE INTO stage_lease(name, owner, nonce) VALUES (?, ?, ?)",
                ("commit-stage", owner, sha256_json({"owner": owner})),
            )
            yield
        finally:
            try:
                connection.rollback()
            except Exception:
                pass
            connection.close()
