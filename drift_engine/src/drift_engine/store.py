"""SQLite drift-adjudication ledger, incremental batches and checkpoints."""

from __future__ import annotations

import hashlib
import os
import sqlite3

from . import adjudication, atomic, codes
from .concurrency import parse_snapshots
from .parsers import Evidence, SnapshotResult
from .spec import BaselineSpec

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS evidence (
    generation INTEGER NOT NULL,
    host TEXT NOT NULL,
    config_key TEXT NOT NULL,
    algebra INTEGER NOT NULL,
    observed_ms INTEGER NOT NULL,
    normalized_json TEXT NOT NULL,
    digest TEXT NOT NULL,
    tombstone INTEGER NOT NULL,
    drift INTEGER NOT NULL,
    severity TEXT,
    time_quantum_ms INTEGER NOT NULL,
    snapshot_id TEXT NOT NULL,
    record_index INTEGER NOT NULL,
    PRIMARY KEY (host, config_key, generation, algebra)
);
CREATE TABLE IF NOT EXISTS ledger (
    host TEXT NOT NULL,
    config_key TEXT NOT NULL,
    verdict TEXT NOT NULL,
    code TEXT,
    first_drift_ms INTEGER,
    frozen INTEGER NOT NULL,
    authoritative_generation INTEGER NOT NULL,
    authoritative_algebra INTEGER NOT NULL,
    decision_watermark_ms INTEGER NOT NULL,
    row_digest TEXT NOT NULL,
    PRIMARY KEY (host, config_key)
);
CREATE TABLE IF NOT EXISTS rejection_log (
    batch_id TEXT NOT NULL,
    snapshot_id TEXT NOT NULL,
    generation INTEGER NOT NULL,
    record_index INTEGER NOT NULL,
    code TEXT NOT NULL,
    message TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS checkpoints (
    batch_id TEXT PRIMARY KEY,
    ledger_digest TEXT NOT NULL,
    spec_merge_version TEXT NOT NULL,
    spec_whitelist_version TEXT NOT NULL,
    checkpoint_path TEXT NOT NULL
);
"""


def row_digest(row: dict) -> str:
    payload = {key: row[key] for key in sorted(row) if key != "row_digest"}
    return hashlib.sha256(atomic.canonical_json(payload).encode("utf-8")).hexdigest()


def ledger_digest(rows: list[dict]) -> str:
    canonical = atomic.canonical_json(sorted(rows, key=lambda row: (row["host"], row["config_key"])))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _evidence_to_row(evidence: Evidence) -> tuple:
    return (
        evidence.generation, evidence.host, evidence.canonical_key, evidence.algebra,
        evidence.observed_ms, atomic.canonical_json(evidence.normalized), evidence.digest,
        int(evidence.tombstone), int(evidence.drift), evidence.severity,
        evidence.time_quantum_ms, evidence.snapshot_id, evidence.record_index,
    )


def _row_to_evidence(cursor: sqlite3.Row) -> Evidence:
    import json
    return Evidence(
        snapshot_id=cursor["snapshot_id"], generation=cursor["generation"],
        record_index=cursor["record_index"], host=cursor["host"],
        canonical_key=cursor["config_key"], observed_ms=cursor["observed_ms"],
        algebra=cursor["algebra"],
        normalized=json.loads(cursor["normalized_json"]),
        digest=cursor["digest"],
        tombstone=bool(cursor["tombstone"]),
        drift=bool(cursor["drift"]),
        severity=cursor["severity"],
        time_quantum_ms=cursor["time_quantum_ms"],
    )


class DriftEngine:
    def __init__(self, db_path: str, spec: BaselineSpec, checkpoint_dir: str):
        self.db_path = db_path
        self.spec = spec
        self.checkpoint_dir = checkpoint_dir
        os.makedirs(os.path.dirname(os.path.abspath(db_path)) or ".", exist_ok=True)
        os.makedirs(checkpoint_dir, exist_ok=True)
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(SCHEMA)
        self._version_rebuild_if_needed()

    def close(self) -> None:
        self.conn.close()

    def _meta(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

    def _set_meta(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO meta(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )

    def _version_rebuild_if_needed(self) -> list[dict]:
        stored_merge = self._meta("merge_version")
        stored_white = self._meta("whitelist_version")
        notes: list[dict] = []
        if stored_merge is None:
            self._set_meta("merge_version", self.spec.merge_version)
            self._set_meta("whitelist_version", self.spec.whitelist_version)
            return notes
        if (stored_merge, stored_white) != self.spec.version_tuple():
            self.conn.execute("DELETE FROM ledger")
            self.conn.execute("DELETE FROM evidence")
            self.conn.execute("DELETE FROM rejection_log")
            self.conn.execute("DELETE FROM checkpoints")
            self._set_meta("merge_version", self.spec.merge_version)
            self._set_meta("whitelist_version", self.spec.whitelist_version)
            notes.append({
                "code": codes.E_VERSION_REBUILD,
                "message": "merge/whitelist version changed; ledger fully rebuilt",
                "from": [stored_merge, stored_white],
                "to": list(self.spec.version_tuple()),
            })
        return notes

    def _load_all_evidence(self) -> list[Evidence]:
        rows = self.conn.execute(
            "SELECT * FROM evidence ORDER BY snapshot_id, record_index"
        ).fetchall()
        return [_row_to_evidence(row) for row in rows]

    def _snapshot_results(self, v1_paths: list[str], v2_targets: list[tuple[str, str]],
                          workers: int = 1) -> list[SnapshotResult]:
        return parse_snapshots(v1_paths, v2_targets, self.spec, workers=workers)

    def import_batch(self, batch_id: str, v1_paths: list[str], v2_targets: list[tuple[str, str]],
                     window_seconds: int, decision_clock_ms: int, workers: int = 1) -> dict:
        results = self._snapshot_results(v1_paths, v2_targets, workers=workers)
        new_evidence = [evidence for result in results for evidence in result.accepted]
        affected = {(evidence.host, evidence.canonical_key) for evidence in new_evidence}

        try:
            with self.conn:  # single transaction; any exception rolls everything back
                for evidence in new_evidence:
                    self.conn.execute(
                        "INSERT OR REPLACE INTO evidence VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        _evidence_to_row(evidence),
                    )
                for result in results:
                    if result.fatal:
                        self.conn.execute(
                            "INSERT INTO rejection_log VALUES (?,?,?,?,?,?)",
                            (batch_id, result.snapshot_id, result.generation, 0,
                             result.fatal["code"], result.fatal["message"]),
                        )
                    for rejection in result.rejected:
                        self.conn.execute(
                            "INSERT INTO rejection_log VALUES (?,?,?,?,?,?)",
                            (batch_id, rejection["snapshot_id"], rejection["generation"],
                             rejection["record_index"], rejection["code"], rejection["message"]),
                        )

                all_evidence = self._load_all_evidence()
                adjudication_result = adjudication.adjudicate_all(
                    all_evidence, self.spec, window_seconds, decision_clock_ms
                )
                decision_index = {(item["host"], item["key"]): item
                                  for item in adjudication_result["decisions"]}

                # Only affected host/key pairs are rewritten; untouched rows stay byte-identical.
                for host, key in sorted(affected):
                    decision = decision_index.get((host, key))
                    if decision is None:
                        continue
                    row = {
                        "host": host,
                        "config_key": key,
                        "verdict": decision["verdict"],
                        "code": decision["code"],
                        "first_drift_ms": decision["first_drift_ms"],
                        "frozen": int(decision["frozen"]),
                        "authoritative_generation": decision["authoritative_generation"],
                        "authoritative_algebra": decision["authoritative_algebra"],
                        "decision_watermark_ms": decision_clock_ms,
                    }
                    row["row_digest"] = row_digest(row)
                    self.conn.execute(
                        "INSERT OR REPLACE INTO ledger VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (row["host"], row["config_key"], row["verdict"], row["code"],
                         row["first_drift_ms"], row["frozen"], row["authoritative_generation"],
                         row["authoritative_algebra"], row["decision_watermark_ms"],
                         row["row_digest"]),
                    )

                self._set_meta("last_batch_id", batch_id)
                self._set_meta("window_seconds", window_seconds)
                self._set_meta("decision_clock_ms", decision_clock_ms)
                digest = self._persist_checkpoint(batch_id, adjudication_result)
        except Exception:
            self.conn.rollback()
            raise

        return {
            "batch_id": batch_id,
            "snapshot_count": len(results),
            "accepted_records": len(new_evidence),
            "rejected_records": [entry for result in results for entry in result.rejected],
            "fatal_snapshots": [result.fatal for result in results if result.fatal],
            "affected_pairs": sorted(affected),
            "conflicts": adjudication_result["conflicts"],
            "ledger_digest": digest,
        }

    def _current_ledger_rows(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM ledger ORDER BY host, config_key"
        ).fetchall()
        return [dict(row) for row in rows]

    def _persist_checkpoint(self, batch_id: str, adjudication_result: dict) -> str:
        rows = self._current_ledger_rows()
        digest = ledger_digest(rows)
        checkpoint = {
            "batch_id": batch_id,
            "ledger_digest": digest,
            "merge_version": self.spec.merge_version,
            "whitelist_version": self.spec.whitelist_version,
            "decision_watermark_ms": int(self._meta("decision_clock_ms")),
            "ledger_rows": rows,
            "conflicts": adjudication_result["conflicts"],
        }
        final_path = os.path.join(self.checkpoint_dir, f"checkpoint-{batch_id}.json")
        atomic.atomic_write_json(final_path, checkpoint)
        self.conn.execute(
            "INSERT OR REPLACE INTO checkpoints VALUES (?,?,?,?,?)",
            (batch_id, digest, self.spec.merge_version, self.spec.whitelist_version, final_path),
        )
        return digest

    def full_rebuild(self, window_seconds: int, decision_clock_ms: int) -> dict:
        """Adjudicate every host/key from scratch (reference implementation)."""
        all_evidence = self._load_all_evidence()
        result = adjudication.adjudicate_all(
            all_evidence, self.spec, window_seconds, decision_clock_ms
        )
        return result

    def verify_checkpoint(self) -> dict:
        """Replay the last complete checkpoint against the live ledger.

        Detects half-written residue: a checkpoint row whose digest or content
        no longer matches the SQLite ledger, or a checkpoint file missing on disk.
        """
        rows = self._current_ledger_rows()
        live_digest = ledger_digest(rows)
        checkpoint_row = self.conn.execute(
            "SELECT * FROM checkpoints ORDER BY batch_id DESC LIMIT 1"
        ).fetchone()
        report = {
            "code": None,
            "live_ledger_digest": live_digest,
            "checkpoint": None,
            "mismatches": [],
        }
        if checkpoint_row is None:
            report["code"] = codes.E_CHECKPOINT_RESIDUE
            report["mismatches"].append("no checkpoint recorded")
            return report
        report["checkpoint"] = dict(checkpoint_row)
        path = checkpoint_row["checkpoint_path"]
        if not os.path.exists(path):
            report["code"] = codes.E_CHECKPOINT_RESIDUE
            report["mismatches"].append(f"checkpoint file missing: {path}")
            return report

        import json
        try:
            with open(path, "r", encoding="utf-8") as handle:
                checkpoint = json.load(handle)
        except json.JSONDecodeError as exc:
            report["code"] = codes.E_CHECKPOINT_RESIDUE
            report["mismatches"].append(f"checkpoint file unreadable/half-written: {exc}")
            return report
        if checkpoint["ledger_digest"] != checkpoint_row["ledger_digest"]:
            report["code"] = codes.E_CHECKPOINT_RESIDUE
            report["mismatches"].append("sqlite checkpoint digest disagrees with snapshot file")
        if checkpoint["ledger_digest"] != live_digest:
            report["code"] = codes.E_CHECKPOINT_RESIDUE
            report["mismatches"].append("checkpoint digest disagrees with live ledger")
        file_rows = sorted(checkpoint["ledger_rows"], key=lambda row: (row["host"], row["config_key"]))
        if file_rows != rows:
            report["code"] = codes.E_CHECKPOINT_RESIDUE
            report["mismatches"].append("checkpoint rows differ field-by-field from live ledger")
        return report

    def ledger_rows(self) -> list[dict]:
        return self._current_ledger_rows()

    def rejections(self) -> list[dict]:
        return [dict(row) for row in self.conn.execute(
            "SELECT * FROM rejection_log ORDER BY batch_id, snapshot_id, record_index"
        ).fetchall()]
