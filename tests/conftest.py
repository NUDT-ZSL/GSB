from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List

import pytest

from audit_masker.models import Event


ROOT = Path(__file__).resolve().parents[1]
SAMPLES = ROOT / "samples" / "generated"


def run_cli(*args: Any, env: Dict[str, str] | None = None) -> subprocess.CompletedProcess:
    command = [sys.executable, "-m", "audit_masker"] + [str(arg) for arg in args]
    merged_env = os.environ.copy()
    if env:
        merged_env.update(env)
    return subprocess.run(
        command,
        cwd=ROOT,
        text=True,
        capture_output=True,
        env=merged_env,
        check=False,
    )


def cli_json(*args: Any, env: Dict[str, str] | None = None) -> Dict[str, Any]:
    completed = run_cli(*args, env=env)
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise AssertionError(
            f"CLI did not return JSON: {completed.stdout!r} / {completed.stderr!r}"
        ) from exc
    payload["_process_exit"] = completed.returncode
    return payload


def write_json(path: Path, value: Any) -> Path:
    path.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )
    return path


def write_jsonl(path: Path, values: Iterable[Any]) -> Path:
    lines = [
        json.dumps(value, ensure_ascii=False, sort_keys=True)
        for value in values
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def event(
    event_id: str,
    seq: int = 0,
    *,
    category: str = "contact",
    payload: Dict[str, str] | None = None,
) -> Dict[str, Any]:
    return {
        "event_id": event_id,
        "tenant": "tenant-a",
        "actor": "actor-a",
        "category": category,
        "ts_ms": 1_760_000_000_000,
        "seq": seq,
        "payload": payload or {"email": "alice@example.com"},
        "idempotency_key": f"idem-{event_id}-{seq}",
    }


def as_event(value: Dict[str, Any]) -> Event:
    return Event(
        event_id=value["event_id"],
        tenant=value["tenant"],
        actor=value["actor"],
        category=value["category"],
        ts_ms=value["ts_ms"],
        seq=value["seq"],
        payload=dict(value["payload"]),
        idempotency_key=value["idempotency_key"],
        sync_marker=value.get("sync_marker"),
    )


def snapshot(
    *,
    v1: List[str] | None = None,
    v2: List[Dict[str, Any]] | None = None,
    clock_ms: int = 1_760_000_000_000,
    relative_seconds: List[int] | None = None,
    defaults: Dict[str, Any] | None = None,
    layout_version: str = "layout-v1",
    masks: List[str] | None = None,
) -> Dict[str, Any]:
    result: Dict[str, Any] = {
        "observation_clock_ms": clock_ms,
        "v1": v1 or [],
        "v2": v2 or [],
        "category_defaults": defaults or {},
        "layout": {
            "mask_methods": masks
            or ["keep", "redact", "email", "last4", "hash"],
            "threshold_mode": "higher_wins",
            "version": layout_version,
        },
    }
    if relative_seconds is not None:
        result["v1_relative_seconds"] = relative_seconds
    return result


def v2_rule(
    name: str,
    *,
    field: str = "payload.email",
    mask: str = "redact",
    threshold: int = 10,
    generation: int = 1,
    effective_ms: int = 1_760_000_000_000,
    tombstone: bool = False,
) -> Dict[str, Any]:
    return {
        "rule_name": name,
        "field_path": field,
        "mask": mask,
        "threshold": threshold,
        "effective_ms": effective_ms,
        "generation": generation,
        "tombstone": tombstone,
    }


def ledger_rows(workspace: Path) -> List[Dict[str, Any]]:
    connection = sqlite3.connect(workspace / "audit_masker.sqlite3")
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            """
            SELECT event_id, seq, field_path, decision, masked_value,
                   winning_rule_name, winning_generation, winning_threshold,
                   basis, matches_json, suppressed_json, business_codes_json,
                   layout_fingerprint, decision_watermark
            FROM ledger ORDER BY event_id, seq, field_path
            """
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        connection.close()


def decision_rows(workspace: Path) -> List[Dict[str, Any]]:
    rows = ledger_rows(workspace)
    result = []
    for row in rows:
        result.append(
            {
                key: value
                for key, value in row.items()
                if key != "decision_watermark"
            }
        )
    return result


@pytest.fixture
def samples_dir() -> Path:
    assert SAMPLES.exists(), "run python samples/generate_samples.py first"
    return SAMPLES
