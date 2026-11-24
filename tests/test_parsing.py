from __future__ import annotations

from sandbox_guard.capabilities import CAPABILITY_BITS
from sandbox_guard.parsing import parse_policy


def test_generated_policy_has_no_rejections(sample_dir):
    policy = parse_policy(__import__("json").loads((sample_dir / "policy.json").read_text("utf-8")))
    assert policy["ok"] is True
    assert policy["observation_clock_ms"] == 1_760_000_000_000
    assert policy["markers"]["method_sync_marker"] == "SBXGUARD-METHOD-SYNC-V1"
    assert len(policy["profiles"]) >= 8


def test_capability_bits_are_stable():
    assert CAPABILITY_BITS["file_read"] == 1
    assert CAPABILITY_BITS["file_write"] == 2
    assert CAPABILITY_BITS["network_outbound"] == 4
    assert CAPABILITY_BITS["process_spawn"] == 8
    assert CAPABILITY_BITS["symlink_follow"] == 16


def test_invalid_records_emit_stable_codes_and_continue(sample_dir):
    import json

    payload = json.loads((sample_dir / "invalid" / "policy-records.json").read_text("utf-8"))
    result = parse_policy(payload)
    codes = {item["code"] for item in result["rejections"]}
    assert {
        "E_PROFILE_PATH",
        "E_DUP_ID",
        "E_CAP_MASK",
        "E_V1_TTL",
        "E_V1_DEPENDENCIES",
        "E_V2_GENERATION",
        "E_DEP_KEY",
    }.issubset(codes)
    for rejection in result["rejections"]:
        assert rejection["source"]
        assert isinstance(rejection["index"], int)
        assert rejection["code"].startswith("E_")

    valid_ids = {profile["id"] for profile in result["profiles"] if profile["valid"]}
    assert "p-valid-after-error" in valid_ids


def test_profile_required_capability_mask_mismatch_is_reported():
    result = parse_policy(
        {
            "observation_clock_ms": 1000,
            "profiles": [
                {
                    "id": "p1",
                    "executable": r"C:\guard\a.exe",
                    "required_capabilities": ["file_read"],
                    "capability_mask": 4,
                }
            ],
        }
    )
    mask_errors = [r for r in result["rejections"] if r["code"] == "E_CAP_MASK"]
    assert mask_errors
    assert mask_errors[0]["source"] == "profiles"
    assert mask_errors[0]["index"] == 0
