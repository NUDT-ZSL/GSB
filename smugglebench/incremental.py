"""Rule-patch invalidation closure and incremental re-adjudication."""

from __future__ import annotations

import copy
import hashlib
from dataclasses import dataclass
from typing import Any, Iterable

from .codes import E_INPUT, E_RECOMPUTE_DIVERGE
from .engine import (
    _final_verdict,
    _provisional_verdict,
    _risk_class,
    _stable_key,
    compare_interpretations,
)
from .models import MutationSpec, Rule, WorkspaceData
from .mutations import apply_mutation
from .rules import (
    RuleEngine,
    detect_rule_cycles,
    latest_rule_records,
    merge_rule_view,
    rule_dependency_closure,
)
from .state_machine import (
    build_connection_results,
    local_interpretation,
    parse_mutated_request,
)
from .util import BusinessError, canonical_json


@dataclass
class PatchResult:
    report: dict[str, Any]
    changed_rules: list[str]
    affected_connections: list[str]
    affected_mutations: list[str]
    verification: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "changed_rules": self.changed_rules,
            "affected_connections": self.affected_connections,
            "affected_mutations": self.affected_mutations,
            "verification": self.verification,
            "report": self.report,
        }


def incremental_recompute(
    data: WorkspaceData,
    baseline_report: dict[str, Any],
    new_rules: Iterable[Rule],
    *,
    evaluation_ms: int | None = None,
) -> PatchResult:
    rules = list(new_rules)
    detect_rule_cycles(rules)
    at_ms = (
        evaluation_ms
        if evaluation_ms is not None
        else int(baseline_report.get("run", {}).get("evaluation_ms", data.observed_clock_ms))
    )
    active, audit = merge_rule_view(rules, at_ms)
    new_engine = RuleEngine(active.values(), audit_rules=rules)

    changed = _changed_rule_names(data.rules, rules)
    closure = rule_dependency_closure(changed, [*data.rules, *rules])
    old_candidates: dict[str, set[str]] = {}
    baseline_mutation_records = _baseline_mutation_list(baseline_report)
    for record in baseline_mutation_records:
        mutation_id = record.get("mutation_id")
        old_candidates[str(mutation_id)] = set(record.get("candidate_rules", []))

    old_universe = set().union(*old_candidates.values()) if old_candidates else set()
    all_must_recompute = bool(closure - old_universe)
    affected_mutations = {
        mutation_id
        for mutation_id, candidates in old_candidates.items()
        if all_must_recompute or bool(candidates & closure)
    }

    connection_candidates: dict[str, set[str]] = {}
    for connection_id, record in baseline_report.get("connections", {}).items():
        names: set[str] = set()
        for hit in record.get("rule_hits", []):
            if isinstance(hit, dict) and hit.get("rule_key"):
                names.add(str(hit["rule_key"]))
        for rule_name in record.get("candidate_rule_evaluations", {}):
            names.add(str(rule_name))
        connection_candidates[connection_id] = names
    affected_connections = {
        connection_id
        for connection_id, candidates in connection_candidates.items()
        if all_must_recompute or bool(candidates & closure)
    }
    for mutation_id in affected_mutations:
        for item in baseline_mutation_records:
            if item.get("mutation_id") == mutation_id:
                affected_connections.add(str(item.get("connection_id")))

    templates = _templates(data, new_engine)
    baseline_mutations = {
        item["mutation_id"]: item
        for item in baseline_mutation_records
    }
    recomputed_mutations: dict[str, dict[str, Any]] = {}
    for spec in data.mutations:
        if spec.mutation_id not in affected_mutations:
            continue
        baseline = baseline_mutations.get(spec.mutation_id)
        if baseline is None:
            raise BusinessError(
                E_RECOMPUTE_DIVERGE,
                "baseline is missing an affected mutation",
                {"mutation_id": spec.mutation_id},
            )
        recomputed_mutations[spec.mutation_id] = _recompute_mutation(
            spec,
            baseline,
            templates,
            new_engine,
        )

    affected_flows = [
        flow for flow in data.flows if flow.connection_id in affected_connections
    ]
    partial_connections = build_connection_results(
        affected_flows,
        rule_engine=new_engine,
    )
    full_connections = build_connection_results(data.flows, rule_engine=new_engine)
    full_mutations = {}
    for spec in data.mutations:
        baseline = baseline_mutations.get(spec.mutation_id)
        if baseline is None:
            continue
        full_mutations[spec.mutation_id] = _recompute_mutation(
            spec,
            baseline,
            templates,
            new_engine,
        )

    mismatches: list[str] = []
    for connection_id in sorted(affected_connections):
        incremental_value = partial_connections.get(connection_id)
        full_value = full_connections.get(connection_id)
        if incremental_value is None or full_value is None:
            mismatches.append(f"connection:{connection_id}:missing")
            continue
        if canonical_json(incremental_value.to_dict()) != canonical_json(
            full_value.to_dict()
        ):
            mismatches.append(f"connection:{connection_id}")
    for mutation_id in sorted(affected_mutations):
        if canonical_json(recomputed_mutations.get(mutation_id)) != canonical_json(
            full_mutations.get(mutation_id)
        ):
            mismatches.append(f"mutation:{mutation_id}")
    if mismatches:
        raise BusinessError(
            E_RECOMPUTE_DIVERGE,
            "incremental replay differs from a from-zero replay",
            {
                "mismatches": mismatches,
                "affected_connections": sorted(affected_connections),
                "affected_mutations": sorted(affected_mutations),
            },
        )

    report = copy.deepcopy(baseline_report)
    report["rules"] = {
        "active": [active[key].to_dict() for key in sorted(active)],
        "merge_audit": audit,
    }
    report["connections"] = {
        key: (
            partial_connections[key].to_dict()
            if key in partial_connections
            else value
        )
        for key, value in report.get("connections", {}).items()
    }
    mutation_map = {
        item["mutation_id"]: item for item in _baseline_mutation_list(report)
    }
    mutation_map.update(recomputed_mutations)
    report["mutations"] = [
        mutation_map[key] for key in sorted(mutation_map)
    ]
    report["mutations"].sort(
        key=lambda item: (
            item.get("stable_key", ""),
            item.get("mutation_id", ""),
        )
    )
    report["incremental_patch"] = {
        "changed_rules": sorted(changed),
        "dependency_closure": sorted(closure),
        "affected_connections": sorted(affected_connections),
        "affected_mutations": sorted(affected_mutations),
        "verification": "EQUAL_TO_FULL_REPLAY",
    }
    report["deterministic_summary"] = {
        "mutation_results": [
            _deterministic(item) for item in report["mutations"]
        ],
        "aggregate": _aggregate(
            report["mutations"],
            completed_connections=len(report.get("connections", {})),
        ),
    }
    return PatchResult(
        report=report,
        changed_rules=sorted(changed),
        affected_connections=sorted(affected_connections),
        affected_mutations=sorted(affected_mutations),
        verification="EQUAL_TO_FULL_REPLAY",
    )


