from __future__ import annotations

from osl_forensics.generator import generate_all
from osl_forensics.verifier import verify_injection_tree


def test_all_sync_markers_match_business_code_and_offset(generated_root):
    report = verify_injection_tree(generated_root / "injections")
    failures = [case for case in report["cases"] if not case["passed"]]
    assert report["total"] >= 22
    assert failures == []
    assert report["ok"] is True
    assert all(case["sync_token_present"] for case in report["cases"])


def test_required_injection_positions_are_present(generated_root):
    report = verify_injection_tree(generated_root / "injections")
    by_id = {case["case_id"]: case for case in report["cases"]}

    assert by_id["oob_offset"]["actual_code"] == "E_BOUNDS"
    assert by_id["overflow_offset"]["actual_code"] == "E_BOUNDS"
    assert by_id["overlap_pair"]["actual_code"] == "E_OVERLAP"
    assert by_id["checksum_bad"]["actual_code"] == "E_CHECKSUM"
    assert by_id["depth_exceeded"]["actual_code"] == "E_DEPTH"
    assert by_id["cycle_ancestor"]["actual_code"] == "E_CYCLE"
    assert by_id["quota_expanded_bytes"]["actual_code"] == "E_QUOTA"


def test_fixed_seed_generation_is_byte_deterministic(tmp_path):
    first = tmp_path / "first"
    second = tmp_path / "second"
    generate_all(first)
    generate_all(second)

    first_samples = sorted(first.rglob("*.osl"))
    assert first_samples
    for sample in first_samples:
        relative = sample.relative_to(first)
        assert sample.read_bytes() == (second / relative).read_bytes()
