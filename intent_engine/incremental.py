"""Resource diffing, reference indexing and invalidation planning."""

from __future__ import annotations

from typing import Any, Iterable

from .constants import STAGES
from .utils import short_hash, stable_hash

DOWNSTREAM = {
    "normalize": ("normalize", "intent", "slots", "sequence", "merge"),
    "intent": ("intent", "slots", "merge"),
    "slots": ("slots", "merge"),
    "sequence": ("sequence", "merge"),
    "merge": ("merge",),
}


def diff_manifest(old: dict[str, str], new: dict[str, str]) -> dict[str, list[str]]:
    old_keys = set(old)
    new_keys = set(new)
    changed = sorted(
        key for key in (old_keys & new_keys) if old.get(key) != new.get(key)
    )
    return {
        "added": sorted(new_keys - old_keys),
        "removed": sorted(old_keys - new_keys),
        "changed": changed,
    }


def _text_forms(sample: Any, cached: dict[str, Any] | None) -> list[str]:
    forms = {sample.text}
    if isinstance(cached, dict):
        stages = cached.get("stages", {})
        normalized = stages.get("normalize") if isinstance(stages, dict) else None
        if isinstance(normalized, dict):
            value = normalized.get("value")
            if isinstance(value, dict) and isinstance(value.get("text"), str):
                forms.add(value["text"])
    return sorted(forms)


def build_reference_index(
    samples: Iterable[Any],
    resources: Any,
    cache: Any,
) -> dict[str, dict[str, list[str]]]:
    """Record which samples reference every live vocabulary/rule entry."""

    result: dict[str, dict[str, list[str]]] = {
        "intent_features": {},
        "intent_definitions": {},
        "entities": {},
        "rule_synonyms": {},
        "rule_conflicts": {},
        "sequence_model": {},
    }
    for definition in resources.intents.values():
        result["intent_definitions"][f"intent_def:{definition.name}"] = []
        for feature in definition.features:
            result["intent_features"].setdefault(f"intent_feature:{definition.name}:{feature.term}", [])
    for entity_type, entries in resources.entities.items():
        for entry in entries:
            result["entities"].setdefault(f"entity:{entity_type}:{entry.term}", [])
    for rule in resources.synonyms:
        result["rule_synonyms"].setdefault(f"rule_synonym:{rule.source}", [])
    for rule in resources.conflicts:
        result["rule_conflicts"].setdefault(f"rule_conflict:{rule.trigger}:{rule.winner}:{rule.loser}", [])

    for sample in sorted(samples, key=lambda item: item.sample_id):
        cached_sample = cache.samples.get(sample.sample_id)
        forms = _text_forms(sample, cached_sample)
        joined = "\n".join(forms)
        for definition in resources.intents.values():
            for feature in definition.features:
                key = f"intent_feature:{definition.name}:{feature.term}"
                if feature.term in joined:
                    result["intent_features"].setdefault(key, []).append(sample.sample_id)
        for definition in resources.intents.values():
            result["intent_definitions"][f"intent_def:{definition.name}"].append(sample.sample_id)
        for entity_type, entries in resources.entities.items():
            for entry in entries:
                key = f"entity:{entity_type}:{entry.term}"
                if entry.term in joined:
                    result["entities"].setdefault(key, []).append(sample.sample_id)
        for rule in resources.synonyms:
            key = f"rule_synonym:{rule.source}"
            if rule.source in joined:
                result["rule_synonyms"].setdefault(key, []).append(sample.sample_id)
        for rule in resources.conflicts:
            key = f"rule_conflict:{rule.trigger}:{rule.winner}:{rule.loser}"
            if rule.trigger in joined:
                result["rule_conflicts"].setdefault(key, []).append(sample.sample_id)

    for category, values in result.items():
        for key in list(values):
            values[key] = sorted(set(values[key]))
    result["sequence_model"]["all"] = [sample.sample_id for sample in sorted(samples, key=lambda item: item.sample_id)]
    return result


def _manifest_key_reference(key: str, refs: dict[str, Any], old_refs: dict[str, Any]) -> tuple[str, list[str]]:
    category = key.split(":", 1)[0] if key else ""
    if category == "intent_feature":
        refkey = key
        category_key = "intent_features"
        stages = DOWNSTREAM["intent"]
    elif category == "intent_def":
        refkey = key
        category_key = "intent_definitions"
        stages = ("slots", "merge")
    elif category == "entity":
        refkey = key
        category_key = "entities"
        stages = ("slots", "sequence", "merge")
    elif category == "rule_synonym":
        refkey = key
        category_key = "rule_synonyms"
        stages = DOWNSTREAM["normalize"]
    elif category == "rule_conflict":
        refkey = key
        category_key = "rule_conflicts"
        stages = DOWNSTREAM["intent"]
    elif category == "sequence_model":
        refkey = "all"
        category_key = "sequence_model"
        stages = DOWNSTREAM["sequence"]
    else:
        return "none", ()
    current = refs.get(category_key, {}).get(refkey, [])
    previous = old_refs.get(category_key, {}).get(refkey, [])
    return refkey, sorted(set(current) | set(previous)), stages


