from __future__ import annotations

from typing import Any, Iterable

from .models import CandidateTriple


def _key(candidate: CandidateTriple) -> tuple[str, int, str, str, str]:
    return (
        candidate.doc_id,
        candidate.sent_no,
        candidate.head_span_id,
        candidate.tail_span_id,
        candidate.relation_type,
    )


_KEY_FIELDS = (
    "doc_id",
    "sent_no",
    "head_span_id",
    "tail_span_id",
    "relation_type",
)


def _brief(candidate: CandidateTriple) -> dict[str, Any]:
    return {
        "triple_id": candidate.triple_id,
        "doc_id": candidate.doc_id,
        "sent_no": candidate.sent_no,
        "head_span_id": candidate.head_span_id,
        "tail_span_id": candidate.tail_span_id,
        "head_start": candidate.head_start,
        "tail_start": candidate.tail_start,
        "head_text": candidate.head_text,
        "tail_text": candidate.tail_text,
        "relation_type": candidate.relation_type,
        "relation_type_id": candidate.relation_type_id,
        "confidence_bp": candidate.confidence_bp,
        "confidence": candidate.confidence,
        "pattern_line": candidate.pattern_line,
        "trigger": candidate.trigger,
        "trigger_offset": candidate.trigger_offset,
    }


def compare_candidate_sets(
    *,
    doc_id: str,
    exact: Iterable[CandidateTriple],
    approximate: Iterable[CandidateTriple],
    mandatory_pattern_lines: Iterable[int] = (),
) -> dict[str, Any]:
    exact_map = {_key(candidate): candidate for candidate in exact}
    approx_map = {_key(candidate): candidate for candidate in approximate}
    missing_keys = sorted(set(exact_map) - set(approx_map))
    extra_keys = sorted(set(approx_map) - set(exact_map))
    shared_keys = sorted(set(exact_map) & set(approx_map))

    def brief_sort(item: dict[str, Any]) -> tuple[Any, ...]:
        return (
            item["head_start"],
            item["tail_start"],
            item["relation_type_id"],
            item["sent_no"],
            item["head_span_id"],
            item["tail_span_id"],
        )

    missing = sorted(
        (_brief(exact_map[key]) for key in missing_keys),
        key=brief_sort,
    )
    extra = sorted(
        (_brief(approx_map[key]) for key in extra_keys),
        key=brief_sort,
    )
    confidence_deviations: list[dict[str, Any]] = []
    pattern_deviations: list[dict[str, Any]] = []
    for key in shared_keys:
        exact_item = exact_map[key]
        approx_item = approx_map[key]
        if exact_item.confidence_bp != approx_item.confidence_bp:
            confidence_deviations.append(
                {
                    "triple": _brief(exact_item),
                    "exact_confidence_bp": exact_item.confidence_bp,
                    "approx_confidence_bp": approx_item.confidence_bp,
                    "delta_bp": approx_item.confidence_bp - exact_item.confidence_bp,
                }
            )
        if (
            exact_item.pattern_line != approx_item.pattern_line
            or exact_item.trigger_offset != approx_item.trigger_offset
        ):
            pattern_deviations.append(
                {
                    "triple": _brief(exact_item),
                    "exact_pattern_line": exact_item.pattern_line,
                    "approx_pattern_line": approx_item.pattern_line,
                    "exact_trigger_offset": exact_item.trigger_offset,
                    "approx_trigger_offset": approx_item.trigger_offset,
                }
            )

    mandatory_lines = set(mandatory_pattern_lines)
    differences: list[dict[str, Any]] = []
    for item in missing:
        cause = "APPROXIMATE_ENUMERATION_OMISSION"
        if item["pattern_line"] in mandatory_lines:
            cause = "MANDATORY_PATTERN_OMISSION"
        differences.append({"kind": cause, "triple": item, "path": "approx_missing"})
    for item in extra:
        differences.append(
            {
                "kind": "APPROXIMATE_ENUMERATION_EXTRA",
                "triple": item,
                "path": "approx_extra",
            }
        )
    for item in confidence_deviations:
        differences.append(
            {
                "kind": "PATTERN_SEMANTICS_MISMATCH",
                "kind_detail": "confidence_deviation",
                "triple": item["triple"],
            }
        )
    for item in pattern_deviations:
        differences.append(
            {
                "kind": "PATTERN_SEMANTICS_MISMATCH",
                "kind_detail": "pattern_or_trigger_deviation",
                "triple": item["triple"],
            }
        )

    recall = 1.0 if not exact_map else len(shared_keys) / len(exact_map)
    precision_vs_exact = (
        1.0 if not approx_map else len(shared_keys) / len(approx_map)
    )
    comparable = not missing and not extra and not confidence_deviations and not pattern_deviations
    first_divergence = None
    if not comparable:
        ordered = (
            missing
            or extra
            or [item["triple"] for item in confidence_deviations]
            or [item["triple"] for item in pattern_deviations]
        )
        first_divergence = ordered[0]
    return {
        "doc_id": doc_id,
        "exact_count": len(exact_map),
        "approx_count": len(approx_map),
        "common_count": len(shared_keys),
        "missing_in_approx": missing,
        "extra_in_approx": extra,
        "confidence_deviations": confidence_deviations,
        "pattern_deviations": pattern_deviations,
        "differences": differences,
        "recall_estimate": round(recall, 6),
        "precision_vs_exact": round(precision_vs_exact, 6),
        "statistics_comparable": comparable,
        "difference_causes": sorted(
            {difference["kind"] for difference in differences}
        ),
        "first_divergence": first_divergence,
    }

