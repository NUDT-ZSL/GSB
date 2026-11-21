from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

from .canonical import canonical_json, sha256_digest
from .errors import EXIT_INTERNAL, EXIT_VERIFY, BusinessError, diagnostic
from .merge import difference_fields, equivalent_observations
from .models import InstanceSpec, InstanceState, ParsedShard, STATES
from .validation import ParsedRegistry


FORMAT_VERSION = 1
ENGINE_VERSION = "1.0.0"


def _b(value: bool) -> int:
    return 1 if value else 0


def _loads(value: Any, default: Any) -> Any:
    if value is None:
        return default
    if isinstance(value, (dict, list, tuple)):
        return value
    try:
        return json.loads(str(value))
    except (TypeError, ValueError):
        return default


@dataclass
class LedgerData:
    instances: dict[str, dict[str, Any]] = field(default_factory=dict)
    shards: dict[str, dict[str, Any]] = field(default_factory=dict)
    observations: dict[tuple[str, str, int, str], dict[str, Any]] = field(
        default_factory=dict
    )
    history: dict[tuple[str, int], dict[str, Any]] = field(default_factory=dict)
    diagnostics: dict[str, dict[str, Any]] = field(default_factory=dict)


def _observation_key(row: dict[str, Any]) -> tuple[str, str, int, str]:
    return (
        str(row["shard_id"]),
        str(row["instance_id"]),
        int(row["obs_seq"]),
        str(row["digest"]),
    )


def _diagnostic_id(payload: dict[str, Any]) -> str:
    stable = {
        key: value
        for key, value in payload.items()
        if key not in {"arrival_a", "arrival_b", "first_seen", "message"}
    }
    digests = sorted(
        [
            str(value)
            for value in (stable.get("digest_a"), stable.get("digest_b"))
            if value
        ]
    )
    if "digest_a" in stable:
        stable["digest_a"] = digests[0] if digests else None
    if "digest_b" in stable:
        stable["digest_b"] = digests[1] if len(digests) > 1 else None
    if "details" in stable and isinstance(stable["details"], dict):
        stable["details"] = {
            key: value
            for key, value in stable["details"].items()
            if key not in {"arrival_order", "record_index"}
        }
        if isinstance(stable["details"].get("schema_versions"), list):
            stable["details"]["schema_versions"] = sorted(
                stable["details"]["schema_versions"]
            )
        if isinstance(stable["details"].get("difference_fields"), list):
            stable["details"]["difference_fields"] = sorted(
                set(stable["details"]["difference_fields"])
            )
        if isinstance(stable["details"].get("raw_digests"), list):
            stable["details"]["raw_digests"] = sorted(
                set(stable["details"]["raw_digests"])
            )
    return sha256_digest(
        {"domain": "probe-ledger-diagnostic-v1", "payload": stable}
    )


