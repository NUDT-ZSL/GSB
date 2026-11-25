"""Disambiguation rule snapshot discovery and validation."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .errors import (
    E_FEATURE_UNKNOWN,
    E_RULE_VERSION_UNKNOWN,
    E_SENSE_OUT_OF_RANGE,
    E_TERM_ENTRY_INVALID,
    E_WEIGHT_NEGATIVE,
    PipelineError,
)
from .glossary import glossary_index
from .util import checksum_object, read_json, rounded


def _number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def discover_rule_snapshots(rules_dir: str | Path) -> dict[str, dict[str, Any]]:
    directory = Path(rules_dir)
    snapshots: dict[str, dict[str, Any]] = {}
    if not directory.exists():
        return snapshots
    for path in sorted(directory.glob("*.json")):
        try:
            raw = read_json(path)
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(raw, dict):
            continue
        version = raw.get("rule_version")
        if isinstance(version, str) and version:
            if version in snapshots:
                raise PipelineError(
                    E_RULE_VERSION_UNKNOWN,
                    "duplicate rule snapshot version",
                    {"rule_version": version, "path": path.name},
                )
            snapshots[version] = raw
    return snapshots


def load_rules(
    config: dict[str, Any],
    glossary: dict[str, Any],
    features: dict[str, Any],
) -> dict[str, Any]:
    requested = config.get("rule_version")
    if not isinstance(requested, str) or not requested:
        raise PipelineError(
            E_RULE_VERSION_UNKNOWN,
            "config.rule_version is required",
            {"requested": requested},
        )
    snapshots = discover_rule_snapshots(
        config["_resolved_paths"]["rules_dir"]
    )
    if requested not in snapshots:
        raise PipelineError(
            E_RULE_VERSION_UNKNOWN,
            "selected disambiguation rule version is unknown",
            {"requested": requested, "available": sorted(snapshots)},
        )
    raw = snapshots[requested]
    if raw.get("glossary_version") not in (None, glossary["glossary_version"]):
        raise PipelineError(
            E_RULE_VERSION_UNKNOWN,
            "rule snapshot targets a different glossary version",
            {
                "rule_version": requested,
                "rule_glossary_version": raw.get("glossary_version"),
                "glossary_version": glossary["glossary_version"],
            },
        )
    scheme = raw.get("scheme")
    if scheme not in {"weighted_evidence", "binary_presence"}:
        raise PipelineError(
            E_RULE_VERSION_UNKNOWN,
            "rule snapshot has an unknown scoring scheme",
            {"rule_version": requested, "scheme": scheme},
        )
    rules = raw.get("rules")
    if not isinstance(rules, list):
        raise PipelineError(
            E_RULE_VERSION_UNKNOWN,
            "rule snapshot rules must be a list",
            {"rule_version": requested},
        )
    senses_by_term, _ = glossary_index(glossary)
    catalog = features["catalog"]
    normalized_rules = []
    seen_rule_ids: set[str] = set()
    for index, rule in enumerate(rules):
        if not isinstance(rule, dict):
            raise PipelineError(
                E_RULE_VERSION_UNKNOWN,
                "rule entry must be an object",
                {"rule_version": requested, "rule_index": index},
            )
        rule_id = rule.get("rule_id")
        if not isinstance(rule_id, str) or not rule_id or rule_id in seen_rule_ids:
            raise PipelineError(
                E_RULE_VERSION_UNKNOWN,
                "rule_id must be unique and non-empty",
                {"rule_version": requested, "rule_index": index, "rule_id": rule_id},
            )
        seen_rule_ids.add(rule_id)
        term_id = rule.get("term_id")
        if term_id not in senses_by_term:
            raise PipelineError(
                E_TERM_ENTRY_INVALID,
                "rule references an unknown glossary term_id",
                {
                    "rule_version": requested,
                    "rule_id": rule_id,
                    "term_id": term_id,
                },
            )
        sense_id = rule.get("sense_id")
        if (
            not isinstance(sense_id, int)
            or isinstance(sense_id, bool)
            or sense_id not in senses_by_term[term_id]
        ):
            raise PipelineError(
                E_SENSE_OUT_OF_RANGE,
                "rule references a sense outside the glossary entry",
                {
                    "rule_version": requested,
                    "rule_id": rule_id,
                    "term_id": term_id,
                    "sense_id": sense_id,
                    "valid_sense_ids": sorted(senses_by_term[term_id]),
                },
            )
        feature_id = rule.get("feature_id")
        if feature_id not in catalog:
            raise PipelineError(
                E_FEATURE_UNKNOWN,
                "rule references an unknown context feature",
                {
                    "rule_version": requested,
                    "rule_id": rule_id,
                    "feature_id": feature_id,
                },
            )
        weight = rule.get("weight")
        if not _number(weight) or weight < 0:
            raise PipelineError(
                E_WEIGHT_NEGATIVE,
                "rule weight must be non-negative",
                {
                    "rule_version": requested,
                    "rule_id": rule_id,
                    "weight": weight,
                },
            )
        normalized_rules.append(
            {
                "rule_id": rule_id,
                "term_id": term_id,
                "sense_id": sense_id,
                "feature_id": feature_id,
                "weight": float(weight),
            }
        )
    normalized_rules.sort(key=lambda item: item["rule_id"])

    priors = raw.get("sense_priors", {})
    if not isinstance(priors, dict):
        raise PipelineError(
            E_RULE_VERSION_UNKNOWN,
            "sense_priors must be an object",
            {"rule_version": requested},
        )
    normalized_priors: dict[str, dict[str, float]] = {}
    for term_id, values in priors.items():
        if term_id not in senses_by_term or not isinstance(values, dict):
            raise PipelineError(
                E_TERM_ENTRY_INVALID,
                "sense_priors references an invalid term",
                {"rule_version": requested, "term_id": term_id},
            )
        normalized_priors[term_id] = {}
        for sense_id_text, value in values.items():
            try:
                sense_id = int(sense_id_text)
            except (TypeError, ValueError):
                sense_id = -1
            if sense_id not in senses_by_term[term_id]:
                raise PipelineError(
                    E_SENSE_OUT_OF_RANGE,
                    "sense_priors references an out-of-range sense",
                    {
                        "rule_version": requested,
                        "term_id": term_id,
                        "sense_id": sense_id_text,
                    },
                )
            if not _number(value) or value < 0:
                raise PipelineError(
                    E_WEIGHT_NEGATIVE,
                    "sense prior must be non-negative",
                    {
                        "rule_version": requested,
                        "term_id": term_id,
                        "sense_id": sense_id,
                        "value": value,
                    },
                )
            normalized_priors[term_id][str(sense_id)] = rounded(float(value), 12)

    normalized = {
        "rule_version": requested,
        "scheme": scheme,
        "glossary_version": glossary["glossary_version"],
        "context_version": raw.get(
            "context_version", features["context_version"]
        ),
        "rules": normalized_rules,
        "sense_priors": normalized_priors,
    }
    normalized["fingerprint"] = checksum_object(normalized)
    return normalized

