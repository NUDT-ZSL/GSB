"""Query/batch/cache consistency comparison."""

from __future__ import annotations

from typing import Any

from .errors import fail
from .util import quantize


def _canonical(value: Any, decimals: int) -> Any:
    if isinstance(value, float):
        return quantize(value, decimals)
    if isinstance(value, dict):
        return {key: _canonical(value[key], decimals) for key in sorted(value)}
    if isinstance(value, list):
        return [_canonical(item, decimals) for item in value]
    return value


def _raise(
    category: str,
    conv_id: str,
    first_diff_msg_no: int | None,
    expected: Any,
    actual: Any,
) -> None:
    fail(
        "E_INCONSISTENT",
        "query and batch results are inconsistent",
        category=category,
        conv_id=conv_id,
        first_diff_msg_no=first_diff_msg_no,
        expected=expected,
        actual=actual,
    )


def compare_results(
    expected: dict[str, Any],
    actual: dict[str, Any],
    decimals: int,
) -> None:
    conv_id = str(expected.get("conv_id", actual.get("conv_id", "")))
    expected_ids = list(expected.get("message_ids", []))
    actual_ids = list(actual.get("message_ids", []))
    if expected_ids != actual_ids:
        first_msg_no = None
        for index in range(max(len(expected_ids), len(actual_ids))):
            left = expected_ids[index] if index < len(expected_ids) else None
            right = actual_ids[index] if index < len(actual_ids) else None
            if left == right:
                continue
            item = left or right
            try:
                first_msg_no = int(item.rsplit(":", 1)[1])
            except (IndexError, ValueError):
                first_msg_no = None
            break
        _raise("message_set", conv_id, first_msg_no, expected_ids, actual_ids)

    if expected.get("batch_no") != actual.get("batch_no"):
        _raise(
            "batch",
            conv_id,
            None,
            expected.get("batch_no"),
            actual.get("batch_no"),
        )

    expected_messages = {
        int(message["msg_no"]): message for message in expected.get("messages", [])
    }
    actual_messages = {
        int(message["msg_no"]): message for message in actual.get("messages", [])
    }
    if set(expected_messages) != set(actual_messages):
        missing = sorted(set(expected_messages) ^ set(actual_messages))
        _raise(
            "message_set",
            conv_id,
            missing[0] if missing else None,
            sorted(expected_messages),
            sorted(actual_messages),
        )

    message_fields = (
        "text",
        "normalized_text",
        "weight",
        "effective_weight",
        "time_decay",
        "sentiment_label",
        "sentiment_scores",
        "sentiment_score",
        "sentiment_terms",
        "risk_hits",
        "risk_intensity",
    )
    for msg_no in sorted(expected_messages):
        left = expected_messages[msg_no]
        right = actual_messages[msg_no]
        for field_name in message_fields:
            if _canonical(left.get(field_name), decimals) != _canonical(
                right.get(field_name), decimals
            ):
                _raise(
                    f"message.{field_name}",
                    conv_id,
                    msg_no,
                    left.get(field_name),
                    right.get(field_name),
                )

    session_fields = (
        "sentiment_scores",
        "sentiment_score",
        "sentiment_label",
        "risk_score",
        "risk_level",
        "risk_candidates",
        "dominant_sentiment_msg_no",
        "dominant_risk_msg_no",
    )
    for field_name in session_fields:
        if _canonical(expected.get(field_name), decimals) != _canonical(
            actual.get(field_name), decimals
        ):
            _raise(
                field_name,
                conv_id,
                None,
                expected.get(field_name),
                actual.get(field_name),
            )

