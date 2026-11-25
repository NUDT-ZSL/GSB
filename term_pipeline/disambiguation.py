"""Deterministic context-based sense selection for both rule snapshots."""

from __future__ import annotations

from typing import Any

from .adaptive import restricted_candidates
from .features import observations_in_window
from .glossary import glossary_index
from .util import rounded


def _restricted_observations(
    candidate: dict[str, Any],
    features: dict[str, Any],
    path_decision: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[str]]:
    context = candidate["context"]
    observations = observations_in_window(
        features,
        candidate["doc_id"],
        candidate["para_no"],
        context["window_start"],
        context["window_end"],
    )
    if path_decision["selected_path"] == "exact":
        return observations, []
    limit = int(path_decision["approximate_limits"]["max_features_per_candidate"])
    catalog = features["catalog"]
    features_present = sorted(
        {observation["feature_id"] for observation in observations},
        key=lambda feature_id: (
            -catalog[feature_id]["weight"],
            feature_id,
        ),
    )
    kept_features = set(features_present[:limit])
    omitted = sorted(features_present[limit:])
    kept = [
        observation
        for observation in observations
        if observation["feature_id"] in kept_features
    ]
    return kept, omitted


def _score_senses(
    candidate: dict[str, Any],
    entry: dict[str, Any],
    observations: list[dict[str, Any]],
    rules: dict[str, Any],
    features: dict[str, Any],
    config: dict[str, Any],
) -> tuple[int, float, dict[str, Any], list[str]]:
    digits = int(config["scoring"]["round_digits"])
    evidence_count: dict[str, int] = {}
    for observation in observations:
        evidence_count[observation["feature_id"]] = (
            evidence_count.get(observation["feature_id"], 0) + 1
        )
    catalog = features["catalog"]
    term_rules = [
        rule
        for rule in rules["rules"]
        if rule["term_id"] == candidate["term_id"]
    ]
    priors = rules["sense_priors"].get(candidate["term_id"], {})
    sense_breakdowns = []
    for sense in entry["senses"]:
        sense_id = sense["sense_id"]
        prior = float(priors.get(str(sense_id), 0.0))
        rule_details = []
        contributions = []
        for rule in term_rules:
            if rule["sense_id"] != sense_id:
                continue
            count = evidence_count.get(rule["feature_id"], 0)
            present = count > 0
            if rules["scheme"] == "weighted_evidence":
                contribution = rule["weight"] * min(3, count)
            else:
                catalog_weight = catalog[rule["feature_id"]]["weight"]
                contribution = rule["weight"] * catalog_weight if present else 0.0
            contribution = rounded(contribution, digits)
            contributions.append(contribution)
            rule_details.append(
                {
                    "rule_id": rule["rule_id"],
                    "feature_id": rule["feature_id"],
                    "feature_present": present,
                    "evidence_count": count,
                    "rule_weight": rule["weight"],
                    "scheme": rules["scheme"],
                    "contribution": contribution,
                }
            )
        score = rounded(prior + sum(contributions), digits)
        sense_breakdowns.append(
            {
                "sense_id": sense_id,
                "definition": sense["definition"],
                "prior": rounded(prior, digits),
                "rule_contributions": rule_details,
                "score": score,
                "formula": "prior+sum(rule_contributions)",
            }
        )
    selected = sorted(sense_breakdowns, key=lambda item: (-item["score"], item["sense_id"]))[0]
    detail = {
        "formula": "select(max(score), tie=sense_id_asc)",
        "selected_rule_version": rules["rule_version"],
        "scheme": rules["scheme"],
        "candidate_confidence": candidate["confidence"],
        "sense_scores": sense_breakdowns,
        "selected_sense_id": selected["sense_id"],
        "selected_score": selected["score"],
    }
    observed_features = sorted(
        {observation["feature_id"] for observation in observations}
    )
    return selected["sense_id"], float(selected["score"]), detail, observed_features


