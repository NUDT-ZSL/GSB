"""Dependency closures, fingerprints, and branch-local invalidation chains."""

from __future__ import annotations

from collections import deque
from typing import Any, Iterable

from .model import ClosureRecord, InvalidationEvent, PolicyCandidate
from .util import canonical_json, sha256_json


def dependency_map(
    candidates: dict[str, PolicyCandidate],
) -> dict[str, tuple[str, ...]]:
    return {
        name: tuple(sorted(candidate.dependencies))
        for name, candidate in candidates.items()
    }


def transitive_closures(
    candidates: dict[str, PolicyCandidate],
) -> dict[str, tuple[str, ...]]:
    graph = dependency_map(candidates)
    memo: dict[str, tuple[str, ...]] = {}

    def closure(name: str) -> tuple[str, ...]:
        if name in memo:
            return memo[name]
        result: set[str] = set()
        for dependency in graph.get(name, ()):
            result.add(dependency)
            result.update(closure(dependency))
        memo[name] = tuple(sorted(result))
        return memo[name]

    return {name: closure(name) for name in sorted(graph)}


def _node_fingerprint_payload(candidate: PolicyCandidate) -> dict[str, Any]:
    return {
        "template_name": candidate.template_name,
        "version": candidate.version,
        "dependencies": list(candidate.dependencies),
        "labels": list(candidate.labels),
        "logical_clock": candidate.logical_clock,
        "policy_generation": candidate.policy_generation,
        "source_format": candidate.source_format,
        "source_version": candidate.source_version,
        "effective_at_ms": candidate.effective_at_ms,
        "tombstone": candidate.tombstone,
        "tombstone_reason": candidate.tombstone_reason,
    }


def node_config_digest(candidate: PolicyCandidate) -> str:
    return sha256_json(_node_fingerprint_payload(candidate))


def closure_digest(name: str, closure: tuple[str, ...]) -> str:
    return sha256_json({"template_name": name, "closure": list(closure)})


def config_fingerprint(
    name: str,
    closure: tuple[str, ...],
    candidates: dict[str, PolicyCandidate],
) -> str:
    involved = sorted(set(closure) | {name})
    return sha256_json(
        {
            "template_name": name,
            "closure": list(closure),
            "configs": [
                _node_fingerprint_payload(candidates[item]) for item in involved
            ],
        }
    )


def closure_dependency_digest(
    closure: tuple[str, ...],
    candidates: dict[str, PolicyCandidate],
) -> str:
    return sha256_json(
        {
            item: {
                "version": candidates[item].version,
                "dependencies": list(candidates[item].dependencies),
            }
            for item in sorted(closure)
        }
    )


def _node_change_reasons(
    previous: PolicyCandidate,
    current: PolicyCandidate,
) -> list[str]:
    reasons: list[str] = []
    if previous.tombstone != current.tombstone:
        reasons.append("tombstone_changed")
    if previous.version != current.version:
        reasons.append("version_changed")
    if previous.labels != current.labels:
        reasons.append("labels_changed")
    if previous.dependencies != current.dependencies:
        reasons.append("dependencies_changed")
    if (
        previous.source_format != current.source_format
        or previous.source_version != current.source_version
        or previous.policy_generation != current.policy_generation
        or previous.logical_clock != current.logical_clock
        or previous.effective_at_ms != current.effective_at_ms
    ):
        reasons.append("source_changed")
    return reasons or ["configuration_changed"]


def _reverse_edges(
    candidates: dict[str, PolicyCandidate],
) -> dict[str, tuple[str, ...]]:
    reverse: dict[str, set[str]] = {name: set() for name in candidates}
    for name, candidate in candidates.items():
        for dependency in candidate.dependencies:
            if dependency in reverse:
                reverse[dependency].add(name)
    return {name: tuple(sorted(values)) for name, values in reverse.items()}


def _paths_between(
    source: str,
    targets: set[str],
    reverse: dict[str, tuple[str, ...]],
) -> list[tuple[str, ...]]:
    paths: set[tuple[str, ...]] = set()
    queue: deque[tuple[str, tuple[str, ...]]] = deque([(source, (source,))])
    while queue:
        current, path = queue.popleft()
        if current != source and current in targets:
            paths.add(path)
            continue
        for dependent in reverse.get(current, ()):
            if dependent in path:
                continue
            queue.append((dependent, path + (dependent,)))
    return sorted(paths)