def _templates(
    data: WorkspaceData,
    engine: RuleEngine,
) -> dict[tuple[str, int], Any]:
    connections = build_connection_results(data.flows, rule_engine=engine)
    return {
        (message.connection_id, message.request_index): message
        for result in connections.values()
        for message in result.messages
    }


def _baseline_mutation_list(report: dict[str, Any]) -> list[dict[str, Any]]:
    mutations = report.get("mutations", [])
    if isinstance(mutations, dict):
        return [
            record
            for _mutation_id, record in sorted(mutations.items())
            if isinstance(record, dict)
        ]
    if isinstance(mutations, list):
        return [record for record in mutations if isinstance(record, dict)]
    return []


def _recompute_mutation(
    spec: MutationSpec,
    baseline: dict[str, Any],
    templates: dict[tuple[str, int], Any],
    engine: RuleEngine,
) -> dict[str, Any]:
    result = copy.deepcopy(baseline)
    template = templates.get((spec.connection_id, spec.request_index))
    if template is None:
        return result
    try:
        raw = apply_mutation(template, spec)
    except (ValueError, BusinessError) as exc:
        result["error"] = (
            exc.to_dict()
            if isinstance(exc, BusinessError)
            else {"code": E_INPUT, "message": str(exc)}
        )
        result["final_verdict"] = "ERROR"
        result["status"] = "ERROR"
        return result
    (
        messages,
        _transitions,
        hits,
        suppressed,
        candidates,
        _errors,
    ) = parse_mutated_request(
        raw,
        connection_id=spec.connection_id,
        rule_engine=engine,
    )
    local = local_interpretation(messages)
    remote = result.get("remote_interpretation")
    discrepancies = compare_interpretations(local, remote)
    provisional = _provisional_verdict(hits, local)
    result["local_interpretation"] = local
    result["rule_hits"] = [hit.to_dict() for hit in hits]
    result["suppressed_hits"] = suppressed
    result["candidate_rules"] = sorted(candidates)
    result["provisional_verdict"] = provisional
    result["discrepancies"] = list(discrepancies)
    result["final_verdict"] = _final_verdict(
        provisional=provisional,
        discrepancies=discrepancies,
        transport_error=result.get("error"),
    )
    if result.get("sent") and result.get("error") is None:
        result["overturn_count"] = int(
            _risk_class(provisional) != _risk_class(result["final_verdict"])
        )
    else:
        result["overturn_count"] = 0
    if result.get("error") is not None:
        result["status"] = "ERROR"
    elif discrepancies:
        result["status"] = "DIVERGENT"
    else:
        result["status"] = "OK"
    if result.get("echo_sha256") is not None:
        result["echo_sha256"] = hashlib.sha256(raw).hexdigest()
    return result


