"""SQLite + JSON checkpoints, policy-generation gates and crash recovery."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

from .codes import (
    CHECKPOINT_STRUCTURE_VERSION,
    HASH_WHITELIST_VERSION,
    MERGE_POLICY_VERSION,
    E_CHECKPOINT_CORRUPT,
    E_CHECKPOINT_VERSION,
    E_HALF_WRITE_RESIDUE,
    RULESET_VERSIONS,
)
from .engine import Ledger
from .merge import canonical_report_digest
from .model import digest_object
from .storage import atomic_write_json, read_json


class CheckpointManager:
    def __init__(self, ledger: str | Path | Ledger, checkpoint_path: str | Path):
        self.ledger = ledger if isinstance(ledger, Ledger) else Ledger(ledger)
        self.path = Path(checkpoint_path)

    def save(self, report: dict[str, Any], *, watermark: int | None = None) -> dict[str, Any]:
        conn = self.ledger.connect()
        try:
            if watermark is None:
                row = conn.execute("SELECT COALESCE(MAX(seq),0) AS seq FROM batches").fetchone()
                watermark = int(row["seq"])
            view_digest = canonical_report_digest(report)
            checkpoint = {
                "schema": "sbom-audit.checkpoint.v1",
                "checkpoint_version": CHECKPOINT_STRUCTURE_VERSION,
                "hash_whitelist_version": HASH_WHITELIST_VERSION,
                "merge_policy_version": MERGE_POLICY_VERSION,
                "ruleset_versions": RULESET_VERSIONS,
                "watermark_batch_seq": watermark,
                "view_digest": view_digest,
                "report": report,
            }
            checkpoint["checkpoint_digest"] = digest_object({k: v for k, v in checkpoint.items() if k != "checkpoint_digest"})
            conn.execute(
                "INSERT INTO checkpoints(seq,checkpoint_json,view_digest) VALUES(?,?,?) "
                "ON CONFLICT(seq) DO UPDATE SET checkpoint_json=excluded.checkpoint_json, view_digest=excluded.view_digest",
                (watermark, json.dumps(checkpoint, sort_keys=True), view_digest),
            )
            self.ledger.meta_set(conn, "view_digest", view_digest)
            self.ledger.meta_set(conn, "watermark", watermark)
            conn.commit()
        finally:
            conn.close()
        atomic_write_json(self.path, checkpoint)
        return checkpoint

    def stale_temp_files(self) -> list[Path]:
        directory = self.path.parent
        if not directory.exists():
            return []
        return sorted(directory.glob(f".{self.path.name}.*.tmp"))

    def quarantine_stale_temp_files(self) -> list[dict[str, Any]]:
        quarantined: list[dict[str, Any]] = []
        for temp in self.stale_temp_files():
            target = temp.with_suffix(temp.suffix + ".rejected")
            try:
                raw = temp.read_bytes()
                json.loads(raw.decode("utf-8"))
                parseable = True
            except Exception:
                parseable = False
            temp.replace(target)
            quarantined.append({"path": str(target), "original_temp": str(temp), "parseable": parseable, "code": E_HALF_WRITE_RESIDUE})
        return quarantined

    def _load_sqlite_checkpoint(self) -> tuple[dict[str, Any] | None, str | None]:
        conn = self.ledger.connect()
        try:
            row = conn.execute("SELECT checkpoint_json FROM checkpoints ORDER BY seq DESC LIMIT 1").fetchone()
            if not row:
                return None, None
            return json.loads(row["checkpoint_json"]), None
        except Exception as exc:
            return None, f"{E_CHECKPOINT_CORRUPT}: {exc}"
        finally:
            conn.close()

    def verify(self) -> dict[str, Any]:
        residues = self.quarantine_stale_temp_files()
        result: dict[str, Any] = {
            "checkpoint_path": str(self.path),
            "valid": False,
            "rebuild_required": False,
            "codes": [E_HALF_WRITE_RESIDUE] if residues else [],
            "half_write_residues": residues,
        }
        if not self.path.exists():
            sqlite_cp, error = self._load_sqlite_checkpoint()
            if error:
                result.update(code=E_CHECKPOINT_CORRUPT, message=error)
                result["codes"].append(E_CHECKPOINT_CORRUPT)
                result["rebuild_required"] = True
                return result
            if sqlite_cp:
                result.update(code=E_CHECKPOINT_CORRUPT, message="JSON checkpoint is missing but SQLite copy exists")
                result["codes"].append(E_CHECKPOINT_CORRUPT)
            else:
                result.update(code=E_CHECKPOINT_CORRUPT, message="no checkpoint file")
            result["rebuild_required"] = True
            return result
        try:
            checkpoint = read_json(self.path)
        except Exception as exc:
            result.update(code=E_CHECKPOINT_CORRUPT, message=f"checkpoint JSON is unparsable: {exc}")
            result["codes"].append(E_CHECKPOINT_CORRUPT)
            result["rebuild_required"] = True
            return result

        version_mismatches = []
        if checkpoint.get("checkpoint_version") != CHECKPOINT_STRUCTURE_VERSION:
            version_mismatches.append("checkpoint_version")
        if checkpoint.get("hash_whitelist_version") != HASH_WHITELIST_VERSION:
            version_mismatches.append("hash_whitelist_version")
        if checkpoint.get("merge_policy_version") != MERGE_POLICY_VERSION:
            version_mismatches.append("merge_policy_version")
        if version_mismatches:
            result.update(
                valid=False,
                rebuild_required=True,
                code=E_CHECKPOINT_VERSION,
                mismatched_versions=version_mismatches,
                checkpoint_versions={
                    "checkpoint_version": checkpoint.get("checkpoint_version"),
                    "hash_whitelist_version": checkpoint.get("hash_whitelist_version"),
                    "merge_policy_version": checkpoint.get("merge_policy_version"),
                },
            )
            result["codes"].append(E_CHECKPOINT_VERSION)
            return result

        expected_digest = checkpoint.get("view_digest")
        report = checkpoint.get("report")
        if not isinstance(report, dict) or canonical_report_digest(report) != expected_digest:
            result.update(valid=False, rebuild_required=True, code=E_CHECKPOINT_CORRUPT, message="checkpoint view digest does not match report")
            result["codes"].append(E_CHECKPOINT_CORRUPT)
            return result
        stored_digest = checkpoint.get("checkpoint_digest")
        unsigned = {k: v for k, v in checkpoint.items() if k != "checkpoint_digest"}
        if stored_digest != digest_object(unsigned):
            result.update(valid=False, rebuild_required=True, code=E_CHECKPOINT_CORRUPT, message="checkpoint envelope digest mismatch")
            result["codes"].append(E_CHECKPOINT_CORRUPT)
            return result

        sqlite_cp, error = self._load_sqlite_checkpoint()
        if error:
            result.update(valid=False, rebuild_required=True, code=E_CHECKPOINT_CORRUPT, message=error)
            result["codes"].append(E_CHECKPOINT_CORRUPT)
            return result
        if sqlite_cp is None or sqlite_cp.get("checkpoint_digest") != stored_digest:
            result.update(valid=False, rebuild_required=True, code=E_CHECKPOINT_CORRUPT, message="SQLite and JSON checkpoints are not the same complete generation")
            result["codes"].append(E_CHECKPOINT_CORRUPT)
            return result

        replay = self.replay_after_watermark(int(checkpoint.get("watermark_batch_seq", 0)), expected_digest)
        result.update(
            valid=not replay.get("rejected"),
            rebuild_required=bool(replay.get("rejected")),
            code=replay.get("code"),
            replay=replay,
            watermark_batch_seq=checkpoint.get("watermark_batch_seq"),
            view_digest=expected_digest,
        )
        if replay.get("code"):
            result["codes"].append(replay["code"])
        return result

    def replay_after_watermark(self, watermark: int, checkpoint_digest: str) -> dict[str, Any]:
        """Replay committed batches and compare ledger view digest metadata."""

        conn = self.ledger.connect()
        try:
            rows = conn.execute("SELECT batch_id, seq, batch_json, result_json FROM batches WHERE seq > ? ORDER BY seq", (watermark,)).fetchall()
            replayed = [{"batch_id": row["batch_id"], "seq": row["seq"]} for row in rows]
            for row in rows:
                json.loads(row["batch_json"])
                result = json.loads(row["result_json"])
                if result.get("status") == "error" and result.get("code") == "E_RECOMPUTE_DIVERGED":
                    return {"rejected": True, "code": E_RECOMPUTE_DIVERGED, "replayed_batches": replayed}
            ledger_digest = self.ledger.meta_get("view_digest")
            max_seq_row = conn.execute("SELECT COALESCE(MAX(seq),0) AS seq FROM batches").fetchone()
            max_seq = int(max_seq_row["seq"])
            if ledger_digest and ledger_digest != checkpoint_digest and watermark >= max_seq:
                return {"rejected": True, "code": E_CHECKPOINT_CORRUPT, "replayed_batches": replayed, "message": "ledger digest differs from complete checkpoint at final watermark"}
            return {"rejected": False, "replayed_batches": replayed, "ledger_view_digest": ledger_digest, "batch_watermark": max_seq}
        except Exception as exc:
            return {"rejected": True, "code": E_CHECKPOINT_CORRUPT, "message": str(exc)}
        finally:
            conn.close()

    def recover_or_rebuild(self, rebuild: Callable[[], dict[str, Any]]) -> tuple[dict[str, Any], bool]:
        verification = self.verify()
        if verification.get("valid"):
            return read_json(self.path), False
        report = rebuild()
        self.save(report)
        return self.path and read_json(self.path), True