def build_closures(
    candidates: dict[str, PolicyCandidate],
    *,
    previous_nodes: dict[str, PolicyCandidate] | None = None,
    previous_closures: dict[str, ClosureRecord] | None = None,
) -> tuple[dict[str, ClosureRecord], list[InvalidationEvent]]:
    closures = transitive_closures(candidates)
    if previous_nodes is None or previous_closures is None:
        records: dict[str, ClosureRecord] = {}
        for name in sorted(candidates):
            closure = closures[name]
            records[name] = ClosureRecord(
                template_name=name,
                closure=closure,
                closure_digest=closure_digest(name, closure),
                config_fingerprint=config_fingerprint(name, closure, candidates),
                dependency_digest=closure_dependency_digest(closure, candidates),
                revision=1,
            )
        return records, []

    change_reasons: dict[str, list[str]] = {}
    for name in sorted(set(previous_nodes) | set(candidates)):
        if name not in previous_nodes:
            change_reasons[name] = ["template_added"]
        elif name not in candidates:
            change_reasons[name] = ["template_removed"]
        else:
            reasons = _node_change_reasons(previous_nodes[name], candidates[name])
            if node_config_digest(previous_nodes[name]) != node_config_digest(
                candidates[name]
            ):
                change_reasons[name] = reasons
    reverse = _reverse_edges(candidates)
    affected: set[str] = set(change_reasons)
    for name in change_reasons:
        for dependent in candidates:
            if name in closures[dependent]:
                affected.add(dependent)

    records: dict[str, ClosureRecord] = {}
    for name in sorted(candidates):
        closure = closures[name]
        if name not in affected and name in previous_closures:
            previous_record = previous_closures[name]
            if previous_record.closure != closure:
                raise AssertionError(
                    f"Closure changed outside affected set for {name}"
                )
            records[name] = ClosureRecord(
                template_name=name,
                closure=closure,
                closure_digest=previous_record.closure_digest,
                config_fingerprint=previous_record.config_fingerprint,
                dependency_digest=previous_record.dependency_digest,
                revision=previous_record.revision,
            )
            continue
        fingerprint = config_fingerprint(name, closure, candidates)
        previous_record = previous_closures.get(name)
        revision = 1
        if previous_record is not None:
            revision = previous_record.revision + (
                1 if previous_record.config_fingerprint != fingerprint else 0
            )
        records[name] = ClosureRecord(
            template_name=name,
            closure=closure,
            closure_digest=closure_digest(name, closure),
            config_fingerprint=fingerprint,
            dependency_digest=closure_dependency_digest(closure, candidates),
            revision=revision,
        )

    events: list[InvalidationEvent] = []
    for trigger in sorted(change_reasons):
        if trigger not in candidates:
            continue
        downstream = sorted(
            name
            for name in candidates
            if name != trigger and trigger in closures[name]
        )
        paths = _paths_between(trigger, set(downstream), reverse)
        before: dict[str, Any] = {}
        after: dict[str, Any] = {}
        for name in [trigger] + downstream:
            old_record = previous_closures.get(name)
            current_record = records[name]
            old_candidate = previous_nodes.get(name)
            new_candidate = candidates[name]
            before[name] = {
                "version": None if old_candidate is None else old_candidate.version,
                "closure_revision": (
                    None if old_record is None else old_record.revision
                ),
                "config_fingerprint": (
                    None if old_record is None else old_record.config_fingerprint
                ),
            }
            after[name] = {
                "version": new_candidate.version,
                "closure_revision": current_record.revision,
                "config_fingerprint": current_record.config_fingerprint,
            }
        events.append(
            InvalidationEvent(
                trigger_template=trigger,
                trigger_version=candidates[trigger].version,
                trigger_generation=candidates[trigger].policy_generation,
                trigger_clock=candidates[trigger].logical_clock,
                reason="+".join(change_reasons[trigger]),
                changed_templates=(trigger,),
                affected_templates=tuple(downstream),
                chains=tuple(paths),
                before=before,
                after=after,
            )
        )
    return records, events


def closure_semantic_rows(
    records: dict[str, ClosureRecord],
) -> list[dict[str, Any]]:
    return [
        {
            "template_name": record.template_name,
            "closure": list(record.closure),
            "closure_digest": record.closure_digest,
            "config_fingerprint": record.config_fingerprint,
            "dependency_digest": record.dependency_digest,
            "revision": record.revision,
        }
        for record in sorted(records.values(), key=lambda item: item.template_name)
    ]


def verify_closure_records(
    candidates: dict[str, PolicyCandidate],
    records: dict[str, ClosureRecord],
) -> list[str]:
    errors: list[str] = []
    expected, _ = build_closures(candidates)
    for name in sorted(expected):
        actual = records.get(name)
        if actual is None:
            errors.append(f"missing closure record for {name}")
            continue
        wanted = expected[name]
        if actual.closure != wanted.closure:
            errors.append(f"closure mismatch for {name}")
        if actual.closure_digest != wanted.closure_digest:
            errors.append(f"closure digest mismatch for {name}")
        if actual.config_fingerprint != wanted.config_fingerprint:
            errors.append(f"config fingerprint mismatch for {name}")
        if actual.dependency_digest != wanted.dependency_digest:
            errors.append(f"dependency digest mismatch for {name}")
    for name in sorted(set(records) - set(expected)):
        errors.append(f"unexpected closure record for {name}")
    return errors


def changed_branch_digest(
    records: dict[str, ClosureRecord],
    names: Iterable[str],
) -> str:
    return sha256_json(
        {
            name: {
                "fingerprint": records[name].config_fingerprint,
                "revision": records[name].revision,
            }
            for name in sorted(names)
            if name in records
        }
    )
