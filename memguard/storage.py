"""SQLite credential ledger plus atomic local checkpoint snapshots."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import signal
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .closure import full_closure_report
from .constants import (
    CREDENTIAL_VERSION,
    LAYOUT_VERSION,
    POLICY_ALGEBRA_VERSION,
    PROT_CALIBER_VERSION,
    HalfWriteError,
    ReplayDiverged,
)


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS region_state (
    endpoint_id TEXT NOT NULL,
    process_guid TEXT NOT NULL,
    region_id TEXT NOT NULL,
    region_generation INTEGER NOT NULL,
    module TEXT NOT NULL,
    map_kind TEXT NOT NULL,
    current_prot_mask INTEGER NOT NULL,
    verdict TEXT NOT NULL,
    business_code TEXT NOT NULL,
    credential_id TEXT,
    last_event_seq INTEGER NOT NULL,
    updated_observed_ms INTEGER NOT NULL,
    PRIMARY KEY (endpoint_id, process_guid, region_id, region_generation)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS credentials (
    credential_id TEXT PRIMARY KEY,
    event_seq INTEGER NOT NULL UNIQUE,
    sync_marker TEXT,
    endpoint_id TEXT NOT NULL,
    process_guid TEXT NOT NULL,
    region_id TEXT NOT NULL,
    region_generation INTEGER NOT NULL,
    business_code TEXT NOT NULL,
    status TEXT NOT NULL,
    payload_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS event_journal (
    event_seq INTEGER PRIMARY KEY,
    sync_marker TEXT,
    verdict TEXT NOT NULL,
    business_code TEXT NOT NULL,
    credential_id TEXT,
    payload_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS rollback_journal (
    rollback_id TEXT PRIMARY KEY,
    scope TEXT NOT NULL,
    root_credential_id TEXT,
    credential_ids_json TEXT NOT NULL,
    payload_json TEXT NOT NULL
);
"""


def maybe_crash(stage: str, credential_count: int = 0) -> None:
    """Deterministic fault hook used by the forced-kill recovery tests."""
    wanted_stage = os.environ.get("MEMGUARD_CRASH_MODE")
    if wanted_stage != stage:
        return
    try:
        threshold = int(os.environ.get("MEMGUARD_CRASH_AFTER_CREDENTIALS", "1"))
    except ValueError:
        threshold = 1
    if credential_count < threshold:
        return
    sigkill = getattr(signal, "SIGKILL", None)
    if sigkill is not None:
        os.kill(os.getpid(), sigkill)
    os._exit(137)


def connect_database(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), timeout=0.2, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=DELETE")
    conn.execute("PRAGMA synchronous=FULL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _meta_dict(conn: sqlite3.Connection) -> Dict[str, str]:
    try:
        rows = conn.execute("SELECT key, value FROM meta").fetchall()
    except sqlite3.DatabaseError:
        return {}
    return {row["key"]: row["value"] for row in rows}


def layout_meta(bundle: Dict[str, Any]) -> Dict[str, str]:
    return {
        "layout_version": LAYOUT_VERSION,
        "prot_caliber_version": PROT_CALIBER_VERSION,
        "credential_version": CREDENTIAL_VERSION,
        "policy_algebra_version": POLICY_ALGEBRA_VERSION,
        "input_fingerprint": bundle.get("fingerprint", ""),
    }


def _archive_existing(state_dir: Path, db_path: Path) -> Path:
    archive_root = state_dir / "archive"
    index = 1
    while (archive_root / f"rebuild-{index:03d}").exists():
        index += 1
    archive_dir = archive_root / f"rebuild-{index:03d}"
    archive_dir.mkdir(parents=True, exist_ok=True)
    for path in [
        db_path,
        Path(str(db_path) + "-wal"),
        Path(str(db_path) + "-shm"),
        Path(str(db_path) + "-journal"),
        state_dir / "checkpoint.json",
    ]:
        if path.exists():
            shutil.move(str(path), str(archive_dir / path.name))
    return archive_dir


def open_ledger(
    state_dir: Path,
    bundle: Dict[str, Any],
    *,
    force_rebuild: bool = False,
) -> Tuple[sqlite3.Connection, Dict[str, Any]]:
    state_dir.mkdir(parents=True, exist_ok=True)
    db_path = state_dir / "guard.sqlite"
    expected = layout_meta(bundle)
    rebuild_info: Dict[str, Any] = {"rebuilt": False}

    if db_path.exists():
        conn = connect_database(db_path)
        existing = _meta_dict(conn)
        mismatch = {
            key: {"old": existing.get(key), "new": value}
            for key, value in expected.items()
            if existing.get(key) != value
        }
        if force_rebuild or mismatch:
            conn.close()
            archive_dir = _archive_existing(state_dir, db_path)
            rebuild_info = {
                "rebuilt": True,
                "reason": "forced" if force_rebuild else "layout_or_generation_changed",
                "mismatch": mismatch,
                "archive_dir": str(archive_dir),
            }
        else:
            conn.executescript(SCHEMA_SQL)
            return conn, rebuild_info

    conn = connect_database(db_path)
    conn.executescript(SCHEMA_SQL)
    conn.execute("BEGIN IMMEDIATE")
    for key, value in expected.items():
        conn.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
            (key, value),
        )
    conn.execute(
        "INSERT OR REPLACE INTO meta(key, value) VALUES ('last_event_seq', '0')"
    )
    conn.commit()
    return conn, rebuild_info


