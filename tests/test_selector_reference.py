from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any, Mapping

from build_routing.engine import ReplayEngine
from build_routing.generator import generate_artifacts
from build_routing.model import (
    ALL_FILTERED,
    LOAD_ERROR,
    READY,
    SINGLE_CANDIDATE,
    FilterRule,
    NormalizedProject,
    Policy,
    Prefix,
)
from build_routing.normalize import load_project


def reference_filter_update(policy: Policy, patch: Mapping[str, Any]) -> Policy:
    weights = dict(policy.community_weights)
    weights.update(
        {
            str(key): int(value)
            for key, value in (patch.get("community_weights") or {}).items()
        }
    )
    if "filters" in patch:
        filters = tuple(
            FilterRule(
                id=str(item["id"]),
                match=str(item.get("match", "any")),
                communities=tuple(sorted(item.get("communities", ()))),
                action=str(item.get("action", "deny")),
            )
            for item in patch["filters"]
        )
    else:
        filters = policy.filters
    return Policy(community_weights=weights, filters=filters)


def reference_rule_excludes(rule: FilterRule, communities: tuple[str, ...]) -> bool:
    candidate_set = set(communities)
    if rule.match == "all":
        matched = set(rule.communities) <= candidate_set
    else:
        matched = bool(set(rule.communities) & candidate_set)
    return matched if rule.action == "deny" else not matched


def reference_select(
    prefix: Prefix,
    project: NormalizedProject,
    policy: Policy,
    links: Mapping[str, bool],
) -> dict[str, Any]:
    hop_ids = {item.id for item in project.next_hops}
    eligible = []
    load_errors = 0
    for candidate in prefix.candidates:
        if candidate.next_hop_id not in hop_ids:
            load_errors += 1
            continue
        if not links.get(candidate.next_hop_id, True):
            continue
        if any(
            reference_rule_excludes(rule, candidate.communities)
            for rule in policy.filters
        ):
            continue
        weight = sum(
            policy.community_weights.get(community, 0)
            for community in candidate.communities
        )
        eligible.append(
            (
                len(candidate.as_path),
                -candidate.local_pref,
                -weight,
                candidate.next_hop_id,
                candidate.id,
                candidate,
                weight,
            )
        )
    eligible.sort(key=lambda item: item[:5])
    if not prefix.candidates or load_errors == len(prefix.candidates):
        status = LOAD_ERROR
    elif not eligible:
        status = ALL_FILTERED
    elif len(eligible) == 1:
        status = SINGLE_CANDIDATE
    else:
        status = READY

    def path(item):
        candidate = item[5]
        return {
            "candidate_id": candidate.id,
            "next_hop_id": candidate.next_hop_id,
            "as_path": list(candidate.as_path),
            "local_pref": candidate.local_pref,
            "community_weight": item[6],
        }

    best = path(eligible[0]) if eligible else None
    backup = path(eligible[1]) if len(eligible) > 1 else None
    return {"status": status, "best": best, "backup": backup}


def selected_tuple(path: Mapping[str, Any] | None) -> tuple[Any, ...] | None:
    if path is None:
        return None
    return (
        path["candidate_id"],
        path["next_hop_id"],
        tuple(path["as_path"]),
        path["local_pref"],
        path["community_weight"],
    )


class IndependentSelectorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temp = tempfile.TemporaryDirectory()
        root = Path(cls.temp.name)
        generate_artifacts(root, seed=20261128)
        cls.project = load_project(root / "project_v1.json")
        cls.events = json.loads((root / "events.json").read_text(encoding="utf-8"))

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temp.cleanup()

    def test_every_event_matches_independent_reference_selector(self) -> None:
        engine = ReplayEngine(self.project, self.events)
        links = {item.id: True for item in self.project.next_hops}
        policy = self.project.policy
        reference_switches: dict[str, list[int]] = {
            prefix.id: [] for prefix in self.project.prefixes
        }
        previous: dict[str, dict[str, Any]] = {
            prefix.id: reference_select(prefix, self.project, policy, links)
            for prefix in self.project.prefixes
        }

        for event in self.events:
            if event["type"] == "link_down":
                links[event["next_hop_id"]] = False
            elif event["type"] == "link_up":
                links[event["next_hop_id"]] = True
            elif event["type"] == "policy_change":
                policy = reference_filter_update(policy, event["policy_patch"])
            engine.apply_event(event)

            for prefix in self.project.prefixes:
                expected = reference_select(prefix, self.project, policy, links)
                actual = engine.states[prefix.id].result
                self.assertEqual(expected["status"], actual.status, prefix.id)
                self.assertEqual(
                    selected_tuple(expected["best"]),
                    selected_tuple(actual.best.to_dict() if actual.best else None),
                    prefix.id,
                )
                self.assertEqual(
                    selected_tuple(expected["backup"]),
                    selected_tuple(actual.backup.to_dict() if actual.backup else None),
                    prefix.id,
                )
                expected_fingerprint = (
                    expected["status"],
                    selected_tuple(expected["best"]),
                    selected_tuple(expected["backup"]),
                )
                old = previous[prefix.id]
                old_fingerprint = (
                    old["status"],
                    selected_tuple(old["best"]),
                    selected_tuple(old["backup"]),
                )
                if expected_fingerprint != old_fingerprint:
                    reference_switches[prefix.id].append(event["at"])
                previous[prefix.id] = expected

        for prefix in self.project.prefixes:
            actual_switch_times = [
                item.at for item in engine.switches[prefix.id]
            ]
            self.assertEqual(
                reference_switches[prefix.id],
                actual_switch_times,
                prefix.id,
            )
        self.assertEqual(engine.converged_at, 1200)
        self.assertEqual(engine.last_event_seq, 12)


if __name__ == "__main__":
    unittest.main()
