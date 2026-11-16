"""Cross-format consistency and numeric-precision reporting."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .codes import E_CROSS_FORMAT_MISMATCH, E_TOKEN_TIME_INFERRED
from .formats import atomic_write_json, normalized_text, utterance_sentence_times
from .models import Transcript, Utterance
from .parsers import FailureRecord
from .timeutil import PrecisionStats

TIME_TOLERANCE_MS = 20


def _safe_utterance(transcript: Transcript, index: int) -> Utterance | None:
    if 0 <= index < len(transcript.utterances):
        return transcript.utterances[index]
    return None


def build_cross_format_report(
    transcript_v1: Transcript,
    transcript_v2: Transcript,
    snapshot_version: int = 0,
    failures: list[FailureRecord] | None = None,
    diagnostic_codes: list[str] | None = None,
    precision_v1: PrecisionStats | None = None,
    precision_v2: PrecisionStats | None = None,
    source_v1: str | None = None,
    source_v2: str | None = None,
    snapshot: dict[str, Any] | None = None,
) -> dict[str, Any]:
    failures = failures or []
    diagnostic_codes = diagnostic_codes or []
    precision = PrecisionStats()
    precision.merge(precision_v1 or PrecisionStats())
    precision.merge(precision_v2 or PrecisionStats())

    utterances: list[dict[str, Any]] = []
    max_index = max(len(transcript_v1.utterances), len(transcript_v2.utterances))
    mismatch_count = 0
    inferred_total = 0
    for index in range(max_index):
        left = _safe_utterance(transcript_v1, index)
        right = _safe_utterance(transcript_v2, index)
        entry_codes: list[str] = []
        if left is None or right is None:
            entry_codes.append(E_CROSS_FORMAT_MISMATCH)
            mismatch_count += 1
            inferred = left.inferred_time_count if left else 0
            inferred += right.inferred_time_count if right else 0
            inferred_total += inferred
            utterances.append(
                {
                    "index": index,
                    "id_v1": left.uid if left else None,
                    "id_v2": right.uid if right else None,
                    "start_v1_ms": utterance_sentence_times(left)[0] if left else None,
                    "end_v1_ms": utterance_sentence_times(left)[1] if left else None,
                    "start_v2_ms": utterance_sentence_times(right)[0]
                    if right
                    else None,
                    "end_v2_ms": utterance_sentence_times(right)[1] if right else None,
                    "start_deviation_ms": None,
                    "end_deviation_ms": None,
                    "max_time_deviation_ms": None,
                    "token_count_v1": len(left.tokens) if left else None,
                    "token_count_v2": len(right.tokens) if right else None,
                    "token_count_difference": (len(left.tokens) - len(right.tokens))
                    if left and right
                    else None,
                    "text_equal": False,
                    "inferred_time_count": inferred,
                    "codes": entry_codes,
                }
            )
            continue

        left_start, left_end = utterance_sentence_times(left)
        right_start, right_end = utterance_sentence_times(right)
        start_deviation = abs(left_start - right_start)
        end_deviation = abs(left_end - right_end)
        token_difference = len(left.tokens) - len(right.tokens)
        text_equal = normalized_text(left.text) == normalized_text(right.text)
        inferred = left.inferred_time_count + right.inferred_time_count
        inferred_total += inferred
        if start_deviation > TIME_TOLERANCE_MS or end_deviation > TIME_TOLERANCE_MS:
            entry_codes.append(E_CROSS_FORMAT_MISMATCH)
        if token_difference != 0:
            entry_codes.append(E_CROSS_FORMAT_MISMATCH)
        if not text_equal:
            entry_codes.append(E_CROSS_FORMAT_MISMATCH)
        entry_codes = list(dict.fromkeys(entry_codes))
        if entry_codes:
            mismatch_count += 1
        utterances.append(
            {
                "index": index,
                "id_v1": left.uid,
                "id_v2": right.uid,
                "start_v1_ms": left_start,
                "end_v1_ms": left_end,
                "start_v2_ms": right_start,
                "end_v2_ms": right_end,
                "start_deviation_ms": start_deviation,
                "end_deviation_ms": end_deviation,
                "max_time_deviation_ms": max(start_deviation, end_deviation),
                "token_count_v1": len(left.tokens),
                "token_count_v2": len(right.tokens),
                "token_count_difference": token_difference,
                "text_equal": text_equal,
                "inferred_time_count": inferred,
                "codes": entry_codes,
            }
        )

    failure_codes = [failure.code for failure in failures]
    codes = list(dict.fromkeys(failure_codes + diagnostic_codes))
    if mismatch_count:
        codes.append(E_CROSS_FORMAT_MISMATCH)
    if inferred_total:
        codes.append(E_TOKEN_TIME_INFERRED)
    codes = list(dict.fromkeys(codes))

    report = {
        "schema_version": 1,
        "kind": "asr-interop-cross-format-diagnostics",
        "source_files": {"v1": source_v1, "v2": source_v2},
        "snapshot_version": snapshot_version,
        "time_tolerance_ms": TIME_TOLERANCE_MS,
        "business_codes": codes,
        "summary": {
            "utterance_count_v1": len(transcript_v1.utterances),
            "utterance_count_v2": len(transcript_v2.utterances),
            "mismatch_utterance_count": mismatch_count,
            "inferred_time_count": inferred_total,
            "failure_count": len(failures),
        },
        "utterances": utterances,
        "precision": {
            "internal_unit": "integer_millisecond",
            "output_seconds_decimal_places": 3,
            "split_grid_ms": 10,
            "rounding_count": precision.rounding_count,
            "parse_rounding_count": precision.parse_rounding_count,
            "grid_rounding_count": precision.grid_rounding_count,
            "max_rounding_error_ms": precision.max_error_us / 1000,
            "max_parse_rounding_error_ms": precision.parse_max_error_us / 1000,
            "max_grid_boundary_error_ms": precision.grid_max_error_us / 1000,
            "output_conversion_rounding_count": 0,
            "meets_10ms_granularity": precision.grid_max_error_us <= 5000,
            "explanation": "All arithmetic uses integer milliseconds; output uses three exact decimal places.",
        },
        "snapshot": snapshot,
        "failures": [failure.to_dict() for failure in failures],
    }
    return report


def write_report(path: str | Path, report: dict[str, Any]) -> None:
    atomic_write_json(path, report)
