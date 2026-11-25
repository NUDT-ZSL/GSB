from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Iterable

from .canonical import BASE_LOGICAL_MS, atomic_write_json, canonical_dumps, read_json
from .errors import Codes, WorkbenchError


ZERO_HASH = "0" * 64
NATIVE_ALGEBRA_GENERATION = 2
MIGRATED_DEFAULT_ALGEBRA_GENERATION = 1
ALGEBRA_SPEC = "neumaier-block-fma64-gemm-bmm/v1"


def guard_schema_transition(current_version: int, incoming_version: int, *, allow_downgrade: bool = False) -> None:
    if incoming_version < current_version and not allow_downgrade:
        raise WorkbenchError(
            Codes.LEDGER_VERSION_REGRESSION,
            "ledger schema version would move backwards without explicit downgrade permission",
            {
                "current_version": current_version,
                "incoming_version": incoming_version,
                "allow_downgrade": allow_downgrade,
            },
        )


class LedgerManager:
    def __init__(self, state_dir: str | Path) -> None:
        self.root = Path(state_dir) / "ledgers"
        self.v1_path = self.root / "v1" / "ledger-v1.json"
        self.v2_dir = self.root / "v2"
        self.shard_dir = self.v2_dir / "shards"
        self.sqlite_path = self.v2_dir / "ledger-meta.sqlite"
        self.version_state_path = self.root / "ledger-version-state.json"
        self.root.mkdir(parents=True, exist_ok=True)

    def load_v1(self) -> dict[str, Any]:
        if not self.v1_path.exists():
            return {"ledger_version": "v1", "schema_version": 1, "base_logical_ms": BASE_LOGICAL_MS, "rows": []}
        return read_json(self.v1_path)

    def write_v1_rows(self, rows: list[dict[str, Any]]) -> dict[str, Any]:
        by_job: dict[str, dict[str, Any]] = {}
        for row in self.load_v1().get("rows", []):
            by_job[row["job_id"]] = row
        for row in rows:
            by_job[row["job_id"]] = {
                "relative_commit_seconds": int(row["relative_commit_seconds"]),
                "job_id": str(row["job_id"]),
                "output_digest": str(row["output_digest"]),
            }
        document = {
            "ledger_version": "v1",
            "schema_version": 1,
            "base_logical_ms": BASE_LOGICAL_MS,
            "row_count": len(by_job),
            "rows": [by_job[job_id] for job_id in sorted(by_job)],
        }
        atomic_write_json(self.v1_path, document)
        return document

    def _connect(self) -> sqlite3.Connection:
        self.v2_dir.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.sqlite_path)
        connection.row_factory = sqlite3.Row
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS ledger_v2 (
                job_id TEXT PRIMARY KEY,
                absolute_logical_ms INTEGER NOT NULL,
                algebra_generation INTEGER NOT NULL,
                algebra_spec TEXT NOT NULL,
                input_digest TEXT NOT NULL,
                result_hash TEXT NOT NULL,
                shard_path TEXT NOT NULL,
                record_json TEXT NOT NULL,
                completion_sources_json TEXT NOT NULL
            )
            """
        )
        connection.commit()
        return connection

    def write_v2_records(self, records: list[dict[str, Any]]) -> dict[str, Any]:
        self.shard_dir.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            for record in records:
                sources = record.get("completion_sources", {})
                shard_path = record["shard_path"]
                connection.execute(
                    """
                    INSERT INTO ledger_v2 VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(job_id) DO UPDATE SET
                        absolute_logical_ms=excluded.absolute_logical_ms,
                        algebra_generation=excluded.algebra_generation,
                        algebra_spec=excluded.algebra_spec,
                        input_digest=excluded.input_digest,
                        result_hash=excluded.result_hash,
                        shard_path=excluded.shard_path,
                        record_json=excluded.record_json,
                        completion_sources_json=excluded.completion_sources_json
                    """,
                    (
                        record["job_id"],
                        int(record["absolute_logical_ms"]),
                        int(record["algebra_generation"]),
                        record.get("algebra_spec", ALGEBRA_SPEC),
                        record["input_digest"],
                        record["result_hash"],
                        shard_path,
                        canonical_dumps(record),
                        canonical_dumps(sources),
                    ),
                )
            db_rows = connection.execute("SELECT * FROM ledger_v2 ORDER BY job_id").fetchall()

        records_by_shard: dict[str, list[dict[str, Any]]] = {f"{index:x}": [] for index in range(16)}
        full_records: list[dict[str, Any]] = []
        import json

        for db_row in db_rows:
            record = json.loads(db_row["record_json"])
            full_records.append(record)
            shard_key = record["result_hash"][:1] if record["result_hash"] else "0"
            if shard_key not in records_by_shard:
                shard_key = format(int(shard_key, 16) % 16, "x")
            records_by_shard[shard_key].append(record)

        for shard_index, shard_records in records_by_shard.items():
            shard_path = self.shard_dir / f"shard-{shard_index}.json"
            atomic_write_json(
                shard_path,
                {
                    "ledger_version": "v2",
                    "schema_version": 2,
                    "shard": shard_index,
                    "record_count": len(shard_records),
                    "records": shard_records,
                },
            )

        document = {
            "ledger_version": "v2",
            "schema_version": 2,
            "sqlite_metadata": "ledgers/v2/ledger-meta.sqlite",
            "shard_count": 16,
            "record_count": len(full_records),
            "records": full_records,
        }
        atomic_write_json(self.v2_dir / "ledger-v2-index.json", document)
        return document

    def upsert_results(
        self,
        results: Iterable[dict[str, Any]],
        *,
        logical_ms: int,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        v1_rows: list[dict[str, Any]] = []
        v2_records: list[dict[str, Any]] = []
        for offset, result in enumerate(sorted(results, key=lambda item: item["job_id"])):
            commit_ms = int(logical_ms) + offset * 1000
            digest = result["result_hash"]
            input_digest = result.get("input_hash", ZERO_HASH)
            v1_rows.append(
                {
                    "relative_commit_seconds": (commit_ms - BASE_LOGICAL_MS) // 1000,
                    "job_id": result["job_id"],
                    "output_digest": digest,
                }
            )
            shard_key = digest[:1]
            v2_records.append(
                {
                    "job_id": result["job_id"],
                    "absolute_logical_ms": commit_ms,
                    "algebra_generation": NATIVE_ALGEBRA_GENERATION,
                    "algebra_spec": ALGEBRA_SPEC,
                    "input_digest": input_digest,
                    "result_hash": digest,
                    "shard_path": f"ledgers/v2/shards/shard-{shard_key}.json",
                    "completion_sources": {
                        "absolute_logical_ms": "runtime.logical_clock",
                        "algebra_generation": "native_default:2",
                        "input_digest": "runtime.input_hash",
                        "result_hash": "runtime.aggregate_hash",
                    },
                }
            )
        v1 = self.write_v1_rows(v1_rows)
        v2 = self.write_v2_records(v2_records)
        return v1, v2

    def migrate_v1_to_v2(self, enrichment: dict[str, dict[str, str]] | None = None) -> dict[str, Any]:
        v1 = self.load_v1()
        enrichment = enrichment or {}
        migrated: list[dict[str, Any]] = []
        item_sources: list[dict[str, Any]] = []
        for row in v1.get("rows", []):
            job_id = row["job_id"]
            extra = enrichment.get(job_id, {})
            output_digest = row.get("output_digest", "")
            valid_hash = isinstance(output_digest, str) and len(output_digest) == 64
            result_hash = output_digest if valid_hash else ZERO_HASH
            result_source = "v1.output_digest" if valid_hash else "default:zero_sha256"
            if extra.get("result_hash"):
                result_hash = extra["result_hash"]
                result_source = "state.result_hash(cross_checked_with_v1)" if valid_hash and extra["result_hash"] == result_hash else "state.result_hash"
            input_digest = extra.get("input_hash", ZERO_HASH)
            input_source = "state.input_hash" if extra.get("input_hash") else "default:zero_sha256"
            absolute_ms = BASE_LOGICAL_MS + int(row.get("relative_commit_seconds", 0)) * 1000
            shard_key = result_hash[:1]
            sources = {
                "absolute_logical_ms": "derived:BASE_LOGICAL_MS + v1.relative_commit_seconds * 1000",
                "algebra_generation": f"default:{MIGRATED_DEFAULT_ALGEBRA_GENERATION}",
                "algebra_spec": f"default:{ALGEBRA_SPEC}",
                "input_digest": input_source,
                "result_hash": result_source,
            }
            record = {
                "job_id": job_id,
                "absolute_logical_ms": absolute_ms,
                "algebra_generation": MIGRATED_DEFAULT_ALGEBRA_GENERATION,
                "algebra_spec": ALGEBRA_SPEC,
                "input_digest": input_digest,
                "result_hash": result_hash,
                "shard_path": f"ledgers/v2/shards/shard-{shard_key}.json",
                "completion_sources": sources,
            }
            migrated.append(record)
            item_sources.append({"job_id": job_id, "sources": sources})
        v2 = self.write_v2_records(migrated)
        report = {
            "direction": "v1_to_v2",
            "migrated_count": len(migrated),
            "item_sources": item_sources,
            "v2_record_count": v2["record_count"],
        }
        atomic_write_json(self.root / "migration-report-v1-to-v2.json", report)
        return report

    def load_v2_records(self) -> list[dict[str, Any]]:
        index_path = self.v2_dir / "ledger-v2-index.json"
        if index_path.exists():
            return read_json(index_path).get("records", [])
        if not self.sqlite_path.exists():
            return []
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM ledger_v2 ORDER BY job_id").fetchall()
        import json

        records = []
        for row in rows:
            value = json.loads(row["record_json"]) if row["record_json"].startswith("{") else None
            records.append(
                value
                or {
                    "job_id": row["job_id"],
                    "absolute_logical_ms": row["absolute_logical_ms"],
                    "algebra_generation": row["algebra_generation"],
                    "input_digest": row["input_digest"],
                    "result_hash": row["result_hash"],
                }
            )
        return records

    def migrate_v2_to_v1(self, *, allow_version_regression: bool = True) -> dict[str, Any]:
        current_version = 2 if self.sqlite_path.exists() or (self.v2_dir / "ledger-v2-index.json").exists() else 1
        guard_schema_transition(current_version, 1, allow_downgrade=allow_version_regression)
        records = self.load_v2_records()
        rows: list[dict[str, Any]] = []
        discarded: list[dict[str, Any]] = []
        for record in records:
            rows.append(
                {
                    "relative_commit_seconds": (int(record["absolute_logical_ms"]) - BASE_LOGICAL_MS) // 1000,
                    "job_id": record["job_id"],
                    "output_digest": record["result_hash"],
                }
            )
            discarded.append(
                {
                    "job_id": record["job_id"],
                    "discarded_fields": ["algebra_generation", "algebra_spec", "input_digest", "completion_sources"],
                    "rule": "v1_flat_schema_cannot_express_field",
                }
            )
        v1 = self.write_v1_rows(rows)
        report = {
            "direction": "v2_to_v1",
            "migrated_count": len(rows),
            "v1_row_count": len(v1["rows"]),
            "discard_rules": discarded,
            "explicit_downgrade": bool(allow_version_regression),
        }
        atomic_write_json(self.root / "migration-report-v2-to-v1.json", report)
        return report

    def semantic_pairs(self) -> list[dict[str, Any]]:
        v1_rows = {row["job_id"]: row["output_digest"] for row in self.load_v1().get("rows", [])}
        v2_records = {record["job_id"]: record["result_hash"] for record in self.load_v2_records()}
        pairs = []
        for job_id in sorted(set(v1_rows) & set(v2_records)):
            pairs.append(
                {
                    "job_id": job_id,
                    "v1_output_digest": v1_rows[job_id],
                    "v2_result_hash": v2_records[job_id],
                    "semantic_equal": v1_rows[job_id] == v2_records[job_id],
                }
            )
        return pairs
