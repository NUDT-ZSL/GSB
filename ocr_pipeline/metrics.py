"""Page-level metrics recomputable purely from per-line details."""

from __future__ import annotations

from .util import round6


def _char_correct(expected: str, actual: str) -> int:
    if len(expected) != len(actual):
        return sum(1 for left, right in zip(expected, actual) if left == right)
    return sum(1 for left, right in zip(expected, actual) if left == right)


def compute_metrics(line_records: list[dict], truth: list) -> dict:
    """Match records to truth by line_no and compute accuracy metrics."""
    truth_by_no = {item.line_no: item.text for item in truth}
    total_chars = 0
    correct_chars = 0
    total_lines = 0
    correct_lines = 0
    per_line: list[dict] = []

    for record in sorted(line_records, key=lambda item: item["line_no"]):
        line_no = record["line_no"]
        if line_no not in truth_by_no:
            continue
        expected = truth_by_no[line_no]
        actual = record["text"]
        total_lines += 1
        total_chars += len(expected)
        line_char_correct = _char_correct(expected, actual)
        correct_chars += line_char_correct
        line_correct = int(actual == expected)
        correct_lines += line_correct
        per_line.append(
            {
                "line_no": line_no,
                "expected": expected,
                "actual": actual,
                "char_correct": line_char_correct,
                "char_total": len(expected),
                "line_correct": bool(line_correct),
            }
        )

    return {
        "line_accuracy": round6(correct_lines / total_lines) if total_lines else 1.0,
        "char_accuracy": round6(correct_chars / total_chars) if total_chars else 1.0,
        "correct_lines": correct_lines,
        "total_lines": total_lines,
        "correct_chars": correct_chars,
        "total_chars": total_chars,
        "per_line": per_line,
    }


def recompute_from_per_line(per_line: list[dict]) -> dict:
    """Independent recomputation path used by the validation tool."""
    total_lines = len(per_line)
    correct_lines = sum(1 for item in per_line if item["line_correct"])
    total_chars = sum(item["char_total"] for item in per_line)
    correct_chars = sum(item["char_correct"] for item in per_line)
    return {
        "line_accuracy": round6(correct_lines / total_lines) if total_lines else 1.0,
        "char_accuracy": round6(correct_chars / total_chars) if total_chars else 1.0,
        "correct_lines": correct_lines,
        "total_lines": total_lines,
        "correct_chars": correct_chars,
        "total_chars": total_chars,
    }