def disambiguate_document(
    doc: dict[str, Any],
    scored_candidates: list[dict[str, Any]],
    glossary: dict[str, Any],
    rules: dict[str, Any],
    features: dict[str, Any],
    path_decision: dict[str, Any],
    config: dict[str, Any],
) -> dict[str, Any]:
    _, entries_by_key = glossary_index(glossary)
    selected_candidates, pruned_candidate_ids = restricted_candidates(
        scored_candidates,
        path_decision,
    )
    results = []
    for candidate in selected_candidates:
        entry = entries_by_key[(candidate["term_id"], candidate["entry_no"])]
        observations, omitted_features = _restricted_observations(
            candidate,
            features,
            path_decision,
        )
        observed_features = sorted(
            {observation["feature_id"] for observation in observations}
        )
        if len(entry["senses"]) == 1:
            sense = entry["senses"][0]
            result = {
                "candidate_id": candidate["candidate_id"],
                "doc_id": doc["doc_id"],
                "para_no": doc["para_no"],
                "term": candidate["term"],
                "term_id": candidate["term_id"],
                "entry_no": candidate["entry_no"],
                "start": candidate["start"],
                "end": candidate["end"],
                "sense_id": sense["sense_id"],
                "definition": sense["definition"],
                "disambiguation_score": 1.0,
                "candidate_confidence": candidate["confidence"],
                "selected_rule_version": rules["rule_version"],
                "path": path_decision["selected_path"],
                "context_features": observed_features,
                "omitted_context_features": omitted_features,
                "score_breakdown": {
                    "formula": "monosemous",
                    "candidate_confidence": candidate["confidence"],
                    "selected_sense_id": sense["sense_id"],
                    "selected_score": 1.0,
                },
                "candidate_score_breakdown": candidate["score_breakdown"],
            }
        else:
            sense_id, score, detail, observed_features = _score_senses(
                candidate,
                entry,
                observations,
                rules,
                features,
                config,
            )
            definition = next(
                sense["definition"]
                for sense in entry["senses"]
                if sense["sense_id"] == sense_id
            )
            detail["omitted_context_features"] = omitted_features
            result = {
                "candidate_id": candidate["candidate_id"],
                "doc_id": doc["doc_id"],
                "para_no": doc["para_no"],
                "term": candidate["term"],
                "term_id": candidate["term_id"],
                "entry_no": candidate["entry_no"],
                "start": candidate["start"],
                "end": candidate["end"],
                "sense_id": sense_id,
                "definition": definition,
                "disambiguation_score": score,
                "candidate_confidence": candidate["confidence"],
                "selected_rule_version": rules["rule_version"],
                "path": path_decision["selected_path"],
                "context_features": observed_features,
                "omitted_context_features": omitted_features,
                "score_breakdown": detail,
                "candidate_score_breakdown": candidate["score_breakdown"],
            }
        results.append(result)
    results.sort(
        key=lambda item: (item["start"], item["end"], item["candidate_id"])
    )
    return {
        "doc_id": doc["doc_id"],
        "path": path_decision["selected_path"],
        "results": results,
        "pruned_candidate_ids": pruned_candidate_ids,
        "selected_rule_version": rules["rule_version"],
    }


def disambiguate_documents(
    documents: list[dict[str, Any]],
    scored_documents: list[dict[str, Any]],
    glossary: dict[str, Any],
    rules: dict[str, Any],
    features: dict[str, Any],
    path_decision: dict[str, Any],
    config: dict[str, Any],
) -> list[dict[str, Any]]:
    scored_by_doc = {item["doc_id"]: item["candidates"] for item in scored_documents}
    result = []
    for doc in documents:
        result.append(
            disambiguate_document(
                doc,
                scored_by_doc[doc["doc_id"]],
                glossary,
                rules,
                features,
                path_decision,
                config,
            )
        )
    return result