def get_watermark(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT value FROM meta WHERE key='last_event_seq'"
    ).fetchone()
    return int(row["value"]) if row else 0


def count_credentials(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT COUNT(*) AS n FROM credentials").fetchone()["n"])


def apply_verdict(conn: sqlite3.Connection, verdict: Dict[str, Any]) -> None:
    """Insert one adjudication. Caller owns the immediate transaction."""
    seq = int(verdict["event_seq"])
    diff = verdict.get("diff", {})
    current_mask = int(diff.get("after_mask", 0))
    region_generation = verdict.get("region_generation", 0)

    conn.execute(
        """
        INSERT INTO region_state(
            endpoint_id, process_guid, region_id, region_generation,
            module, map_kind, current_prot_mask, verdict, business_code,
            credential_id, last_event_seq, updated_observed_ms
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(endpoint_id, process_guid, region_id, region_generation)
        DO UPDATE SET
            module=excluded.module,
            map_kind=excluded.map_kind,
            current_prot_mask=excluded.current_prot_mask,
            verdict=excluded.verdict,
            business_code=excluded.business_code,
            credential_id=excluded.credential_id,
            last_event_seq=excluded.last_event_seq,
            updated_observed_ms=excluded.updated_observed_ms
        """,
        (
            verdict["endpoint_id"],
            verdict["process_guid"],
            verdict["region_id"],
            region_generation,
            verdict.get("module", ""),
            verdict.get("map_kind", ""),
            current_mask,
            verdict["verdict"],
            verdict["business_code"],
            verdict.get("credential_id"),
            seq,
            verdict.get("observed_ms", 0),
        ),
    )

    credential = verdict.get("credential")
    if credential:
        conn.execute(
            """
            INSERT INTO credentials(
                credential_id, event_seq, sync_marker, endpoint_id,
                process_guid, region_id, region_generation, business_code,
                status, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                credential["credential_id"],
                seq,
                credential.get("sync_marker"),
                credential["endpoint_id"],
                credential["process_guid"],
                credential["region_id"],
                credential["region_generation"],
                credential["business_code"],
                credential["status"],
                json.dumps(credential, ensure_ascii=False, sort_keys=True),
            ),
        )

    conn.execute(
        """
        INSERT INTO event_journal(
            event_seq, sync_marker, verdict, business_code,
            credential_id, payload_json
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            seq,
            verdict.get("sync_marker"),
            verdict["verdict"],
            verdict["business_code"],
            verdict.get("credential_id"),
            json.dumps(verdict, ensure_ascii=False, sort_keys=True, default=str),
        ),
    )
    conn.execute(
        "INSERT OR REPLACE INTO meta(key, value) VALUES ('last_event_seq', ?)",
        (str(seq),),
    )


def load_credentials(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    rows = conn.execute(
        "SELECT payload_json FROM credentials ORDER BY credential_id"
    ).fetchall()
    return [json.loads(row["payload_json"]) for row in rows]


def load_verdicts(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    rows = conn.execute(
        "SELECT payload_json FROM event_journal ORDER BY event_seq"
    ).fetchall()
    return [json.loads(row["payload_json"]) for row in rows]


def load_region_states(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM region_state ORDER BY endpoint_id, process_guid, region_id"
    ).fetchall()
    return [dict(row) for row in rows]


def canonical_checksum(body: Dict[str, Any]) -> str:
    material = {key: value for key, value in body.items() if key != "sha256"}
    encoded = json.dumps(material, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def atomic_write_json(
    path: Path, payload: Dict[str, Any], *, crash_stage: Optional[str] = None
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(str(path) + ".tmp")
    body = dict(payload)
    body["sha256"] = canonical_checksum(body)
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(body, handle, ensure_ascii=False, indent=2, sort_keys=True, default=str)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())

    if crash_stage:
        maybe_crash(crash_stage, int(body.get("credential_count_for_fault", 0)))

    with temporary.open("r", encoding="utf-8") as handle:
        reparsed = json.load(handle)
    if reparsed.get("sha256") != body["sha256"]:
        raise ValueError("temporary snapshot failed checksum validation")

    os.replace(temporary, path)
    try:
        directory_fd = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except OSError:
        pass


def snapshot_payload(
    conn: sqlite3.Connection, bundle: Dict[str, Any]
) -> Dict[str, Any]:
    credentials = load_credentials(conn)
    regions = bundle.get("regions", [])
    closures = full_closure_report(credentials, regions)
    watermark = get_watermark(conn)
    return {
        "schema": "memguard-checkpoint-v1",
        "layout_version": LAYOUT_VERSION,
        "prot_caliber_version": PROT_CALIBER_VERSION,
        "credential_version": CREDENTIAL_VERSION,
        "policy_algebra_version": POLICY_ALGEBRA_VERSION,
        "last_event_seq": watermark,
        "credential_count": len(credentials),
        "region_states": load_region_states(conn),
        "credentials": credentials,
        "verdicts": load_verdicts(conn),
        "closures": closures,
        "credential_count_for_fault": count_credentials(conn),
    }


def write_checkpoint(
    conn: sqlite3.Connection, bundle: Dict[str, Any], state_dir: Path
) -> Path:
    payload = snapshot_payload(conn, bundle)
    path = state_dir / "checkpoint.json"
    atomic_write_json(path, payload, crash_stage="snapshot_temp")
    return path


def find_temp_residue(state_dir: Path) -> List[Path]:
    if not state_dir.exists():
        return []
    return sorted(path for path in state_dir.glob("*.tmp") if path.is_file())


def quarantine_temp_files(state_dir: Path) -> List[Dict[str, str]]:
    quarantined = []
    temps = find_temp_residue(state_dir)
    if not temps:
        return quarantined
    target_dir = state_dir / "quarantine"
    target_dir.mkdir(parents=True, exist_ok=True)
    for path in temps:
        index = 1
        target = target_dir / f"halfwrite-{index:03d}-{path.name}"
        while target.exists():
            index += 1
            target = target_dir / f"halfwrite-{index:03d}-{path.name}"
        shutil.move(str(path), str(target))
        quarantined.append(
            {
                "original_path": str(path),
                "quarantined_path": str(target),
                "business_code": "E_HALF_WRITE_RESIDUE",
            }
        )
    return quarantined


def read_checkpoint(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    checksum = payload.get("sha256")
    if checksum != canonical_checksum(payload):
        raise ValueError("checkpoint checksum mismatch")
    for key in (
        "layout_version",
        "credential_version",
        "policy_algebra_version",
        "last_event_seq",
        "credentials",
        "credential_count",
        "region_states",
        "closures",
    ):
        if key not in payload:
            raise ValueError(f"checkpoint missing {key}")
    if payload["layout_version"] != LAYOUT_VERSION:
        raise ValueError("checkpoint layout version mismatch")
    if payload.get("credential_version") != CREDENTIAL_VERSION:
        raise ValueError("checkpoint credential structure version mismatch")
    if payload.get("policy_algebra_version") != POLICY_ALGEBRA_VERSION:
        raise ValueError("checkpoint policy algebra version mismatch")
    if payload.get("prot_caliber_version") != PROT_CALIBER_VERSION:
        raise ValueError("checkpoint protection caliber version mismatch")
    return payload


def reconcile_checkpoint(
    conn: sqlite3.Connection,
    bundle: Dict[str, Any],
    state_dir: Path,
    *,
    quarantine_corrupt: bool = True,
) -> Dict[str, Any]:
    """Make derived snapshot catch up to committed SQLite state."""
    path = state_dir / "checkpoint.json"
    db_watermark = get_watermark(conn)
    db_credential_ids = {
        row["credential_id"]
        for row in conn.execute("SELECT credential_id FROM credentials")
    }
    warnings: List[Dict[str, Any]] = []

    if path.exists():
        try:
            checkpoint = read_checkpoint(path)
            if int(checkpoint["last_event_seq"]) > db_watermark:
                raise ReplayDiverged(
                    "snapshot watermark is ahead of committed SQLite ledger",
                    details={
                        "checkpoint_seq": checkpoint["last_event_seq"],
                        "sqlite_seq": db_watermark,
                    },
                )
            snapshot_ids = {
                cred["credential_id"] for cred in checkpoint.get("credentials", [])
            }
            unknown = sorted(snapshot_ids - db_credential_ids)
            if unknown:
                raise ReplayDiverged(
                    "snapshot contains credentials absent from SQLite ledger",
                    details={"unknown_credentials": unknown},
                )
        except (json.JSONDecodeError, ValueError, KeyError) as exc:
            if isinstance(exc, ReplayDiverged):
                raise
            warnings.append(
                {
                    "business_code": "E_CHECKPOINT_CORRUPT",
                    "message": str(exc),
                    "path": str(path),
                }
            )
            if quarantine_corrupt:
                quarantine_dir = state_dir / "quarantine"
                quarantine_dir.mkdir(exist_ok=True)
                target = quarantine_dir / f"corrupt-{path.name}"
                shutil.move(str(path), str(target))
                warnings[-1]["quarantined_path"] = str(target)

    write_checkpoint(conn, bundle, state_dir)
    return {
        "checkpoint_path": str(path),
        "watermark": db_watermark,
        "warnings": warnings,
    }


def verify_sqlite(conn: sqlite3.Connection) -> Dict[str, Any]:
    result = conn.execute("PRAGMA quick_check").fetchone()[0]
    return {"quick_check": result, "ok": result == "ok"}
