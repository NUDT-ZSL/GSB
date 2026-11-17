"""Committed runtime state and derived summaries."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .errors import Failure
from .loader import RISK_LEVELS, Sample
from .scoring import (
    find_dictionary_matches,
    normalize_text,
    rank_sample_ids,
    result_from_artifacts,
    risk_distribution,
)
from .util import sha256_obj


STAGES = ("normalize", "sentiment", "risk", "fusion")


def build_reference_index(
    samples: list[Sample], rules: dict[str, Any]
) -> dict[str, Any]:
    normalized: dict[str, str] = {}
    sentiment_refs: dict[str, set[str]] = {
        word: set() for word in rules["sentiment_lexicon"]
    }
    risk_refs: dict[str, set[str]] = {
        keyword: set() for keyword in rules["risk_keywords"]
    }
    degree_refs: dict[str, set[str]] = {
        word: set() for word in rules["degree_adverbs"]
    }
    negation_refs: dict[str, set[str]] = {
        word: set() for word in rules["negation_words"]
    }
    level_refs: dict[str, set[str]] = {level: set() for level in RISK_LEVELS}
    sentiment_samples: set[str] = set()
    risk_samples: set[str] = set()

    for sample in samples:
        text = normalize_text(sample.text)
        normalized[sample.sample_id] = text
        for match in find_dictionary_matches(text, rules["sentiment_lexicon"]):
            sentiment_refs[match["word"]].add(sample.sample_id)
            sentiment_samples.add(sample.sample_id)
        for match in find_dictionary_matches(text, rules["risk_keywords"]):
            keyword = match["word"]
            risk_refs[keyword].add(sample.sample_id)
            level = rules["risk_keywords"][keyword]["level"]
            level_refs[level].add(sample.sample_id)
            risk_samples.add(sample.sample_id)
        for match in find_dictionary_matches(text, rules["degree_adverbs"]):
            degree_refs[match["word"]].add(sample.sample_id)
        for match in find_dictionary_matches(text, rules["negation_words"]):
            negation_refs[match["word"]].add(sample.sample_id)

    return {
        "normalized": normalized,
        "sentiment_words": sentiment_refs,
        "risk_keywords": risk_refs,
        "degree_words": degree_refs,
        "negation_words": negation_refs,
        "risk_levels": level_refs,
        "sentiment_samples": sentiment_samples,
        "risk_samples": risk_samples,
        "all_samples": {sample.sample_id for sample in samples},
    }


@dataclass
class RuntimeState:
    version: int
    command_count: int
    samples: list[Sample]
    rules: dict[str, Any]
    config: dict[str, Any]
    artifacts: dict[str, dict[str, dict[str, Any]]]
    failures: list[Failure]
    base_config_hash: str
    base_resource_fingerprint: str
    samples_fingerprint: str
    stage_versions: dict[str, int] = field(
        default_factory=lambda: {stage: 0 for stage in STAGES}
    )
    last_audit: dict[str, Any] = field(default_factory=dict)

    @property
    def config_hash(self) -> str:
        return sha256_obj(self.config)

    @property
    def resource_fingerprint(self) -> str:
        return sha256_obj(self.rules)

    def results(self) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for sample in self.samples:
            artifacts = self.artifacts.get(sample.sample_id, {})
            if all(stage in artifacts for stage in STAGES):
                results.append(result_from_artifacts(sample, artifacts))
        return results

    def completed_sample_ids(self) -> list[str]:
        completed: list[str] = []
        for sample in self.samples:
            artifacts = self.artifacts.get(sample.sample_id, {})
            if all(stage in artifacts for stage in STAGES):
                completed.append(sample.sample_id)
        return completed

    def summary(self) -> dict[str, Any]:
        results = self.results()
        distribution = risk_distribution(results)
        total = len(self.samples)
        completed = len(results)
        return {
            "state_version": self.version,
            "completed_sample_count": completed,
            "total_sample_count": total,
            "progress": {
                "completed": completed,
                "total": total,
                "percent": round(100.0 * completed / total, 4) if total else 100.0,
                "complete": completed == total,
            },
            "risk_distribution": distribution,
            "affected_sample_ids": list(self.last_audit.get("affected_sample_ids", [])),
            "reused_sample_count": int(self.last_audit.get("reused_sample_count", 0)),
            "recomputed_sample_count": int(
                self.last_audit.get("recomputed_sample_count", 0)
            ),
            "ranked_sample_ids": rank_sample_ids(results),
        }

    def canonical_payload(self) -> dict[str, Any]:
        return {
            "samples": self.results(),
            "failures": [
                failure.to_dict() for failure in sorted(self.failures, key=lambda x: x.sort_key())
            ],
        }

