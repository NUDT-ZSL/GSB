"""Cross-path consistency adjudication with auditable differences."""

from __future__ import annotations

from typing import Any

from .disambiguation import disambiguate_documents
from .errors import E_PATH_DIVERGENCE, PipelineError
from .scoring import recompute_score
from .util import rounded


def _decision_for_path(
    selected: dict[str, Any],
    path: str,
) -> dict[str, Any]:
    decision = {
        "selected_path": path,
        "approximate_limits": selected["approximate_limits"],
        "coverage": selected["coverage"],
    }
    return decision


def _aggregate_by_sense(
    documents: list[dict[str, Any]],
    field: str,
) -> dict[tuple[str, int], dict[str, Any]]:
    grouped: dict[tuple[str, int], dict[str, Any]] = {}
    for doc in documents:
        for result in doc["results"]:
            key = (result["term"], result["sense_id"])
            entry = grouped.setdefault(
                key,
                {
                    "term": result["term"],
                    "sense_id": result["sense_id"],
                    "count": 0,
                    "confidence_sum": 0.0,
                    "disambiguation_score_sum": 0.0,
                },
            )
            entry["count"] += 1
            entry["confidence_sum"] += float(result["candidate_confidence"])
            entry["disambiguation_score_sum"] += float(
                result["disambiguation_score"]
            )
    for entry in grouped.values():
        entry["confidence_mean"] = rounded(
            entry["confidence_sum"] / entry["count"], 12
        )
        entry["disambiguation_score_mean"] = rounded(
            entry["disambiguation_score_sum"] / entry["count"], 12
        )
    return grouped


def _recompute_disambiguation_score(result: dict[str, Any]) -> float:
    breakdown = result["score_breakdown"]
    if breakdown["formula"] == "monosemous":
        return float(breakdown["selected_score"])
    digits = 12
    selected_sense = next(
        item
        for item in breakdown["sense_scores"]
        if item["sense_id"] == breakdown["selected_sense_id"]
    )
    return rounded(
        selected_sense["prior"]
        + sum(
            contribution["contribution"]
            for contribution in selected_sense["rule_contributions"]
        ),
        digits,
    )


