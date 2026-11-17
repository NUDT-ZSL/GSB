from __future__ import annotations

import json
from pathlib import Path

from supply_diag.checkpoint import (
    load_checkpoint,
    make_checkpoint_entry,
    save_checkpoint,
)
from supply_diag.diagnostics import DiagnosticEngine
from supply_diag.generator import generate_samples
from supply_diag.manifest import parse_manifest_file
from supply_diag.report import canonical_json_bytes


def _entry(tmp_path: Path) -> dict:
    generate_samples(tmp_path)
    path = tmp_path / "manifests/delta-secure.json"
    parsed = parse_manifest_file(path)
    result = DiagnosticEngine().diagnose(parsed).to_dict()
    entry = make_checkpoint_entry(parsed, result)
    assert entry is not None
    return entry


def _rewrite_with_payload(path: Path, payload: dict) -> None:
    checksum = __import__("hashlib").sha256(canonical_json_bytes(payload)).hexdigest()
    path.write_text(
        json.dumps({"payload": payload, "checksum": checksum}, sort_keys=True),
        encoding="utf-8",
    )


def test_checkpoint_roundtrip_and_watermark(tmp_path: Path) -> None:
    entry = _entry(tmp_path)
    checkpoint = tmp_path / "checkpoint.json"
    payload = save_checkpoint(checkpoint, entries=[entry], sequence=2)
    assert payload["watermark"] == "3.2.1"
    assert payload["aggregate"]["applied_manifest_count"] == 1
    loaded = load_checkpoint(checkpoint)
    assert loaded.compatible
    assert loaded.present
    assert loaded.sequence == 2
    assert list(loaded.entries) == [entry["key"]]


def test_checksum_mismatch_triggers_full_rebuild(tmp_path: Path) -> None:
    entry = _entry(tmp_path)
    checkpoint = tmp_path / "checkpoint.json"
    save_checkpoint(checkpoint, entries=[entry])
    document = json.loads(checkpoint.read_text(encoding="utf-8"))
    document["payload"]["watermark"] = "999.0.0"
    checkpoint.write_text(json.dumps(document), encoding="utf-8")
    loaded = load_checkpoint(checkpoint)
    assert not loaded.compatible
    assert loaded.reason == "checkpoint_checksum_mismatch"


def test_schema_or_whitelist_change_triggers_rebuild(tmp_path: Path) -> None:
    entry = _entry(tmp_path)
    checkpoint = tmp_path / "checkpoint.json"
    save_checkpoint(checkpoint, entries=[entry])
    document = json.loads(checkpoint.read_text(encoding="utf-8"))

    schema_payload = dict(document["payload"])
    schema_payload["schema_version"] = "0.0"
    _rewrite_with_payload(checkpoint, schema_payload)
    schema_state = load_checkpoint(checkpoint)
    assert not schema_state.compatible
    assert schema_state.reason == "checkpoint_schema_version_changed"

    whitelist_payload = dict(document["payload"])
    whitelist_payload["hash_algorithm_whitelist_version"] = "0:md5"
    _rewrite_with_payload(checkpoint, whitelist_payload)
    whitelist_state = load_checkpoint(checkpoint)
    assert not whitelist_state.compatible
    assert (
        whitelist_state.reason == "hash_algorithm_whitelist_version_changed"
    )


def test_incomplete_checkpoint_is_ignored(tmp_path: Path) -> None:
    entry = _entry(tmp_path)
    checkpoint = tmp_path / "checkpoint.json"
    save_checkpoint(checkpoint, entries=[entry])
    document = json.loads(checkpoint.read_text(encoding="utf-8"))
    incomplete_payload = dict(document["payload"])
    incomplete_payload["complete"] = False
    _rewrite_with_payload(checkpoint, incomplete_payload)
    loaded = load_checkpoint(checkpoint)
    assert not loaded.compatible
    assert loaded.reason == "checkpoint_incomplete"

