from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass, replace
from typing import Any, Mapping, Sequence

from .model import (
    ALL_FILTERED,
    LOAD_ERROR,
    READY,
    SINGLE_CANDIDATE,
    FilterRule,
    NormalizedProject,
    Policy,
    Prefix,
    ReferenceIssue,
    RouteCandidate,
    SelectionResult,
)
from .selector import select_prefix, community_weight
from .stats import build_availability_stats

EVENT_TYPES = {"link_down", "link_up", "policy_change"}


class EventValidationError(ValueError):
    def __init__(self, issues: Sequence[Mapping[str, Any]]):
        self.issues = [dict(item) for item in issues]
        super().__init__(f"invalid event stream: {len(self.issues)} issue(s)")


@dataclass
class PrefixState:
    result: SelectionResult
    converged_at: int = 0
    last_change_at: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "result": self.result.to_dict(),
            "converged_at": self.converged_at,
            "last_change_at": self.last_change_at,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "PrefixState":
        return cls(
            result=SelectionResult.from_dict(raw["result"]),
            converged_at=int(raw.get("converged_at", 0)),
            last_change_at=int(raw.get("last_change_at", 0)),
        )


@dataclass
class SwitchRecord:
    prefix_id: str
    event_seq: int
    at: int
    event_type: str
    old_best: Mapping[str, Any] | None
    new_best: Mapping[str, Any] | None
    old_backup: Mapping[str, Any] | None
    new_backup: Mapping[str, Any] | None
    change_kind: str
    details: Mapping[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "prefix_id": self.prefix_id,
            "event_seq": self.event_seq,
            "at": self.at,
            "event_type": self.event_type,
            "old_best": copy.deepcopy(self.old_best),
            "new_best": copy.deepcopy(self.new_best),
            "old_backup": copy.deepcopy(self.old_backup),
            "new_backup": copy.deepcopy(self.new_backup),
            "change_kind": self.change_kind,
            "details": copy.deepcopy(self.details),
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "SwitchRecord":
        return cls(
            prefix_id=str(raw["prefix_id"]),
            event_seq=int(raw["event_seq"]),
            at=int(raw["at"]),
            event_type=str(raw["event_type"]),
            old_best=raw.get("old_best"),
            new_best=raw.get("new_best"),
            old_backup=raw.get("old_backup"),
            new_backup=raw.get("new_backup"),
            change_kind=str(raw["change_kind"]),
            details=dict(raw.get("details", {})),
        )


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def project_hash(project: NormalizedProject) -> str:
    return hashlib.sha256(canonical_json(project.to_dict()).encode("utf-8")).hexdigest()


def _filter_ids_for_candidate(
    candidate: RouteCandidate, policy: Policy
) -> tuple[str, ...]:
    result: list[str] = []
    for rule in policy.filters:
        matched = rule.matched_communities(candidate.communities)
        excluded = (
            (rule.action == "deny" and bool(matched))
            or (rule.action == "allow" and not matched)
        )
        if excluded:
            result.append(rule.id)
    return tuple(result)


def derived_affected_prefixes(
    project: NormalizedProject,
    old_policy: Policy,
    new_policy: Policy,
) -> set[str]:
    """Find prefixes whose filter outcome or ranking key can change.

    Candidate iteration is cheap. Route scoring is performed only for the
    prefixes returned by this function.
    """

    affected: set[str] = set()
    for prefix in project.prefixes:
        for candidate in prefix.candidates:
            old_filters = _filter_ids_for_candidate(candidate, old_policy)
            new_filters = _filter_ids_for_candidate(candidate, new_policy)
            if old_filters != new_filters:
                affected.add(prefix.id)
                break
            old_weight = sum(
                old_policy.community_weights.get(item, 0)
                for item in candidate.communities
            )
            new_weight = sum(
                new_policy.community_weights.get(item, 0)
                for item in candidate.communities
            )
            if old_weight != new_weight:
                affected.add(prefix.id)
                break
    return affected


def _event_body(event: Mapping[str, Any]) -> dict[str, Any]:
    body = dict(event)
    payload = body.pop("payload", None)
    if isinstance(payload, Mapping):
        merged = dict(payload)
        merged.update(body)
        return merged
    return body


def validate_events(
    project: NormalizedProject, events: Sequence[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    next_hop_ids = {item.id for item in project.next_hops}
    seen_seq: set[int] = set()
    seen_time_seq: set[tuple[int, int]] = set()
    last_time = -1
    for index, raw in enumerate(events):
        event = _event_body(raw)
        seq = event.get("seq")
        at = event.get("at")
        event_type = event.get("type")
        if not isinstance(seq, int) or seq <= 0:
            issues.append(
                {
                    "code": "E_EVENT",
                    "index": index,
                    "field": "seq",
                    "message": "seq must be a positive integer",
                }
            )
            continue
        if seq in seen_seq:
            issues.append(
                {
                    "code": "E_EVENT",
                    "index": index,
                    "field": "seq",
                    "message": f"duplicate event seq {seq}",
                }
            )
        seen_seq.add(seq)
        if not isinstance(at, int) or at < 0:
            issues.append(
                {
                    "code": "E_EVENT",
                    "index": index,
                    "field": "at",
                    "message": "at must be a non-negative integer",
                }
            )
            continue
        key = (at, seq)
        if key in seen_time_seq:
            issues.append(
                {
                    "code": "E_EVENT",
                    "index": index,
                    "field": "at",
                    "message": "duplicate (at, seq) pair",
                }
            )
        seen_time_seq.add(key)
        if at < last_time:
            issues.append(
                {
                    "code": "E_EVENT",
                    "index": index,
                    "field": "at",
                    "message": "events must be ordered by logical time",
                }
            )
        last_time = max(last_time, at)
        if event_type not in EVENT_TYPES:
            issues.append(
                {
                    "code": "E_EVENT",
                    "index": index,
                    "field": "type",
                    "message": f"unsupported event type {event_type!r}",
                }
            )
        if event_type in {"link_down", "link_up"}:
            hop = event.get("next_hop_id")
            if hop not in next_hop_ids:
                issues.append(
                    ReferenceIssue(
                        code="E_REFERENCE",
                        entity="event",
                        entity_id=str(seq),
                        field="next_hop_id",
                        reference=str(hop),
                        message=f"event references missing next hop {hop}",
                        source_schema=project.source_schema,
                    ).to_dict()
                )
        if event_type == "policy_change":
            patch = event.get("policy_patch")
            if not isinstance(patch, Mapping):
                issues.append(
                    {
                        "code": "E_EVENT",
                        "index": index,
                        "field": "policy_patch",
                        "message": "policy_change requires a policy_patch object",
                    }
                )
                continue
            try:
                policy_patch(policy=project.policy, patch=patch)
            except (KeyError, TypeError, ValueError) as exc:
                issues.append(
                    {
                        "code": "E_EVENT",
                        "index": index,
                        "field": "policy_patch",
                        "message": f"invalid policy patch: {exc}",
                    }
                )
    return issues


def policy_patch(policy: Policy, patch: Mapping[str, Any]) -> Policy:
    weights = dict(policy.community_weights)
    if "community_weights" in patch:
        weights.update(
            {
                str(key): int(value)
                for key, value in (patch.get("community_weights") or {}).items()
            }
        )
    if "replace_community_weights" in patch:
        weights = {
            str(key): int(value)
            for key, value in (patch.get("replace_community_weights") or {}).items()
        }

    rules = list(policy.filters)
    if "filters" in patch:
        rules = []
        for item in patch.get("filters") or ():
            action = str(item.get("action", "deny"))
            if action not in {"deny", "allow"}:
                raise ValueError(f"unsupported filter action {action!r}")
            rules.append(
                FilterRule(
                    id=str(item["id"]),
                    match=str(item.get("match", "any")),
                    communities=tuple(
                        sorted(
                            str(value)
                            for value in item.get("communities", ())
                        )
                    ),
                    action=action,
                )
            )
    remove_ids = {str(value) for value in patch.get("remove_filter_ids", ())}
    if remove_ids:
        rules = [rule for rule in rules if rule.id not in remove_ids]
    for item in patch.get("add_filters", ()):
        action = str(item.get("action", "deny"))
        if action not in {"deny", "allow"}:
            raise ValueError(f"unsupported filter action {action!r}")
        rules.append(
            FilterRule(
                id=str(item["id"]),
                match=str(item.get("match", "any")),
                communities=tuple(
                    sorted(
                        str(value)
                        for value in item.get("communities", ())
                    )
                ),
                action=action,
            )
        )
    return Policy(community_weights=weights, filters=tuple(rules))


class ReplayEngine:
    def __init__(
        self,
        project: NormalizedProject,
        events: Sequence[Mapping[str, Any]] = (),
        *,
        initial_time: int = 0,
        validate: bool = True,
    ):
        self.project = project
        self.events = [dict(_event_body(item)) for item in events]
        if validate:
            issues = validate_events(project, self.events)
            if issues:
                raise EventValidationError(issues)
        self.initial_time = initial_time
        self.policy = project.policy
        self.link_status = {item.id: True for item in project.next_hops}
        self.states: dict[str, PrefixState] = {}
        self.switches: dict[str, list[SwitchRecord]] = {
            item.id: [] for item in project.prefixes
        }
        self.recompute_counts: dict[str, int] = {}
        self.last_event_seq = 0
        self.converged_at = initial_time
        self._initialize()

    def _project_with_current_policy(self) -> NormalizedProject:
        if self.policy is self.project.policy:
            return self.project
        return replace(self.project, policy=self.policy)

    def _select(self, prefix: Prefix) -> SelectionResult:
        return select_prefix(
            prefix,
            self._project_with_current_policy(),
            self.link_status,
            reference_issues=self.project.reference_issues,
        )

    def _initialize(self) -> None:
        for prefix in self.project.prefixes:
            result = self._select(prefix)
            self.states[prefix.id] = PrefixState(
                result=result,
                converged_at=self.initial_time,
                last_change_at=self.initial_time,
            )
            self.recompute_counts[prefix.id] = 1

    def _link_affected(self, next_hop_id: str) -> list[str]:
        return [
            prefix.id
            for prefix in self.project.prefixes
            if any(
                candidate.next_hop_id == next_hop_id
                for candidate in prefix.candidates
            )
        ]

    def apply_event(self, event: Mapping[str, Any]) -> dict[str, Any]:
        body = _event_body(event)
        issues = validate_events(self.project, [body])
        if issues:
            raise EventValidationError(issues)
        # Reordering is disallowed by the public runner; apply_event also enforces
        # it so direct callers cannot silently replay an older event.
        if int(body["seq"]) <= self.last_event_seq:
            raise ValueError(
                f"event seq {body['seq']} is not newer than committed seq "
                f"{self.last_event_seq}"
            )

        event_type = str(body["type"])
        at = int(body["at"])
        affected: list[str]
        old_results: dict[str, SelectionResult] = {}

        if event_type in {"link_down", "link_up"}:
            next_hop_id = str(body["next_hop_id"])
            affected = self._link_affected(next_hop_id)
            self.link_status[next_hop_id] = event_type == "link_up"
        else:
            new_policy = policy_patch(self.policy, body["policy_patch"])
            affected_set = derived_affected_prefixes(
                self.project, self.policy, new_policy
            )
            affected = [
                prefix.id
                for prefix in self.project.prefixes
                if prefix.id in affected_set
            ]
            self.policy = new_policy

        for prefix_id in affected:
            old_results[prefix_id] = self.states[prefix_id].result

        changes: list[dict[str, Any]] = []
        for prefix_id in affected:
            prefix = self.project.prefix_by_id()[prefix_id]
            old_state = self.states[prefix_id]
            new_result = self._select(prefix)
            self.recompute_counts[prefix_id] += 1
            changed = old_state.result.selected_paths_changed(new_result)
            change_kind = self._change_kind(old_state.result, new_result)
            new_state = PrefixState(
                result=new_result,
                converged_at=at,
                last_change_at=at if changed else old_state.last_change_at,
            )
            self.states[prefix_id] = new_state

            if changed:
                record = SwitchRecord(
                    prefix_id=prefix_id,
                    event_seq=int(body["seq"]),
                    at=at,
                    event_type=event_type,
                    old_best=(
                        old_state.result.best.to_dict()
                        if old_state.result.best
                        else None
                    ),
                    new_best=new_result.best.to_dict() if new_result.best else None,
                    old_backup=(
                        old_state.result.backup.to_dict()
                        if old_state.result.backup
                        else None
                    ),
                    new_backup=(
                        new_result.backup.to_dict() if new_result.backup else None
                    ),
                    change_kind=change_kind,
                    details={
                        "event": copy.deepcopy(body),
                        "old_status": old_state.result.status,
                        "new_status": new_result.status,
                    },
                )
                self.switches[prefix_id].append(record)
                changes.append(
                    {
                        "prefix_id": prefix_id,
                        "change_kind": change_kind,
                        "old_best_next_hop_id": (
                            old_state.result.best.next_hop_id
                            if old_state.result.best
                            else None
                        ),
                        "new_best_next_hop_id": (
                            new_result.best.next_hop_id
                            if new_result.best
                            else None
                        ),
                        "old_backup_next_hop_id": (
                            old_state.result.backup.next_hop_id
                            if old_state.result.backup
                            else None
                        ),
                        "new_backup_next_hop_id": (
                            new_result.backup.next_hop_id
                            if new_result.backup
                            else None
                        ),
                    }
                )

        self.last_event_seq = int(body["seq"])
        if affected:
            self.converged_at = max(self.converged_at, at)
        return {
            "event": copy.deepcopy(body),
            "affected_prefix_ids": affected,
            "unaffected_prefix_ids": [
                prefix.id
                for prefix in self.project.prefixes
                if prefix.id not in set(affected)
            ],
            "changes": changes,
            "recomputed_count": len(affected),
            "change_count": len(changes),
            "converged_at": at,
            "all_prefixes_stable": True,
        }

    @staticmethod
    def _change_kind(old: SelectionResult, new: SelectionResult) -> str:
        if old.selected_fingerprint() == new.selected_fingerprint():
            return "none"
        old_best = old.best.next_hop_id if old.best else None
        new_best = new.best.next_hop_id if new.best else None
        old_backup = old.backup.next_hop_id if old.backup else None
        new_backup = new.backup.next_hop_id if new.backup else None
        kinds: list[str] = []
        if old_best != new_best:
            kinds.append("best")
        if old_backup != new_backup:
            kinds.append("backup")
        if not kinds:
            kinds.append("criteria")
        return "+".join(kinds)

    def run(self, *, max_events: int | None = None) -> dict[str, Any]:
        pending = [event for event in self.events if event["seq"] > self.last_event_seq]
        if max_events is not None:
            pending = pending[:max_events]
        reports = [self.apply_event(event) for event in pending]
        return {
            "event_reports": reports,
            "processed_event_count": len(reports),
            "last_event_seq": self.last_event_seq,
            "converged_at": self.converged_at,
        }

    def current_report(self) -> dict[str, Any]:
        return {
            "project_id": self.project.project_id,
            "project_hash": project_hash(self.project),
            "schema_version": self.project.schema_version,
            "source_schema": self.project.source_schema,
            "last_event_seq": self.last_event_seq,
            "converged_at": self.converged_at,
            "prefixes": {
                prefix.id: {
                    "node_id": prefix.build_node_id,
                    "state": self.states[prefix.id].to_dict(),
                    "switch_count": len(self.switches[prefix.id]),
                    "switch_sequence": [
                        item.to_dict() for item in self.switches[prefix.id]
                    ],
                }
                for prefix in self.project.prefixes
            },
            "stats": self.stats(),
        }

    def stats(self) -> dict[str, Any]:
        return build_availability_stats(
            self.project,
            {prefix_id: state.result for prefix_id, state in self.states.items()},
        )

    def switch_sequences(self) -> dict[str, list[dict[str, Any]]]:
        return {
            prefix.id: [item.to_dict() for item in self.switches[prefix.id]]
            for prefix in self.project.prefixes
        }

    def runtime_state(self) -> dict[str, Any]:
        return {
            "project_id": self.project.project_id,
            "project_hash": project_hash(self.project),
            "last_event_seq": self.last_event_seq,
            "converged_at": self.converged_at,
            "policy": self.policy.to_dict(),
            "link_status": dict(sorted(self.link_status.items())),
            "prefix_states": {
                prefix.id: self.states[prefix.id].to_dict()
                for prefix in self.project.prefixes
            },
            "switch_sequences": self.switch_sequences(),
            "recompute_counts": dict(sorted(self.recompute_counts.items())),
        }

    def restore_runtime(self, runtime: Mapping[str, Any]) -> None:
        if str(runtime.get("project_id")) != self.project.project_id:
            raise ValueError("runtime project id does not match loaded project")
        if str(runtime.get("project_hash")) != project_hash(self.project):
            raise ValueError("runtime project hash does not match loaded project")
        prefix_ids = {item.id for item in self.project.prefixes}
        raw_states = runtime.get("prefix_states") or {}
        if set(raw_states) != prefix_ids:
            raise ValueError("runtime prefix state set does not match project")
        self.policy = Policy.from_dict(runtime["policy"])
        self.link_status = {
            str(key): bool(value)
            for key, value in (runtime.get("link_status") or {}).items()
        }
        missing_hops = set(self.link_status) ^ {
            item.id for item in self.project.next_hops
        }
        if missing_hops:
            raise ValueError(
                f"runtime link status set does not match next hops: "
                f"{sorted(missing_hops)}"
            )
        self.states = {
            prefix_id: PrefixState.from_dict(raw)
            for prefix_id, raw in raw_states.items()
        }
        self.switches = {
            prefix_id: [
                SwitchRecord.from_dict(item)
                for item in (runtime.get("switch_sequences") or {}).get(
                    prefix_id, []
                )
            ]
            for prefix_id in prefix_ids
        }
        self.recompute_counts = {
            str(key): int(value)
            for key, value in (runtime.get("recompute_counts") or {}).items()
        }
        for prefix_id in prefix_ids:
            self.recompute_counts.setdefault(prefix_id, 1)
        self.last_event_seq = int(runtime.get("last_event_seq", 0))
        self.converged_at = int(runtime.get("converged_at", self.initial_time))

    def full_recompute_snapshot(self) -> dict[str, SelectionResult]:
        return {
            prefix.id: self._select(prefix) for prefix in self.project.prefixes
        }

    def recompute_incremental_policy(
        self,
        patch: Mapping[str, Any],
        *,
        at: int | None = None,
        record_switches: bool = True,
        verify_full: bool = True,
    ) -> dict[str, Any]:
        """Apply a local policy change and recompute only affected prefixes.

        The returned report names both the individually recomputed and untouched
        prefixes, then optionally verifies every incremental result against a
        full selection pass.
        """

        at_value = self.converged_at if at is None else int(at)
        old_policy = self.policy
        new_policy = policy_patch(old_policy, patch)
        affected_set = derived_affected_prefixes(
            self.project, old_policy, new_policy
        )
        affected = [
            prefix.id
            for prefix in self.project.prefixes
            if prefix.id in affected_set
        ]
        unaffected = [
            prefix.id
            for prefix in self.project.prefixes
            if prefix.id not in affected_set
        ]
        unchanged_state_before = {
            prefix_id: self.states[prefix_id].to_dict()
            for prefix_id in unaffected
        }
        unchanged_switches_before = {
            prefix_id: [item.to_dict() for item in self.switches[prefix_id]]
            for prefix_id in unaffected
        }

        self.policy = new_policy
        changed: list[dict[str, Any]] = []
        for prefix_id in affected:
            prefix = self.project.prefix_by_id()[prefix_id]
            old_state = self.states[prefix_id]
            new_result = self._select(prefix)
            self.recompute_counts[prefix_id] += 1
            changed_flag = old_state.result.selected_paths_changed(new_result)
            self.states[prefix_id] = PrefixState(
                result=new_result,
                converged_at=at_value,
                last_change_at=at_value if changed_flag else old_state.last_change_at,
            )
            if changed_flag:
                kind = self._change_kind(old_state.result, new_result)
                record = SwitchRecord(
                    prefix_id=prefix_id,
                    event_seq=self.last_event_seq,
                    at=at_value,
                    event_type="local_policy_change",
                    old_best=(
                        old_state.result.best.to_dict()
                        if old_state.result.best
                        else None
                    ),
                    new_best=new_result.best.to_dict() if new_result.best else None,
                    old_backup=(
                        old_state.result.backup.to_dict()
                        if old_state.result.backup
                        else None
                    ),
                    new_backup=(
                        new_result.backup.to_dict() if new_result.backup else None
                    ),
                    change_kind=kind,
                    details={"policy_patch": copy.deepcopy(dict(patch))},
                )
                if record_switches:
                    self.switches[prefix_id].append(record)
                changed.append(record.to_dict())

        full_results = self.full_recompute_snapshot() if verify_full else {}
        mismatches: list[str] = []
        if verify_full:
            for prefix_id, full_result in full_results.items():
                if (
                    self.states[prefix_id].result.selected_fingerprint()
                    != full_result.selected_fingerprint()
                ):
                    mismatches.append(prefix_id)

        if any(
            self.states[prefix_id].to_dict() != unchanged_state_before[prefix_id]
            for prefix_id in unaffected
        ):
            raise AssertionError("an unaffected prefix state changed")
        if any(
            [item.to_dict() for item in self.switches[prefix_id]]
            != unchanged_switches_before[prefix_id]
            for prefix_id in unaffected
        ):
            raise AssertionError("an unaffected switch sequence changed")
        if mismatches:
            raise AssertionError(
                "incremental selection differs from full recompute for "
                + ", ".join(sorted(mismatches))
            )
        if affected:
            self.converged_at = max(self.converged_at, at_value)

        return {
            "at": at_value,
            "recomputed_prefix_ids": affected,
            "unchanged_prefix_ids": unaffected,
            "recomputed_count": len(affected),
            "unchanged_count": len(unaffected),
            "changes": changed,
            "full_recompute_match": not mismatches,
            "unaffected_preserved": True,
        }