class LedgerStore:
    def __init__(
        self,
        workspace: str | os.PathLike[str],
        *,
        db_path: str | os.PathLike[str] | None = None,
        ledger_path: str | os.PathLike[str] | None = None,
        diagnostics_path: str | os.PathLike[str] | None = None,
    ) -> None:
        self.workspace = Path(workspace)
        self.db_path = Path(db_path) if db_path else self.workspace / "ledger.sqlite3"
        self.ledger_path = (
            Path(ledger_path) if ledger_path else self.workspace / "ledger.json"
        )
        self.diagnostics_path = (
            Path(diagnostics_path)
            if diagnostics_path
            else self.workspace / "diagnostics.json"
        )
        self.conn: sqlite3.Connection | None = None

    @property
    def exists(self) -> bool:
        return self.db_path.exists()

    def open(self) -> "LedgerStore":
        if self.conn is not None:
            return self
        if not self.db_path.exists():
            raise BusinessError(
                "E_NO_LEDGER",
                "账本数据库不存在，请先执行 load 加载注册清单",
                {"db": str(self.db_path)},
                EXIT_VERIFY,
            )
        self.conn = sqlite3.connect(
            str(self.db_path),
            timeout=30.0,
            isolation_level=None,
        )
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=FULL")
        self.conn.execute("PRAGMA busy_timeout=30000")
        self._create_schema()
        return self

    def close(self) -> None:
        if self.conn is not None:
            self.conn.close()
            self.conn = None

    def __enter__(self) -> "LedgerStore":
        return self.open()

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()

    def _create_schema(self) -> None:
        assert self.conn is not None
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS batches (
                batch_id INTEGER PRIMARY KEY,
                kind TEXT NOT NULL,
                parent_batch_id INTEGER,
                pointer_version INTEGER NOT NULL,
                state_digest TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS pointer (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                batch_id INTEGER NOT NULL,
                pointer_version INTEGER NOT NULL,
                state_digest TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS instances (
                batch_id INTEGER NOT NULL,
                instance_id TEXT NOT NULL,
                service_name TEXT NOT NULL,
                config_json TEXT NOT NULL,
                status TEXT NOT NULL,
                state_generation INTEGER NOT NULL,
                consecutive_failures INTEGER NOT NULL,
                lease_remaining INTEGER NOT NULL,
                backoff_level INTEGER NOT NULL,
                backoff_ms INTEGER NOT NULL,
                watermark INTEGER NOT NULL,
                last_basis_json TEXT,
                version_conflict INTEGER NOT NULL,
                review_required INTEGER NOT NULL,
                PRIMARY KEY (batch_id, instance_id)
            );
            CREATE TABLE IF NOT EXISTS shards (
                batch_id INTEGER NOT NULL,
                shard_id TEXT NOT NULL,
                content_digest TEXT NOT NULL,
                stream_id TEXT NOT NULL,
                shard_index INTEGER,
                shard_count INTEGER,
                expected_shard_ids_json TEXT NOT NULL,
                file_name TEXT NOT NULL,
                coverage_json TEXT NOT NULL,
                observation_count INTEGER NOT NULL,
                PRIMARY KEY (batch_id, shard_id)
            );
            CREATE TABLE IF NOT EXISTS observations (
                batch_id INTEGER NOT NULL,
                shard_id TEXT NOT NULL,
                instance_id TEXT NOT NULL,
                obs_seq INTEGER NOT NULL,
                digest TEXT NOT NULL,
                schema_version INTEGER NOT NULL,
                logical_clock_ms INTEGER NOT NULL,
                observed_at_ms INTEGER NOT NULL,
                status_codes_json TEXT NOT NULL,
                latency_summary_ms REAL NOT NULL,
                latency_histogram_json TEXT,
                time_conversion_json TEXT,
                canonical_json TEXT NOT NULL,
                semantic_digest TEXT NOT NULL,
                outcome TEXT NOT NULL,
                arrival_order INTEGER NOT NULL,
                applied INTEGER NOT NULL,
                PRIMARY KEY (batch_id, shard_id, instance_id, obs_seq, digest)
            );
            CREATE INDEX IF NOT EXISTS idx_observations_batch_instance_seq
                ON observations(batch_id, instance_id, obs_seq);
            CREATE TABLE IF NOT EXISTS history (
                batch_id INTEGER NOT NULL,
                instance_id TEXT NOT NULL,
                generation INTEGER NOT NULL,
                status TEXT NOT NULL,
                consecutive_failures INTEGER NOT NULL,
                lease_remaining INTEGER NOT NULL,
                backoff_level INTEGER NOT NULL,
                backoff_ms INTEGER NOT NULL,
                basis_kind TEXT NOT NULL,
                obs_seq INTEGER NOT NULL,
                basis_digest TEXT NOT NULL,
                outcome TEXT NOT NULL,
                logical_clock_ms INTEGER NOT NULL,
                PRIMARY KEY (batch_id, instance_id, generation)
            );
            CREATE TABLE IF NOT EXISTS diagnostics (
                batch_id INTEGER NOT NULL,
                diag_id TEXT NOT NULL,
                code TEXT NOT NULL,
                message TEXT NOT NULL,
                instance_id TEXT,
                obs_seq INTEGER,
                shard_id TEXT,
                digest_a TEXT,
                digest_b TEXT,
                arrival_a INTEGER,
                arrival_b INTEGER,
                details_json TEXT NOT NULL,
                PRIMARY KEY (batch_id, diag_id)
            );
            """
        )

    def _meta(self, key: str) -> str | None:
        assert self.conn is not None
        row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return None if row is None else str(row["value"])

    def _manifest_digest(self) -> str | None:
        return self._meta("manifest_digest")

    def _pointer(self) -> dict[str, Any] | None:
        assert self.conn is not None
        row = self.conn.execute("SELECT * FROM pointer WHERE id=1").fetchone()
        return None if row is None else dict(row)

    def _load_data(self) -> LedgerData:
        assert self.conn is not None
        pointer = self._pointer()
        data = LedgerData()
        if pointer is None:
            return data
        batch_id = int(pointer["batch_id"])
        for row in self.conn.execute(
            "SELECT * FROM instances WHERE batch_id=? ORDER BY instance_id",
            (batch_id,),
        ):
            item = dict(row)
            item["config"] = _loads(item.pop("config_json"), {})
            item["last_basis"] = _loads(item.pop("last_basis_json"), None)
            item["version_conflict"] = bool(item["version_conflict"])
            item["review_required"] = bool(item["review_required"])
            data.instances[str(item["instance_id"])] = item
        for row in self.conn.execute(
            "SELECT * FROM shards WHERE batch_id=? ORDER BY shard_id",
            (batch_id,),
        ):
            item = dict(row)
            item["expected_shard_ids"] = _loads(
                item.pop("expected_shard_ids_json"), []
            )
            item["coverage"] = _loads(item.pop("coverage_json"), [])
            data.shards[str(item["shard_id"])] = item
        for row in self.conn.execute(
            "SELECT * FROM observations WHERE batch_id=? "
            "ORDER BY instance_id, obs_seq, logical_clock_ms, digest, shard_id",
            (batch_id,),
        ):
            item = dict(row)
            item["status_codes"] = _loads(item.pop("status_codes_json"), [])
            item["latency_histogram"] = _loads(
                item.pop("latency_histogram_json"), None
            )
            item["time_conversion"] = _loads(
                item.pop("time_conversion_json"), None
            )
            item["canonical"] = _loads(item.pop("canonical_json"), {})
            item["applied"] = bool(item["applied"])
            data.observations[_observation_key(item)] = item
        for row in self.conn.execute(
            "SELECT * FROM history WHERE batch_id=? "
            "ORDER BY instance_id, generation",
            (batch_id,),
        ):
            item = dict(row)
            data.history[(str(item["instance_id"]), int(item["generation"]))] = item
        for row in self.conn.execute(
            "SELECT * FROM diagnostics WHERE batch_id=? ORDER BY diag_id",
            (batch_id,),
        ):
            item = dict(row)
            item["details"] = _loads(item.pop("details_json"), {})
            data.diagnostics[str(item["diag_id"])] = item
        return data

    def _add_diagnostic(
        self,
        data: LedgerData,
        payload: dict[str, Any],
    ) -> str:
        diag_id = _diagnostic_id(payload)
        if diag_id in data.diagnostics:
            return diag_id
        item = {
            "diag_id": diag_id,
            "code": str(payload.get("code", "E_DIAGNOSTIC")),
            "message": str(payload.get("message", "")),
            "instance_id": payload.get("instance_id"),
            "obs_seq": payload.get("obs_seq"),
            "shard_id": payload.get("shard_id"),
            "digest_a": payload.get("digest_a"),
            "digest_b": payload.get("digest_b"),
            "arrival_a": payload.get("arrival_a"),
            "arrival_b": payload.get("arrival_b"),
            "details": {
                key: value
                for key, value in payload.items()
                if key
                not in {
                    "code",
                    "message",
                    "instance_id",
                    "obs_seq",
                    "shard_id",
                    "digest_a",
                    "digest_b",
                    "arrival_a",
                    "arrival_b",
                }
            },
        }
        data.diagnostics[diag_id] = item
        return diag_id

    def _snapshot(
        self,
        data: LedgerData,
        manifest_digest: str,
    ) -> tuple[dict[str, Any], bytes, str]:
        histories: dict[str, list[dict[str, Any]]] = {}
        for (instance_id, generation), row in data.history.items():
            histories.setdefault(instance_id, []).append(
                {
                    "generation": int(generation),
                    "status": str(row["status"]),
                    "consecutive_failures": int(row["consecutive_failures"]),
                    "lease_remaining": int(row["lease_remaining"]),
                    "backoff_level": int(row["backoff_level"]),
                    "backoff_ms": int(row["backoff_ms"]),
                    "basis_kind": str(row["basis_kind"]),
                    "obs_seq": int(row["obs_seq"]),
                    "digest": str(row["basis_digest"]),
                    "outcome": str(row["outcome"]),
                    "logical_clock_ms": int(row["logical_clock_ms"]),
                }
            )
        for rows in histories.values():
            rows.sort(key=lambda item: int(item["generation"]))

        instances: list[dict[str, Any]] = []
        for instance_id in sorted(data.instances):
            row = data.instances[instance_id]
            config = row["config"]
            instances.append(
                {
                    "instance_id": instance_id,
                    "service_name": str(row["service_name"]),
                    "endpoint": config.get("endpoint"),
                    "expected_status_codes": sorted(
                        config.get("expected_status_codes", [])
                    ),
                    "serialization_baseline": config.get("serialization_baseline"),
                    "status": str(row["status"]),
                    "state_generation": int(row["state_generation"]),
                    "consecutive_failures": int(row["consecutive_failures"]),
                    "lease_remaining": int(row["lease_remaining"]),
                    "backoff_level": int(row["backoff_level"]),
                    "backoff_ms": int(row["backoff_ms"]),
                    "watermark": int(row["watermark"]),
                    "last_basis": row["last_basis"],
                    "version_conflict": bool(row["version_conflict"]),
                    "review_required": bool(row["review_required"]),
                    "history": histories.get(instance_id, []),
                }
            )

        shards: list[dict[str, Any]] = []
        for shard_id in sorted(data.shards):
            row = data.shards[shard_id]
            shards.append(
                {
                    "shard_id": shard_id,
                    "content_digest": str(row["content_digest"]),
                    "stream_id": str(row["stream_id"]),
                    "shard_index": row["shard_index"],
                    "shard_count": row["shard_count"],
                    "expected_shard_ids": sorted(row["expected_shard_ids"]),
                    "file_name": str(row["file_name"]),
                    "coverage": sorted(
                        row["coverage"],
                        key=lambda item: (
                            str(item.get("instance_id")),
                            int(item.get("min_seq", 0)),
                        ),
                    ),
                    "observation_count": int(row["observation_count"]),
                }
            )
        shards.sort(
            key=lambda item: (
                str(item["stream_id"]),
                -1 if item["shard_index"] is None else int(item["shard_index"]),
                str(item["shard_id"]),
            )
        )

        buffer_entries: list[dict[str, Any]] = []
        for row in data.observations.values():
            if row["applied"]:
                continue
            entry = {
                "instance_id": str(row["instance_id"]),
                "obs_seq": int(row["obs_seq"]),
                "schema_version": int(row["schema_version"]),
                "logical_clock_ms": int(row["logical_clock_ms"]),
                "observed_at_ms": int(row["observed_at_ms"]),
                "status_codes": sorted(row["status_codes"]),
                "latency_summary_ms": float(row["latency_summary_ms"]),
                "digest": str(row["digest"]),
                "semantic_digest": str(row["semantic_digest"]),
                "outcome": str(row["outcome"]),
                "shard_id": str(row["shard_id"]),
            }
            if row.get("latency_histogram") is not None:
                entry["latency_histogram"] = row["latency_histogram"]
            if row.get("time_conversion") is not None:
                entry["time_conversion"] = row["time_conversion"]
            buffer_entries.append(entry)
        buffer_entries.sort(
            key=lambda item: (
                item["instance_id"],
                item["obs_seq"],
                item["logical_clock_ms"],
                item["digest"],
                item["shard_id"],
            )
        )
        buffer_digest = sha256_digest(buffer_entries)

        conversions_by_key: dict[tuple[str, int, str], dict[str, Any]] = {}
        for row in data.observations.values():
            if int(row["schema_version"]) != 1 or row.get("time_conversion") is None:
                continue
            key = (
                str(row["instance_id"]),
                int(row["obs_seq"]),
                str(row["digest"]),
            )
            conversions_by_key[key] = {
                "instance_id": key[0],
                "obs_seq": key[1],
                "digest": key[2],
                "logical_clock_ms": int(row["logical_clock_ms"]),
                "time_conversion": row["time_conversion"],
            }
        time_conversions = [
            conversions_by_key[key] for key in sorted(conversions_by_key)
        ]
        time_conversions_digest = sha256_digest(time_conversions)

        diagnostics: list[dict[str, Any]] = []
        for diag_id in sorted(data.diagnostics):
            row = data.diagnostics[diag_id]
            details = row["details"]
            if isinstance(details, dict):
                details = dict(details)
                if isinstance(details.get("difference_fields"), list):
                    details["difference_fields"] = sorted(
                        set(details["difference_fields"])
                    )
            diagnostics.append(
                {
                    "id": diag_id,
                    "code": str(row["code"]),
                    "instance_id": row["instance_id"],
                    "obs_seq": row["obs_seq"],
                    "shard_id": row["shard_id"],
                    "digests": sorted(
                        [
                            str(value)
                            for value in (row["digest_a"], row["digest_b"])
                            if value
                        ]
                    ),
                    "details": details,
                }
            )
        diagnostics.sort(
            key=lambda item: (
                item["code"],
                item["instance_id"] or "",
                -1 if item["obs_seq"] is None else int(item["obs_seq"]),
                item["id"],
            )
        )

        pointer_version = sum(len(rows) for rows in histories.values())
        shard_registry_digest = sha256_digest(shards)
        diagnostics_digest = sha256_digest(diagnostics)
        instances_digest = sha256_digest(instances)
        snapshot = {
            "format_version": FORMAT_VERSION,
            "engine_version": ENGINE_VERSION,
            "manifest_digest": manifest_digest,
            "pointer_version": pointer_version,
            "instances_digest": instances_digest,
            "shard_registry_digest": shard_registry_digest,
            "diagnostics_digest": diagnostics_digest,
            "time_conversions_digest": time_conversions_digest,
            "time_conversions": time_conversions,
            "buffer": {
                "digest": buffer_digest,
                "count": len(buffer_entries),
                "entries": buffer_entries,
            },
            "instances": instances,
            "shards": shards,
            "diagnostics": diagnostics,
        }
        raw = (canonical_json(snapshot) + "\n").encode("utf-8")
        return snapshot, raw, sha256_digest(raw)

    def _write_atomic(self, path: Path, payload: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
        with open(tmp, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        try:
            directory_fd = os.open(str(path.parent), os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            pass

    def _diagnostics_payload(self, data: LedgerData) -> bytes:
        rows = sorted(
            data.diagnostics.values(),
            key=lambda item: (
                str(item["code"]),
                str(item.get("instance_id") or ""),
                -1 if item.get("obs_seq") is None else int(item["obs_seq"]),
                str(item["diag_id"]),
            ),
        )
        payload = {
            "format_version": FORMAT_VERSION,
            "diagnostics": [
                {
                    "id": row["diag_id"],
                    "code": row["code"],
                    "message": row["message"],
                    "instance_id": row["instance_id"],
                    "obs_seq": row["obs_seq"],
                    "shard_id": row["shard_id"],
                    "digest_a": row["digest_a"],
                    "digest_b": row["digest_b"],
                    "arrival_a": row["arrival_a"],
                    "arrival_b": row["arrival_b"],
                    "details": row["details"],
                }
                for row in rows
            ],
        }
        return (canonical_json(payload) + "\n").encode("utf-8")

    def _create_temp_tables(self) -> None:
        assert self.conn is not None
        for table in (
            "instances",
            "shards",
            "observations",
            "history",
            "diagnostics",
        ):
            self.conn.execute(f"DROP TABLE IF EXISTS temp.stage_{table}")
            self.conn.execute(
                f"CREATE TEMP TABLE stage_{table} AS SELECT * FROM {table} WHERE 0"
            )

    def _fill_temp_tables(self, data: LedgerData) -> None:
        assert self.conn is not None
        self.conn.executemany(
            "INSERT INTO temp.stage_instances VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                (
                    str(row["instance_id"]),
                    str(row["instance_id"]),
                    str(row["service_name"]),
                    canonical_json(row["config"]),
                    str(row["status"]),
                    int(row["state_generation"]),
                    int(row["consecutive_failures"]),
                    int(row["lease_remaining"]),
                    int(row["backoff_level"]),
                    int(row["backoff_ms"]),
                    int(row["watermark"]),
                    None
                    if row.get("last_basis") is None
                    else canonical_json(row["last_basis"]),
                    _b(bool(row["version_conflict"])),
                    _b(bool(row["review_required"])),
                )
                for row in data.instances.values()
            ],
        )
        self.conn.executemany(
            "INSERT INTO temp.stage_shards VALUES (?,?,?,?,?,?,?,?,?,?)",
            [
                (
                    str(row["shard_id"]),
                    str(row["shard_id"]),
                    str(row["content_digest"]),
                    str(row["stream_id"]),
                    row["shard_index"],
                    row["shard_count"],
                    canonical_json(row["expected_shard_ids"]),
                    str(row["file_name"]),
                    canonical_json(row["coverage"]),
                    int(row["observation_count"]),
                )
                for row in data.shards.values()
            ],
        )
        self.conn.executemany(
            "INSERT INTO temp.stage_observations VALUES "
            "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                (
                    str(row["shard_id"]),
                    str(row["shard_id"]),
                    str(row["instance_id"]),
                    int(row["obs_seq"]),
                    str(row["digest"]),
                    int(row["schema_version"]),
                    int(row["logical_clock_ms"]),
                    int(row["observed_at_ms"]),
                    canonical_json(row["status_codes"]),
                    float(row["latency_summary_ms"]),
                    None
                    if row.get("latency_histogram") is None
                    else canonical_json(row["latency_histogram"]),
                    None
                    if row.get("time_conversion") is None
                    else canonical_json(row["time_conversion"]),
                    canonical_json(row["canonical"]),
                    str(row["semantic_digest"]),
                    str(row["outcome"]),
                    int(row["arrival_order"]),
                    _b(bool(row["applied"])),
                )
                for row in data.observations.values()
            ],
        )
        self.conn.executemany(
            "INSERT INTO temp.stage_history VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                (
                    str(row["instance_id"]),
                    str(row["instance_id"]),
                    int(row["generation"]),
                    str(row["status"]),
                    int(row["consecutive_failures"]),
                    int(row["lease_remaining"]),
                    int(row["backoff_level"]),
                    int(row["backoff_ms"]),
                    str(row["basis_kind"]),
                    int(row["obs_seq"]),
                    str(row["basis_digest"]),
                    str(row["outcome"]),
                    int(row["logical_clock_ms"]),
                )
                for row in data.history.values()
            ],
        )
        self.conn.executemany(
            "INSERT INTO temp.stage_diagnostics VALUES "
            "(?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                (
                    str(row["diag_id"]),
                    str(row["diag_id"]),
                    str(row["code"]),
                    str(row["message"]),
                    row.get("instance_id"),
                    row.get("obs_seq"),
                    row.get("shard_id"),
                    row.get("digest_a"),
                    row.get("digest_b"),
                    row.get("arrival_a"),
                    row.get("arrival_b"),
                    canonical_json(row["details"]),
                )
                for row in data.diagnostics.values()
            ],
        )

    def _commit(
        self,
        kind: str,
        data: LedgerData,
        *,
        meta_updates: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        assert self.conn is not None
        meta_updates = dict(meta_updates or {})
        manifest_digest = meta_updates.get("manifest_digest") or self._manifest_digest()
        if not manifest_digest:
            raise BusinessError(
                "E_NO_MANIFEST",
                "账本尚未写入注册清单摘要",
                {},
                EXIT_INTERNAL,
            )
        snapshot, raw_snapshot, state_digest = self._snapshot(data, manifest_digest)
        diagnostics_payload = self._diagnostics_payload(data)
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            pointer = self._pointer()
            parent_batch = None if pointer is None else int(pointer["batch_id"])
            batch_id = int(
                self.conn.execute(
                    "SELECT COALESCE(MAX(batch_id), 0) + 1 FROM batches"
                ).fetchone()[0]
            )
            for key, value in sorted(meta_updates.items()):
                self.conn.execute(
                    "INSERT INTO meta(key, value) VALUES (?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (key, value),
                )
            self._create_temp_tables()
            self._fill_temp_tables(data)
            self.conn.execute(
                "INSERT INTO instances "
                "SELECT ?, instance_id, service_name, config_json, status, "
                "state_generation, consecutive_failures, lease_remaining, "
                "backoff_level, backoff_ms, watermark, last_basis_json, "
                "version_conflict, review_required FROM temp.stage_instances",
                (batch_id,),
            )
            self.conn.execute(
                "INSERT INTO shards "
                "SELECT ?, shard_id, content_digest, stream_id, shard_index, "
                "shard_count, expected_shard_ids_json, file_name, coverage_json, "
                "observation_count FROM temp.stage_shards",
                (batch_id,),
            )
            self.conn.execute(
                "INSERT INTO observations "
                "SELECT ?, shard_id, instance_id, obs_seq, digest, schema_version, "
                "logical_clock_ms, observed_at_ms, status_codes_json, "
                "latency_summary_ms, latency_histogram_json, time_conversion_json, "
                "canonical_json, semantic_digest, outcome, arrival_order, applied "
                "FROM temp.stage_observations",
                (batch_id,),
            )
            self.conn.execute(
                "INSERT INTO history "
                "SELECT ?, instance_id, generation, status, consecutive_failures, "
                "lease_remaining, backoff_level, backoff_ms, basis_kind, obs_seq, "
                "basis_digest, outcome, logical_clock_ms FROM temp.stage_history",
                (batch_id,),
            )
            self.conn.execute(
                "INSERT INTO diagnostics "
                "SELECT ?, diag_id, code, message, instance_id, obs_seq, shard_id, "
                "digest_a, digest_b, arrival_a, arrival_b, details_json "
                "FROM temp.stage_diagnostics",
                (batch_id,),
            )
            pointer_version = int(snapshot["pointer_version"])
            self.conn.execute(
                "INSERT INTO batches"
                "(batch_id, kind, parent_batch_id, pointer_version, state_digest) "
                "VALUES (?,?,?,?,?)",
                (batch_id, kind, parent_batch, pointer_version, state_digest),
            )
            self.conn.execute(
                "INSERT INTO pointer(id, batch_id, pointer_version, state_digest) "
                "VALUES (1,?,?,?) ON CONFLICT(id) DO UPDATE SET "
                "batch_id=excluded.batch_id, "
                "pointer_version=excluded.pointer_version, "
                "state_digest=excluded.state_digest",
                (batch_id, pointer_version, state_digest),
            )
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        self._write_atomic(self.ledger_path, raw_snapshot)
        self._write_atomic(self.diagnostics_path, diagnostics_payload)
        return {
            "batch_id": batch_id,
            "pointer_version": int(snapshot["pointer_version"]),
            "state_digest": state_digest,
            "snapshot": snapshot,
        }

    def initialize_registry(self, registry: ParsedRegistry) -> dict[str, Any]:
        if self.conn is None and self.db_path.exists():
            self.open()
        existing = self._manifest_digest() if self.conn is not None else None
        if existing is not None:
            if existing != registry.digest:
                raise BusinessError(
                    "E_MANIFEST_CONFLICT",
                    "该账本已绑定另一份注册清单，拒绝覆盖",
                    {
                        "existing_manifest_digest": existing,
                        "incoming_manifest_digest": registry.digest,
                    },
                    EXIT_VERIFY,
                )
            return {"created": False, "manifest_digest": registry.digest}

        if not self.db_path.parent.exists():
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
        if self.conn is None:
            self.conn = sqlite3.connect(
                str(self.db_path),
                timeout=30.0,
                isolation_level=None,
            )
            self.conn.row_factory = sqlite3.Row
            self.conn.execute("PRAGMA foreign_keys=ON")
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA synchronous=FULL")
            self.conn.execute("PRAGMA busy_timeout=30000")
            self._create_schema()

        data = LedgerData()
        for instance_id, spec in sorted(registry.instances.items()):
            data.instances[instance_id] = {
                "instance_id": instance_id,
                "service_name": spec.service_name,
                "config": spec.to_config(),
                "status": spec.initial_state,
                "state_generation": spec.state_generation,
                "consecutive_failures": 0,
                "lease_remaining": spec.lease_generation,
                "backoff_level": 0,
                "backoff_ms": 0,
                "watermark": 0,
                "last_basis": None,
                "version_conflict": False,
                "review_required": False,
            }
        result = self._commit(
            "init_registry",
            data,
            meta_updates={
                "format_version": str(FORMAT_VERSION),
                "engine_version": ENGINE_VERSION,
                "manifest_digest": registry.digest,
                "manifest_json": canonical_json(registry.manifest),
            },
        )
        return {"created": True, **result}

    def load_shard(self, shard: ParsedShard) -> dict[str, Any]:
        data = self._load_data()
        existing = data.shards.get(shard.shard_id)
        if existing is not None:
            if existing["content_digest"] != shard.content_digest:
                raise BusinessError(
                    "E_SHARD_CONFLICT",
                    "同一 shard_id 已绑定不同内容摘要",
                    {
                        "shard_id": shard.shard_id,
                        "existing_digest": existing["content_digest"],
                        "incoming_digest": shard.content_digest,
                    },
                    EXIT_VERIFY,
                )
            return {
                "shard_id": shard.shard_id,
                "already_loaded": True,
                "content_digest": shard.content_digest,
                "observations_added": 0,
                "conflicts": [],
            }

        new_rows: list[dict[str, Any]] = []
        for obs in shard.observations:
            row = {
                "shard_id": obs.shard_id,
                "instance_id": obs.instance_id,
                "obs_seq": obs.obs_seq,
                "digest": obs.digest,
                "schema_version": obs.schema_version,
                "logical_clock_ms": obs.logical_clock_ms,
                "observed_at_ms": obs.observed_at_ms,
                "status_codes": list(obs.status_codes),
                "latency_summary_ms": obs.latency_summary_ms,
                "latency_histogram": obs.latency_histogram,
                "time_conversion": obs.time_conversion,
                "canonical": obs.canonical,
                "semantic_digest": obs.semantic_digest,
                "outcome": obs.outcome,
                "arrival_order": obs.arrival_order,
                "applied": False,
            }
            state = data.instances[obs.instance_id]
            if obs.obs_seq <= int(state["watermark"]):
                row["applied"] = True
            new_rows.append(row)

        combined: dict[tuple[str, int], list[dict[str, Any]]] = {}
        for row in data.observations.values():
            combined.setdefault(
                (str(row["instance_id"]), int(row["obs_seq"])), []
            ).append(row)
        for row in new_rows:
            combined.setdefault(
                (str(row["instance_id"]), int(row["obs_seq"])), []
            ).append(row)

        conflicts: list[dict[str, Any]] = []
        equivalent_overlaps = 0
        for (instance_id, obs_seq), group in sorted(combined.items()):
            if len(group) < 2:
                continue
            has_new = any(row in new_rows for row in group)
            if not has_new:
                continue
            config = data.instances[instance_id]["config"]
            for left_index, left in enumerate(group):
                for right in group[left_index + 1 :]:
                    if left["digest"] == right["digest"]:
                        continue
                    if equivalent_observations(left, right, config):
                        equivalent_overlaps += 1
                        continue
                    if int(left["schema_version"]) != int(right["schema_version"]):
                        differences = difference_fields(left, right, config)
                        payload = diagnostic(
                            "E_VERSION_CONFLICT",
                            "重叠观测跨版本语义不等价",
                            instance_id=instance_id,
                            obs_seq=obs_seq,
                            shard_id=right["shard_id"],
                            digest_a=str(left["digest"]),
                            digest_b=str(right["digest"]),
                            arrival_a=int(left["arrival_order"]),
                            arrival_b=int(right["arrival_order"]),
                            schema_versions=[
                                int(left["schema_version"]),
                                int(right["schema_version"]),
                            ],
                            difference_fields=sorted(set(differences)),
                        )
                        data.instances[instance_id]["version_conflict"] = True
                        data.instances[instance_id]["review_required"] = True
                    else:
                        late = obs_seq <= int(
                            data.instances[instance_id]["watermark"]
                        )
                        payload = diagnostic(
                            "E_OBS_SEQ_ROLLBACK"
                            if late
                            else "E_CONFLICT_OBS",
                            "迟到观测与已提交序号内容互斥"
                            if late
                            else "重叠分片在同一观测序号上出现互斥内容",
                            instance_id=instance_id,
                            obs_seq=obs_seq,
                            shard_id=right["shard_id"],
                            digest_a=str(left["digest"]),
                            digest_b=str(right["digest"]),
                            arrival_a=int(left["arrival_order"]),
                            arrival_b=int(right["arrival_order"]),
                            schema_versions=[
                                int(left["schema_version"]),
                                int(right["schema_version"]),
                            ],
                        )
                    self._add_diagnostic(data, payload)
                    conflicts.append(payload)

        data.shards[shard.shard_id] = {
            **shard.to_registry_row(),
            "shard_id": shard.shard_id,
        }
        for row in new_rows:
            data.observations[_observation_key(row)] = row
        result = self._commit("load_shard", data)
        return {
            "shard_id": shard.shard_id,
            "already_loaded": False,
            "content_digest": shard.content_digest,
            "observations_added": len(new_rows),
            "equivalent_overlaps": equivalent_overlaps,
            "conflicts": conflicts,
            **result,
        }

    def instance_ids(self) -> list[str]:
        data = self._load_data()
        return sorted(data.instances)

    def get_instance_state(self, instance_id: str) -> dict[str, Any]:
        data = self._load_data()
        if instance_id not in data.instances:
            raise BusinessError(
                "E_UNKNOWN_INSTANCE",
                "账本中不存在该实例",
                {"instance_id": instance_id},
                EXIT_VERIFY,
            )
        return data.instances[instance_id]

    def observation_rows(
        self,
        instance_id: str,
        *,
        pending_only: bool = False,
    ) -> list[dict[str, Any]]:
        data = self._load_data()
        rows = [
            row
            for row in data.observations.values()
            if str(row["instance_id"]) == instance_id
            and (not pending_only or not bool(row["applied"]))
        ]
        rows.sort(
            key=lambda row: (
                int(row["obs_seq"]),
                int(row["logical_clock_ms"]),
                str(row["digest"]),
                str(row["shard_id"]),
            )
        )
        return rows

    def history_rows(self, instance_id: str) -> list[dict[str, Any]]:
        data = self._load_data()
        rows = [
            row
            for (row_instance, _), row in data.history.items()
            if row_instance == instance_id
        ]
        rows.sort(key=lambda row: int(row["generation"]))
        return rows

    def diagnostics_rows(self) -> list[dict[str, Any]]:
        data = self._load_data()
        rows = list(data.diagnostics.values())
        rows.sort(key=lambda row: str(row["diag_id"]))
        return rows

    def next_arrival_order(self) -> int:
        data = self._load_data()
        if not data.observations:
            return 1
        return (
            max(int(row["arrival_order"]) for row in data.observations.values()) + 1
        )

    def apply_event(self, instance_id: str, event: dict[str, Any]) -> dict[str, Any]:
        data = self._load_data()
        if instance_id not in data.instances:
            raise BusinessError(
                "E_UNKNOWN_INSTANCE",
                "无法为未声明实例提交状态",
                {"instance_id": instance_id},
                EXIT_INTERNAL,
            )
        row = data.instances[instance_id]
        state = InstanceState(
            instance_id=instance_id,
            service_name=str(row["service_name"]),
            config=row["config"],
            status=str(row["status"]),
            state_generation=int(row["state_generation"]),
            consecutive_failures=int(row["consecutive_failures"]),
            lease_remaining=int(row["lease_remaining"]),
            backoff_level=int(row["backoff_level"]),
            backoff_ms=int(row["backoff_ms"]),
            watermark=int(row["watermark"]),
            last_basis=row["last_basis"],
            version_conflict=bool(row["version_conflict"]),
            review_required=bool(row["review_required"]),
        )
        if int(event["obs_seq"]) <= state.watermark:
            raise BusinessError(
                "E_OBS_SEQ_ROLLBACK",
                "提交观测序号不得低于已提交水位",
                {
                    "instance_id": instance_id,
                    "watermark": state.watermark,
                    "obs_seq": event["obs_seq"],
                },
                EXIT_INTERNAL,
            )
        transition = state.apply(event)
        row["status"] = state.status
        row["state_generation"] = state.state_generation
        row["consecutive_failures"] = state.consecutive_failures
        row["lease_remaining"] = state.lease_remaining
        row["backoff_level"] = state.backoff_level
        row["backoff_ms"] = state.backoff_ms
        row["watermark"] = state.watermark
        row["last_basis"] = state.last_basis
        history_row = {
            "instance_id": instance_id,
            "generation": transition.generation,
            "status": transition.status,
            "consecutive_failures": transition.consecutive_failures,
            "lease_remaining": transition.lease_remaining,
            "backoff_level": transition.backoff_level,
            "backoff_ms": transition.backoff_ms,
            "basis_kind": transition.basis_kind,
            "obs_seq": transition.obs_seq,
            "basis_digest": transition.basis_digest,
            "outcome": transition.outcome,
            "logical_clock_ms": transition.logical_clock_ms,
        }
        data.history[(instance_id, transition.generation)] = history_row
        for key, obs_row in data.observations.items():
            if (
                str(obs_row["instance_id"]) == instance_id
                and int(obs_row["obs_seq"]) <= state.watermark
            ):
                obs_row["applied"] = True
        result = self._commit(
            "apply_observation"
            if transition.basis_kind == "observation"
            else "apply_missing",
            data,
        )
        return {
            "transition": transition.to_dict(),
            "state": {
                "status": state.status,
                "state_generation": state.state_generation,
                "consecutive_failures": state.consecutive_failures,
                "lease_remaining": state.lease_remaining,
                "backoff_level": state.backoff_level,
                "backoff_ms": state.backoff_ms,
                "watermark": state.watermark,
                "last_basis": state.last_basis,
            },
            **result,
        }

    def mark_review(
        self,
        instance_id: str,
        payloads: Iterable[dict[str, Any]],
        *,
        version_conflict: bool,
    ) -> dict[str, Any] | None:
        data = self._load_data()
        if instance_id not in data.instances:
            return None
        changed = False
        if version_conflict and not data.instances[instance_id]["version_conflict"]:
            data.instances[instance_id]["version_conflict"] = True
            changed = True
        if not data.instances[instance_id]["review_required"]:
            data.instances[instance_id]["review_required"] = True
            changed = True
        for payload in payloads:
            before = set(data.diagnostics)
            self._add_diagnostic(data, payload)
            changed = changed or set(data.diagnostics) != before
        if not changed:
            return None
        return self._commit("mark_review", data)

    def add_diagnostics(self, payloads: Iterable[dict[str, Any]]) -> dict[str, Any] | None:
        data = self._load_data()
        changed = False
        for payload in payloads:
            before = set(data.diagnostics)
            self._add_diagnostic(data, payload)
            changed = changed or set(data.diagnostics) != before
        if not changed:
            return None
        return self._commit("add_diagnostics", data)

    def seal_status(self, force: bool = False) -> dict[str, Any]:
        data = self._load_data()
        streams: dict[str, list[dict[str, Any]]] = {}
        for row in data.shards.values():
            streams.setdefault(str(row["stream_id"]), []).append(row)
        details: list[dict[str, Any]] = []
        sealed = bool(force)
        if not streams:
            sealed = True
        all_complete = True
        for stream_id in sorted(streams):
            rows = streams[stream_id]
            counts = {row["shard_count"] for row in rows}
            indexes = {
                int(row["shard_index"])
                for row in rows
                if row["shard_index"] is not None
            }
            expected_ids = sorted(
                {
                    shard_id
                    for row in rows
                    for shard_id in row["expected_shard_ids"]
                }
            )
            loaded_ids = {str(row["shard_id"]) for row in rows}
            if len(counts) == 1 and None not in counts:
                count = int(next(iter(counts)))
                complete = indexes == set(range(count))
                if expected_ids:
                    complete = complete and set(expected_ids) <= loaded_ids
            elif expected_ids:
                complete = set(expected_ids) <= loaded_ids
            else:
                complete = True
            all_complete = all_complete and complete
            details.append(
                {
                    "stream_id": stream_id,
                    "loaded_shards": sorted(loaded_ids),
                    "shard_indexes": sorted(indexes),
                    "shard_count": None if None in counts else int(next(iter(counts))),
                    "expected_shard_ids": expected_ids,
                    "complete": complete,
                }
            )
        if streams:
            sealed = sealed or all_complete
        return {"sealed": sealed, "streams": details}

    def materialize(self) -> dict[str, Any]:
        data = self._load_data()
        manifest_digest = self._manifest_digest()
        if not manifest_digest:
            raise BusinessError(
                "E_NO_MANIFEST",
                "账本缺少注册清单摘要",
                {},
                EXIT_VERIFY,
            )
        snapshot, raw, digest = self._snapshot(data, manifest_digest)
        pointer = self._pointer()
        if pointer is not None:
            assert self.conn is not None
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                self.conn.execute(
                    "UPDATE batches SET state_digest=? WHERE batch_id=?",
                    (digest, int(pointer["batch_id"])),
                )
                self.conn.execute(
                    "UPDATE pointer SET state_digest=? WHERE id=1",
                    (digest,),
                )
                self.conn.execute("COMMIT")
            except Exception:
                self.conn.execute("ROLLBACK")
                raise
        self._write_atomic(self.ledger_path, raw)
        self._write_atomic(self.diagnostics_path, self._diagnostics_payload(data))
        return {
            "pointer_version": int(snapshot["pointer_version"]),
            "state_digest": digest,
            "snapshot": snapshot,
        }

    def status(self, instance_id: str | None = None) -> dict[str, Any]:
        data = self._load_data()
        rows: list[dict[str, Any]] = []
        for key in sorted(data.instances):
            if instance_id is not None and key != instance_id:
                continue
            row = data.instances[key]
            rows.append(
                {
                    "instance_id": key,
                    "service_name": str(row["service_name"]),
                    "state_generation": int(row["state_generation"]),
                    "consecutive_failures": int(row["consecutive_failures"]),
                    "lease_remaining": int(row["lease_remaining"]),
                    "status": str(row["status"]),
                    "backoff_level": int(row["backoff_level"]),
                    "backoff_ms": int(row["backoff_ms"]),
                    "watermark": int(row["watermark"]),
                    "last_basis": row["last_basis"],
                    "version_conflict": bool(row["version_conflict"]),
                    "review_required": bool(row["review_required"]),
                }
            )
        if instance_id is not None and not rows:
            raise BusinessError(
                "E_UNKNOWN_INSTANCE",
                "账本中不存在该实例",
                {"instance_id": instance_id},
                EXIT_VERIFY,
            )
        pointer = self._pointer()
        return {
            "instances": rows,
            "pointer_version": None
            if pointer is None
            else int(pointer["pointer_version"]),
        }

    def verify(
        self,
        *,
        expected_manifest_digest: str | None = None,
        expected_shards: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        errors: list[dict[str, Any]] = []
        data = self._load_data()
        manifest_digest = self._manifest_digest()
        pointer = self._pointer()
        if manifest_digest is None or pointer is None:
            errors.append(
                diagnostic("E_VERIFY_POINTER", "账本指针或注册清单摘要缺失")
            )
            return {"ok": False, "errors": errors}
        if expected_manifest_digest and expected_manifest_digest != manifest_digest:
            errors.append(
                diagnostic(
                    "E_VERIFY_MANIFEST",
                    "注册清单摘要与账本绑定值不一致",
                    expected=expected_manifest_digest,
                    actual=manifest_digest,
                )
            )
        if expected_shards:
            for shard_id, content_digest in sorted(expected_shards.items()):
                row = data.shards.get(shard_id)
                if row is None:
                    errors.append(
                        diagnostic(
                            "E_VERIFY_SHARDS",
                            "账本缺少磁盘分片",
                            shard_id=shard_id,
                            expected=content_digest,
                        )
                    )
                elif row["content_digest"] != content_digest:
                    errors.append(
                        diagnostic(
                            "E_VERIFY_SHARDS",
                            "账本分片摘要与磁盘分片不一致",
                            shard_id=shard_id,
                            digest_a=str(row["content_digest"]),
                            digest_b=content_digest,
                        )
                    )

        snapshot, raw, digest = self._snapshot(data, manifest_digest)
        if int(pointer["pointer_version"]) != int(snapshot["pointer_version"]):
            errors.append(
                diagnostic(
                    "E_VERIFY_POINTER",
                    "指针版本与状态转移历史不一致",
                    stored=int(pointer["pointer_version"]),
                    calculated=int(snapshot["pointer_version"]),
                )
            )
        if str(pointer["state_digest"]) != digest:
            errors.append(
                diagnostic(
                    "E_VERIFY_POINTER",
                    "指针状态摘要与重建账本不一致",
                    digest_a=str(pointer["state_digest"]),
                    digest_b=digest,
                )
            )
        assert self.conn is not None
        batch_row = self.conn.execute(
            "SELECT * FROM batches WHERE batch_id=?",
            (int(pointer["batch_id"]),),
        ).fetchone()
        if batch_row is None:
            errors.append(
                diagnostic(
                    "E_VERIFY_POINTER",
                    "账本指针引用了不存在的批次",
                    batch_id=int(pointer["batch_id"]),
                )
            )
        else:
            if int(batch_row["pointer_version"]) != int(pointer["pointer_version"]):
                errors.append(
                    diagnostic(
                        "E_VERIFY_POINTER",
                        "批次版本与当前指针版本不一致",
                        stored=int(batch_row["pointer_version"]),
                        pointer=int(pointer["pointer_version"]),
                    )
                )
            if str(batch_row["state_digest"]) != str(pointer["state_digest"]):
                errors.append(
                    diagnostic(
                        "E_VERIFY_POINTER",
                        "批次状态摘要与当前指针摘要不一致",
                        digest_a=str(batch_row["state_digest"]),
                        digest_b=str(pointer["state_digest"]),
                    )
                )
        if not self.ledger_path.exists():
            errors.append(
                diagnostic(
                    "E_VERIFY_SNAPSHOT",
                    "规范化账本快照文件不存在",
                    path=str(self.ledger_path),
                )
            )
        else:
            disk_raw = self.ledger_path.read_bytes()
            disk_digest = sha256_digest(disk_raw)
            if disk_raw != raw:
                errors.append(
                    diagnostic(
                        "E_VERIFY_SNAPSHOT",
                        "磁盘账本快照与 SQLite 当前批次不一致",
                        digest_a=disk_digest,
                        digest_b=digest,
                    )
                )

        if not self.diagnostics_path.exists():
            errors.append(
                diagnostic(
                    "E_VERIFY_DIAGNOSTICS",
                    "差异报告文件不存在",
                    path=str(self.diagnostics_path),
                )
            )
        elif self.diagnostics_path.read_bytes() != self._diagnostics_payload(data):
            errors.append(
                diagnostic(
                    "E_VERIFY_DIAGNOSTICS",
                    "磁盘差异报告与 SQLite 当前批次不一致",
                )
            )

        for observation in data.observations.values():
            state = data.instances.get(str(observation["instance_id"]))
            if state is None:
                errors.append(
                    diagnostic(
                        "E_VERIFY_SHARDS",
                        "观测引用了未声明实例",
                        instance_id=str(observation["instance_id"]),
                        obs_seq=int(observation["obs_seq"]),
                        shard_id=str(observation["shard_id"]),
                    )
                )
                continue
            if (
                not observation["applied"]
                and int(observation["obs_seq"]) <= int(state["watermark"])
            ):
                errors.append(
                    diagnostic(
                        "E_VERIFY_BUFFER",
                        "水位以下观测仍处于待裁决状态",
                        instance_id=str(observation["instance_id"]),
                        obs_seq=int(observation["obs_seq"]),
                        shard_id=str(observation["shard_id"]),
                    )
                )

        for instance_id in sorted(data.instances):
            row = data.instances[instance_id]
            config = row["config"]
            history = sorted(
                (
                    history_row
                    for (history_instance, _), history_row in data.history.items()
                    if history_instance == instance_id
                ),
                key=lambda item: int(item["generation"]),
            )
            initial_generation = int(config.get("state_generation", 1))
            previous = initial_generation
            seen: set[int] = set()
            for history_row in history:
                generation = int(history_row["generation"])
                if generation in seen or generation <= previous:
                    errors.append(
                        diagnostic(
                            "E_VERIFY_GENERATION",
                            "状态世代号非严格单调递增",
                            instance_id=instance_id,
                            obs_seq=int(history_row["obs_seq"]),
                            generation=generation,
                            previous=previous,
                        )
                    )
                seen.add(generation)
                previous = generation
            if history:
                last = history[-1]
                checks = {
                    "status": str(row["status"]),
                    "consecutive_failures": int(row["consecutive_failures"]),
                    "lease_remaining": int(row["lease_remaining"]),
                    "backoff_level": int(row["backoff_level"]),
                    "backoff_ms": int(row["backoff_ms"]),
                    "state_generation": int(row["state_generation"]),
                    "watermark": int(row["watermark"]),
                }
                expected = {
                    "status": str(last["status"]),
                    "consecutive_failures": int(last["consecutive_failures"]),
                    "lease_remaining": int(last["lease_remaining"]),
                    "backoff_level": int(last["backoff_level"]),
                    "backoff_ms": int(last["backoff_ms"]),
                    "state_generation": int(last["generation"]),
                    "watermark": int(last["obs_seq"]),
                }
                if checks != expected:
                    errors.append(
                        diagnostic(
                            "E_VERIFY_GENERATION",
                            "实例当前状态与最后一条转移历史不一致",
                            instance_id=instance_id,
                            current=checks,
                            expected=expected,
                        )
                    )
            elif int(row["state_generation"]) != initial_generation:
                errors.append(
                    diagnostic(
                        "E_VERIFY_GENERATION",
                        "无转移历史时状态世代必须等于清单初始值",
                        instance_id=instance_id,
                    )
                )
            if str(row["status"]) not in STATES:
                errors.append(
                    diagnostic(
                        "E_VERIFY_GENERATION",
                        "实例状态不在允许集合内",
                        instance_id=instance_id,
                        status=row["status"],
                    )
                )

        recalculated_buffer = sha256_digest(snapshot["buffer"]["entries"])
        if recalculated_buffer != snapshot["buffer"]["digest"]:
            errors.append(
                diagnostic(
                    "E_VERIFY_BUFFER",
                    "待裁决缓冲区摘要不自洽",
                    digest_a=snapshot["buffer"]["digest"],
                    digest_b=recalculated_buffer,
                )
            )
        if sha256_digest(snapshot["shards"]) != snapshot["shard_registry_digest"]:
            errors.append(
                diagnostic("E_VERIFY_SHARDS", "分片清单摘要不自洽")
            )
        if sha256_digest(snapshot["diagnostics"]) != snapshot["diagnostics_digest"]:
            errors.append(
                diagnostic("E_VERIFY_DIAGNOSTICS", "差异报告摘要不自洽")
            )
        if (
            sha256_digest(snapshot["time_conversions"])
            != snapshot["time_conversions_digest"]
        ):
            errors.append(
                diagnostic("E_VERIFY_CONVERSION", "v1 时刻换算清单摘要不自洽")
            )
        if sha256_digest(snapshot["instances"]) != snapshot["instances_digest"]:
            errors.append(
                diagnostic("E_VERIFY_GENERATION", "实例账本摘要不自洽")
            )

        return {
            "ok": not errors,
            "errors": errors,
            "pointer_version": int(pointer["pointer_version"]),
            "state_digest": digest,
            "buffer_digest": snapshot["buffer"]["digest"],
            "shard_registry_digest": snapshot["shard_registry_digest"],
        }