def _changed_rule_names(
    old_rules: Iterable[Rule],
    new_rules: Iterable[Rule],
) -> set[str]:
    old = latest_rule_records(old_rules)
    new = latest_rule_records(new_rules)
    changed: set[str] = set()
    for name in sorted(set(old) | set(new)):
        if name not in old or name not in new:
            changed.add(name)
            continue
        if _signature(old[name]) != _signature(new[name]):
            changed.add(name)
    return changed


def _signature(rule: Rule) -> str:
    return canonical_json(
        {
            "generation": rule.generation,
            "effective_from_ms": rule.effective_from_ms,
            "effective_to_ms": rule.effective_to_ms,
            "enabled": rule.enabled,
            "tombstone": rule.tombstone,
            "transitions": [item.to_dict() for item in rule.transitions],
            "depends_on": sorted(rule.depends_on),
        }
    )


def _deterministic(item: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "mutation_id",
        "connection_id",
        "stable_key",
        "status",
        "provisional_verdict",
        "final_verdict",
        "overturn_count",
        "discrepancies",
        "local_interpretation",
        "remote_interpretation",
        "rule_hits",
        "suppressed_hits",
        "candidate_rules",
        "error",
        "echo_sha256",
        "echo_bytes_seen",
        "delay_observation",
    )
    return {key: item.get(key) for key in keys}


def _aggregate(
    mutations: list[dict[str, Any]],
    *,
    completed_connections: int,
) -> dict[str, Any]:
    verdicts: dict[str, int] = {}
    discrepancies: dict[str, int] = {}
    errors: dict[str, int] = {}
    overturns = 0
    for item in mutations:
        verdict = str(item.get("final_verdict", "UNKNOWN"))
        verdicts[verdict] = verdicts.get(verdict, 0) + 1
        for code in item.get("discrepancies", []):
            discrepancies[code] = discrepancies.get(code, 0) + 1
        if item.get("error"):
            code = str(item["error"].get("code", "UNKNOWN"))
            errors[code] = errors.get(code, 0) + 1
        overturns += int(item.get("overturn_count", 0))
    return {
        "completed_connections": completed_connections,
        "mutation_count": len(mutations),
        "verdicts": dict(sorted(verdicts.items())),
        "discrepancies": dict(sorted(discrepancies.items())),
        "errors": dict(sorted(errors.items())),
        "overturn_count": overturns,
    }
