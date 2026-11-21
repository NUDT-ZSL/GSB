from __future__ import annotations

from typing import Any, Callable, Iterable

from .canonical import quantize
from .errors import BusinessError, ErrorCode
from .models import LabelSet


def _summarize_line(line: dict[str, Any]) -> dict[str, Any]:
    return {
        "doc_id": line["doc_id"],
        "line_no": line["line_no"],
        "token_count": line["token_count"],
        "path_score": line["path_score"],
        "mean_confidence": line["mean_confidence"],
        "label_counts": dict(line["label_counts"]),
        "degradation_count": len(line.get("degradations", [])),
    }


def _check_label_counts(
    summary_line: dict[str, Any],
    labels: LabelSet,
) -> None:
    invalid = sorted(set(summary_line["label_counts"]) - set(labels.labels))
    if invalid:
        raise BusinessError(
            ErrorCode.E_LABEL_INVALID,
            "汇总行包含越界标签",
            doc_id=summary_line["doc_id"],
            line_no=summary_line["line_no"],
            stage="summarize",
            details={"invalid_labels": invalid},
        )


def run_summarization(
    tagging_payload: dict[str, Any],
    labels: LabelSet,
    *,
    reusable_lines: dict[str, dict[str, Any]] | None = None,
    recompute_doc_ids: Iterable[str] | None = None,
    progress_hook: Callable[[str, int, str], None] | None = None,
) -> dict[str, Any]:
    recompute = set(recompute_doc_ids) if recompute_doc_ids is not None else None
    reusable_lines = reusable_lines or {}
    lines: list[dict[str, Any]] = []
    degradations: list[dict[str, Any]] = []
    aggregate_labels = {label: 0 for label in labels.labels}
    token_count = 0
    confidence_sum = 0.0

    for processed, line in enumerate(tagging_payload["lines"], start=1):
        doc_id = line["doc_id"]
        if recompute is not None and doc_id not in recompute and doc_id in reusable_lines:
            summary_line = reusable_lines[doc_id]
        else:
            summary_line = _summarize_line(line)
        _check_label_counts(summary_line, labels)
        lines.append(summary_line)
        token_count += int(summary_line["token_count"])
        confidence_sum += float(summary_line["mean_confidence"])
        for label, count in summary_line["label_counts"].items():
            aggregate_labels[label] += int(count)
        degradations.extend(line.get("degradations", []))
        if progress_hook is not None:
            progress_hook("summarize", processed, doc_id)

    return {
        "stage": "summarize",
        "labels": labels.to_ref(),
        "label_set": list(labels.labels),
        "doc_count": len(lines),
        "line_count": len(lines),
        "token_count": token_count,
        "mean_confidence": (
            quantize(confidence_sum / float(len(lines))) if lines else 0.0
        ),
        "label_counts": {
            label: count for label, count in aggregate_labels.items() if count
        },
        "degradation_count": len(degradations),
        "degradations": degradations,
        "lines": lines,
    }
