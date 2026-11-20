"""Bidirectional, loss-aware migration between rule formats v1/v2/v3."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Mapping

from .errors import format_corrupt, unsupported_version
from .models import Condition, RewriteRule, RuleSet
from .rules import (
    parse_rules_text,
    ruleset_checksum,
    serialize_ruleset,
)

DEFAULT_BRIDGE_CONFIDENCE_WEIGHT = 1.0
DEFAULT_MAX_EXPANSION_DEPTH = 1
DEFAULT_ENABLED = True


@dataclass(frozen=True)
class MigrationPolicies:
    multi_target: str = "flatten"
    condition: str = "flatten"

    def validate(self) -> None:
        if self.multi_target not in {"flatten", "drop"}:
            raise format_corrupt(
                "unsupported multi-target migration policy",
                policy=self.multi_target,
                allowed=["flatten", "drop"],
            )
        if self.condition not in {"flatten", "drop"}:
            raise format_corrupt(
                "unsupported condition migration policy",
                policy=self.condition,
                allowed=["flatten", "drop"],
            )


@dataclass(frozen=True)
class MigrationResult:
    ruleset: RuleSet
    report: Mapping[str, Any]

    def bytes(self) -> bytes:
        return serialize_ruleset(self.ruleset)

    def reread(self) -> RuleSet:
        return parse_rules_text(
            self.bytes().decode("utf-8"),
            source="<migration-output>",
            expected_version=self.ruleset.format_version,
        )


def migrate_rules(
    ruleset: RuleSet,
    target_version: str,
    *,
    policies: MigrationPolicies | Mapping[str, str] | None = None,
) -> MigrationResult:
    if target_version not in {"v1", "v2", "v3"}:
        raise unsupported_version(
            "unsupported migration target version",
            version=target_version,
        )
    if isinstance(policies, Mapping):
        policy = MigrationPolicies(**dict(policies))
    else:
        policy = policies or MigrationPolicies()
    policy.validate()

    completions: list[dict[str, Any]] = []
    flattened: list[dict[str, Any]] = []
    drops: list[dict[str, Any]] = []

    source_version = ruleset.format_version
    if source_version == target_version:
        migrated_rules = list(ruleset.rules)
    elif target_version in {"v2", "v3"} and _version_rank(target_version) > _version_rank(
        source_version
    ):
        migrated_rules = _upgrade(
            ruleset.rules,
            target_version=target_version,
            completions=completions,
        )
    else:
        migrated_rules = _downgrade(
            ruleset.rules,
            target_version=target_version,
            policy=policy,
            flattened=flattened,
            drops=drops,
        )

    result = RuleSet(format_version=target_version, rules=tuple(migrated_rules))
    report = {
        "kind": "rule_migration",
        "from_version": source_version,
        "to_version": target_version,
        "policies": {
            "multi_target": policy.multi_target,
            "condition": policy.condition,
        },
        "source_checksum": ruleset_checksum(ruleset),
        "target_checksum": ruleset_checksum(result),
        "rule_count_in": len(ruleset.rules),
        "rule_count_out": len(result.rules),
        "completions": sorted(
            completions,
            key=lambda item: (item["rule_id"], item["field"], item["source"]),
        ),
        "flattened": sorted(
            flattened,
            key=lambda item: (item["rule_id"], item["kind"], item["target_lang"]),
        ),
        "drops": sorted(
            drops,
            key=lambda item: (item["rule_id"], item["kind"], item["path"]),
        ),
    }
    return MigrationResult(ruleset=result, report=report)


def _version_rank(version: str) -> int:
    if version not in {"v1", "v2", "v3"}:
        raise unsupported_version("unsupported rule version", version=version)
    return int(version[1:])


def _upgrade(
    rules: tuple[RewriteRule, ...],
    *,
    target_version: str,
    completions: list[dict[str, Any]],
) -> list[RewriteRule]:
    upgraded: list[RewriteRule] = []
    for rule in rules:
        if target_version == "v2" and len(rule.targets) != 1:
            raise format_corrupt(
                "v2 cannot represent multi-target rule during upgrade",
                rule_id=rule.rule_id,
            )
        weight = rule.bridge_confidence_weight
        depth = rule.max_expansion_depth
        enabled = rule.enabled
        if weight is None:
            weight = DEFAULT_BRIDGE_CONFIDENCE_WEIGHT
            completions.append(
                _completion(rule, "bridge_confidence_weight", weight)
            )
        if depth is None:
            depth = DEFAULT_MAX_EXPANSION_DEPTH
            completions.append(_completion(rule, "max_expansion_depth", depth))
        if enabled is None:
            enabled = DEFAULT_ENABLED
            completions.append(_completion(rule, "enabled", enabled))

        condition = rule.condition
        if target_version == "v3" and condition is None and rule.condition_terms:
            condition = Condition(
                op="all",
                children=tuple(
                    Condition(op="term_present", term=term)
                    for term in rule.condition_terms
                ),
            )
        out_condition_terms = () if target_version == "v3" else rule.condition_terms

        upgraded.append(
            replace(
                rule,
                condition=condition,
                condition_terms=out_condition_terms,
                bridge_confidence_weight=weight,
                max_expansion_depth=depth,
                enabled=enabled,
            )
        )
    return upgraded


def _completion(rule: RewriteRule, field: str, value: Any) -> dict[str, Any]:
    return {
        "rule_id": rule.rule_id,
        "field": field,
        "value": value,
        "source": f"schema_default:{field}",
    }


def _downgrade(
    rules: tuple[RewriteRule, ...],
    *,
    target_version: str,
    policy: MigrationPolicies,
    flattened: list[dict[str, Any]],
    drops: list[dict[str, Any]],
) -> list[RewriteRule]:
    downgraded: list[RewriteRule] = []
    for rule in rules:
        targets = rule.targets
        if len(targets) > 1 and policy.multi_target == "drop":
            kept = targets[:1]
            for target in targets[1:]:
                drops.append(
                    {
                        "rule_id": rule.rule_id,
                        "kind": "multi_target",
                        "path": f"targets[{target.lang},{target.term}]",
                        "value": target.to_dict(),
                        "reason": "target format can represent only one target and policy=drop",
                    }
                )
            targets = kept

        outbound_targets = targets if policy.multi_target == "flatten" else targets[:1]
        for target_index, target in enumerate(outbound_targets):
            out_rule_id = rule.rule_id
            if len(outbound_targets) > 1 and target_index > 0:
                out_rule_id = f"{rule.rule_id}@{target.lang}"
            condition = rule.condition
            condition_terms = rule.condition_terms

            if target_version == "v1":
                if condition is not None or condition_terms:
                    drops.append(
                        {
                            "rule_id": rule.rule_id,
                            "kind": "condition",
                            "path": "condition",
                            "value": condition.to_dict() if condition else list(condition_terms),
                            "reason": "v1 cannot express conditions",
                        }
                    )
                condition = None
                condition_terms = ()
                for field in (
                    "bridge_confidence_weight",
                    "max_expansion_depth",
                    "enabled",
                ):
                    value = getattr(rule, field)
                    if value is not None:
                        drops.append(
                            {
                                "rule_id": rule.rule_id,
                                "kind": "optional_field",
                                "path": field,
                                "value": value,
                                "reason": "v1 cannot express migration defaults",
                            }
                        )
                weight = None
                depth = None
                enabled = None
            else:
                weight = rule.bridge_confidence_weight
                depth = rule.max_expansion_depth
                enabled = rule.enabled
                if condition is not None:
                    terms = condition.conjunction_terms()
                    if policy.condition == "flatten" and terms is not None:
                        condition_terms = terms
                        flattened.append(
                            {
                                "rule_id": rule.rule_id,
                                "kind": "condition",
                                "target_lang": target.lang,
                                "term": target.term,
                                "from": condition.to_dict(),
                                "to_condition_terms": list(terms),
                            }
                        )
                    else:
                        drops.append(
                            {
                                "rule_id": rule.rule_id,
                                "kind": "condition",
                                "path": "condition",
                                "value": condition.to_dict(),
                                "reason": "v2 cannot express this nested/non-conjunctive condition",
                            }
                        )
                    condition = None

            if len(outbound_targets) > 1:
                flattened.append(
                    {
                        "rule_id": out_rule_id,
                        "kind": "multi_target",
                        "target_lang": target.lang,
                        "term": target.term,
                        "from": [item.to_dict() for item in rule.targets],
                        "to_target": target.to_dict(),
                    }
                )
            downgraded.append(
                RewriteRule(
                    rule_id=out_rule_id,
                    source_lang=rule.source_lang,
                    source_term=rule.source_term,
                    targets=(target,),
                    condition=condition,
                    bridge_confidence_weight=weight,
                    max_expansion_depth=depth,
                    enabled=enabled,
                    condition_terms=condition_terms,
                )
            )

    # Multi-target flattening can create duplicate IDs when an explicit rule
    # already used the generated suffix. Fail instead of silently collapsing.
    ids = [rule.rule_id for rule in downgraded]
    if len(ids) != len(set(ids)):
        raise format_corrupt("migration produced duplicate rule_id values")
    return downgraded


def migrate_file(
    source_path: str,
    target_version: str,
    *,
    expected_version: str | None = None,
    policies: MigrationPolicies | Mapping[str, str] | None = None,
) -> MigrationResult:
    from .rules import parse_rules

    source = parse_rules(source_path, expected_version=expected_version)
    return migrate_rules(source, target_version, policies=policies)


def expressible_projection(ruleset: RuleSet, version: str) -> dict[str, Any]:
    """Return the fields the target format can express, for round-trip checks."""

    if version not in {"v1", "v2", "v3"}:
        raise unsupported_version("unsupported rule version", version=version)
    rows: list[dict[str, Any]] = []
    for rule in sorted(ruleset.rules, key=lambda item: item.rule_id):
        if version == "v1" and len(rule.targets) > 1:
            continue
        row: dict[str, Any] = {
            "rule_id": rule.rule_id,
            "source_lang": rule.source_lang,
            "source_term": rule.source_term,
            "targets": [
                target.to_dict()
                for target in sorted(
                    rule.targets,
                    key=lambda item: (item.lang, item.term),
                )
            ],
        }
        if version in {"v2", "v3"}:
            if rule.bridge_confidence_weight is not None:
                row["bridge_confidence_weight"] = rule.bridge_confidence_weight
            if rule.max_expansion_depth is not None:
                row["max_expansion_depth"] = rule.max_expansion_depth
            if rule.enabled is not None:
                row["enabled"] = rule.enabled
        if version == "v3":
            if rule.condition is not None:
                row["condition"] = rule.condition.to_dict()
            elif rule.condition_terms:
                row["condition"] = Condition(
                    op="all",
                    children=tuple(
                        Condition(op="term_present", term=term)
                        for term in rule.condition_terms
                    ),
                ).to_dict()
        if version == "v2" and rule.condition_terms:
            row["condition_terms"] = list(rule.condition_terms)
        rows.append(row)
    return {"format_version": version, "rules": rows}
