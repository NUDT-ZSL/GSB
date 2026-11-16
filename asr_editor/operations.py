"""Interoperable edit operations on the unified token-level representation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .codes import E_EDIT_INVALID, E_TIME_OVERFLOW
from .models import Origin, Transcript, Utterance
from .parsers import has_invalid_utf8
from .timeutil import (
    GRID_MS,
    PrecisionStats,
    seconds_text,
    seconds_to_ms,
    split_boundary,
)


class OperationError(ValueError):
    def __init__(self, code: str, message: str, item_index: int | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.item_index = item_index


@dataclass(slots=True)
class OperationResult:
    transcript: Transcript
    summary: str
    details: dict[str, Any]
    grid_rounding_count: int = 0
    grid_max_error_us: int = 0


def _require_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise OperationError(
            E_EDIT_INVALID, f"{name} must be an integer millisecond value"
        )
    return value


def _optional_int(value: Any, name: str) -> int | None:
    if value is None:
        return None
    return _require_int(value, name)


def _find_utterance(
    transcript: Transcript, operation: dict[str, Any]
) -> tuple[int, Utterance]:
    uid = operation.get("utterance_id", operation.get("id"))
    raw_index = operation.get("utterance_index")
    if uid is not None:
        for index, utterance in enumerate(transcript.utterances):
            if utterance.uid == uid:
                return index, utterance
        raise OperationError(E_EDIT_INVALID, f"utterance {uid!r} not found")
    if raw_index is not None:
        index = _require_int(raw_index, "utterance_index")
        if index < 0 or index >= len(transcript.utterances):
            raise OperationError(
                E_EDIT_INVALID, f"utterance index {index} out of range"
            )
        return index, transcript.utterances[index]
    raise OperationError(
        E_EDIT_INVALID, "operation requires utterance_id or utterance_index"
    )


def _unique_id(transcript: Transcript, base: str) -> str:
    existing = {utterance.uid for utterance in transcript.utterances}
    if base not in existing:
        return base
    suffix = 2
    while f"{base}-{suffix}" in existing:
        suffix += 1
    return f"{base}-{suffix}"


def _merge(transcript: Transcript, operation: dict[str, Any]) -> OperationResult:
    left_index, _ = _find_utterance(transcript, operation)
    other_uid = operation.get(
        "other_utterance_id", operation.get("with", operation.get("next"))
    )
    if other_uid is not None:
        try:
            right_index = next(
                index
                for index, utterance in enumerate(transcript.utterances)
                if utterance.uid == other_uid
            )
        except StopIteration as exc:
            raise OperationError(
                E_EDIT_INVALID, f"utterance {other_uid!r} not found"
            ) from exc
    else:
        right_index = left_index + 1
    if right_index < 0 or right_index >= len(transcript.utterances):
        raise OperationError(E_EDIT_INVALID, "no adjacent utterance to merge")
    if abs(right_index - left_index) != 1:
        raise OperationError(E_EDIT_INVALID, "merge requires adjacent utterances")
    first_index = min(left_index, right_index)
    second_index = max(left_index, right_index)
    first = transcript.utterances[first_index]
    second = transcript.utterances[second_index]

    merged_tokens = list(first.tokens) + list(second.tokens)
    if not merged_tokens:
        raise OperationError(E_EDIT_INVALID, "cannot merge utterances without tokens")
    new_start = merged_tokens[0].start_ms
    new_end = merged_tokens[-1].end_ms
    first_origin = first.origin
    second_origin = second.origin
    if first_origin and second_origin:
        origin = Origin(
            source_version=first_origin.source_version,
            start_ms=first_origin.start_ms,
            end_ms=second_origin.end_ms,
            text=f"{first_origin.text or ''} {second_origin.text or ''}".strip(),
            start_raw=first_origin.start_raw,
            end_raw=second_origin.end_raw,
            source_file=first_origin.source_file,
            source_line=first_origin.source_line,
        )
    else:
        origin = first_origin or second_origin

    merged = Utterance(
        uid=first.uid,
        start_ms=new_start,
        end_ms=new_end,
        tokens=merged_tokens,
        origin=origin,
    )
    transcript.utterances = [
        utterance
        for index, utterance in enumerate(transcript.utterances)
        if index not in (first_index, second_index)
    ]
    transcript.utterances.insert(first_index, merged)
    transcript.utterances.sort(
        key=lambda utterance: (utterance.start_ms, utterance.end_ms)
    )
    return OperationResult(
        transcript=transcript,
        summary=f"merged {first.uid} and {second.uid} as {merged.uid}",
        details={
            "merged_ids": [first.uid, second.uid],
            "result_id": merged.uid,
            "token_count": len(merged_tokens),
        },
    )


def _split(transcript: Transcript, operation: dict[str, Any]) -> OperationResult:
    _, utterance = _find_utterance(transcript, operation)
    raw_token_index = operation.get(
        "token_index", operation.get("at_token_index", operation.get("before_token"))
    )
    token_index = _require_int(raw_token_index, "token_index")
    if token_index <= 0 or token_index >= len(utterance.tokens):
        raise OperationError(
            E_EDIT_INVALID,
            f"token_index must be between 1 and {len(utterance.tokens) - 1}",
        )

    right_tokens = utterance.tokens[token_index:]
    left_tokens = utterance.tokens[:token_index]
    boundary, grid_error = split_boundary(right_tokens[0].start_ms, GRID_MS)
    # Preserve all original word times.  Splitting at an existing token
    # boundary only snaps the first token of the right-hand group to the
    # required 10 ms grid; it never stretches the preceding token across an
    # original inter-word or inter-utterance pause.
    original_boundary = right_tokens[0].start_ms
    boundary = max(left_tokens[-1].end_ms, boundary, 0)
    boundary = min(right_tokens[0].end_ms, boundary)
    right_tokens[0].start_ms = boundary
    grid_error = abs(boundary - original_boundary)
    max_error_us = grid_error * 1000

    left = Utterance(
        uid=utterance.uid,
        start_ms=left_tokens[0].start_ms,
        end_ms=left_tokens[-1].end_ms,
        tokens=left_tokens,
        origin=utterance.origin,
    )
    right_uid = operation.get("new_id") or f"{utterance.uid}-split-{token_index}"
    right_uid = _unique_id(transcript, str(right_uid))
    right_text = " ".join(token.text for token in right_tokens)
    if utterance.origin:
        right_origin = Origin(
            source_version=utterance.origin.source_version,
            start_ms=right_tokens[0].start_ms,
            end_ms=right_tokens[-1].end_ms,
            text=right_text,
            start_raw=seconds_text(right_tokens[0].start_ms),
            end_raw=seconds_text(right_tokens[-1].end_ms),
            source_file=utterance.origin.source_file,
            source_line=utterance.origin.source_line,
        )
    else:
        right_origin = None
    right = Utterance(
        uid=right_uid,
        start_ms=right_tokens[0].start_ms,
        end_ms=right_tokens[-1].end_ms,
        tokens=right_tokens,
        origin=right_origin,
    )
    location = next(
        index
        for index, item in enumerate(transcript.utterances)
        if item.uid == utterance.uid
    )
    transcript.utterances[location] = left
    transcript.utterances.insert(location + 1, right)
    return OperationResult(
        transcript=transcript,
        summary=f"split {left.uid} at token {token_index} into {left.uid} and {right.uid}",
        details={
            "left_id": left.uid,
            "right_id": right.uid,
            "split_boundary_ms": right_tokens[0].start_ms,
            "grid_adjustment_ms": grid_error,
        },
        grid_rounding_count=1 if grid_error else 0,
        grid_max_error_us=max_error_us,
    )


def _shift(transcript: Transcript, operation: dict[str, Any]) -> OperationResult:
    start_ms = operation.get("start_ms")
    end_ms = operation.get("end_ms")
    if start_ms is None and "start" in operation:
        start_ms = seconds_to_ms(operation["start"])[0]
    if end_ms is None and "end" in operation:
        end_ms = seconds_to_ms(operation["end"])[0]
    start_ms = _require_int(start_ms, "start_ms")
    end_ms = _require_int(end_ms, "end_ms")
    delta_ms = operation.get("delta_ms", operation.get("shift_ms"))
    delta_ms = _require_int(delta_ms, "delta_ms")
    if start_ms < 0 or end_ms < start_ms:
        raise OperationError(E_EDIT_INVALID, "shift interval is invalid")

    selected: list[tuple[Utterance, int]] = []
    for utterance in transcript.utterances:
        for token_index, token in enumerate(utterance.tokens):
            if token.start_ms < end_ms and token.end_ms > start_ms:
                selected.append((utterance, token_index))
    for utterance, token_index in selected:
        token = utterance.tokens[token_index]
        if token.start_ms + delta_ms < 0 or token.end_ms + delta_ms < 0:
            raise OperationError(
                E_TIME_OVERFLOW,
                "timeline shift would produce a negative timestamp",
                token_index,
            )

    touched_utterances: set[int] = set()
    for utterance, token_index in selected:
        token = utterance.tokens[token_index]
        token.start_ms += delta_ms
        token.end_ms += delta_ms
        touched_utterances.add(id(utterance))
    for utterance in transcript.utterances:
        if id(utterance) in touched_utterances:
            utterance.reflow_bounds()
    transcript.utterances.sort(
        key=lambda utterance: (utterance.start_ms, utterance.end_ms)
    )
    return OperationResult(
        transcript=transcript,
        summary=f"shifted {len(selected)} tokens in [{start_ms}, {end_ms}) by {delta_ms} ms",
        details={
            "tokens_shifted": len(selected),
            "delta_ms": delta_ms,
            "range_ms": [start_ms, end_ms],
        },
    )


def _replace(transcript: Transcript, operation: dict[str, Any]) -> OperationResult:
    _, utterance = _find_utterance(transcript, operation)
    raw_token_index = operation.get("token_index")
    token_index = _require_int(raw_token_index, "token_index")
    if token_index < 0 or token_index >= len(utterance.tokens):
        raise OperationError(E_EDIT_INVALID, f"token index {token_index} out of range")
    new_text = operation.get("text", operation.get("new_text"))
    if not isinstance(new_text, str) or not new_text.strip():
        raise OperationError(E_EDIT_INVALID, "replacement token text cannot be empty")
    if has_invalid_utf8(new_text):
        raise OperationError(
            E_EDIT_INVALID, "replacement token text is not valid UTF-8"
        )
    token = utterance.tokens[token_index]
    old_text = token.text
    token.text = new_text
    return OperationResult(
        transcript=transcript,
        summary=f"replaced token {old_text!r} with {new_text!r} in {utterance.uid}",
        details={
            "utterance_id": utterance.uid,
            "token_index": token_index,
            "old_text": old_text,
            "new_text": new_text,
        },
    )


def apply_operation(
    transcript: Transcript, operation: dict[str, Any]
) -> OperationResult:
    if not isinstance(operation, dict):
        raise OperationError(E_EDIT_INVALID, "operation must be an object")
    op_name = operation.get("op", operation.get("type"))
    if not isinstance(op_name, str):
        raise OperationError(E_EDIT_INVALID, "operation requires op/type")
    op_name = op_name.lower()
    working = transcript.normalized_copy()
    if op_name == "merge":
        return _merge(working, operation)
    if op_name == "split":
        return _split(working, operation)
    if op_name == "shift":
        return _shift(working, operation)
    if op_name in {"replace", "replace_token", "text"}:
        return _replace(working, operation)
    raise OperationError(E_EDIT_INVALID, f"unknown operation {op_name!r}")


def apply_operation_with_precision(
    transcript: Transcript,
    operation: dict[str, Any],
    precision: PrecisionStats,
) -> OperationResult:
    result = apply_operation(transcript, operation)
    if result.grid_rounding_count:
        precision.grid_rounding_count += result.grid_rounding_count
    if result.grid_max_error_us:
        precision.grid_max_error_us = max(
            precision.grid_max_error_us, result.grid_max_error_us
        )
    return result
