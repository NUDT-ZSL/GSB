from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from .canonical import atomic_write_bytes, canonical_bytes, sha256_bytes


def _source_chain(
    context: dict[str, Any], event_id: str, segment_id: str | None
) -> dict[str, Any]:
    assignment_keys = context.get("assignment_keys_by_segment") or {}
    event_provenance = context.get("event_provenance_by_event") or {}
    provenance = event_provenance.get(event_id) or {}
    return {
        "event_id": event_id,
        "segment_id": segment_id,
        "frame_index": provenance.get("frame_index"),
        "frame_content_key": provenance.get("frame_content_key"),
        "feature_key": provenance.get("feature_key"),
        "identity_assignment_key": assignment_keys.get(segment_id or ""),
        "config_key": context.get("config_key"),
        "rules_v1_key": context.get("rules_v1_key"),
        "rules_v2_key": context.get("rules_v2_key"),
        "rule_version": context.get("rule_version"),
    }


def build_ledger_rows(
    classified_turns: list[dict[str, Any]],
    segments: list[dict[str, Any]],
    assignments: dict[str, dict[str, Any]],
    context: dict[str, Any],
) -> list[dict[str, Any]]:
    segment_by_start = {int(segment["start_ms"]): segment for segment in segments}
    rows: list[dict[str, Any]] = []
    for index, turn in enumerate(classified_turns, start=1):
        at_ms = int(turn["at_ms"])
        segment = segment_by_start.get(at_ms)
        segment_id = str(segment["segment_id"]) if segment else None
        assignment = assignments.get(segment_id or "") if segment_id else None
        speaker_id = assignment["speaker_id"] if assignment else None
        row_id = f"LED-{index:04d}"
        evidence = dict(turn.get("classification_evidence") or {})
        common = {
            "ledger_id": row_id,
            "ledger_no": index,
            "event_id": turn["event_id"],
            "segment_id": segment_id,
            "at_ms": at_ms,
            "speaker_id": speaker_id,
            "anomaly_type": turn["anomaly_type"],
            "business_code": turn["business_code"],
            "source_chain": _source_chain(context, str(turn["event_id"]), segment_id),
        }
        v1_view = {
            **common,
            "schema": "meeting-audit/ledger-v1-flat",
            "rule_generation": "v1",
            "time_base": "relative_seconds",
            "time_second": at_ms / 1000.0,
            "threshold_rapid_switch_ms": evidence.get("rapid_switch_ms"),
            "threshold_silence_long_ms": evidence.get("silence_long_ms"),
            "threshold_preempt_energy_ratio_q": evidence.get("preempt_energy_ratio_q"),
            "turn_interval_ms": evidence.get("turn_interval_ms"),
            "preceding_silence_ms": evidence.get("preceding_silence_ms"),
            "energy_ratio_q": evidence.get("energy_ratio_q"),
            "speaker_status": assignment.get("status") if assignment else None,
        }
        v2_view = {
            **common,
            "schema": "meeting-audit/ledger-v2-abs-ms",
            "rule_generation": "v2",
            "time_base": "absolute_ms",
            "rule_version": turn.get("rule_version"),
            "condition": {
                "anomaly_type": turn["anomaly_type"],
                "evidence": evidence,
            },
            "identity": {
                "speaker_id": speaker_id,
                "status": assignment.get("status") if assignment else None,
                "reason": assignment.get("reason") if assignment else None,
            },
        }
        rows.append({"common": common, "v1": v1_view, "v2": v2_view})
    return rows


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> str:
    data = b"".join(canonical_bytes(row) + b"\n" for row in rows)
    atomic_write_bytes(path, data)
    return sha256_bytes(data)


def write_dual_ledger(
    ledger_dir: str | Path, rows: list[dict[str, Any]]
) -> dict[str, Any]:
    directory = Path(ledger_dir)
    directory.mkdir(parents=True, exist_ok=True)
    v1_path = directory / "ledger_v1.jsonl"
    v2_path = directory / "ledger_v2.jsonl"
    common_path = directory / "ledger_common.jsonl"
    v1_key = write_jsonl(v1_path, [row["v1"] for row in rows])
    v2_key = write_jsonl(v2_path, [row["v2"] for row in rows])
    common_key = write_jsonl(common_path, [row["common"] for row in rows])
    sqlite_path = directory / "ledger.sqlite"
    write_sqlite_ledger(sqlite_path, rows)
    return {
        "v1_path": str(v1_path),
        "v2_path": str(v2_path),
        "common_path": str(common_path),
        "sqlite_path": str(sqlite_path),
        "v1_key": v1_key,
        "v2_key": v2_key,
        "common_key": common_key,
    }


def write_sqlite_ledger(path: str | Path, rows: list[dict[str, Any]]) -> None:
    db_path = Path(path)
    if db_path.exists():
        db_path.unlink()
    con = sqlite3.connect(str(db_path))
    try:
        con.execute(
            """
            CREATE TABLE ledger_common (
                ledger_id TEXT PRIMARY KEY,
                event_id TEXT NOT NULL,
                segment_id TEXT,
                at_ms INTEGER NOT NULL,
                speaker_id TEXT,
                anomaly_type TEXT NOT NULL,
                business_code TEXT NOT NULL,
                source_key TEXT NOT NULL
            )
            """
        )
        con.execute(
            """
            CREATE TABLE ledger_v1 (
                ledger_id TEXT PRIMARY KEY,
                time_second REAL NOT NULL,
                at_ms INTEGER NOT NULL,
                business_code TEXT NOT NULL,
                flat_row_json TEXT NOT NULL
            )
            """
        )
        con.execute(
            """
            CREATE TABLE ledger_v2 (
                ledger_id TEXT PRIMARY KEY,
                at_ms INTEGER NOT NULL,
                anomaly_type TEXT NOT NULL,
                rule_generation TEXT NOT NULL,
                business_code TEXT NOT NULL,
                shard_json TEXT NOT NULL
            )
            """
        )
        for row in rows:
            common = row["common"]
            v1 = row["v1"]
            v2 = row["v2"]
            con.execute(
                "INSERT INTO ledger_common VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    common["ledger_id"],
                    common["event_id"],
                    common["segment_id"],
                    common["at_ms"],
                    common["speaker_id"],
                    common["anomaly_type"],
                    common["business_code"],
                    common["source_chain"].get("frame_content_key")
                    or common["source_chain"]["config_key"],
                ),
            )
            con.execute(
                "INSERT INTO ledger_v1 VALUES (?, ?, ?, ?, ?)",
                (
                    v1["ledger_id"],
                    v1["time_second"],
                    v1["at_ms"],
                    v1["business_code"],
                    canonical_bytes(v1).decode(),
                ),
            )
            con.execute(
                "INSERT INTO ledger_v2 VALUES (?, ?, ?, ?, ?, ?)",
                (
                    v2["ledger_id"],
                    v2["at_ms"],
                    v2["anomaly_type"],
                    v2["rule_generation"],
                    v2["business_code"],
                    canonical_bytes(v2).decode(),
                ),
            )
        con.commit()
    finally:
        con.close()


def read_jsonl(path: str | Path) -> list[dict[str, Any]]:
    import json

    return [
        json.loads(line)
        for line in Path(path).read_text(encoding="utf-8").splitlines()
        if line
    ]