def _injection_stage_hashes(injections: Any, sample_ids: Iterable[str]) -> dict[str, dict[str, str]]:
    result: dict[str, dict[str, str]] = {}
    for sample_id in sorted(sample_ids):
        result[sample_id] = {}
        for stage in STAGES:
            items = [item.as_dict() for item in injections.for_sample_stage(sample_id, stage)]
            if items:
                result[sample_id][stage] = stable_hash(items)
    return result


def plan_invalidations(
    *,
    samples: Iterable[Any],
    resources: Any,
    cache: Any,
    injections: Any,
    text_hashes: dict[str, str],
) -> dict[str, Any]:
    """Return predicted invalidation stages plus deterministic diagnostics."""

    sample_list = sorted(samples, key=lambda item: item.sample_id)
    sample_ids = [sample.sample_id for sample in sample_list]
    refs = build_reference_index(sample_list, resources, cache)
    old_refs = cache.references if isinstance(cache.references, dict) else {}
    resource_diff = diff_manifest(cache.resource_manifest, resources.entry_manifest)
    config_old = cache.config_product_manifest if isinstance(cache.config_product_manifest, dict) else {}
    config_diff = diff_manifest(config_old, resources.config.product_manifest)
    current_injection_hashes = _injection_stage_hashes(injections, sample_ids)
    old_injection_hashes = cache.injection_stage_hashes if isinstance(cache.injection_stage_hashes, dict) else {}

    invalidated: dict[str, set[str]] = {sample_id: set() for sample_id in sample_ids}
    reasons: dict[str, set[str]] = {sample_id: set() for sample_id in sample_ids}
    cold: set[str] = set()

    for sample in sample_list:
        sid = sample.sample_id
        cached_sample = cache.samples.get(sid)
        if not isinstance(cached_sample, dict):
            cold.add(sid)
            invalidated[sid].update(STAGES)
            reasons[sid].add("cold_cache")
            continue
        if cached_sample.get("text_hash") not in (None, text_hashes[sid]):
            invalidated[sid].update(STAGES)
            reasons[sid].add("input_text_changed")

    for field_name in ("added", "removed", "changed"):
        for key in resource_diff[field_name]:
            refkey, affected, stages = _manifest_key_reference(key, refs, old_refs)
            if not affected:
                continue
            for sid in affected:
                if not isinstance(cache.samples.get(sid), dict):
                    continue
                invalidated[sid].update(stages)
                reasons[sid].add(f"resource_{field_name}:{key}")

    config_stage_map = {
        "normalize": DOWNSTREAM["normalize"],
        "intent": DOWNSTREAM["intent"],
        "slots": DOWNSTREAM["slots"],
        "sequence": DOWNSTREAM["sequence"],
        "merge": DOWNSTREAM["merge"],
    }
    for field_name in ("added", "removed", "changed"):
        for key in config_diff[field_name]:
            stages = config_stage_map.get(key, ())
            for sid in sample_ids:
                if not isinstance(cache.samples.get(sid), dict):
                    continue
                invalidated[sid].update(stages)
                reasons[sid].add(f"config_{field_name}:{key}")

    for sid in sample_ids:
        if not isinstance(cache.samples.get(sid), dict):
            continue
        current = current_injection_hashes.get(sid, {})
        old = old_injection_hashes.get(sid, {}) if isinstance(old_injection_hashes, dict) else {}
        for stage in STAGES:
            if current.get(stage) != old.get(stage):
                invalidated[sid].update(DOWNSTREAM[stage])
                reasons[sid].add(f"injection_changed:{stage}")

    per_stage_counts = {stage: 0 for stage in STAGES}
    for stages in invalidated.values():
        for stage in stages:
            if stage in per_stage_counts:
                per_stage_counts[stage] += 1
    return {
        "resource_diff": resource_diff,
        "config_diff": config_diff,
        "cold_samples": sorted(cold),
        "invalidated_stages": {
            sid: [stage for stage in STAGES if stage in invalidated[sid]]
            for sid in sample_ids
            if invalidated[sid]
        },
        "invalidated_stage_counts": per_stage_counts,
        "reasons": {sid: sorted(values) for sid, values in reasons.items() if values},
        "affected_samples": sorted(sid for sid, stages in invalidated.items() if stages),
        "reference_counts": {
            category: len(values)
            for category, values in refs.items()
        },
        "reference_digest": short_hash(refs, 24),
        "injection_stage_hashes": current_injection_hashes,
        "references": refs,
    }
