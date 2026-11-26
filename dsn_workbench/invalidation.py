from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .cache import STAGES, ContentAddressedCache, make_stage_key
from .config import ConfigBundle
from .util import content_hash, sentence_sort_key


STAGE_INDEX = {stage: index for index, stage in enumerate(STAGES)}


@dataclass
class InvalidationPlan:
    previous_manifest_found: bool
    affected_sentences: list[str] = field(default_factory=list)
    reuse_sentences: list[str] = field(default_factory=list)
    reasons: dict[str, str] = field(default_factory=dict)
    first_invalid_stage: dict[str, str | None] = field(default_factory=dict)
    invalidated_stages: dict[str, list[str]] = field(default_factory=dict)
    reused_stages: int = 0
    semantic_changes: dict[str, bool] = field(default_factory=dict)
    changed_terms: list[str] = field(default_factory=list)
    changed_rules: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "previous_manifest_found": self.previous_manifest_found,
            "affected_sentences": self.affected_sentences,
            "reuse_sentences": self.reuse_sentences,
            "recompute_sentences": self.affected_sentences,
            "reused_sentences": self.reuse_sentences,
            "reasons": self.reasons,
            "first_invalid_stage": self.first_invalid_stage,
            "invalidated_stages": self.invalidated_stages,
            "reused_stages": self.reused_stages,
            "semantic_changes": self.semantic_changes,
            "changed_terms": self.changed_terms,
            "changed_rules": self.changed_rules,
        }


