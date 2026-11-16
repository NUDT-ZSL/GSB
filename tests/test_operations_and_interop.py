from __future__ import annotations

from pathlib import Path

import pytest

from asr_editor.codes import E_TIME_OVERFLOW, E_TOKEN_TIME_INFERRED
from asr_editor.formats import build_sidecar, write_sidecar, write_v1, write_v2
from asr_editor.interop import upgrade_v2
from asr_editor.operations import OperationError, apply_operation
from asr_editor.parsers import parse_v1_file, parse_v2_file


def _initial(sample_dir: Path):
    return parse_v1_file(sample_dir / "sample01.v1.json").transcript


def test_merge_split_shift_replace(sample_dir: Path) -> None:
    current = _initial(sample_dir)
    merged = apply_operation(
        current,
        {"op": "merge", "utterance_id": "utt-001", "other_utterance_id": "utt-002"},
    ).transcript
    assert len(merged.utterances) == 2
    assert len(merged.utterances[0].tokens) == 6

    split = apply_operation(
        merged,
        {"op": "split", "utterance_id": "utt-001", "token_index": 3},
    ).transcript
    assert len(split.utterances) == 3
    assert split.utterances[0].end_ms == 1250
    assert split.utterances[1].start_ms == 1500
    assert split.utterances[0].end_ms <= split.utterances[1].start_ms
    assert split.utterances[0].end_ms % 10 == 0

    shifted = apply_operation(
        split,
        {"op": "shift", "start_ms": 1500, "end_ms": 2900, "delta_ms": 250},
    ).transcript
    assert shifted.utterances[1].start_ms == 1750
    assert shifted.utterances[1].end_ms == 3150

    replaced = apply_operation(
        shifted,
        {"op": "replace", "utterance_id": "utt-003", "token_index": 1, "text": "总结"},
    ).transcript
    assert replaced.utterances[2].tokens[1].text == "总结"
    assert replaced.utterances[2].text == "生成 总结 完成"


def test_shift_overflow_is_rejected_without_mutation(sample_dir: Path) -> None:
    current = _initial(sample_dir)
    before = current.to_dict()
    with pytest.raises(OperationError) as exc_info:
        apply_operation(
            current, {"op": "shift", "start_ms": 0, "end_ms": 1500, "delta_ms": -10}
        )
    assert exc_info.value.code == E_TIME_OVERFLOW
    assert current.to_dict() == before


def test_split_boundary_rounds_to_grid(sample_dir: Path) -> None:
    current = _initial(sample_dir)
    current.utterances[0].tokens[1].start_ms = 1234
    current.utterances[0].tokens[1].end_ms = 1500
    result = apply_operation(
        current, {"op": "split", "utterance_id": "utt-001", "token_index": 1}
    )
    assert result.details["split_boundary_ms"] == 1230
    assert result.grid_rounding_count == 1
    assert result.grid_max_error_us == 4000


def test_downgrade_upgrade_sidecar_restores_exact_tokens(
    sample_dir: Path, tmp_path: Path
) -> None:
    transcript = _initial(sample_dir)
    v1_path = tmp_path / "a.v1.json"
    v2_path = tmp_path / "a.v2.srt"
    sidecar_path = tmp_path / "a.tokens.meta.json"
    write_v1(v1_path, transcript)
    write_v2(v2_path, transcript)
    write_sidecar(sidecar_path, build_sidecar(transcript))
    parsed_v2 = parse_v2_file(v2_path)
    upgraded = upgrade_v2(parsed_v2.transcript, sidecar_path)
    assert upgraded.sidecar_used is True
    assert upgraded.inferred_time_count == 0
    assert not upgraded.failures
    for original, restored in zip(
        transcript.utterances, upgraded.transcript.utterances
    ):
        assert [
            (token.text, token.start_ms, token.end_ms) for token in original.tokens
        ] == [(token.text, token.start_ms, token.end_ms) for token in restored.tokens]


def test_upgrade_without_sidecar_marks_inferred(
    sample_dir: Path, tmp_path: Path
) -> None:
    transcript = _initial(sample_dir)
    v2_path = tmp_path / "a.v2.srt"
    write_v2(v2_path, transcript)
    parsed_v2 = parse_v2_file(v2_path)
    upgraded = upgrade_v2(parsed_v2.transcript, None)
    assert upgraded.sidecar_used is False
    assert upgraded.inferred_time_count == transcript.token_count
    assert E_TOKEN_TIME_INFERRED in upgraded.diagnostic_codes
    assert upgraded.transcript.timing_origin == "v2-inferred"
    assert all(
        utterance.origin is not None for utterance in upgraded.transcript.utterances
    )


def test_upgrade_with_hash_mismatch_falls_back(
    sample_dir: Path, tmp_path: Path
) -> None:
    transcript = _initial(sample_dir)
    v2_path = tmp_path / "a.v2.srt"
    sidecar_path = tmp_path / "a.tokens.meta.json"
    write_v2(v2_path, transcript)
    write_sidecar(sidecar_path, build_sidecar(transcript))
    v2_path.write_text(
        v2_path.read_text(encoding="utf-8").replace(
            "00:00:00,000 --> 00:00:01,250", "00:00:00,010 --> 00:00:01,250", 1
        ),
        encoding="utf-8",
    )
    parsed_v2 = parse_v2_file(v2_path)
    upgraded = upgrade_v2(parsed_v2.transcript, sidecar_path)
    assert upgraded.sidecar_used is False
    assert upgraded.inferred_time_count == transcript.token_count
    assert E_TOKEN_TIME_INFERRED in upgraded.diagnostic_codes


def test_tampered_sidecar_is_reported_and_falls_back(
    sample_dir: Path, tmp_path: Path
) -> None:
    transcript = _initial(sample_dir)
    v2_path = tmp_path / "a.v2.srt"
    sidecar_path = tmp_path / "a.tokens.meta.json"
    write_v2(v2_path, transcript)
    sidecar = build_sidecar(transcript)
    sidecar["utterances"][0]["tokens"][0]["end_ms"] += 1
    write_sidecar(sidecar_path, sidecar)
    parsed_v2 = parse_v2_file(v2_path)
    upgraded = upgrade_v2(parsed_v2.transcript, sidecar_path)
    assert upgraded.sidecar_used is False
    assert upgraded.failures
    assert upgraded.failures[0].stage == "upgrade"
    assert E_TOKEN_TIME_INFERRED in upgraded.diagnostic_codes


def test_non_adjacent_merge_is_rejected(sample_dir: Path) -> None:
    current = _initial(sample_dir)
    with pytest.raises(OperationError):
        apply_operation(
            current,
            {"op": "merge", "utterance_id": "utt-001", "other_utterance_id": "utt-003"},
        )
