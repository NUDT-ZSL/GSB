from __future__ import annotations

from copy import deepcopy
from typing import Any

from .canonical import sha256_json


DEFAULT_RULE = {
    "rule_key": "__default__",
    "source": "default",
    "generation": 0,
    "declared_clock_ms": 0,
    "effective_at_ms": 0,
    "tombstone": False,
    "enabled": True,
    "upstream_rules": [],
    "instance_ids": [],
    "config": {"suspect_threshold": 1, "lost_threshold": 3},
}


def observation_sort_key(record: dict[str, Any]) -> tuple[str, int, int, str]:
    """Stable global order: instance, seq, newer clock first, digest ascending."""

    return (
        record["instance_id"],
        record["seq"],
        -int(record["logical_clock_ms"]),
        record["digest"],
    )


def select_winning_observations(records: list[dict[str, Any]]) -> dict[str, Any]:
    by_instance_seq: dict[tuple[str, int], list[dict[str, Any]]] = {}
    for record in records:
        by_instance_seq.setdefault((record["instance_id"], record["seq"]), []).append(record)

    winners: list[dict[str, Any]] = []
    version_decisions: list[dict[str, Any]] = []
    for (instance_id, seq), versions in sorted(by_instance_seq.items()):
        ordered = sorted(
            versions,
            key=lambda item: (-int(item["logical_clock_ms"]), item["digest"], item.get("source", "")),
        )
        winner = ordered[0]
        winners.append(winner)
        if len(ordered) > 1:
            version_decisions.append(
                {
                    "instance_id": instance_id,
                    "seq": seq,
                    "winner_digest": winner["digest"],
                    "winner_logical_clock_ms": winner["logical_clock_ms"],
                    "ordering": "higher_logical_clock_then_digest_ascending",
                    "candidate_digests": [item["digest"] for item in ordered],
                }
            )
    winners.sort(key=lambda item: (item["instance_id"], item["seq"]))
    return {"winners": winners, "version_decisions": version_decisions}


def buffer_digest(records: list[dict[str, Any]]) -> str:
    stable = sorted(
        (
            {
                "instance_id": r["instance_id"],
                "seq": r["seq"],
                "logical_clock_ms": r["logical_clock_ms"],
                "status_code": r["status_code"],
                "latency_ms": r["latency_ms"],
                "digest": r["digest"],
            }
            for r in records
        ),
        key=lambda item: (item["instance_id"], item["seq"], item["digest"]),
    )
    return sha256_json(stable)