def compare_paths(
    documents: list[dict[str, Any]],
    scored_documents: list[dict[str, Any]],
    glossary: dict[str, Any],
    rules: dict[str, Any],
    features: dict[str, Any],
    selected_decision: dict[str, Any],
    config: dict[str, Any],
    *,
    strict: bool = False,
) -> dict[str, Any]:
    exact_decision = _decision_for_path(selected_decision, "exact")
    approximate_decision = _decision_for_path(selected_decision, "approximate")
    exact_docs = disambiguate_documents(
        documents,
        scored_documents,
        glossary,
        rules,
        features,
        exact_decision,
        config,
    )
    approximate_docs = disambiguate_documents(
        documents,
        scored_documents,
        glossary,
        rules,
        features,
        approximate_decision,
        config,
    )

    exact_by_candidate = {
        result["candidate_id"]: result
        for doc in exact_docs
        for result in doc["results"]
    }
    approximate_by_candidate = {
        result["candidate_id"]: result
        for doc in approximate_docs
        for result in doc["results"]
    }

    exact_candidate_ids = set(exact_by_candidate)
    approximate_candidate_ids = set(approximate_by_candidate)
    omitted_ids = sorted(exact_candidate_ids - approximate_candidate_ids)
    extra_ids = sorted(approximate_candidate_ids - exact_candidate_ids)
    candidate_pruning_omissions = [
        {
            "candidate_id": candidate_id,
            "doc_id": exact_by_candidate[candidate_id]["doc_id"],
            "term": exact_by_candidate[candidate_id]["term"],
            "sense_id": exact_by_candidate[candidate_id]["sense_id"],
            "classification": "candidate_pruning_omission",
        }
        for candidate_id in omitted_ids
    ]
    unexpected_candidates = [
        {
            "candidate_id": candidate_id,
            "doc_id": approximate_by_candidate[candidate_id]["doc_id"],
            "term": approximate_by_candidate[candidate_id]["term"],
            "classification": "unexpected_candidate",
        }
        for candidate_id in extra_ids
    ]

    exact_terms = sorted({result["term"] for result in exact_by_candidate.values()})
    approximate_terms = sorted(
        {result["term"] for result in approximate_by_candidate.values()}
    )
    exact_senses = sorted(
        {
            (result["term"], result["sense_id"])
            for result in exact_by_candidate.values()
        }
    )
    approximate_senses = sorted(
        {
            (result["term"], result["sense_id"])
            for result in approximate_by_candidate.values()
        }
    )

    exact_aggregate = _aggregate_by_sense(exact_docs, "exact")
    approximate_aggregate = _aggregate_by_sense(approximate_docs, "approximate")
    sense_keys = sorted(set(exact_aggregate) | set(approximate_aggregate))
    per_sense_confidence_deviation = []
    max_confidence_delta = 0.0
    sense_counts_match = True
    for key in sense_keys:
        exact = exact_aggregate.get(
            key,
            {
                "count": 0,
                "confidence_mean": 0.0,
                "disambiguation_score_mean": 0.0,
            },
        )
        approximate = approximate_aggregate.get(
            key,
            {
                "count": 0,
                "confidence_mean": 0.0,
                "disambiguation_score_mean": 0.0,
            },
        )
        delta = rounded(
            float(approximate["confidence_mean"])
            - float(exact["confidence_mean"]),
            12,
        )
        max_confidence_delta = max(max_confidence_delta, abs(delta))
        if exact["count"] != approximate["count"]:
            sense_counts_match = False
        per_sense_confidence_deviation.append(
            {
                "term": key[0],
                "sense_id": key[1],
                "exact_count": exact["count"],
                "approximate_count": approximate["count"],
                "exact_confidence_mean": exact["confidence_mean"],
                "approximate_confidence_mean": approximate["confidence_mean"],
                "confidence_delta": delta,
                "exact_disambiguation_score_mean": exact[
                    "disambiguation_score_mean"
                ],
                "approximate_disambiguation_score_mean": approximate[
                    "disambiguation_score_mean"
                ],
                "disambiguation_score_delta": rounded(
                    float(approximate["disambiguation_score_mean"])
                    - float(exact["disambiguation_score_mean"]),
                    12,
                ),
            }
        )

    common_ids = sorted(exact_candidate_ids & approximate_candidate_ids)
    context_comparison_changes = []
    contribution_rows = []
    recomputation_errors: list[dict[str, Any]] = []
    for candidate_id in common_ids:
        exact = exact_by_candidate[candidate_id]
        approximate = approximate_by_candidate[candidate_id]
        exact_recomputed = _recompute_disambiguation_score(exact)
        approximate_recomputed = _recompute_disambiguation_score(approximate)
        exact_candidate_recomputed = recompute_score(
            exact["candidate_score_breakdown"]
        )
        approximate_candidate_recomputed = recompute_score(
            approximate["candidate_score_breakdown"]
        )
        if (
            abs(
                exact_candidate_recomputed
                - float(exact["candidate_confidence"])
            )
            > 1e-12
            or abs(
                approximate_candidate_recomputed
                - float(approximate["candidate_confidence"])
            )
            > 1e-12
        ):
            recomputation_errors.append(
                {
                    "candidate_id": candidate_id,
                    "type": "candidate_confidence",
                    "exact_recomputed": exact_candidate_recomputed,
                    "exact_recorded": exact["candidate_confidence"],
                    "approximate_recomputed": approximate_candidate_recomputed,
                    "approximate_recorded": approximate[
                        "candidate_confidence"
                    ],
                }
            )
        if (
            abs(exact_recomputed - float(exact["disambiguation_score"]))
            > 1e-12
            or abs(
                approximate_recomputed
                - float(approximate["disambiguation_score"])
            )
            > 1e-12
        ):
            recomputation_errors.append(
                {
                    "candidate_id": candidate_id,
                    "type": "disambiguation_score",
                    "exact_recomputed": exact_recomputed,
                    "exact_recorded": exact["disambiguation_score"],
                    "approximate_recomputed": approximate_recomputed,
                    "approximate_recorded": approximate[
                        "disambiguation_score"
                    ],
                }
            )
        if (
            exact["sense_id"] != approximate["sense_id"]
            or exact["disambiguation_score"]
            != approximate["disambiguation_score"]
        ):
            change = {
                "candidate_id": candidate_id,
                "doc_id": exact["doc_id"],
                "term": exact["term"],
                "exact_sense_id": exact["sense_id"],
                "approximate_sense_id": approximate["sense_id"],
                "exact_disambiguation_score": exact["disambiguation_score"],
                "approximate_disambiguation_score": approximate[
                    "disambiguation_score"
                ],
                "score_delta": rounded(
                    float(approximate["disambiguation_score"])
                    - float(exact["disambiguation_score"]),
                    12,
                ),
                "exact_omitted_features": exact["omitted_context_features"],
                "approximate_omitted_features": approximate[
                    "omitted_context_features"
                ],
                "classification": (
                    "context_comparison_change"
                    if exact["omitted_context_features"]
                    == approximate["omitted_context_features"]
                    else "context_feature_restriction"
                ),
            }
            context_comparison_changes.append(change)
            contribution_rows.append(
                {
                    "candidate_id": candidate_id,
                    "exact": {
                        "score_breakdown": exact["score_breakdown"],
                        "candidate_score_breakdown": exact[
                            "candidate_score_breakdown"
                        ],
                        "recomputed_disambiguation_score": exact_recomputed,
                    },
                    "approximate": {
                        "score_breakdown": approximate["score_breakdown"],
                        "candidate_score_breakdown": approximate[
                            "candidate_score_breakdown"
                        ],
                        "recomputed_disambiguation_score": approximate_recomputed,
                    },
                }
            )

    exact_by_doc = {doc["doc_id"]: doc for doc in exact_docs}
    approximate_by_doc = {doc["doc_id"]: doc for doc in approximate_docs}
    per_document_adjudication = []
    for doc in documents:
        doc_id = doc["doc_id"]
        exact_results = exact_by_doc[doc_id]["results"]
        approximate_results = approximate_by_doc[doc_id]["results"]
        exact_ids = {result["candidate_id"] for result in exact_results}
        approximate_ids = {
            result["candidate_id"] for result in approximate_results
        }
        common = exact_ids & approximate_ids
        changed = [
            result["candidate_id"]
            for result in exact_results
            if result["candidate_id"] in common
            and (
                result["sense_id"]
                != approximate_by_candidate[result["candidate_id"]]["sense_id"]
                or result["disambiguation_score"]
                != approximate_by_candidate[result["candidate_id"]][
                    "disambiguation_score"
                ]
            )
        ]
        per_document_adjudication.append(
            {
                "doc_id": doc_id,
                "exact_result_count": len(exact_results),
                "approximate_result_count": len(approximate_results),
                "exact_terms": sorted({result["term"] for result in exact_results}),
                "approximate_terms": sorted(
                    {result["term"] for result in approximate_results}
                ),
                "candidate_pruned_ids": sorted(exact_ids - approximate_ids),
                "context_changed_candidate_ids": sorted(changed),
                "coverage_recall_estimate": rounded(
                    len(common) / len(exact_ids) if exact_ids else 1.0, 12
                ),
            }
        )

    recall_floor = float(
        config["disambiguation"].get("comparison_recall_floor", 1.0)
    )
    coverage_recall = (
        len(common_ids) / len(exact_candidate_ids) if exact_candidate_ids else 1.0
    )
    confidence_tolerance = float(
        config["disambiguation"].get(
            "comparison_confidence_tolerance", 1e-9
        )
    )
    term_signature_equal = exact_terms == approximate_terms
    sense_signature_equal = exact_senses == approximate_senses
    index_stats_comparable = (
        not candidate_pruning_omissions
        and not unexpected_candidates
        and term_signature_equal
        and sense_signature_equal
        and sense_counts_match
        and coverage_recall >= recall_floor
        and max_confidence_delta <= confidence_tolerance
    )

    difference_classes = []
    if candidate_pruning_omissions or unexpected_candidates:
        difference_classes.append("candidate_pruning")
    if context_comparison_changes:
        difference_classes.append("context_comparison")
    if not index_stats_comparable and not difference_classes:
        difference_classes.append("statistical_mismatch")

    report = {
        "rule_version": rules["rule_version"],
        "exact": {
            "doc_count": len(exact_docs),
            "candidate_count": len(exact_by_candidate),
            "term_count": len(exact_terms),
        },
        "approximate": {
            "doc_count": len(approximate_docs),
            "candidate_count": len(approximate_by_candidate),
            "term_count": len(approximate_terms),
            "limits": selected_decision["approximate_limits"],
        },
        "term_set_diff": {
            "exact_only": sorted(set(exact_terms) - set(approximate_terms)),
            "approximate_only": sorted(
                set(approximate_terms) - set(exact_terms)
            ),
        },
        "sense_set_diff": {
            "exact_only": [
                {"term": term, "sense_id": sense_id}
                for term, sense_id in sorted(
                    set(exact_senses) - set(approximate_senses)
                )
            ],
            "approximate_only": [
                {"term": term, "sense_id": sense_id}
                for term, sense_id in sorted(
                    set(approximate_senses) - set(exact_senses)
                )
            ],
        },
        "per_sense_confidence_deviation": per_sense_confidence_deviation,
        "coverage_recall_estimate": rounded(coverage_recall, 12),
        "coverage_recall_floor": recall_floor,
        "max_candidate_confidence_delta": rounded(max_confidence_delta, 12),
        "candidate_confidence_tolerance": confidence_tolerance,
        "difference_classes": difference_classes,
        "candidate_pruning_omissions": candidate_pruning_omissions,
        "unexpected_candidates": unexpected_candidates,
        "context_comparison_changes": context_comparison_changes,
        "per_document_adjudication": per_document_adjudication,
        "recomputation": {
            "contribution_deltas": contribution_rows,
            "checked_candidate_count": len(common_ids),
            "all_scores_recomputed": not recomputation_errors,
            "errors": recomputation_errors,
        },
        "index_stats_comparable": index_stats_comparable,
        "adjudication": (
            "comparable"
            if index_stats_comparable
            else "not_comparable"
        ),
        "documents_covered_by_both_paths": len(documents),
    }
    if strict and not index_stats_comparable:
        raise PipelineError(
            E_PATH_DIVERGENCE,
            "exact and restricted-approximate paths are not index-comparable",
            {
                "first_deviation": first_path_deviation(report),
                "difference_classes": difference_classes,
                "coverage_recall_estimate": report[
                    "coverage_recall_estimate"
                ],
                "term_set_diff": report["term_set_diff"],
                "sense_set_diff": report["sense_set_diff"],
                "report": report,
            },
        )
    return report


