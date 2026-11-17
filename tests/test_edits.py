from __future__ import annotations

from pathlib import Path

import numpy as np

from tests.conftest import copy_demo, write_jsonl
from tests.helpers import assert_outputs_equal, phoneme_report, read_outputs, render


def test_incremental_scopes_and_full_render_equivalence(tmp_path: Path) -> None:
    incremental_input = copy_demo(tmp_path / "incremental_input", with_commands=False)
    work = tmp_path / "incremental_work"
    baseline = render(incremental_input, work)
    assert baseline.status == "complete"

    seq1 = tmp_path / "seq1.jsonl"
    write_jsonl(
        seq1,
        [
            {
                "seq": 1,
                "utterance_id": "utt_alpha",
                "phoneme_index": 1,
                "op": "set_energy_scale",
                "value": 1.2,
            }
        ],
    )
    energy_edit = render(incremental_input, work, extra_commands=[seq1])
    assert energy_edit.status == "complete"
    assert energy_edit.report["diagnostics"]["cache"] == {
        "feature_hits": 110,
        "feature_misses": 0,
        "energy_hits": 100,
        "energy_misses": 10,
        "concat_hits": 1,
        "concat_misses": 1,
    }
    assert phoneme_report(energy_edit.report, "utt_alpha", 0)["cache"]["reused"] is True
    assert phoneme_report(energy_edit.report, "utt_alpha", 1)["cache"]["reused"] is False
    alpha_record = next(
        item
        for item in energy_edit.report["command_application_records"]
        if item["seq"] == 1
    )
    assert alpha_record["invalidated_states"] == ["ENERGY_NORMALIZE", "FRAME_CONCAT"]

    seq2 = tmp_path / "seq2.jsonl"
    write_jsonl(
        seq2,
        [
            {
                "seq": 2,
                "utterance_id": "utt_alpha",
                "phoneme_index": 1,
                "op": "set_duration",
                "duration_frames": 14,
            }
        ],
    )
    duration_edit = render(incremental_input, work, extra_commands=[seq2])
    assert duration_edit.status == "complete"
    first = phoneme_report(duration_edit.report, "utt_alpha", 0)
    second = phoneme_report(duration_edit.report, "utt_alpha", 1)
    third = phoneme_report(duration_edit.report, "utt_alpha", 2)
    assert first["cache"]["reused"] is True
    assert second["cache"]["feature_hits"] == 10
    assert second["cache"]["feature_misses"] == 4
    assert second["cache"]["energy_hits"] == 0
    assert second["cache"]["energy_misses"] == 14
    assert third["cache"]["reused"] is False
    assert third["invalidation_ranges"] == [[0, 6]]
    assert duration_edit.report["diagnostics"]["cache"] == {
        "feature_hits": 88,
        "feature_misses": 26,
        "energy_hits": 78,
        "energy_misses": 36,
        "concat_hits": 1,
        "concat_misses": 1,
    }

    seq3 = tmp_path / "seq3.jsonl"
    write_jsonl(
        seq3,
        [
            {
                "seq": 3,
                "utterance_id": "utt_beta",
                "phoneme_index": 2,
                "op": "set_f0_scale",
                "value": 1.3,
            }
        ],
    )
    f0_edit = render(incremental_input, work, extra_commands=[seq3])
    assert f0_edit.status == "complete"
    beta_t = phoneme_report(f0_edit.report, "utt_beta", 2)
    beta_sil = phoneme_report(f0_edit.report, "utt_beta", 3)
    assert beta_t["cache"]["feature_misses"] == 8
    assert "F0_TRACK" in beta_t["invalidated_states"]
    assert beta_sil["cache"]["reused"] is True

    merged_input = copy_demo(tmp_path / "merged_input", with_commands=False)
    write_jsonl(
        merged_input / "edit_commands.jsonl",
        [
            {
                "seq": 1,
                "utterance_id": "utt_alpha",
                "phoneme_index": 1,
                "op": "set_energy_scale",
                "value": 1.2,
            },
            {
                "seq": 2,
                "utterance_id": "utt_alpha",
                "phoneme_index": 1,
                "op": "set_duration",
                "duration_frames": 14,
            },
            {
                "seq": 3,
                "utterance_id": "utt_beta",
                "phoneme_index": 2,
                "op": "set_f0_scale",
                "value": 1.3,
            },
        ],
    )
    full = render(merged_input, tmp_path / "full_work")
    assert full.status == "complete"
    assert_outputs_equal(read_outputs(work), read_outputs(tmp_path / "full_work"))


def test_range_errors_are_skipped_without_mutation_and_duplicate_is_idempotent(
    tmp_path: Path,
) -> None:
    sample = copy_demo(tmp_path / "range_input", with_commands=False)
    work = tmp_path / "range_work"
    baseline = render(sample, work)
    before = read_outputs(work)

    invalid = tmp_path / "invalid_commands.jsonl"
    write_jsonl(
        invalid,
        [
            {
                "seq": 10,
                "utterance_id": "utt_alpha",
                "phoneme_index": 0,
                "op": "replace_frames",
                "start_frame": 0,
                "end_frame": 9999,
                "frame_count": 1,
                "frames": [{"f0_hz": 140.0}],
            },
            {
                "seq": 11,
                "utterance_id": "utt_alpha",
                "phoneme_index": 0,
                "op": "insert_frames",
                "start_frame": 0,
                "frame_count": 2,
                "frames": [{"f0_hz": 140.0}],
            },
            {
                "seq": 12,
                "utterance_id": "utt_alpha",
                "phoneme_index": 2,
                "op": "delete_frames",
                "start_frame": 0,
                "end_frame": 6,
            },
        ],
    )
    rejected = render(sample, work, extra_commands=[invalid])
    assert rejected.status == "complete"
    codes = {failure["code"] for failure in rejected.report["failures"]}
    assert "E_RANGE_OUT_OF_BOUND" in codes
    assert rejected.report["business_code_summary"][0]["code"] == "E_RANGE_OUT_OF_BOUND"
    assert_outputs_equal(before, read_outputs(work))
    rejected_records = [
        record
        for record in rejected.report["command_application_records"]
        if record["status"] == "rejected"
    ]
    assert [record["seq"] for record in rejected_records] == [10, 11, 12]
    assert all(record["code"] == "E_RANGE_OUT_OF_BOUND" for record in rejected_records)

    repeated = render(sample, work, extra_commands=[invalid])
    assert repeated.status == "complete"
    skipped = [
        record
        for record in repeated.report["command_application_records"]
        if record["status"] == "skipped_duplicate" and record["seq"] in {10, 11, 12}
    ]
    assert [record["seq"] for record in skipped] == [10, 11, 12]
    assert_outputs_equal(before, read_outputs(work))
