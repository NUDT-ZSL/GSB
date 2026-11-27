from __future__ import annotations

from georope.generator import generate_incremental_sample
from georope.service import incremental_request


def test_incremental_sample_only_changed_block_is_recomputed() -> None:
    payload = generate_incremental_sample(42)
    result = incremental_request(payload)
    report = result["incremental_recomputation"]
    assert report["unaffected_encoding_preserved"] is True
    changed = [
        block
        for block in report["block_changes"]
        if block["parameter_changes"]
        and block["old_block_id"] == "v1-tile-r0c0"
    ]
    assert len(changed) == 1
    assert "origin" in changed[0]["parameter_changes"]
    assert "channel_group_or_frequency_parameters" in changed[0]["parameter_changes"]
    assert changed[0]["affected_channels"] == list(range(12))
    combination_reports = {
        item["combination"]: item for item in report["combinations"]
    }
    assert combination_reports["low_only"]["recomputed_channel_position_count"] == 48
    assert combination_reports["mid_high"]["recomputed_channel_position_count"] == 96
    assert combination_reports["all_bands"]["recomputed_channel_position_count"] == 144
    for item in combination_reports.values():
        assert item["unaffected_encoding_preserved"] is True
        assert item["unaffected_encoding_differences"] == []