def first_path_deviation(report: dict[str, Any]) -> dict[str, Any]:
    if report["candidate_pruning_omissions"]:
        item = report["candidate_pruning_omissions"][0]
        return {
            "type": "candidate_pruning_omission",
            "candidate_id": item["candidate_id"],
            "doc_id": item["doc_id"],
            "term": item["term"],
        }
    if report["term_set_diff"]["exact_only"]:
        return {
            "type": "term_set_difference",
            "term": report["term_set_diff"]["exact_only"][0],
        }
    if report["term_set_diff"]["approximate_only"]:
        return {
            "type": "term_set_difference",
            "term": report["term_set_diff"]["approximate_only"][0],
        }
    if report["sense_set_diff"]["exact_only"]:
        return {
            "type": "sense_set_difference",
            **report["sense_set_diff"]["exact_only"][0],
        }
    if report["sense_set_diff"]["approximate_only"]:
        return {
            "type": "sense_set_difference",
            **report["sense_set_diff"]["approximate_only"][0],
        }
    for item in report["per_sense_confidence_deviation"]:
        if item["exact_count"] != item["approximate_count"]:
            return {
                "type": "sense_count_mismatch",
                "term": item["term"],
                "sense_id": item["sense_id"],
                "exact_count": item["exact_count"],
                "approximate_count": item["approximate_count"],
            }
    return {
        "type": "coverage_recall_below_floor",
        "coverage_recall_estimate": report["coverage_recall_estimate"],
        "coverage_recall_floor": report["coverage_recall_floor"],
    }