def _dictionary_semantics(entries: list[dict[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for entry in entries:
        result[entry["entry_id"]] = (
            entry["term"],
            entry["tag"],
            tuple(entry.get("domains") or []),
            tuple(entry.get("locales") or []),
        )
    return result


def _rule_semantics(rules: list[dict[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for rule in rules:
        result[rule["rule_id"]] = (
            rule["pattern"],
            rule["source"],
            rule["tag"],
            tuple(rule.get("domains") or []),
            tuple(rule.get("locales") or []),
        )
    return result


def _changed_terms(
    old_config: dict[str, Any], new_config: dict[str, Any]
) -> tuple[set[str], dict[str, Any]]:
    old_entries = _dictionary_semantics(old_config["dictionary"])
    new_entries = _dictionary_semantics(new_config["dictionary"])
    changed: set[str] = set()
    for entry_id in set(old_entries) | set(new_entries):
        if old_entries.get(entry_id) != new_entries.get(entry_id):
            old_entry = old_entries.get(entry_id)
            new_entry = new_entries.get(entry_id)
            if old_entry is not None:
                changed.add(old_entry[0])
            if new_entry is not None:
                changed.add(new_entry[0])
    return changed, {"old": old_entries, "new": new_entries}


def _changed_rules(
    old_config: dict[str, Any], new_config: dict[str, Any]
) -> tuple[set[str], dict[str, Any]]:
    old_rules = _rule_semantics(old_config["rules"])
    new_rules = _rule_semantics(new_config["rules"])
    changed: set[str] = set()
    for rule_id in set(old_rules) | set(new_rules):
        if old_rules.get(rule_id) != new_rules.get(rule_id):
            changed.add(rule_id)
    return changed, {"old": old_rules, "new": new_rules}


def _rule_matches(text: str, rule_spec: tuple[Any, ...]) -> bool:
    try:
        return re.search(rule_spec[0], text) is not None
    except re.error:
        return True


def _compute_reasons(
    old_manifest: dict[str, Any],
    old_config: dict[str, Any],
    new_bundle: ConfigBundle,
    tasks: list[dict[str, Any]],
) -> tuple[dict[str, str], dict[str, str | None], set[str], set[str], set[str], dict[str, bool]]:
    new_config = new_bundle.data
    old_sentences = old_manifest.get("sentences", {})
    semantic_changes = {
        "normalization": old_config["normalization"] != new_config["normalization"]
        or old_config["locale"] != new_config["locale"]
        or old_config["domain"] != new_config["domain"],
        "dictionary": old_config["dictionary"] != new_config["dictionary"]
        or old_config["dictionary_version"] != new_config["dictionary_version"],
        "rules": old_config["rules"] != new_config["rules"]
        or old_config["rule_version"] != new_config["rule_version"],
        "disambiguation": old_config["disambiguation"]
        != new_config["disambiguation"],
        "tagging": {
            "domain_tags": old_config["domain_tags"],
            "domain_pos_table": old_config["domain_pos_table"],
            "tagger": old_config["tagger"],
        }
        != {
            "domain_tags": new_config["domain_tags"],
            "domain_pos_table": new_config["domain_pos_table"],
            "tagger": new_config["tagger"],
        },
        "scheduling_metadata": any(
            old_config.get(field) != new_config.get(field)
            for field in (
                "workers",
                "batch_size",
                "logical_clock_ms",
                "generation",
            )
        ),
    }
    changed_term_set, _ = _changed_terms(old_config, new_config)
    changed_rule_set, rule_specs = _changed_rules(old_config, new_config)
    changed_terms = sorted(changed_term_set)
    changed_rules = sorted(changed_rule_set)

    reasons: dict[str, str] = {}
    first_stage: dict[str, str | None] = {}
    affected: set[str] = set()
    reuse: set[str] = set()
    for task in sorted(tasks, key=lambda row: sentence_sort_key(row["sentence_id"])):
        sentence_id = str(task["sentence_id"])
        raw_text = str(task["raw_text"])
        previous = old_sentences.get(sentence_id)
        previous_text = (
            str(previous.get("normalized_text", raw_text))
            if previous
            else raw_text
        )
        if previous is None:
            reasons[sentence_id] = "new_sentence"
            first_stage[sentence_id] = "normalize"
            affected.add(sentence_id)
            continue

        invalid_index = len(STAGES)
        stage_reasons: list[str] = []
        if previous.get("raw_hash") != content_hash(raw_text):
            invalid_index = 0
            stage_reasons.append("raw_text_changed")
        if (
            previous.get("domain") != task["domain"]
            or previous.get("locale") != task["lang"]
        ):
            invalid_index = 0
            stage_reasons.append("task_domain_or_locale_changed")
        if semantic_changes["normalization"]:
            invalid_index = 0
            stage_reasons.append("normalization_config_changed")

        dictionary_affected = any(
            term and (term in raw_text or term in previous_text)
            for term in changed_terms
        )
        if not dictionary_affected:
            dependencies = set(previous.get("dictionary_terms", []))
            dictionary_affected = bool(dependencies & changed_term_set)
        if dictionary_affected:
            invalid_index = min(invalid_index, STAGE_INDEX["candidates"])
            stage_reasons.append("dictionary_revision_matches_sentence")

        rule_affected = False
        old_rule_ids = set(previous.get("rule_ids", []))
        if old_rule_ids & changed_rule_set:
            rule_affected = True
        for rule_id in changed_rule_set:
            spec = rule_specs["new"].get(rule_id) or rule_specs["old"].get(rule_id)
            if spec is not None and (
                _rule_matches(raw_text, spec)
                or _rule_matches(previous_text, spec)
            ):
                rule_affected = True
                break
        if rule_affected:
            invalid_index = min(invalid_index, STAGE_INDEX["candidates"])
            stage_reasons.append("rule_revision_matches_sentence")

        if (
            semantic_changes["disambiguation"]
            and int(previous.get("candidate_overlap_count", 0)) > 1
        ):
            invalid_index = min(invalid_index, STAGE_INDEX["disambiguation"])
            stage_reasons.append("disambiguation_config_changed")

        if semantic_changes["tagging"]:
            invalid_index = min(invalid_index, STAGE_INDEX["tagging"])
            stage_reasons.append("tagging_config_changed")

        missing_stage = None
        previous_stages = previous.get("stages", {})
        for stage in STAGES:
            if stage not in previous_stages:
                missing_stage = stage
                break
        if missing_stage is not None:
            invalid_index = min(invalid_index, STAGE_INDEX[missing_stage])
            stage_reasons.append(f"cache_entry_missing:{missing_stage}")

        if invalid_index >= len(STAGES):
            reasons[sentence_id] = (
                "scheduling_metadata_only"
                if any(
                    changed
                    for name, changed in semantic_changes.items()
                    if name != "scheduling_metadata"
                )
                or semantic_changes["scheduling_metadata"]
                else "no_semantic_change"
            )
            first_stage[sentence_id] = None
            reuse.add(sentence_id)
        else:
            reasons[sentence_id] = "+".join(stage_reasons) or "cache_missing"
            first_stage[sentence_id] = STAGES[invalid_index]
            affected.add(sentence_id)
    return reasons, first_stage, affected, reuse, changed_term_set, semantic_changes


def plan_and_alias(
    *,
    old_manifest: dict[str, Any] | None,
    new_bundle: ConfigBundle,
    tasks: list[dict[str, Any]],
    cache: ContentAddressedCache,
) -> InvalidationPlan:
    if not old_manifest:
        return InvalidationPlan(previous_manifest_found=False)
    old_config = old_manifest.get("config")
    if not isinstance(old_config, dict):
        return InvalidationPlan(previous_manifest_found=True)

    (
        reasons,
        first_stage,
        affected,
        reuse,
        changed_term_set,
        semantic_changes,
    ) = _compute_reasons(old_manifest, old_config, new_bundle, tasks)
    changed_rule_set, _ = _changed_rules(old_config, new_bundle.data)
    plan = InvalidationPlan(
        previous_manifest_found=True,
        reasons=reasons,
        first_invalid_stage=first_stage,
        semantic_changes=semantic_changes,
        changed_terms=sorted(changed_term_set),
        changed_rules=sorted(changed_rule_set),
    )

    old_sentences = old_manifest.get("sentences", {})
    for task in tasks:
        sentence_id = str(task["sentence_id"])
        if sentence_id not in first_stage:
            continue
        invalid = first_stage[sentence_id]
        invalid_index = len(STAGES) if invalid is None else STAGE_INDEX[invalid]
        previous = old_sentences.get(sentence_id, {})
        previous_stages = previous.get("stages", {})
        upstream_hash = "root"
        all_reused = True
        for index, stage in enumerate(STAGES):
            new_key = make_stage_key(
                stage=stage,
                sentence_id=sentence_id,
                raw_text=str(task["raw_text"]),
                config=new_bundle,
                upstream_hash=upstream_hash,
                domain=str(task["domain"]),
                locale=str(task["lang"]),
            )
            if index >= invalid_index:
                all_reused = False
                break
            old_entry = previous_stages.get(stage)
            if not isinstance(old_entry, dict):
                all_reused = False
                invalid_index = index
                first_stage[sentence_id] = stage
                break
            old_key = old_entry.get("key")
            object_hash = old_entry.get("object_hash")
            if (
                not isinstance(old_key, dict)
                or not isinstance(object_hash, str)
                or not cache.alias(
                    old_key,
                    new_key,
                    stage,
                    expected_object_hash=object_hash,
                )
            ):
                all_reused = False
                invalid_index = index
                first_stage[sentence_id] = stage
                break
            plan.reused_stages += 1
            upstream_hash = object_hash

        if all_reused:
            reuse.add(sentence_id)
            affected.discard(sentence_id)
            reasons[sentence_id] = reasons.get(sentence_id, "no_semantic_change")
            first_stage[sentence_id] = None
        else:
            affected.add(sentence_id)
            reuse.discard(sentence_id)
            if first_stage[sentence_id] is None:
                first_stage[sentence_id] = STAGES[invalid_index]
            reasons[sentence_id] = (
                reasons.get(sentence_id)
                if reasons.get(sentence_id)
                not in (None, "scheduling_metadata_only", "no_semantic_change")
                else f"cache_alias_failed:{first_stage[sentence_id]}"
            )

    plan.affected_sentences = sorted(affected, key=sentence_sort_key)
    plan.reuse_sentences = sorted(reuse, key=sentence_sort_key)
    plan.reasons = reasons
    plan.first_invalid_stage = first_stage
    for sentence_id in plan.affected_sentences + plan.reuse_sentences:
        invalid = first_stage.get(sentence_id)
        plan.invalidated_stages[sentence_id] = (
            []
            if invalid is None
            else list(STAGES[STAGE_INDEX[invalid] :])
        )
    return plan

