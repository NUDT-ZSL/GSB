"""Cross-version merge with complete provenance and conflict reporting."""

from __future__ import annotations

import copy
from typing import Any

from .canonical import canonical_json


def _candidate_summary(entry: dict[str, Any]) -> dict[str, Any]:
    return {
        "candidate_id": entry["candidate_id"],
        "source_version": entry["source_version"],
        "source_priority": entry["source_priority"],
        "source_file": entry["source_file"],
        "effective_at_ms": entry["effective_at_ms"],
        "generation": entry["generation"],
        "tombstone": entry["tombstone"],
        "params": copy.deepcopy(entry["params"]),
        "upstreams": list(entry["upstreams"]),
        "original_time": copy.deepcopy(entry["original_time"]),
        "compatibility_defaults": list(entry.get("compatibility_defaults", [])),
    }


def _decision_basis(winner: dict[str, Any], candidates: list[dict[str, Any]]) -> str:
    if len(candidates) == 1:
        return "single_candidate"
    runner_up = candidates[1]
    if winner["effective_at_ms"] != runner_up["effective_at_ms"]:
        return "higher_effective_at_ms"
    if winner["source_priority"] != runner_up["source_priority"]:
        return "higher_source_priority"
    if winner["generation"] != runner_up["generation"]:
        return "higher_generation"
    return "candidate_id_tiebreak"


def merge_versions(
    migrated: dict[str, Any],
    *,
    environment_order: list[str],
) -> dict[str, Any]:
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for entry in migrated["entries"]:
        groups.setdefault((entry["environment"], entry["service"]), []).append(entry)

    env_rank = {name: index for index, name in enumerate(environment_order)}
    services: dict[str, dict[str, dict[str, Any]]] = {
        environment: {} for environment in environment_order
    }
    conflicts: list[dict[str, Any]] = []
    decisions: list[dict[str, Any]] = []

    for (environment, service) in sorted(
        groups,
        key=lambda item: (env_rank.get(item[0], len(env_rank)), item[0], item[1]),
    ):
        candidates = sorted(
            groups[(environment, service)],
            key=lambda item: (
                -item["effective_at_ms"],
                -item["source_priority"],
                -item["generation"],
                item["candidate_id"],
            ),
        )
        winner = candidates[0]
        basis = _decision_basis(winner, candidates)
        winner_defaults = set(winner.get("compatibility_defaults", []))
        parameter_keys = sorted(
            {key for candidate in candidates for key in candidate["params"]}
        )
        params: dict[str, Any] = {}
        if not winner["tombstone"]:
            for parameter in parameter_keys:
                value = copy.deepcopy(winner["params"].get(parameter))
                params[parameter] = {
                    "value": value,
                    "source_version": winner["source_version"],
                    "source_file": winner["source_file"],
                    "source_candidate_id": winner["candidate_id"],
                    "source_effective_at_ms": winner["effective_at_ms"],
                    "source_generation": winner["generation"],
                    "original_time": copy.deepcopy(winner["original_time"]),
                    "decision_basis": basis,
                    "compatibility_default": parameter in winner_defaults,
                }
                candidate_values = [
                    {
                        "source_version": candidate["source_version"],
                        "source_file": candidate["source_file"],
                        "effective_at_ms": candidate["effective_at_ms"],
                        "generation": candidate["generation"],
                        "value": copy.deepcopy(candidate["params"].get(parameter)),
                    }
                    for candidate in candidates
                    if not candidate["tombstone"]
                ]
                distinct = {
                    canonical_json(item["value"]) for item in candidate_values
                }
                if len(distinct) > 1:
                    conflicts.append(
                        {
                            "environment": environment,
                            "service": service,
                            "parameter": parameter,
                            "kind": "parameter_value",
                            "selected": {
                                "source_version": winner["source_version"],
                                "source_file": winner["source_file"],
                                "candidate_id": winner["candidate_id"],
                                "value": value,
                            },
                            "discarded": [
                                item
                                for item in candidate_values
                                if item["source_version"] != winner["source_version"]
                                or item["source_file"] != winner["source_file"]
                                or canonical_json(item["value"])
                                != canonical_json(value)
                            ],
                            "decision_basis": basis,
                        }
                    )

        tombstone_states = {candidate["tombstone"] for candidate in candidates}
        if len(tombstone_states) > 1:
            conflicts.append(
                {
                    "environment": environment,
                    "service": service,
                    "parameter": "__service_state__",
                    "kind": "tombstone_vs_live",
                    "selected": {
                        "source_version": winner["source_version"],
                        "source_file": winner["source_file"],
                        "candidate_id": winner["candidate_id"],
                        "tombstone": winner["tombstone"],
                    },
                    "discarded": [
                        {
                            "source_version": candidate["source_version"],
                            "source_file": candidate["source_file"],
                            "candidate_id": candidate["candidate_id"],
                            "tombstone": candidate["tombstone"],
                        }
                        for candidate in candidates
                        if candidate["tombstone"] != winner["tombstone"]
                    ],
                    "decision_basis": basis,
                }
            )

        if not winner["tombstone"]:
            candidate_upstreams = {
                canonical_json(candidate["upstreams"])
                for candidate in candidates
                if not candidate["tombstone"]
            }
            if len(candidate_upstreams) > 1:
                conflicts.append(
                    {
                        "environment": environment,
                        "service": service,
                        "parameter": "__upstreams__",
                        "kind": "dependency_set",
                        "selected": {
                            "source_version": winner["source_version"],
                            "source_file": winner["source_file"],
                            "candidate_id": winner["candidate_id"],
                            "value": winner["upstreams"],
                        },
                        "discarded": [
                            {
                                "source_version": candidate["source_version"],
                                "source_file": candidate["source_file"],
                                "candidate_id": candidate["candidate_id"],
                                "value": candidate["upstreams"],
                            }
                            for candidate in candidates
                            if not candidate["tombstone"]
                            and canonical_json(candidate["upstreams"])
                            != canonical_json(winner["upstreams"])
                        ],
                        "decision_basis": basis,
                    }
                )

        expires_at_ms = winner["effective_at_ms"] + winner["ttl_ms"]
        services[environment][service] = {
            "environment": environment,
            "service": service,
            "tombstoned": winner["tombstone"],
            "expired_at_clock": (
                migrated["clock_ms"] > expires_at_ms
                if not winner["tombstone"]
                else False
            ),
            "expires_at_ms": expires_at_ms,
            "effective_at_ms": winner["effective_at_ms"],
            "generation": winner["generation"],
            "selected_source": {
                "source_version": winner["source_version"],
                "source_file": winner["source_file"],
                "candidate_id": winner["candidate_id"],
                "original_time": copy.deepcopy(winner["original_time"]),
            },
            "decision": {
                "basis": basis,
                "candidates": [_candidate_summary(item) for item in candidates],
            },
            "params": params,
            "upstreams": list(winner["upstreams"]),
        }
        decisions.append(
            {
                "environment": environment,
                "service": service,
                "basis": basis,
                "winner_candidate_id": winner["candidate_id"],
                "winner_source_version": winner["source_version"],
                "candidates": [_candidate_summary(item) for item in candidates],
            }
        )

    return {
        "clock_ms": migrated["clock_ms"],
        "descriptor": migrated["descriptor"],
        "v1": migrated["v1"],
        "v2": migrated["v2"],
        "environments": services,
        "conflicts": conflicts,
        "merge_decisions": decisions,
        "migrations": migrated.get("migrations", []),
        "compatibility_degradations": migrated.get(
            "compatibility_degradations", []
        ),
        "source_fingerprint": migrated["source_fingerprint"],
    }
