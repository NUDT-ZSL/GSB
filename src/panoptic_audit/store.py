from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from pathlib import Path
from typing import Any

from .errors import AuditError, ErrorCode
from .hashing import canonical_checksum


class LedgerStore:
    def __init__(self, conn: sqlite3.Connection, path: Path, high_water_path: Path, ledger_version: Any):
        self.conn = conn
        self.path = path
        self.high_water_path = high_water_path
        self.ledger_version = ledger_version

    @classmethod
    def create(
        cls,
        path: str | Path,
        *,
        ledger_version: Any,
        adjudicated: Any,
        source_checksums: list[dict[str, str]],
        high_water_path: str | Path | None = None,
    ) -> "LedgerStore":
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            path.unlink()
        hwm_path = Path(high_water_path) if high_water_path else path.with_suffix(".hwm.json")
        if hwm_path.exists():
            hwm_path.unlink()

        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        store = cls(conn, path, hwm_path, ledger_version)
        store._create_schema()
        store._initialize_snapshot(adjudicated, source_checksums)
        store.verify()
        return store

    @classmethod
    def open(
        cls,
        path: str | Path,
        high_water_path: str | Path | None = None,
    ) -> "LedgerStore":
        path = Path(path)
        hwm_path = Path(high_water_path) if high_water_path else path.with_suffix(".hwm.json")
        if not path.exists() or path.stat().st_size == 0:
            raise AuditError(ErrorCode.SNAPSHOT_CORRUPT, "ledger database is missing or empty", {"path": str(path)})
        try:
            conn = sqlite3.connect(path)
            conn.row_factory = sqlite3.Row
            integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        except sqlite3.Error as exc:
            raise AuditError(ErrorCode.SNAPSHOT_CORRUPT, "ledger database cannot be opened", {"path": str(path)}) from exc
        if integrity != "ok":
            raise AuditError(ErrorCode.SNAPSHOT_CORRUPT, "ledger integrity check failed", {"detail": integrity})

        try:
            ledger_version = conn.execute("SELECT value FROM ledger_meta WHERE key='ledger_version'").fetchone()[0]
        except sqlite3.Error as exc:
            conn.close()
            raise AuditError(ErrorCode.SNAPSHOT_CORRUPT, "ledger metadata is missing or corrupt", {}) from exc

        store = cls(conn, path, hwm_path, json.loads(ledger_version))
        store.verify()
        store._check_high_water_mark()
        return store

    def _create_schema(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE ledger_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE source_checksum (
                format TEXT NOT NULL,
                block_id TEXT NOT NULL,
                checksum TEXT NOT NULL,
                PRIMARY KEY(format, block_id)
            );
            CREATE TABLE pixel_ledger (
                coordinate TEXT PRIMARY KEY,
                y INTEGER NOT NULL,
                x INTEGER NOT NULL,
                category_id INTEGER NOT NULL,
                instance_id INTEGER NOT NULL,
                ignore_flag INTEGER NOT NULL,
                conflict_flag INTEGER NOT NULL,
                sources_json TEXT NOT NULL,
                vote_json TEXT NOT NULL,
                checksum TEXT NOT NULL
            );
            CREATE TABLE probe_ledger (
                uid TEXT PRIMARY KEY,
                sequence_no INTEGER NOT NULL,
                probe_id TEXT NOT NULL,
                committed_revision INTEGER NOT NULL,
                record_json TEXT NOT NULL,
                checksum TEXT NOT NULL
            );
            CREATE TABLE progress_snapshot (
                revision INTEGER PRIMARY KEY,
                parent_revision INTEGER NOT NULL,
                payload_json TEXT NOT NULL,
                checksum TEXT NOT NULL
            );
            CREATE TABLE commit_head (
                id INTEGER PRIMARY KEY CHECK(id = 1),
                revision INTEGER NOT NULL,
                probe_checksum TEXT NOT NULL,
                progress_checksum TEXT NOT NULL,
                combined_checksum TEXT NOT NULL
            );
            """
        )
        self.conn.execute("INSERT INTO ledger_meta(key, value) VALUES(?, ?)", ("ledger_version", json.dumps(self.ledger_version)))

    def _initialize_snapshot(self, adjudicated: Any, source_checksums: list[dict[str, str]]) -> None:
        source_rows = [
            (item["format"], item["block_id"], item["checksum"])
            for item in source_checksums
        ]
        self.conn.executemany(
            "INSERT OR REPLACE INTO source_checksum(format, block_id, checksum) VALUES(?, ?, ?)",
            source_rows,
        )

        pixel_rows = []
        for y in range(adjudicated.height):
            for x in range(adjudicated.width):
                pixel = adjudicated.pixel_at(x, y)
                pixel_rows.append((
                    f"{x},{y}", y, x,
                    pixel["winner"]["category"],
                    pixel["winner"]["instance"],
                    int(pixel["winner"]["ignore"]),
                    int(pixel["is_conflict"]),
                    json.dumps(pixel["sources_hit"], sort_keys=True),
                    json.dumps(pixel, sort_keys=True, ensure_ascii=False),
                    pixel["pixel_checksum"],
                ))
        self.conn.executemany(
            """INSERT INTO pixel_ledger
               (coordinate,y,x,category_id,instance_id,ignore_flag,conflict_flag,sources_json,vote_json,checksum)
               VALUES(?,?,?,?,?,?,?,?,?,?)""",
            pixel_rows,
        )

        progress = {
            "revision": 0,
            "parent_revision": -1,
            "ledger_version": self.ledger_version,
            "confirmed_uids": [],
            "candidate_reparse_uids": [],
            "spent_ns": 0,
            "budget_ns": None,
            "rounds_committed": 0,
            "degraded": False,
        }
        progress_checksum = canonical_checksum(progress)
        self.conn.execute(
            "INSERT INTO progress_snapshot(revision,parent_revision,payload_json,checksum) VALUES(?,?,?,?)",
            (0, -1, canonical_json_with_checksum(progress, progress_checksum), progress_checksum),
        )
        probe_checksum = canonical_checksum([])
        combined = canonical_checksum({
            "revision": 0,
            "probe_checksum": probe_checksum,
            "progress_checksum": progress_checksum,
            "ledger_version": self.ledger_version,
        })
        self.conn.execute(
            "INSERT INTO commit_head VALUES(1, 0, ?, ?, ?)",
            (probe_checksum, progress_checksum, combined),
        )
        self.conn.commit()
        self._write_high_water_mark(0, combined)

    def _write_high_water_mark(self, revision: int, combined_checksum: str) -> None:
        payload = {
            "max_revision": revision,
            "head_checksum": combined_checksum,
            "ledger_version": self.ledger_version,
        }
        payload["hwm_checksum"] = canonical_checksum({k: v for k, v in payload.items() if k != "hwm_checksum"})
        self.high_water_path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix=self.high_water_path.name + ".", dir=self.high_water_path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, sort_keys=True, separators=(",", ":"))
            os.replace(tmp_name, self.high_water_path)
        except Exception:
            Path(tmp_name).unlink(missing_ok=True)
            raise

    def _check_high_water_mark(self) -> None:
        if not self.high_water_path.exists():
            return
        try:
            payload = json.loads(self.high_water_path.read_text(encoding="utf-8"))
            declared = payload.pop("hwm_checksum")
            actual = canonical_checksum(payload)
            if declared != actual:
                raise AuditError(ErrorCode.SNAPSHOT_CORRUPT, "high-water mark checksum failed", {"path": str(self.high_water_path)})
        except (json.JSONDecodeError, KeyError) as exc:
            raise AuditError(ErrorCode.SNAPSHOT_CORRUPT, "high-water mark is corrupt", {"path": str(self.high_water_path)}) from exc

        head = self.head()
        if int(payload["max_revision"]) > int(head["revision"]):
            raise AuditError(
                ErrorCode.SNAPSHOT_ROLLBACK,
                "ledger snapshot version rolled back",
                {"high_water_revision": payload["max_revision"], "opened_revision": head["revision"]},
            )
        if int(payload["max_revision"]) < int(head["revision"]):
            self._write_high_water_mark(int(head["revision"]), str(head["combined_checksum"]))

    def head(self) -> dict[str, Any]:
        row = self.conn.execute("SELECT * FROM commit_head WHERE id=1").fetchone()
        if row is None:
            raise AuditError(ErrorCode.SNAPSHOT_CORRUPT, "commit head is missing")
        return dict(row)

    def existing_uids(self) -> set[str]:
        rows = self.conn.execute("SELECT uid FROM probe_ledger").fetchall()
        return {row["uid"] for row in rows}

    def load_probe_records(self) -> list[dict[str, Any]]:
        rows = self.conn.execute("SELECT record_json FROM probe_ledger ORDER BY sequence_no, uid").fetchall()
        return [json.loads(row["record_json"]) for row in rows]

    def load_progress(self, revision: int | None = None) -> dict[str, Any]:
        if revision is None:
            row = self.conn.execute(
                "SELECT payload_json FROM progress_snapshot ORDER BY revision DESC LIMIT 1"
            ).fetchone()
        else:
            row = self.conn.execute(
                "SELECT payload_json FROM progress_snapshot WHERE revision=?", (revision,)
            ).fetchone()
        if row is None:
            raise AuditError(ErrorCode.SNAPSHOT_CORRUPT, "progress snapshot is missing", {"revision": revision})
        return json.loads(row["payload_json"])

    def load_degradation_history(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT revision, payload_json FROM progress_snapshot ORDER BY revision"
        ).fetchall()
        history: list[dict[str, Any]] = []
        for row in rows:
            payload = json.loads(row["payload_json"])
            reason = payload.get("degradation_reason") or {}
            history.append({
                "revision": row["revision"],
                "rounds_committed": payload.get("rounds_committed", 0),
                "degraded": bool(payload.get("degraded")),
                "evaluation": reason,
            })
        return history

    def source_checksums(self) -> list[dict[str, str]]:
        rows = self.conn.execute("SELECT format, block_id, checksum FROM source_checksum ORDER BY format, block_id").fetchall()
        return [dict(row) for row in rows]

    def commit_round(
        self,
        records: list[dict[str, Any]],
        *,
        budget_ns: int | None,
        degraded: bool,
        degradation_reason: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        existing = self.load_probe_records()
        known_uids = {record["uid"] for record in existing}
        revision = int(self.head()["revision"]) + 1
        for record in records:
            if record["uid"] in known_uids:
                raise AuditError(ErrorCode.INCONSISTENT, "confirmed probe cannot be billed twice", {"uid": record["uid"]})
            record["committed_revision"] = revision
            body = {key: value for key, value in record.items() if key != "record_checksum"}
            record["record_checksum"] = canonical_checksum(body)

        try:
            with self.conn:
                for record in records:
                    self.conn.execute(
                        """INSERT INTO probe_ledger
                           (uid, sequence_no, probe_id, committed_revision, record_json, checksum)
                           VALUES(?, ?, ?, ?, ?, ?)""",
                        (
                            record["uid"],
                            int(record["sequence"]),
                            record["probe_id"],
                            int(record["committed_revision"]),
                            json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                            record["record_checksum"],
                        ),
                    )

                all_records = self.load_probe_records()
                # The current transaction sees rows inserted above.
                probe_checksum = canonical_checksum([
                    {"uid": item["uid"], "record_checksum": item["record_checksum"]}
                    for item in sorted(all_records, key=lambda item: item["uid"])
                ])
                spent_ns = sum(int(item["timing"]["charged_ns"]) for item in all_records)
                confirmed_uids = [item["uid"] for item in sorted(all_records, key=lambda item: int(item["sequence"]))]
                candidate_uids = [
                    item["uid"]
                    for item in sorted(all_records, key=lambda item: int(item["sequence"]))
                    if item.get("candidate_reparse") or item["execution"]["speed_class"] == "slow"
                ]
                rounds_committed = int(self.load_progress().get("rounds_committed", 0)) + 1
                progress = {
                    "revision": revision,
                    "parent_revision": revision - 1,
                    "ledger_version": self.ledger_version,
                    "confirmed_uids": confirmed_uids,
                    "candidate_reparse_uids": candidate_uids,
                    "spent_ns": spent_ns,
                    "budget_ns": budget_ns,
                    "remaining_budget_ns": None if budget_ns is None else max(0, int(budget_ns) - spent_ns),
                    "rounds_committed": rounds_committed,
                    "degraded": degraded,
                    "degradation_reason": degradation_reason or {},
                    "probe_ledger_checksum": probe_checksum,
                }
                progress_checksum = canonical_checksum(progress)
                self.conn.execute(
                    "INSERT INTO progress_snapshot(revision,parent_revision,payload_json,checksum) VALUES(?,?,?,?)",
                    (revision, revision - 1, canonical_json_with_checksum(progress, progress_checksum), progress_checksum),
                )
                combined = canonical_checksum({
                    "revision": revision,
                    "probe_checksum": probe_checksum,
                    "progress_checksum": progress_checksum,
                    "ledger_version": self.ledger_version,
                })
                self.conn.execute(
                    "UPDATE commit_head SET revision=?, probe_checksum=?, progress_checksum=?, combined_checksum=? WHERE id=1",
                    (revision, probe_checksum, progress_checksum, combined),
                )
        except sqlite3.Error as exc:
            raise AuditError(ErrorCode.SNAPSHOT_CORRUPT, "atomic ledger commit failed", {"revision": revision}) from exc

        self._write_high_water_mark(revision, combined)
        return {"revision": revision, "probe_checksum": probe_checksum, "progress_checksum": progress_checksum, "combined_checksum": combined}

    def verify(self) -> None:
        try:
            integrity = self.conn.execute("PRAGMA integrity_check").fetchone()[0]
            if integrity != "ok":
                raise AuditError(ErrorCode.SNAPSHOT_CORRUPT, "ledger integrity check failed", {"detail": integrity})

            meta = {row[0]: row[1] for row in self.conn.execute("SELECT key, value FROM ledger_meta")}
            if "ledger_version" not in meta:
                raise AuditError(ErrorCode.SNAPSHOT_CORRUPT, "ledger version metadata missing")

            pixel_rows = self.conn.execute("SELECT vote_json, checksum FROM pixel_ledger").fetchall()
            for row in pixel_rows:
                pixel = json.loads(row["vote_json"])
                actual = canonical_checksum({k: v for k, v in pixel.items() if k != "pixel_checksum"})
                if actual != row["checksum"] or pixel.get("pixel_checksum") != row["checksum"]:
                    raise AuditError(ErrorCode.SNAPSHOT_CORRUPT, "pixel ledger checksum failed", {"coordinate": pixel.get("coordinate")})

            probe_rows = self.conn.execute("SELECT record_json, checksum FROM probe_ledger").fetchall()
            probe_refs = []
            for row in probe_rows:
                record = json.loads(row["record_json"])
                declared = record.get("record_checksum")
                actual = canonical_checksum({k: v for k, v in record.items() if k != "record_checksum"})
                if declared != row["checksum"] or actual != row["checksum"]:
                    raise AuditError(ErrorCode.SNAPSHOT_CORRUPT, "probe ledger checksum failed", {"uid": record.get("uid")})
                probe_refs.append({"uid": record["uid"], "record_checksum": row["checksum"]})

            head = self.conn.execute("SELECT * FROM commit_head WHERE id=1").fetchone()
            if head is None:
                if probe_rows:
                    raise AuditError(ErrorCode.SNAPSHOT_CORRUPT, "commit head missing")
                return
            head = dict(head)
            expected_probe_checksum = canonical_checksum(sorted(probe_refs, key=lambda item: item["uid"]))
            # Head checksum is sequence-independent because refs include stable UIDs.
            if expected_probe_checksum != head["probe_checksum"]:
                raise AuditError(ErrorCode.SNAPSHOT_CORRUPT, "probe ledger head checksum failed", {})

            progress_row = self.conn.execute(
                "SELECT payload_json, checksum FROM progress_snapshot WHERE revision=?",
                (head["revision"],),
            ).fetchone()
            if progress_row is None:
                raise AuditError(ErrorCode.SNAPSHOT_CORRUPT, "head progress snapshot missing", {"revision": head["revision"]})
            progress = json.loads(progress_row["payload_json"])
            progress_declared = progress.pop("progress_checksum", None)
            expected_progress_checksum = canonical_checksum(progress)
            if progress_row["checksum"] != expected_progress_checksum or progress_declared != expected_progress_checksum:
                raise AuditError(ErrorCode.SNAPSHOT_CORRUPT, "progress ledger checksum failed", {"revision": head["revision"]})

            expected_combined = canonical_checksum({
                "revision": head["revision"],
                "probe_checksum": head["probe_checksum"],
                "progress_checksum": head["progress_checksum"],
                "ledger_version": self.ledger_version,
            })
            if expected_combined != head["combined_checksum"]:
                raise AuditError(ErrorCode.SNAPSHOT_CORRUPT, "commit head checksum failed", {"revision": head["revision"]})
        except sqlite3.Error as exc:
            raise AuditError(ErrorCode.SNAPSHOT_CORRUPT, "ledger verification query failed", {"error": str(exc)}) from exc

    def close(self) -> None:
        self.conn.close()


def canonical_json_with_checksum(payload: dict[str, Any], checksum: str) -> str:
    enriched = dict(payload)
    enriched["progress_checksum"] = checksum
    return json.dumps(enriched, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
