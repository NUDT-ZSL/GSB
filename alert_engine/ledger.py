"""SQLite-backed rule and patch ledger with per-batch transactions."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Iterable

from .codes import Code
from .errors import EngineError
from .models import PatchBatch, RulePatch, RuleVersion
from .rules import RuleCatalog, validate_generation_chain
from .util import FileLock, digest_json, stable_json


_BASELINE_COLUMNS = (
    "rule_key",
    "source_rule_key",
    "source",
    "effective_ms",
    "effective_error_ms",
    "generation",
    "dedup_window_ms",
    "window_error_ms",
    "match_strings_json",
    "enabled",
    "tombstone",
    "parent_rule_keys_json",
    "record_index",
    "shard_id",
)


class RuleLedger:
    def __init__(self, path: Path, *, lock_path: Path | None = None):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock_path = Path(lock_path or (self.path.parent / ".engine.lock"))
        self.connection = sqlite3.connect(
            self.path,
            timeout=30.0,
            isolation_level=None,
        )
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=FULL")
        self.connection.execute("PRAGMA foreign_keys=ON")
        self._ensure_schema()

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "RuleLedger":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def _ensure_schema(self) -> None:
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS engine_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS baseline_versions (
                baseline_uid TEXT PRIMARY KEY,
                rule_key TEXT NOT NULL,
                source_rule_key TEXT NOT NULL,
                source TEXT NOT NULL,
                effective_ms INTEGER NOT NULL,
                effective_error_ms INTEGER NOT NULL,
                generation INTEGER NOT NULL,
                dedup_window_ms INTEGER NOT NULL,
                window_error_ms INTEGER NOT NULL,
                match_strings_json TEXT NOT NULL,
                enabled INTEGER NOT NULL,
                tombstone INTEGER NOT NULL,
                parent_rule_keys_json TEXT NOT NULL,
                record_index INTEGER NOT NULL,
                shard_id TEXT
            );
            CREATE TABLE IF NOT EXISTS patch_batches (
                batch_id TEXT PRIMARY KEY,
                logical_ts_ms INTEGER NOT NULL,
                arrival_index INTEGER NOT NULL,
                record_index INTEGER NOT NULL,
                payload_json TEXT NOT NULL,
                payload_sha256 TEXT NOT NULL,
                applied_seq INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS patch_rules (
                batch_id TEXT NOT NULL,
                local_index INTEGER NOT NULL,
                rule_key TEXT NOT NULL,
                source_rule_key TEXT NOT NULL,
                effective_ms INTEGER NOT NULL,
                generation INTEGER NOT NULL,
                dedup_window_ms INTEGER NOT NULL,
                match_strings_json TEXT NOT NULL,
                enabled INTEGER NOT NULL,
                tombstone INTEGER NOT NULL,
                parent_rule_keys_json TEXT NOT NULL,
                PRIMARY KEY (batch_id, local_index),
                FOREIGN KEY (batch_id) REFERENCES patch_batches(batch_id)
            );
            CREATE TABLE IF NOT EXISTS checkpoints (
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                input_fingerprint TEXT NOT NULL,
                structure_version INTEGER NOT NULL,
                merge_semantics_version INTEGER NOT NULL,
                applied_watermark_ms INTEGER,
                cluster_digest TEXT NOT NULL,
                snapshot_name TEXT NOT NULL,
                snapshot_sha256 TEXT NOT NULL,
                complete INTEGER NOT NULL,
                logical_ts_ms INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_checkpoints_complete
                ON checkpoints(complete, seq DESC);
            """
        )

    def reset(self, *, keep_checkpoints: bool = False) -> None:
        with FileLock(self.lock_path):
            self.connection.execute("BEGIN IMMEDIATE")
            try:
                self.connection.execute("DELETE FROM patch_rules")
                self.connection.execute("DELETE FROM patch_batches")
                self.connection.execute("DELETE FROM baseline_versions")
                self.connection.execute(
                    "DELETE FROM engine_meta WHERE key NOT LIKE 'checkpoint.%'"
                )
                if not keep_checkpoints:
                    self.connection.execute("DELETE FROM checkpoints")
                self.connection.execute("COMMIT")
            except Exception:
                self.connection.execute("ROLLBACK")
                raise

    def seed_baseline(self, versions: Iterable[RuleVersion]) -> None:
        rows = []
        for version in versions:
            uid = digest_json(
                {
                    "source": version.source,
                    "rule_key": version.rule_key,
                    "effective_ms": version.effective_ms,
                    "generation": version.generation,
                    "record_index": version.record_index,
                    "shard_id": version.shard_id,
                }
            )
            rows.append(
                (
                    uid,
                    version.rule_key,
                    version.source_rule_key,
                    version.source,
                    version.effective_ms,
                    version.effective_error_ms,
                    version.generation,
                    version.dedup_window_ms,
                    version.window_error_ms,
                    stable_json(list(version.match_strings)),
                    1 if version.enabled else 0,
                    1 if version.tombstone else 0,
                    stable_json(list(version.parent_rule_keys)),
                    version.record_index,
                    version.shard_id,
                )
            )
        with FileLock(self.lock_path):
            self.connection.execute("BEGIN IMMEDIATE")
            try:
                self.connection.executemany(
                    """
                    INSERT OR IGNORE INTO baseline_versions (
                        baseline_uid, rule_key, source_rule_key, source,
                        effective_ms, effective_error_ms, generation,
                        dedup_window_ms, window_error_ms, match_strings_json,
                        enabled, tombstone, parent_rule_keys_json, record_index,
                        shard_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    rows,
                )
                self.connection.execute(
                    """
                    INSERT INTO engine_meta(key, value)
                    VALUES('baseline_seeded', '1')
                    ON CONFLICT(key) DO UPDATE SET value=excluded.value
                    """
                )
                self.connection.execute("COMMIT")
            except Exception:
                self.connection.execute("ROLLBACK")
                raise

    def baseline_versions(self) -> list[RuleVersion]:
        cursor = self.connection.execute(
            """
            SELECT * FROM baseline_versions
             ORDER BY rule_key, effective_ms, generation, source, record_index
            """
        )
        result: list[RuleVersion] = []
        for row in cursor:
            result.append(
                RuleVersion(
                    rule_key=row["rule_key"],
                    source_rule_key=row["source_rule_key"],
                    source=row["source"],
                    effective_ms=row["effective_ms"],
                    effective_error_ms=row["effective_error_ms"],
                    generation=row["generation"],
                    dedup_window_ms=row["dedup_window_ms"],
                    window_error_ms=row["window_error_ms"],
                    match_strings=tuple(json.loads(row["match_strings_json"])),
                    enabled=bool(row["enabled"]),
                    tombstone=bool(row["tombstone"]),
                    parent_rule_keys=tuple(
                        json.loads(row["parent_rule_keys_json"])
                    ),
                    record_index=row["record_index"],
                    shard_id=row["shard_id"],
                )
            )
        return result

    def applied_batches(self) -> list[PatchBatch]:
        cursor = self.connection.execute(
            """
            SELECT batch_id, logical_ts_ms, record_index, arrival_index,
                   payload_json
              FROM patch_batches
             ORDER BY applied_seq
            """
        )
        result: list[PatchBatch] = []
        for row in cursor:
            payload = json.loads(row["payload_json"])
            rules = tuple(
                RulePatch(
                    rule_key=item["rule_key"],
                    source_rule_key=item["source_rule_key"],
                    effective_ms=item["effective_ms"],
                    generation=item["generation"],
                    dedup_window_ms=item["dedup_window_ms"],
                    match_strings=tuple(item["match_strings"]),
                    enabled=bool(item["enabled"]),
                    tombstone=bool(item["tombstone"]),
                    parent_rule_keys=tuple(item["parent_rule_keys"]),
                    record_index=item["record_index"],
                )
                for item in payload["rules"]
            )
            result.append(
                PatchBatch(
                    batch_id=row["batch_id"],
                    logical_ts_ms=row["logical_ts_ms"],
                    rules=rules,
                    record_index=row["record_index"],
                    arrival_index=row["arrival_index"],
                )
            )
        return result

    def applied_batch_ids(self) -> set[str]:
        cursor = self.connection.execute("SELECT batch_id FROM patch_batches")
        return {row["batch_id"] for row in cursor}

    def applied_watermark_ms(self) -> int | None:
        row = self.connection.execute(
            "SELECT MAX(logical_ts_ms) AS watermark FROM patch_batches"
        ).fetchone()
        return None if row is None else row["watermark"]

    def patch_rejection_records(self) -> list[dict]:
        row = self.connection.execute(
            "SELECT value FROM engine_meta WHERE key='patch_rejections'"
        ).fetchone()
        if row is None:
            return []
        try:
            value = json.loads(row["value"])
        except json.JSONDecodeError:
            return []
        return value if isinstance(value, list) else []

    def append_patch_rejection(self, rejection: dict) -> None:
        rejections = self.patch_rejection_records()
        rejections.append(rejection)
        with FileLock(self.lock_path):
            self.connection.execute("BEGIN IMMEDIATE")
            try:
                self.connection.execute(
                    """
                    INSERT INTO engine_meta(key, value)
                    VALUES('patch_rejections', ?)
                    ON CONFLICT(key) DO UPDATE SET value=excluded.value
                    """,
                    (stable_json(rejections),),
                )
                self.connection.execute("COMMIT")
            except Exception:
                self.connection.execute("ROLLBACK")
                raise

    def apply_patch_batch(self, batch: PatchBatch) -> None:
        """Apply one complete batch or leave every ledger table untouched."""

        with FileLock(self.lock_path):
            self.connection.execute("BEGIN IMMEDIATE")
            try:
                duplicate = self.connection.execute(
                    "SELECT 1 FROM patch_batches WHERE batch_id=?",
                    (batch.batch_id,),
                ).fetchone()
                if duplicate is not None:
                    raise EngineError(
                        Code.E_PATCH_DUP,
                        "patch batch was already applied",
                        details={"batch_id": batch.batch_id},
                    )

                previous_batches = self.applied_batches()
                candidate_batches = previous_batches + [batch]
                baseline = self.baseline_versions()
                candidate_catalog = RuleCatalog(
                    v1_versions=[
                        item for item in baseline if item.source == "v1"
                    ],
                    v2_versions=[
                        item for item in baseline if item.source == "v2"
                    ],
                    patch_batches=candidate_batches,
                    observation_clock_ms=_observation_clock_from_meta(
                        self.connection
                    ),
                )
                previous_catalog = RuleCatalog(
                    v1_versions=[
                        item for item in baseline if item.source == "v1"
                    ],
                    v2_versions=[
                        item for item in baseline if item.source == "v2"
                    ],
                    patch_batches=previous_batches,
                    observation_clock_ms=_observation_clock_from_meta(
                        self.connection
                    ),
                )
                before = len(previous_catalog.chain_issues)
                after = len(candidate_catalog.chain_issues)
                if after > before:
                    raise EngineError(
                        Code.E_PATCH_CHAIN,
                        "patch breaks rule generation monotonicity",
                        details={
                            "batch_id": batch.batch_id,
                            "new_issues": candidate_catalog.chain_issues[
                                before:
                            ],
                        },
                    )

                payload = {
                    "batch_id": batch.batch_id,
                    "logical_ts_ms": batch.logical_ts_ms,
                    "rules": [
                        {
                            "rule_key": rule.rule_key,
                            "source_rule_key": rule.source_rule_key,
                            "effective_ms": rule.effective_ms,
                            "generation": rule.generation,
                            "dedup_window_ms": rule.dedup_window_ms,
                            "match_strings": list(rule.match_strings),
                            "enabled": rule.enabled,
                            "tombstone": rule.tombstone,
                            "parent_rule_keys": list(rule.parent_rule_keys),
                            "record_index": rule.record_index,
                        }
                        for rule in batch.rules
                    ],
                }
                payload_json = stable_json(payload)
                next_seq_row = self.connection.execute(
                    """
                    SELECT COALESCE(MAX(applied_seq), 0) + 1 AS value
                      FROM patch_batches
                    """
                ).fetchone()
                self.connection.execute(
                    """
                    INSERT INTO patch_batches(
                        batch_id, logical_ts_ms, arrival_index, record_index,
                        payload_json, payload_sha256, applied_seq
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        batch.batch_id,
                        batch.logical_ts_ms,
                        batch.arrival_index,
                        batch.record_index,
                        payload_json,
                        digest_json(payload),
                        next_seq_row["value"],
                    ),
                )
                self.connection.executemany(
                    """
                    INSERT INTO patch_rules(
                        batch_id, local_index, rule_key, source_rule_key,
                        effective_ms, generation, dedup_window_ms,
                        match_strings_json, enabled, tombstone,
                        parent_rule_keys_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            batch.batch_id,
                            rule.record_index,
                            rule.rule_key,
                            rule.source_rule_key,
                            rule.effective_ms,
                            rule.generation,
                            rule.dedup_window_ms,
                            stable_json(list(rule.match_strings)),
                            1 if rule.enabled else 0,
                            1 if rule.tombstone else 0,
                            stable_json(list(rule.parent_rule_keys)),
                        )
                        for rule in batch.rules
                    ],
                )
                self.connection.execute("COMMIT")
            except Exception:
                self.connection.execute("ROLLBACK")
                raise

    def set_meta(self, key: str, value: object) -> None:
        with FileLock(self.lock_path):
            self.connection.execute("BEGIN IMMEDIATE")
            try:
                self.connection.execute(
                    """
                    INSERT INTO engine_meta(key, value)
                    VALUES(?, ?)
                    ON CONFLICT(key) DO UPDATE SET value=excluded.value
                    """,
                    (key, stable_json(value)),
                )
                self.connection.execute("COMMIT")
            except Exception:
                self.connection.execute("ROLLBACK")
                raise

    def get_meta(self, key: str) -> object | None:
        row = self.connection.execute(
            "SELECT value FROM engine_meta WHERE key=?", (key,)
        ).fetchone()
        if row is None:
            return None
        try:
            return json.loads(row["value"])
        except json.JSONDecodeError:
            return row["value"]


def build_catalog_from_ledger(
    ledger: RuleLedger,
    *,
    observation_clock_ms: int,
    batches: Iterable[PatchBatch] | None = None,
) -> RuleCatalog:
    baseline = ledger.baseline_versions()
    return RuleCatalog(
        v1_versions=[item for item in baseline if item.source == "v1"],
        v2_versions=[item for item in baseline if item.source == "v2"],
        patch_batches=ledger.applied_batches() if batches is None else list(batches),
        observation_clock_ms=observation_clock_ms,
    )


def _observation_clock_from_meta(connection: sqlite3.Connection) -> int:
    row = connection.execute(
        "SELECT value FROM engine_meta WHERE key='observation_clock_ms'"
    ).fetchone()
    if row is None:
        return 0
    try:
        value = json.loads(row["value"])
    except json.JSONDecodeError:
        return 0
    return int(value) if isinstance(value, int) and not isinstance(value, bool) else 0