def invalid_rule_closure(rules: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Return tombstoned rules and all rules transitively depending on them."""

    invalid: dict[str, dict[str, Any]] = {}

    def reason_for(key: str, stack: tuple[str, ...] = ()) -> tuple[bool, str | None]:
        if key in invalid:
            return True, invalid[key].get("reason")
        rule = rules.get(key)
        if rule is None:
            return True, "missing_rule"
        if rule.get("tombstone") or not rule.get("enabled", True):
            return True, "rule_tombstoned"
        if key in stack:
            return True, "dependency_cycle"
        for upstream in rule.get("upstream_rules", []):
            bad, reason = reason_for(upstream, stack + (key,))
            if bad:
                if reason == "rule_tombstoned":
                    return True, "upstream_rule_tombstoned"
                if reason and reason.startswith("upstream_"):
                    return True, reason
                return True, f"upstream_{reason or 'invalid'}"
        return False, None

    for key in sorted(rules):
        bad, reason = reason_for(key)
        if bad:
            invalid[key] = {"rule_key": key, "invalid": True, "reason": reason}
    return invalid


def _fresh_core(instance: dict[str, Any]) -> dict[str, Any]:
    return {
        "health_status": "healthy",
        "consecutive_failures": 0,
        "last_seq": 0,
        "clock_watermark_ms": 0,
        "counts": {
            "committed_observations": 0,
            "success": 0,
            "status_mismatch": 0,
            "missing_holes": 0,
            "quarantined": 0,
            "failure_increments": 0,
        },
        "decisions": [],
        "version_decisions": [],
    }


def _rule_for_instance(instance_id: str, view: dict[str, Any] | None) -> tuple[dict[str, Any], dict[str, Any] | None]:
    if not view:
        return deepcopy(DEFAULT_RULE), None
    binding = view.get("instance_bindings", {}).get(instance_id)
    if binding and binding.get("rule_key") in view.get("rules", {}):
        rule = view["rules"][binding["rule_key"]]
        return rule, binding
    for rule in view.get("rules", {}).values():
        if instance_id in rule.get("instance_ids", []):
            return rule, {"instance_id": instance_id, "rule_key": rule["rule_key"], "source": rule.get("source")}
    return deepcopy(DEFAULT_RULE), None


def _append_hole(core: dict[str, Any], seq: int, rule: dict[str, Any]) -> None:
    before = core["health_status"]
    decision = {
        "seq": seq,
        "kind": "missing_hole",
        "verdict": "missing",
        "applied": True,
        "real_failure": False,
        "failure_increment": 0,
        "status_code": None,
        "logical_clock_ms": None,
        "latency_ms": None,
        "digest": None,
        "isolation_reason": None,
        "health_before": before,
        "health_after": core["health_status"],
        "consecutive_failures_after": core["consecutive_failures"],
        "rule_key": rule["rule_key"],
        "rule_generation": rule.get("generation", 0),
    }
    core["decisions"].append(decision)
    core["last_seq"] = seq
    core["counts"]["missing_holes"] += 1


def _advance_one(
    core: dict[str, Any],
    record: dict[str, Any],
    instance: dict[str, Any],
    rule: dict[str, Any],
    invalid_info: dict[str, Any] | None,
) -> dict[str, Any]:
    seq = record["seq"]
    while core["last_seq"] + 1 < seq:
        _append_hole(core, core["last_seq"] + 1, rule)

    before_health = core["health_status"]
    before_failures = core["consecutive_failures"]
    isolation_reason = None
    record_lease = record.get("lease_generation", instance["lease_generation"])
    if record_lease < instance["lease_generation"]:
        isolation_reason = "lease_generation_behind"
    elif invalid_info:
        isolation_reason = invalid_info.get("reason", "rule_invalid")

    expected_codes = set(instance["expected_status_codes"])
    if isolation_reason:
        verdict = "quarantined"
        applied = False
        failure_increment = 0
        health_after = before_health
        failures_after = before_failures
        core["counts"]["quarantined"] += 1
    elif record["status_code"] in expected_codes:
        verdict = "success"
        applied = True
        failure_increment = 0
        failures_after = 0
        health_after = "healthy"
        core["counts"]["success"] += 1
    else:
        verdict = "status_mismatch"
        applied = True
        failure_increment = 1
        failures_after = before_failures + 1
        thresholds = rule.get("config", DEFAULT_RULE["config"])
        if failures_after >= thresholds["lost_threshold"]:
            health_after = "lost"
        elif failures_after >= thresholds["suspect_threshold"]:
            health_after = "suspect"
        else:
            health_after = "healthy"
        core["counts"]["status_mismatch"] += 1
        core["counts"]["failure_increments"] += 1

    decision = {
        "seq": seq,
        "kind": "observation",
        "verdict": verdict,
        "applied": applied,
        "real_failure": verdict == "status_mismatch",
        "failure_increment": failure_increment,
        "status_code": record["status_code"],
        "logical_clock_ms": record["logical_clock_ms"],
        "latency_ms": record["latency_ms"],
        "digest": record["digest"],
        "source_shard": record.get("source_shard"),
        "isolation_reason": isolation_reason,
        "health_before": before_health,
        "health_after": health_after,
        "consecutive_failures_before": before_failures,
        "consecutive_failures_after": failures_after,
        "rule_key": rule["rule_key"],
        "rule_generation": rule.get("generation", 0),
    }
    core["decisions"].append(decision)
    core["last_seq"] = seq
    core["consecutive_failures"] = failures_after
    core["health_status"] = health_after
    core["clock_watermark_ms"] = max(core["clock_watermark_ms"], int(record["logical_clock_ms"]))
    core["counts"]["committed_observations"] += 1
    return decision


def advance_instance(
    instance: dict[str, Any],
    records: list[dict[str, Any]],
    view: dict[str, Any] | None,
    *,
    previous_entry: dict[str, Any] | None = None,
    commit_limit_seq: int | None = None,
) -> dict[str, Any]:
    rule, binding = _rule_for_instance(instance["instance_id"], view)
    invalid_map = invalid_rule_closure(view["rules"]) if view else {}
    invalid_info = invalid_map.get(rule["rule_key"]) if rule.get("rule_key") != "__default__" else None

    selected = select_winning_observations(records)
    winners = selected["winners"]
    if commit_limit_seq is not None:
        winners = [record for record in winners if record["seq"] <= commit_limit_seq]

    if previous_entry:
        core = {
            "health_status": previous_entry["health_status"],
            "consecutive_failures": previous_entry["consecutive_failures"],
            "last_seq": previous_entry["committed_through_seq"],
            "clock_watermark_ms": previous_entry.get("instance_clock_watermark_ms", previous_entry.get("clock_watermark_ms", 0)),
            "counts": deepcopy(previous_entry.get("counts", _fresh_core(instance)["counts"])),
            "decisions": deepcopy(previous_entry.get("decisions", [])),
            "version_decisions": deepcopy(previous_entry.get("version_decisions", [])),
        }
        existing_seqs = {item["seq"] for item in core["decisions"]}
        winners = [record for record in winners if record["seq"] > core["last_seq"] and record["seq"] not in existing_seqs]
    else:
        core = _fresh_core(instance)

    for record in winners:
        _advance_one(core, record, instance, rule, invalid_info)

    entry = {
        "instance_id": instance["instance_id"],
        "service_name": instance["service_name"],
        "endpoint": instance["endpoint"],
        "health_status": core["health_status"],
        "consecutive_failures": core["consecutive_failures"],
        "state_generation": instance["state_generation"],
        "lease_generation": instance["lease_generation"],
        "bound_rule_key": rule["rule_key"],
        "rule_generation": rule.get("generation", 0),
        "rule_content_hash": sha256_json(rule),
        "rule_source": rule.get("source", "default"),
        "rule_enabled": rule.get("enabled", True) and invalid_info is None,
        "rule_invalid_reason": invalid_info.get("reason") if invalid_info else None,
        "binding_source": (binding or {}).get("source", rule.get("source", "default")),
        "committed_through_seq": core["last_seq"],
        "clock_watermark_ms": max(core["clock_watermark_ms"], 0),
        "instance_clock_watermark_ms": core["clock_watermark_ms"],
        "counts": core["counts"],
        "decisions": core["decisions"],
        "version_decisions": selected["version_decisions"] + core.get("version_decisions", []),
    }
    if commit_limit_seq is not None:
        entry["commit_limit_seq"] = commit_limit_seq
    return entry


def simulate_registry(
    registry: dict[str, Any],
    records: list[dict[str, Any]],
    view: dict[str, Any] | None,
) -> dict[str, dict[str, Any]]:
    entries: dict[str, dict[str, Any]] = {}
    by_instance: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        by_instance.setdefault(record["instance_id"], []).append(record)
    for instance in registry["instances"]:
        entries[instance["instance_id"]] = advance_instance(instance, by_instance.get(instance["instance_id"], []), view)
    return entries


def canonical_ledger(entries: dict[str, dict[str, Any]], *, registry_hash: str | None, rule_view_hash: str | None, clock_watermark_ms: int) -> dict[str, Any]:
    instances = []
    for instance_id in sorted(entries):
        entry = entries[instance_id]
        operational_keys = {"instance_clock_watermark_ms", "loaded_shards"}
        clean = {key: value for key, value in entry.items() if key not in operational_keys}
        clean_decisions = []
        for decision in clean.get("decisions", []):
            clean_decisions.append({key: value for key, value in decision.items() if key != "source_shard"})
        clean["decisions"] = clean_decisions
        instances.append(clean)
    payload = {
        "schema": "health-ledger/v1",
        "registry_hash": registry_hash,
        "rule_view_hash": rule_view_hash,
        "clock_watermark_ms": clock_watermark_ms,
        "instances": instances,
    }
    payload["ledger_hash"] = sha256_json({key: value for key, value in payload.items() if key != "ledger_hash"})
    return payload


def compare_instance_results(
    reference: dict[str, dict[str, Any]],
    candidate: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    first_instance = None
    first_seq = None
    mismatches: list[dict[str, Any]] = []
    for instance_id in sorted(set(reference) | set(candidate)):
        left = reference.get(instance_id)
        right = candidate.get(instance_id)
        if left is None or right is None:
            first_instance = first_instance or instance_id
            mismatches.append({"instance_id": instance_id, "reason": "missing_side"})
            continue
        for field in ("health_status", "consecutive_failures", "rule_generation", "bound_rule_key"):
            if left.get(field) != right.get(field):
                first_instance = first_instance or instance_id
                mismatches.append({"instance_id": instance_id, "field": field, "expected": left.get(field), "actual": right.get(field)})
        left_decisions = {item["seq"]: item for item in left.get("decisions", [])}
        right_decisions = {item["seq"]: item for item in right.get("decisions", [])}
        for seq in sorted(set(left_decisions) | set(right_decisions)):
            ld = left_decisions.get(seq)
            rd = right_decisions.get(seq)
            if ld is None or rd is None:
                first_instance = first_instance or instance_id
                first_seq = first_seq or seq
                mismatches.append({"instance_id": instance_id, "seq": seq, "field": "decision_presence", "expected": ld is not None, "actual": rd is not None})
                continue
            for field in ("verdict", "health_after", "consecutive_failures_after", "applied", "isolation_reason"):
                if ld.get(field) != rd.get(field):
                    first_instance = first_instance or instance_id
                    first_seq = first_seq or seq
                    mismatches.append({"instance_id": instance_id, "seq": seq, "field": field, "expected": ld.get(field), "actual": rd.get(field)})
                    if first_seq is None:
                        first_seq = seq
                    break
    return {
        "matches": not mismatches,
        "first_mismatch_instance_id": first_instance,
        "first_mismatch_seq": first_seq,
        "mismatches": mismatches[:20],
        "mismatch_count": len(mismatches),
    }


def source_health_diff(
    registry: dict[str, Any],
    records: list[dict[str, Any]],
    v1_view: dict[str, Any] | None,
    v2_view: dict[str, Any] | None,
    merged_view: dict[str, Any],
) -> list[dict[str, Any]]:
    v1_entries = simulate_registry(registry, records, v1_view) if v1_view else {}
    v2_entries = simulate_registry(registry, records, v2_view) if v2_view else {}
    result = []
    for instance in registry["instances"]:
        instance_id = instance["instance_id"]
        left = v1_entries.get(instance_id)
        right = v2_entries.get(instance_id)
        if not left or not right:
            continue
        comparison = compare_instance_results({instance_id: left}, {instance_id: right})
        binding = merged_view.get("instance_bindings", {}).get(instance_id, {})
        result.append(
            {
                "instance_id": instance_id,
                "different": not comparison["matches"],
                "first_different_seq": comparison["first_mismatch_seq"],
                "mismatch_count": comparison["mismatch_count"],
                "v1": _source_summary(left),
                "v2": _source_summary(right),
                "arbitrated_source": binding.get("source", merged_view.get("rules", {}).get(binding.get("rule_key"), {}).get("source")),
                "comparison": comparison,
            }
        )
    return result


def _source_summary(entry: dict[str, Any]) -> dict[str, Any]:
    return {
        "health_status": entry["health_status"],
        "consecutive_failures": entry["consecutive_failures"],
        "bound_rule_key": entry["bound_rule_key"],
        "rule_generation": entry["rule_generation"],
        "rule_source": entry["rule_source"],
        "ledger_entry_hash": sha256_json(entry),
    }
