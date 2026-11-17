"""Local resource package loading and stable fingerprints."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .constants import ENTITY_TYPES, ENTITY_TYPE_ALIASES, precision_digits
from .utils import canonical_json, read_json, stable_hash


def _finite_float(value: Any, *, field_name: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be numeric") from exc
    if not math.isfinite(number):
        raise ValueError(f"{field_name} must be finite")
    return number


def _canonical_entity_type(value: str) -> str:
    key = value.strip()
    return ENTITY_TYPE_ALIASES.get(key.lower(), ENTITY_TYPE_ALIASES.get(key, key))


@dataclass(frozen=True)
class IntentFeature:
    term: str
    weight: float
    raw: dict[str, Any]


@dataclass(frozen=True)
class IntentDefinition:
    name: str
    features: tuple[IntentFeature, ...]
    allowed_entity_types: tuple[str, ...]
    raw: dict[str, Any]


@dataclass(frozen=True)
class EntityEntry:
    entity_type: str
    term: str
    priority: int
    weight: float
    aliases: tuple[str, ...]
    raw: dict[str, Any]

    @property
    def length(self) -> int:
        return len(self.term)


@dataclass(frozen=True)
class SynonymRule:
    source: str
    target: str
    raw: dict[str, Any]


@dataclass(frozen=True)
class ConflictRule:
    trigger: str
    winner: str
    loser: str
    penalty: float
    reason: str
    raw: dict[str, Any]


@dataclass(frozen=True)
class EngineConfig:
    raw: dict[str, Any]
    concurrency: int
    max_edit_batch_size: int
    confidence_threshold: float
    precision_name: str | int
    digits: int
    snapshot_commit_interval: int
    graceful_exit_wait_ms: int
    normalize: dict[str, Any]
    scoring: dict[str, Any]
    slots: dict[str, Any]
    merge: dict[str, Any]
    sequence: dict[str, Any]
    config_hash: str
    product_manifest: dict[str, str]


@dataclass
class ResourceBundle:
    base_dir: Path
    intents: dict[str, IntentDefinition]
    entities: dict[str, tuple[EntityEntry, ...]]
    sequence_model: dict[str, Any]
    rules: dict[str, Any]
    config: EngineConfig
    entry_manifest: dict[str, str]
    file_hashes: dict[str, str]
    resource_hash: str
    synonyms: tuple[SynonymRule, ...] = field(default_factory=tuple)
    conflicts: tuple[ConflictRule, ...] = field(default_factory=tuple)

    @property
    def intent_names(self) -> tuple[str, ...]:
        return tuple(sorted(self.intents))

    @property
    def entity_types(self) -> tuple[str, ...]:
        return tuple(sorted(self.entities))

    @property
    def entity_terms(self) -> tuple[str, ...]:
        terms: set[str] = set()
        for entries in self.entities.values():
            terms.update(entry.term for entry in entries)
        return tuple(sorted(terms))

    def all_entity_entries(self) -> tuple[EntityEntry, ...]:
        result: list[EntityEntry] = []
        for entity_type in self.entity_types:
            result.extend(self.entities[entity_type])
        return tuple(result)

    def allowed_types_for(self, intent_name: str) -> tuple[str, ...]:
        definition = self.intents.get(intent_name)
        if definition is None:
            return ()
        return definition.allowed_entity_types

    def as_summary(self) -> dict[str, Any]:
        return {
            "base_dir": str(self.base_dir),
            "intent_names": list(self.intent_names),
            "entity_types": list(self.entity_types),
            "intent_feature_count": sum(len(item.features) for item in self.intents.values()),
            "entity_entry_count": sum(len(items) for items in self.entities.values()),
            "synonym_rule_count": len(self.synonyms),
            "conflict_rule_count": len(self.conflicts),
            "resource_hash": self.resource_hash,
            "config_hash": self.config.config_hash,
        }


def _load_config(raw: dict[str, Any]) -> EngineConfig:
    def integer(name: str, default: int, minimum: int = 0) -> int:
        value = raw.get(name, default)
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"config.{name} must be an integer")
        if value < minimum:
            raise ValueError(f"config.{name} must be >= {minimum}")
        return value

    concurrency = integer("concurrency", raw.get("workers", 4), 1)
    max_edit_batch_size = integer("max_edit_batch_size", 32, 1)
    snapshot_commit_interval = integer("snapshot_commit_interval", 10, 1)
    graceful_exit_wait_ms = integer("graceful_exit_wait_ms", 2000, 0)
    threshold_raw = raw.get("confidence_threshold", 0.5)
    threshold = _finite_float(threshold_raw, field_name="confidence_threshold")
    if threshold < 0.0 or threshold > 1.0:
        raise ValueError("confidence_threshold must be in [0, 1]")
    precision_raw = raw.get("precision", raw.get("precision_tier", "high"))
    digits = precision_digits(precision_raw)
    normalize = dict(
        {
            "unicode_form": "NFKC",
            "collapse_whitespace": True,
            "strip": True,
            "apply_synonyms": True,
        }
    )
    normalize.update(raw.get("normalize", {}) if isinstance(raw.get("normalize", {}), dict) else {})
    scoring = dict(
        {
            "length_bonus": 0.15,
            "position_decay": 0.02,
            "default_conflict_penalty": 0.75,
        }
    )
    scoring.update(raw.get("scoring", raw.get("score", {})) if isinstance(raw.get("scoring", raw.get("score", {})), dict) else {})
    slots = dict(
        {
            "incompatible_penalty": 0.5,
            "require_intent_compatibility": True,
            "source_priority": ["dictionary", "sequence"],
        }
    )
    slots.update(raw.get("slots", raw.get("slot", {})) if isinstance(raw.get("slots", raw.get("slot", {})), dict) else {})
    merge = dict(
        {
            "source_priority": ["dictionary", "sequence"],
            "deduplicate": True,
        }
    )
    merge.update(raw.get("merge", {}) if isinstance(raw.get("merge", {}), dict) else {})
    sequence = dict({"round_path_score": True})
    sequence.update(raw.get("sequence", {}) if isinstance(raw.get("sequence", {}), dict) else {})
    product_manifest = {
        "normalize": stable_hash(normalize),
        "intent": stable_hash({"scoring": scoring, "precision": digits, "threshold": threshold}),
        "slots": stable_hash({"slots": slots, "precision": digits}),
        "sequence": stable_hash({"sequence": sequence, "precision": digits}),
        "merge": stable_hash({"merge": merge, "slots": slots, "precision": digits}),
    }
    return EngineConfig(
        raw=raw,
        concurrency=concurrency,
        max_edit_batch_size=max_edit_batch_size,
        confidence_threshold=threshold,
        precision_name=precision_raw,
        digits=digits,
        snapshot_commit_interval=snapshot_commit_interval,
        graceful_exit_wait_ms=graceful_exit_wait_ms,
        normalize=normalize,
        scoring=scoring,
        slots=slots,
        merge=merge,
        sequence=sequence,
        config_hash=stable_hash(raw),
        product_manifest=product_manifest,
    )


def _parse_intents(raw: Any, entity_types: set[str]) -> dict[str, IntentDefinition]:
    items: list[dict[str, Any]]
    if isinstance(raw, dict) and isinstance(raw.get("intents"), list):
        items = list(raw["intents"])
    elif isinstance(raw, list):
        items = list(raw)
    elif isinstance(raw, dict):
        items = []
        for name, value in raw.items():
            if isinstance(value, dict):
                item = dict(value)
                item.setdefault("name", name)
                items.append(item)
            else:
                items.append({"name": name, "features": value})
    else:
        raise ValueError("intents resource must contain an intents list or a mapping")

    result: dict[str, IntentDefinition] = {}
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("each intent definition must be an object")
        name = item.get("name", item.get("intent"))
        if not isinstance(name, str) or not name.strip():
            raise ValueError("intent name must be a non-empty string")
        name = name.strip()
        if name in result:
            raise ValueError(f"duplicate intent name: {name}")
        raw_features = item.get("features", item.get("keywords", item.get("terms", [])))
        feature_items: list[dict[str, Any]] = []
        if isinstance(raw_features, dict):
            feature_items = [
                {"term": term, "weight": weight} for term, weight in raw_features.items()
            ]
        elif isinstance(raw_features, list):
            for feature in raw_features:
                if isinstance(feature, str):
                    feature_items.append({"term": feature, "weight": 1.0})
                elif isinstance(feature, dict):
                    feature_items.append(feature)
                else:
                    raise ValueError(f"invalid feature in intent {name}")
        else:
            raise ValueError(f"features for intent {name} must be a mapping or list")
        features: list[IntentFeature] = []
        for feature in feature_items:
            term = feature.get("term", feature.get("word", feature.get("value")))
            if not isinstance(term, str) or not term:
                raise ValueError(f"feature term in intent {name} must be non-empty")
            weight = _finite_float(feature.get("weight", 1.0), field_name=f"weight for {name}/{term}")
            features.append(IntentFeature(term=term, weight=weight, raw=dict(feature)))
        features.sort(key=lambda item: (item.term, item.weight, canonical_json(item.raw)))
        allowed_raw = item.get("allowed_entity_types", item.get("entity_types", sorted(entity_types)))
        if allowed_raw in (None, "*", "all"):
            allowed = tuple(sorted(entity_types))
        elif isinstance(allowed_raw, str):
            allowed = (_canonical_entity_type(allowed_raw),)
        elif isinstance(allowed_raw, list):
            allowed = tuple(sorted({_canonical_entity_type(str(value)) for value in allowed_raw}))
        else:
            raise ValueError(f"allowed_entity_types for intent {name} must be a list or '*'")
        unknown = set(allowed) - entity_types
        if unknown:
            raise ValueError(f"intent {name} references unknown entity types: {sorted(unknown)}")
        result[name] = IntentDefinition(
            name=name,
            features=tuple(features),
            allowed_entity_types=allowed,
            raw=dict(item),
        )
    if not result:
        raise ValueError("intent vocabulary is empty")
    return dict(sorted(result.items()))


def _parse_entities(raw: Any, allowed_types: set[str]) -> dict[str, tuple[EntityEntry, ...]]:
    if isinstance(raw, dict) and isinstance(raw.get("entities"), dict):
        raw = raw["entities"]
    if not isinstance(raw, dict):
        raise ValueError("entity resource must be a mapping keyed by entity type")
    result: dict[str, list[EntityEntry]] = {entity_type: [] for entity_type in sorted(allowed_types)}
    default_priority = 100
    for raw_type, values in raw.items():
        entity_type = _canonical_entity_type(str(raw_type))
        if entity_type not in allowed_types:
            raise ValueError(f"unknown entity type: {raw_type}")
        if isinstance(values, dict):
            values = [
                {"term": term, **(value if isinstance(value, dict) else {"weight": value})}
                for term, value in values.items()
            ]
        if not isinstance(values, list):
            raise ValueError(f"entity entries for {entity_type} must be a list or mapping")
        for index, value in enumerate(values):
            if isinstance(value, str):
                item: dict[str, Any] = {"term": value}
            elif isinstance(value, dict):
                item = dict(value)
            else:
                raise ValueError(f"invalid entity entry in {entity_type}")
            term = item.get("term", item.get("word", item.get("value")))
            if not isinstance(term, str) or not term:
                raise ValueError(f"entity term in {entity_type} must be non-empty")
            priority_raw = item.get("priority", default_priority + index)
            if isinstance(priority_raw, bool) or not isinstance(priority_raw, int):
                priority = int(priority_raw)
            else:
                priority = priority_raw
            weight = _finite_float(item.get("weight", 1.0), field_name=f"weight for entity {term}")
            aliases_raw = item.get("aliases", [])
            aliases = tuple(str(alias) for alias in aliases_raw) if isinstance(aliases_raw, list) else ()
            result[entity_type].append(
                EntityEntry(
                    entity_type=entity_type,
                    term=term,
                    priority=priority,
                    weight=weight,
                    aliases=aliases,
                    raw=item,
                )
            )
    for entity_type, entries in result.items():
        entries.sort(key=lambda item: (-len(item.term), item.priority, item.term, canonical_json(item.raw)))
    return {entity_type: tuple(entries) for entity_type, entries in sorted(result.items())}


def _parse_rules(raw: Any) -> tuple[dict[str, Any], tuple[SynonymRule, ...], tuple[ConflictRule, ...]]:
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ValueError("rules resource must be an object")
    raw_synonyms = raw.get("synonyms", raw.get("synonym_normalization", []))
    if isinstance(raw_synonyms, dict):
        raw_synonyms = [{"source": source, "target": target} for source, target in raw_synonyms.items()]
    if not isinstance(raw_synonyms, list):
        raise ValueError("synonyms must be a list or mapping")
    synonyms: list[SynonymRule] = []
    for item in raw_synonyms:
        if isinstance(item, (list, tuple)) and len(item) == 2:
            item = {"source": item[0], "target": item[1]}
        if not isinstance(item, dict):
            raise ValueError("synonym rule must be an object")
        source = item.get("source", item.get("from", item.get("term")))
        target = item.get("target", item.get("to", item.get("replacement")))
        if not isinstance(source, str) or not source or not isinstance(target, str):
            raise ValueError("synonym rule needs non-empty source and target strings")
        synonyms.append(SynonymRule(source=source, target=target, raw=dict(item)))
    synonyms.sort(key=lambda item: (-len(item.source), item.source, item.target, canonical_json(item.raw)))

    raw_conflicts = raw.get("conflicts", raw.get("intent_conflicts", []))
    if isinstance(raw_conflicts, dict):
        raw_conflicts = list(raw_conflicts.values())
    if not isinstance(raw_conflicts, list):
        raise ValueError("conflicts must be a list")
    conflicts: list[ConflictRule] = []
    for item in raw_conflicts:
        if not isinstance(item, dict):
            raise ValueError("conflict rule must be an object")
        trigger = item.get("trigger", item.get("when"))
        winner = item.get("winner", item.get("primary"))
        loser = item.get("loser", item.get("secondary"))
        if not all(isinstance(value, str) and value for value in (trigger, winner, loser)):
            raise ValueError("conflict rule needs trigger, winner and loser strings")
        penalty = _finite_float(item.get("penalty", 0.75), field_name="conflict penalty")
        reason = str(item.get("reason", "intent_conflict"))
        conflicts.append(
            ConflictRule(
                trigger=trigger,
                winner=winner,
                loser=loser,
                penalty=penalty,
                reason=reason,
                raw=dict(item),
            )
        )
    conflicts.sort(
        key=lambda item: (item.trigger, item.winner, item.loser, item.penalty, item.reason)
    )
    parsed_rules = dict(raw)
    parsed_rules["synonyms"] = [item.raw for item in synonyms]
    parsed_rules["conflicts"] = [item.raw for item in conflicts]
    return parsed_rules, tuple(synonyms), tuple(conflicts)


def _build_entry_manifest(
    intents: dict[str, IntentDefinition],
    entities: dict[str, tuple[EntityEntry, ...]],
    synonyms: Iterable[SynonymRule],
    conflicts: Iterable[ConflictRule],
    sequence_model: dict[str, Any],
) -> dict[str, str]:
    manifest: dict[str, str] = {}
    for name, definition in sorted(intents.items()):
        definition_raw = {key: value for key, value in definition.raw.items() if key not in {"features", "keywords", "terms"}}
        manifest[f"intent_def:{name}"] = stable_hash(definition_raw)
        feature_groups: dict[str, list[dict[str, Any]]] = {}
        for feature in definition.features:
            feature_groups.setdefault(feature.term, []).append(feature.raw)
        for term, raws in sorted(feature_groups.items()):
            manifest[f"intent_feature:{name}:{term}"] = stable_hash(sorted(raws, key=canonical_json))
    entity_groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for entity_type, entries in entities.items():
        for entry in entries:
            entity_groups.setdefault((entity_type, entry.term), []).append(entry.raw)
    for (entity_type, term), raws in sorted(entity_groups.items()):
        manifest[f"entity:{entity_type}:{term}"] = stable_hash(sorted(raws, key=canonical_json))
    synonym_groups: dict[str, list[dict[str, Any]]] = {}
    for rule in synonyms:
        synonym_groups.setdefault(rule.source, []).append(rule.raw)
    for source, raws in sorted(synonym_groups.items()):
        manifest[f"rule_synonym:{source}"] = stable_hash(sorted(raws, key=canonical_json))
    conflict_groups: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for rule in conflicts:
        conflict_groups.setdefault((rule.trigger, rule.winner, rule.loser), []).append(rule.raw)
    for (trigger, winner, loser), raws in sorted(conflict_groups.items()):
        manifest[f"rule_conflict:{trigger}:{winner}:{loser}"] = stable_hash(sorted(raws, key=canonical_json))
    manifest["sequence_model"] = stable_hash(sequence_model)
    return manifest


def _find_resource_file(base: Path, names: tuple[str, ...]) -> Path:
    for name in names:
        candidate = base / name
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(f"none of the resource files exist in {base}: {', '.join(names)}")


def load_resources(
    resources_dir: str | Path,
    config_path: str | Path | None = None,
    *,
    entity_types: Iterable[str] = ENTITY_TYPES,
) -> ResourceBundle:
    """Load the four resource files and the engine configuration."""

    base = Path(resources_dir)
    if not base.is_dir():
        raise FileNotFoundError(f"resource directory does not exist: {base}")
    intent_path = _find_resource_file(base, ("intents.json", "intent_vocab.json", "vocabulary.json"))
    entity_path = _find_resource_file(base, ("entities.json", "entity_dict.json", "dictionary.json"))
    model_path = _find_resource_file(base, ("sequence_model.json", "bieso_params.json", "sequence_params.json"))
    rules_path = _find_resource_file(base, ("rules.json", "rule_table.json"))
    config_file = Path(config_path) if config_path is not None else _find_resource_file(base, ("config.json", "intent_config.json"))

    intent_raw = read_json(intent_path)
    entity_raw = read_json(entity_path)
    model_raw = read_json(model_path)
    rules_raw = read_json(rules_path)
    config_raw = read_json(config_file)
    if not isinstance(model_raw, dict):
        raise ValueError("sequence model must be an object")
    if not isinstance(config_raw, dict):
        raise ValueError("engine config must be an object")
    config = _load_config(config_raw)
    allowed_types = {_canonical_entity_type(str(item)) for item in entity_types}
    intents = _parse_intents(intent_raw, allowed_types)
    entities = _parse_entities(entity_raw, allowed_types)
    rules, synonyms, conflicts = _parse_rules(rules_raw)
    entry_manifest = _build_entry_manifest(intents, entities, synonyms, conflicts, model_raw)
    file_hashes = {
        "intents": stable_hash(intent_raw),
        "entities": stable_hash(entity_raw),
        "sequence_model": stable_hash(model_raw),
        "rules": stable_hash(rules_raw),
    }
    resource_hash = stable_hash(
        {
            "entry_manifest": entry_manifest,
            "files": file_hashes,
        }
    )
    return ResourceBundle(
        base_dir=base,
        intents=intents,
        entities=entities,
        sequence_model=model_raw,
        rules=rules,
        config=config,
        entry_manifest=entry_manifest,
        file_hashes=file_hashes,
        resource_hash=resource_hash,
        synonyms=synonyms,
        conflicts=conflicts,
    )


def config_from_mapping(raw: dict[str, Any]) -> EngineConfig:
    """Public helper used by tests and embedders that do not use a file."""

    if not isinstance(raw, dict):
        raise ValueError("config must be a mapping")
    return _load_config(raw)
