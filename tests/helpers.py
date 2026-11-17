from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path
from typing import Any

from config_forensics.canonical import normalize_config_path, observation_digest_payload

TEST_KEY = "test-offline-key"


def digest_for(path: str, value: Any, algorithm: str = "sha256") -> str:
    payload = observation_digest_payload(normalize_config_path(path), value)
    if algorithm == "sha256":
        return hashlib.sha256(payload).hexdigest()
    return hmac.new(TEST_KEY.encode("utf-8"), payload, hashlib.sha256).hexdigest()


def make_record(
    *,
    evidence_id: str,
    host_id: str,
    sequence: int,
    path: str,
    value: Any,
    previous: str | None = None,
    algorithm: str = "sha256",
    digest_bits: int = 256,
    digest_override: str | None = None,
    sync_marker: str | None = None,
) -> dict[str, Any]:
    return {
        "evidence_id": evidence_id,
        "host_id": host_id,
        "sequence": sequence,
        "path": path,
        "value": value,
        "algorithm": algorithm,
        "digest_bits": digest_bits,
        "digest": digest_override
        if digest_override is not None
        else digest_for(path, value, algorithm),
        "previous_evidence_id": previous,
        "sync_marker": sync_marker or f"sync:{host_id}:{sequence}",
        "sender_observed_value": value,
    }


def write_json(path: Path, value: Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return path


def write_evidence_file(
    directory: Path,
    name: str,
    records: list[dict[str, Any]],
    *,
    file_id: str | None = None,
    hmac_key: str | None = None,
) -> Path:
    payload: dict[str, Any] = {
        "schema_version": 1,
        "file_id": file_id or name,
        "records": records,
    }
    if hmac_key is not None:
        payload["hmac_keys"] = {"test-key": hmac_key}
    return write_json(directory / name, payload)


def write_baseline(path: Path, entries: list[dict[str, Any]]) -> Path:
    return write_json(path, {"schema_version": 1, "baselines": entries})
