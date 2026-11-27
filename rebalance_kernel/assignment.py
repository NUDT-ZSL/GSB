"""Deterministic topic-scoped partition assignment."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from .model import NormalizedProject, partition_id, stable_uint


@dataclass(frozen=True)
class AssignmentResult:
    assignments: dict[str, str | None]
    affinity_candidates: dict[str, str | None]
    used_weights: dict[str, float]
    fallback_unweighted: bool = False

    def as_ordered_dict(self) -> dict[str, Any]:
        return {
            "assignments": {
                pid: self.assignments[pid] for pid in sorted(self.assignments)
            },
            "affinity_candidates": {
                pid: self.affinity_candidates[pid]
                for pid in sorted(self.affinity_candidates)
            },
            "used_weights": {
                member_id: self.used_weights[member_id]
                for member_id in sorted(self.used_weights)
            },
            "fallback_unweighted": self.fallback_unweighted,
        }


def _affinity_candidate(
    topic: str,
    partition: int,
    keys: Iterable[str],
    eligible_members: list[str],
) -> str | None:
    if not eligible_members:
        return None
    sorted_keys = sorted(set(keys))
    key_signature = "\x1e".join(sorted_keys)
    if not key_signature:
        key_signature = f"empty:{topic}:{partition}"

    def score(member_id: str) -> tuple[int, str]:
        value = stable_uint(
            f"affinity\x1f{topic}\x1f{partition}\x1f{key_signature}\x1f{member_id}"
        )
        return value, member_id

    return max(eligible_members, key=score)


def compute_assignment(
    project: NormalizedProject,
    alive_members: Iterable[str],
    rates: Mapping[str, float],
    keys_by_partition: Mapping[str, Iterable[str]],
    topics: Iterable[str] | None = None,
) -> AssignmentResult:
    """Assign topic partitions with stable affinity hints and weighted rotation.

    Each topic is an independent coordination domain. This is what makes a
    rate-change rebalance of only the affected topics byte-for-byte equivalent
    to a full rebalance for those same partitions.
    """
    alive = sorted(set(alive_members))
    selected_topics = (
        sorted(project.topics) if topics is None else sorted(set(topics))
    )
    unknown_topics = set(selected_topics) - set(project.topics)
    if unknown_topics:
        raise ValueError(f"unknown topics: {sorted(unknown_topics)}")

    assignments: dict[str, str | None] = {}
    candidates: dict[str, str | None] = {}
    used_weights: dict[str, float] = {}
    fallback_any = False

    for topic in selected_topics:
        eligible = [
            member_id
            for member_id in alive
            if topic in project.members[member_id].subscriptions
        ]
        partitions = list(project.topics[topic].partitions)

        for member_id in eligible:
            used_weights[member_id] = max(0.0, float(rates[member_id]))

        if not eligible:
            for partition in partitions:
                pid = partition_id(topic, partition)
                assignments[pid] = None
                candidates[pid] = None
            continue

        topic_candidates = {
            partition: _affinity_candidate(
                topic,
                partition,
                keys_by_partition.get(partition_id(topic, partition), ()),
                eligible,
            )
            for partition in partitions
        }
        candidates.update(
            {
                partition_id(topic, partition): candidate
                for partition, candidate in topic_candidates.items()
            }
        )

        weights = {
            member_id: max(0.0, float(rates[member_id]))
            for member_id in eligible
        }
        total_weight = sum(weights.values())
        fallback = total_weight <= 0
        if fallback:
            fallback_any = True
            weights = {member_id: 1.0 for member_id in eligible}
            total_weight = float(len(eligible))

        payload = {
            member_id: weights[member_id] + 1e-12
            for member_id in eligible
        }
        current = {member_id: 0.0 for member_id in eligible}
        ordered_partitions = sorted(
            partitions,
            key=lambda partition: (
                stable_uint(f"partition-order\x1f{topic}\x1f{partition}"),
                partition,
            ),
        )
        for partition in ordered_partitions:
            for member_id in eligible:
                current[member_id] += payload[member_id]
            chosen = min(
                eligible,
                key=lambda member_id: (
                    -current[member_id],
                    0
                    if member_id == topic_candidates[partition]
                    else 1,
                    member_id,
                ),
            )
            assignments[partition_id(topic, partition)] = chosen
            current[chosen] -= total_weight

    return AssignmentResult(
        assignments={pid: assignments[pid] for pid in sorted(assignments)},
        affinity_candidates={
            pid: candidates[pid] for pid in sorted(candidates)
        },
        used_weights={
            member_id: used_weights[member_id]
            for member_id in sorted(used_weights)
        },
        fallback_unweighted=fallback_any,
    )


def assignment_counts(
    assignments: Mapping[str, str | None],
    member_ids: Iterable[str],
) -> dict[str, int]:
    counts = {member_id: 0 for member_id in sorted(set(member_ids))}
    for owner in assignments.values():
        if owner is not None:
            counts.setdefault(owner, 0)
            counts[owner] += 1
    return {member_id: counts[member_id] for member_id in sorted(counts)}


def partition_change_report(
    before: Mapping[str, str | None],
    after: Mapping[str, str | None],
    member_ids: Iterable[str],
) -> dict[str, object]:
    pids = sorted(set(before) | set(after))
    before_by_member = {
        member_id: sorted(pid for pid in pids if before.get(pid) == member_id)
        for member_id in sorted(set(member_ids))
    }
    after_by_member = {
        member_id: sorted(pid for pid in pids if after.get(pid) == member_id)
        for member_id in sorted(set(member_ids))
    }
    changes = []
    for member_id in sorted(set(member_ids)):
        old = set(before_by_member[member_id])
        new = set(after_by_member[member_id])
        changes.append(
            {
                "member_id": member_id,
                "before": sorted(old),
                "after": sorted(new),
                "gained": sorted(new - old),
                "lost": sorted(old - new),
                "retained": sorted(old & new),
            }
        )
    transfers = [
        {
            "partition": pid,
            "from_member_id": before.get(pid),
            "to_member_id": after.get(pid),
        }
        for pid in pids
        if before.get(pid) != after.get(pid)
    ]
    return {
        "assignment_changed": any(
            before.get(pid) != after.get(pid) for pid in pids
        ),
        "by_member": changes,
        "transfers": transfers,
    }
