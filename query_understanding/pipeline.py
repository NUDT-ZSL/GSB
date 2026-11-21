"""End-to-end query understanding pipeline."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from dataclasses import replace

from .clicks import ClickPolicy, rerank_intents
from .dictionary import Dictionary
from .errors import E_INPUT_INVALID, INJECTION_CODES, QUError
from .mappings import MappingTable
from .matcher import EvidenceExtractor, EvidenceResult
from .selfcheck import self_check
from .slots import extract_slots
from .taxonomy import Taxonomy
from .tokenize import char_fallback_tokens, tokenize_text
from .util import read_json, sha256_obj


PATH_EXACT = "exact"
PATH_APPROX = "pruned_approx"

DEFAULT_CONFIG: dict[str, Any] = {
    "click_policy": {
        "feedback_rank_max": 2,
        "feedback_min_dwell_ms": 300,
        "eval_rank_min": 3,
        "eval_rank_max": 5,
        "click_weight": 0.55,
        "ambiguity_margin": 0.08,
        "softmax_temperature": 0.18,
        "top_k": 3,
    },
    "approximate": {
        "max_candidates_per_token": 16,
        "max_candidate_intents": 8,
        "fuzzy_threshold": 0.62,
    },
    "dictionary_max_entries": None,
    "allow_dictionary_truncate": False,
    "strict_self_check": True,
}


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = {key: value for key, value in base.items()}
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _fault_for_stage(
    stage: str,
    query: dict[str, Any],
    config: dict[str, Any],
    inject: dict[str, Any] | None,
) -> dict[str, Any] | None:
    if inject and inject.get("stage") == stage:
        return {
            "business_code": str(
                inject.get("business_code", INJECTION_CODES[stage])
            ),
            "fallback_category": str(
                inject.get(
                    "fallback_category",
                    _default_fallback_category(stage),
                )
            ),
            "trigger": "parameter",
            "message": str(inject.get("message", f"injected {stage} fault")),
        }
    input_stage = query.get("_fault_stage")
    if input_stage == stage:
        return {
            "business_code": str(
                query.get("_fault_code", INJECTION_CODES[stage])
            ),
            "fallback_category": str(
                query.get(
                    "_fault_category",
                    _default_fallback_category(stage),
                )
            ),
            "trigger": "input",
            "message": f"input-triggered {stage} fault",
        }
    config_fault = config.get("fault_injection", {}).get(stage)
    if config_fault:
        if not isinstance(config_fault, dict):
            config_fault = {}
        query_ids = config_fault.get("query_ids")
        if query_ids is None or str(query.get("query_id")) in {str(value) for value in query_ids}:
            return {
                "business_code": str(
                    config_fault.get("business_code", INJECTION_CODES[stage])
                ),
                "fallback_category": str(
                    config_fault.get(
                        "fallback_category",
                        _default_fallback_category(stage),
                    )
                ),
                "trigger": "config",
                "message": str(
                    config_fault.get("message", f"config-injected {stage} fault")
                ),
            }
    return None


def _default_fallback_category(stage: str) -> str:
    return {
        "normalize": "raw_text_passthrough",
        "tokenize": "char_fallback",
        "slot": "slots_dropped",
        "intent": "intent_default_unknown",
    }[stage]


class QueryEngine:
    """Composes deterministic normalization, tagging, and ranking."""

    def __init__(
        self,
        *,
        mappings: MappingTable,
        taxonomy: Taxonomy,
        dictionary: Dictionary,
        config: dict[str, Any] | None = None,
    ):
        self.mappings = mappings
        self.taxonomy = taxonomy
        self.dictionary = dictionary
        self.config = _deep_merge(DEFAULT_CONFIG, config or {})
        self.extractor = EvidenceExtractor(dictionary, taxonomy)
        self.click_policy = ClickPolicy.from_dict(self.config.get("click_policy"))

    @classmethod
    def from_files(
        cls,
        data_dir: str | Path,
        config: dict[str, Any] | None = None,
    ) -> "QueryEngine":
        root = Path(data_dir)
        merged = _deep_merge(DEFAULT_CONFIG, config or {})
        mappings = MappingTable.from_payload(read_json(root / "mappings.json"))
        taxonomy = Taxonomy.from_file(root / "taxonomy.json")
        dictionary = Dictionary.from_file(
            root / "dictionary.json",
            max_entries=merged.get("dictionary_max_entries"),
            allow_truncate=bool(merged.get("allow_dictionary_truncate", False)),
        )
        return cls(
            mappings=mappings,
            taxonomy=taxonomy,
            dictionary=dictionary,
            config=merged,
        )

    @classmethod
    def from_bundle(
        cls,
        bundle: dict[str, Any],
        config_override: dict[str, Any] | None = None,
    ) -> "QueryEngine":
        merged = _deep_merge(
            _deep_merge(DEFAULT_CONFIG, bundle.get("config", {})),
            config_override or {},
        )
        mappings = MappingTable.from_payload(bundle["mappings"])
        taxonomy = Taxonomy.from_payload(bundle["taxonomy"])
        dictionary = Dictionary.from_payload(
            bundle["dictionary"],
            max_entries=merged.get("dictionary_max_entries"),
            allow_truncate=bool(merged.get("allow_dictionary_truncate", False)),
        )
        return cls(
            mappings=mappings,
            taxonomy=taxonomy,
            dictionary=dictionary,
            config=merged,
        )

    def _fault_record(
        self,
        stage: str,
        fault: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "stage": stage,
            "business_code": fault["business_code"],
            "fallback_category": fault["fallback_category"],
            "severity": "warning",
            "trigger": fault["trigger"],
            "message": fault["message"],
        }

    def _fallback_ranking(self) -> tuple[dict[str, Any], dict[str, Any]]:
        fallback = ("general", "unknown_query")
        if self.taxonomy.intent(*fallback) is None:
            fallback = self.taxonomy.sorted_intents()[0]
        ranking = {
            "domain": fallback[0],
            "intent_class": fallback[1],
            "intent_confidence": 0.0,
            "domain_confidence": 0.0,
            "domain_ranking": [],
            "top_k_intents": [
                {
                    "domain": fallback[0],
                    "intent_class": fallback[1],
                    "score": 0.0,
                    "confidence": 0.0,
                    "lexical_score": 0.0,
                    "click_score": 0.0,
                }
            ],
            "disambiguation": {
                "ambiguous": False,
                "stage1_margin": 0.0,
                "ambiguity_margin": self.click_policy.ambiguity_margin,
                "rerank_applied": False,
                "click_weight": self.click_policy.click_weight,
                "softmax_temperature": self.click_policy.softmax_temperature,
                "feedback_clicks_used": [],
                "feedback_clicks_excluded": [],
                "click_window": {
                    "rank_max": self.click_policy.feedback_rank_max,
                    "min_dwell_ms": self.click_policy.feedback_min_dwell_ms,
                },
                "confidence_policy": {
                    "kind": "degraded_constant",
                    "temperature": None,
                    "score_mix": None,
                },
            },
            "stage1_candidates": [],
        }
        debug = {
            "candidate_intents": [
                {"domain": fallback[0], "intent_class": fallback[1]}
            ],
            "lexical_scores": [],
            "evidence": [],
            "stats": {},
        }
        return ranking, debug

    def process(
        self,
        query: dict[str, Any],
        *,
        path: str = PATH_EXACT,
        inject: dict[str, Any] | None = None,
        click_policy: ClickPolicy | None = None,
    ) -> dict[str, Any]:
        for field in ("query_id", "query_text", "session_id", "timestamp_ms", "locale"):
            if field not in query:
                raise QUError(
                    E_INPUT_INVALID,
                    f"query row is missing required field {field}",
                    stage="input",
                    details={"query": query},
                )
        if path not in {PATH_EXACT, PATH_APPROX}:
            raise QUError(
                E_INPUT_INVALID,
                "unknown path",
                stage="input",
                details={"path": path},
            )
        policy = click_policy or self.click_policy
        if path == PATH_APPROX:
            disambiguation_override = self.config.get(
                "approximate_disambiguation", {}
            )
            if disambiguation_override:
                policy = replace(
                    policy,
                    **{
                        key: value
                        for key, value in disambiguation_override.items()
                        if key in policy.to_dict()
                    },
                )
        faults: list[dict[str, Any]] = []
        degradations: list[dict[str, Any]] = []

        normalize_fault = _fault_for_stage(
            "normalize", query, self.config, inject
        )
        if normalize_fault is not None:
            normalized = str(query["query_text"])
            faults.append(self._fault_record("normalize", normalize_fault))
            degradations.append(
                {
                    "stage": "normalize",
                    "business_code": normalize_fault["business_code"],
                    "category": normalize_fault["fallback_category"],
                }
            )
        else:
            normalized = self.mappings.normalize(str(query["query_text"]))

        tokenize_fault = _fault_for_stage(
            "tokenize", query, self.config, inject
        )
        if tokenize_fault is not None:
            tokens = char_fallback_tokens(
                normalized, tokenize_fault["fallback_category"]
            )
            faults.append(self._fault_record("tokenize", tokenize_fault))
            degradations.append(
                {
                    "stage": "tokenize",
                    "business_code": tokenize_fault["business_code"],
                    "category": tokenize_fault["fallback_category"],
                }
            )
        else:
            tokens = tokenize_text(normalized, self.dictionary)

        intent_fault = _fault_for_stage("intent", query, self.config, inject)
        slot_fault = _fault_for_stage("slot", query, self.config, inject)
        if intent_fault is not None:
            ranking, evidence_debug = self._fallback_ranking()
            faults.append(self._fault_record("intent", intent_fault))
            degradations.append(
                {
                    "stage": "intent",
                    "business_code": intent_fault["business_code"],
                    "category": intent_fault["fallback_category"],
                }
            )
            evidence = EvidenceResult(
                evidence=[],
                candidate_intents=[],
                lexical_scores={},
                stats={
                    "token_count": len(tokens),
                    "exact_entry_hits": 0,
                    "fuzzy_comparisons": 0,
                    "pruned_entry_hits": 0,
                },
            )
        else:
            if path == PATH_EXACT:
                evidence = self.extractor.exact(tokens)
            else:
                approx = self.config.get("approximate", {})
                evidence = self.extractor.approximate(
                    tokens,
                    max_candidates_per_token=int(
                        approx.get("max_candidates_per_token", 16)
                    ),
                    max_candidate_intents=int(
                        approx.get("max_candidate_intents", 8)
                    ),
                    fuzzy_threshold=float(approx.get("fuzzy_threshold", 0.62)),
                )
            ranking = rerank_intents(
                evidence.lexical_scores,
                list(query.get("raw_clicks", [])),
                self.taxonomy,
                policy,
            )
            evidence_debug = {
                "candidate_intents": [
                    {"domain": domain, "intent_class": intent}
                    for domain, intent in sorted(evidence.lexical_scores)
                ],
                "lexical_scores": evidence.to_dict()["lexical_scores"],
                "evidence": evidence.evidence,
                "stats": evidence.stats,
            }

        if slot_fault is not None or intent_fault is not None:
            slots: list[dict[str, Any]] = []
            dropped_slots: list[dict[str, Any]] = []
            if slot_fault is not None:
                faults.append(self._fault_record("slot", slot_fault))
                degradations.append(
                    {
                        "stage": "slot",
                        "business_code": slot_fault["business_code"],
                        "category": slot_fault["fallback_category"],
                    }
                )
        else:
            slots, dropped_slots = extract_slots(
                normalized,
                evidence.evidence,
                ranking["domain"],
                ranking["intent_class"],
                self.taxonomy,
            )

        path_policy: dict[str, Any]
        if path == PATH_EXACT:
            path_policy = {
                "candidate_strategy": "full_dictionary_exact",
                "disambiguation": "candidate_lexical_then_click_rerank",
                "dictionary_candidate_cap": None,
            }
        else:
            approx = self.config.get("approximate", {})
            path_policy = {
                "candidate_strategy": "restricted_candidate_pruning",
                "disambiguation": "pruned_lexical_then_click_rerank",
                "max_candidates_per_token": int(
                    approx.get("max_candidates_per_token", 16)
                ),
                "max_candidate_intents": int(
                    approx.get("max_candidate_intents", 8)
                ),
                "fuzzy_threshold": float(approx.get("fuzzy_threshold", 0.62)),
                "disambiguation_override": self.config.get(
                    "approximate_disambiguation", {}
                ),
            }

        result: dict[str, Any] = {
            "schema_version": "1.0",
            "query_id": str(query["query_id"]),
            "query_text": str(query["query_text"]),
            "session_id": str(query["session_id"]),
            "timestamp_ms": int(query["timestamp_ms"]),
            "locale": str(query["locale"]),
            "path": path,
            "path_policy": path_policy,
            "normalized_text": normalized,
            "tokens": tokens,
            "domain": ranking["domain"],
            "intent_class": ranking["intent_class"],
            "domain_confidence": ranking["domain_confidence"],
            "domain_ranking": ranking["domain_ranking"],
            "intent_confidence": ranking["intent_confidence"],
            "top_k_intents": ranking["top_k_intents"],
            "slots": slots,
            "dropped_slot_candidates": dropped_slots,
            "disambiguation": ranking["disambiguation"],
            "stage1_candidates": ranking["stage1_candidates"],
            "faults": faults,
            "degradations": degradations,
            "degradation_categories": [
                f"{row['stage']}:{row['category']}" for row in degradations
            ],
            "debug": {
                "candidate_intents": evidence_debug["candidate_intents"],
                "lexical_scores": evidence_debug["lexical_scores"],
                "stats": evidence_debug["stats"],
                "evidence_count": len(evidence_debug["evidence"]),
            },
        }
        if not faults:
            result["faults"] = []
        check = self_check(
            result,
            self.taxonomy,
            raise_on_error=bool(self.config.get("strict_self_check", True)),
        )
        result["self_check"] = check
        result["result_checksum"] = sha256_obj(result)
        return result

    def process_many(
        self,
        queries: list[dict[str, Any]],
        *,
        path: str = PATH_EXACT,
        click_policy: ClickPolicy | None = None,
    ) -> list[dict[str, Any]]:
        return [
            self.process(query, path=path, click_policy=click_policy)
            for query in queries
        ]

