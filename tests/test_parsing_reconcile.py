from __future__ import annotations

from supply_chain_audit.codes import (
    E_ALGO_CONFLICT,
    E_DUP_KEY,
    E_HASH_ALGO,
    E_HASH_MISMATCH,
    E_SIGNATURE_STATUS,
    E_TIME_CONFLICT,
)
from supply_chain_audit.generator import ORIGIN_MS


def test_seed_generation_has_expected_files(sample_paths):
    assert sample_paths["v1"].endswith("v1_snapshot.json")
    assert sample_paths["v2_manifest"].endswith("v2_manifest.json")
    assert sample_paths["sqlite_metadata"].endswith("metadata.sqlite3")


def test_parser_continues_after_invalid_records(parsed_v1, parsed_v2):
    assert len(parsed_v1.claims) == 12
    codes = {item["code"] for item in parsed_v1.rejections}
    assert E_HASH_ALGO in codes
    assert E_SIGNATURE_STATUS in codes
    assert E_DUP_KEY in codes

    for rejection in parsed_v1.rejections + parsed_v2.rejections:
        assert rejection["snapshot_id"]
        assert rejection["artifact_key"]
        assert rejection["code"].startswith("E_")

    assert parsed_v2.ok
    assert len(parsed_v2.claims) == 11


def test_time_conversion_uses_clock_and_reports_bounds(engine):
    core = engine.view["artifacts"]["foundation/core@1.4.0"]
    conversion = core["time_conversion"]
    assert conversion["origin_ms"] == ORIGIN_MS
    assert conversion["relative_seconds"] == 120
    assert conversion["absolute_ms"] == ORIGIN_MS + 120_000
    assert conversion["lower_endpoint_ms"] == ORIGIN_MS + 120_000
    assert conversion["upper_endpoint_ms"] == ORIGIN_MS + 120_999
    assert conversion["error_bound_ms"] == 999
    assert conversion["midpoint_error_bound_ms"] == 499.5
    # v2 exact millisecond inside the second interval refines the point.
    assert core["published_at_ms"] == ORIGIN_MS + 120_500
    assert core["error_bound_ms"] == 0


def test_time_conflict_is_explicit(engine):
    log = engine.view["artifacts"]["infra/log@1.8.2"]
    assert E_TIME_CONFLICT in log["business_codes"]
    conflict = next(item for item in engine.view["conflicts"] if item["code"] == E_TIME_CONFLICT)
    assert conflict["artifact_key"] == "infra/log@1.8.2"
    assert conflict["interval_intersection"]["lower_endpoint_ms"] > conflict["interval_intersection"]["upper_endpoint_ms"]


def test_hex_case_normalization_and_hash_conflict_dispatch(engine, parsed_v1):
    core_v1 = next(claim for claim in parsed_v1.claims if claim["artifact_key"] == "foundation/core@1.4.0")
    assert any("A" <= char <= "F" for char in core_v1["digest_declared"])
    core = engine.view["artifacts"]["foundation/core@1.4.0"]
    assert core["digest_comparison"]["consistent"] is True

    crypto = engine.view["artifacts"]["security/crypto@3.0.1"]
    assert E_HASH_MISMATCH in crypto["business_codes"]
    mismatch = next(c for c in engine.view["conflicts"] if c["code"] == E_HASH_MISMATCH)
    assert mismatch["generations"] == [1, 2]
    assert len({item["digest_prefix"] for item in mismatch["evidence"] if item["digest_prefix"]}) >= 2
    assert all(len(item["digest_prefix"]) == 12 for item in mismatch["evidence"] if item["digest_prefix"])

    tls = engine.view["artifacts"]["net/tls@4.2.0"]
    assert E_ALGO_CONFLICT in tls["business_codes"]
    assert E_HASH_MISMATCH not in tls["business_codes"]

    width_rejection = next(r for r in engine.view["rejections"] if r["code"] == E_HASH_ALGO and r["artifact_key"] == "broken/hashwidth@0.1.0")
    assert width_rejection["expected_width_bits"] == 256
    assert width_rejection["actual_width_bits"] == 16
    rejected_artifact = engine.view["artifacts"]["broken/hashwidth@0.1.0"]
    assert rejected_artifact["status"] == "rejected"
    assert rejected_artifact["error_bound_ms"] == 999
    assert rejected_artifact["time_conversion"]["upper_endpoint_ms"] > rejected_artifact["published_at_ms"]


def test_view_is_json_serializable(engine):
    import json

    json.dumps(engine.view, ensure_ascii=False, sort_keys=True)
