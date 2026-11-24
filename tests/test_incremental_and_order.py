from __future__ import annotations

import json
from pathlib import Path

from tsaudit.canon import canonical_text
from tsaudit.engine import AuditEngine
from tsaudit.generator import OBSERVATION_CLOCK_MS


def _verdict_map(engine: AuditEngine) -> dict[str, dict]:
    return {row["artifact_key"]: row for row in engine.store.load_verdict_results()}


def test_revocation_reanchors_to_alternative_and_matches_full(
    sample_dir: Path, tmp_path: Path
) -> None:
    incremental_dir = tmp_path / "incremental"
    fresh_dir = tmp_path / "fresh"
    engine = AuditEngine(incremental_dir)
    baseline = engine.adjudicate_sample(sample_dir)
    multi = next(a for a in baseline["artifacts"] if a["artifact_id"] == "art-multi")
    assert multi["primary_token_id"] == "tok-multi-a"

    updated = engine.import_update(sample_dir / "updates" / "revoke-alpha.json")
    by_id = {a["artifact_id"]: a for a in updated["artifacts"]}
    assert by_id["art-multi"]["primary_token_id"] == "tok-multi-b"
    assert by_id["art-ok"]["status"] == "unanchored"
    assert by_id["art-reanchor"]["primary_token_id"] == "tok-backup"

    transition = next(
        x for x in updated["reanchor_transitions"] if x["artifact_id"] == "art-multi"
    )
    assert transition["previous_primary_token_id"] == "tok-multi-a"
    assert transition["new_primary_token_id"] == "tok-multi-b"

    fresh = AuditEngine(fresh_dir)
    fresh.adjudicate_sample(sample_dir)
    fresh.import_update(sample_dir / "updates" / "revoke-alpha.json")
    incremental_rows = engine.store.load_verdict_results()
    fresh_rows = fresh.store.load_verdict_results()
    assert canonical_text(sorted(incremental_rows, key=lambda x: x["artifact_key"])) == \
        canonical_text(sorted(fresh_rows, key=lambda x: x["artifact_key"]))
    assert engine.verify_checkpoint(recover=True)["ledger_consistent"] is True


def test_snapshot_arrival_order_does_not_change_verdicts(
    sample_dir: Path, tmp_path: Path
) -> None:
    first = AuditEngine(tmp_path / "v1-v2")
    second = AuditEngine(tmp_path / "v2-v1")
    first.adjudicate_sample(sample_dir, arrival_order=("v1", "v2"))
    second.adjudicate_sample(sample_dir, arrival_order=("v2", "v1"))
    first_rows = _verdict_map(first)
    second_rows = _verdict_map(second)
    assert set(first_rows) == set(second_rows)
    for key in first_rows:
        assert canonical_text(first_rows[key]) == canonical_text(second_rows[key]), key


def test_generation_tombstone_propagates_to_token_and_artifact(
    sample_dir: Path, workspace: Path
) -> None:
    engine = AuditEngine(workspace)
    engine.adjudicate_sample(sample_dir)
    report = engine.import_update(sample_dir / "updates" / "tombstone-generation.json")
    multi = next(a for a in report["artifacts"] if a["artifact_id"] == "art-multi")
    # tok-multi-b is the Beta generation-3 token and is tombstoned; Alpha a is
    # still active in this update, so the stable earliest token remains primary.
    assert multi["primary_token_id"] == "tok-multi-a"
    assert "tok-multi-b" in report["closure"]["affected_token_ids"]


def test_only_changed_verdict_rows_are_replaced_on_revocation(
    sample_dir: Path, workspace: Path
) -> None:
    engine = AuditEngine(workspace)
    engine.adjudicate_sample(sample_dir)
    updated = engine.import_update(sample_dir / "updates" / "revoke-alpha.json")
    updated_keys = set(updated["incremental"]["updated_artifact_keys"])
    # Backup-only and no-coverage conclusions do not need row replacement.
    assert not any(key.startswith("art-no-coverage|") for key in updated_keys)
    assert any(key.startswith("art-ok|") for key in updated_keys)
    assert any(key.startswith("art-multi|") for key in updated_keys)


def test_signature_batch_arrival_order_is_irrelevant(
    sample_dir: Path, tmp_path: Path
) -> None:
    payload = json.loads((sample_dir / "signatures.json").read_text(encoding="utf-8"))
    truth = json.loads((sample_dir / "truth.json").read_text(encoding="utf-8"))
    valid_markers = {
        case["sync_marker"]
        for case in truth["sync_checks"]
        if case["kind"] == "signature" and not case["expected"].get("rejected")
    }
    valid = [r for r in payload["records"] if r.get("sync_marker") in valid_markers]
    first_half = valid[:6]
    second_half = valid[6:]
    batch_a = tmp_path / "a.json"
    batch_b = tmp_path / "b.json"
    batch_a.write_text(json.dumps({"records": first_half}), encoding="utf-8")
    batch_b.write_text(json.dumps({"records": second_half}), encoding="utf-8")

    async_dir = tmp_path / "sig-order-1"
    reverse_dir = tmp_path / "sig-order-2"
    engine = AuditEngine(async_dir)
    engine.import_v1(sample_dir / "snapshots/v1/trust_v1.json", OBSERVATION_CLOCK_MS)
    engine.import_v2(sample_dir / "snapshots/v2")
    engine.import_tokens(sample_dir / "tokens.json")
    engine.import_signatures(batch_a)
    engine.import_signatures(batch_b)

    reverse = AuditEngine(reverse_dir)
    reverse.import_v1(sample_dir / "snapshots/v1/trust_v1.json", OBSERVATION_CLOCK_MS)
    reverse.import_v2(sample_dir / "snapshots/v2")
    reverse.import_tokens(sample_dir / "tokens.json")
    reverse.import_signatures(batch_b)
    reverse.import_signatures(batch_a)

    rows = _verdict_map(engine)
    reverse_rows = _verdict_map(reverse)
    assert set(rows) == set(reverse_rows)
    for key in rows:
        assert canonical_text(rows[key]) == canonical_text(reverse_rows[key]), key
