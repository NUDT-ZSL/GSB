from __future__ import annotations

import json
from pathlib import Path

from account_verify.parsing import (
    ParseBatch,
    parse_credentials,
    parse_policy_v1,
    parse_policy_v2_shard,
    parse_sample_sources,
    parse_tokens,
)


def test_invalid_records_have_source_index_and_stable_codes(sample_dir: Path, truth):
    payload = json.loads((sample_dir / "invalid_records.json").read_text(encoding="utf-8"))
    batch = ParseBatch()
    parse_credentials(payload["credentials"], batch)
    parse_tokens(payload["tokens"], batch)
    parse_policy_v1({"policies": payload["policy_v1"]}, batch)
    parse_policy_v2_shard({"policies": payload["policy_v2"]}, batch, "policy_v2")

    codes = [item["code"] for item in batch.rejections]
    for expected_code in truth["expected_invalid_codes"]:
        assert expected_code in codes
    for rejection in batch.rejections:
        assert rejection["source_category"]
        assert isinstance(rejection["record_index"], int)
        assert rejection["record_index"] >= 0
        assert rejection["code"].startswith("E_")

    # Valid later records survive their invalid neighbors.
    assert len(batch.credentials) == 1
    assert len(batch.tokens) == 1


def test_one_bad_file_does_not_stop_other_sources(tmp_path: Path, sample_dir: Path):
    bad_credentials = tmp_path / "credentials.json"
    bad_credentials.write_text("{not json", encoding="utf-8")
    batch = parse_sample_sources(
        credentials_path=bad_credentials,
        tokens_path=sample_dir / "tokens.json",
        policy_v1_path=sample_dir / "policy_v1.json",
        policy_v2_db_path=sample_dir / "policy_v2_meta.sqlite",
    )
    assert any(item["code"] == "E_JSON_PARSE" and item["source_category"] == "credentials" for item in batch.rejections)
    assert batch.tokens
    assert batch.policy_v1
    assert batch.policy_v2


def test_v2_sqlite_shard_hash_mismatch_is_rejected(sample_dir: Path):
    shard = sample_dir / "policy_v2" / "shard_000.json"
    data = json.loads(shard.read_text(encoding="utf-8"))
    data["tampered"] = True
    shard.write_text(json.dumps(data), encoding="utf-8")
    batch = parse_sample_sources(policy_v2_db_path=sample_dir / "policy_v2_meta.sqlite")
    assert any(item["code"] == "E_SHARD_HASH_MISMATCH" for item in batch.rejections)
