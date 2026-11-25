"""Revision-triggered invalidation and incremental refresh verification."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Callable, Iterable

from .adjudicate import AdjudicationPair, AdjudicationReport, adjudicate
from .hashing import digest_json
from .models import Utterance


Interval = tuple[int, int]


@dataclass(frozen=True)
class Trigger:
    kind: str
    interval: Interval
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "interval": list(self.interval), "detail": self.detail}


@dataclass
class RefreshPlan:
    root_node: str
    utterance_id: str
    changed_fields: list[str]
    triggers: list[Trigger]
    invalidated_nodes: list[str]
    reused_nodes: list[str]
    reasons: dict[str, list[str]] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "root_node": self.root_node,
            "utterance_id": self.utterance_id,
            "changed_fields": self.changed_fields,
            "triggers": [trigger.to_dict() for trigger in self.triggers],
            "invalidated_nodes": self.invalidated_nodes,
            "reused_nodes": self.reused_nodes,
            "reasons": self.reasons,
        }


@dataclass
class RefreshRunResult:
    plan: RefreshPlan
    recomputed: list[str]
    reused: list[str]
    results: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan": self.plan.to_dict(),
            "recomputed": self.recomputed,
            "reused": self.reused,
            "result_count": len(self.results),
        }


def versioned_nodes(
    v1: Iterable[Utterance],
    v2: Iterable[Utterance],
) -> list[tuple[str, Utterance]]:
    return [(f"v1:{item.utterance_id}", item) for item in v1] + [
        (f"v2:{item.utterance_id}", item) for item in v2
    ]


def _intervals_overlap(left: Interval, right: Interval) -> bool:
    return max(left[0], right[0]) < min(left[1], right[1])


def _word_triggers(old: Utterance, new: Utterance) -> list[Trigger]:
    triggers: list[Trigger] = []
    max_words = max(len(old.words), len(new.words))
    for index in range(max_words):
        old_word = old.words[index] if index < len(old.words) else None
        new_word = new.words[index] if index < len(new.words) else None
        if old_word == new_word:
            continue
        starts = [word.start_ms for word in (old_word, new_word) if word is not None]
        ends = [word.end_ms for word in (old_word, new_word) if word is not None]
        interval = (min(starts), max(ends))
        timing_changed = (
            old_word is None
            or new_word is None
            or (old_word.start_ms, old_word.end_ms) != (new_word.start_ms, new_word.end_ms)
        )
        triggers.append(
            Trigger(
                "word_time" if timing_changed else "word_text",
                interval,
                {"ordinal": index, "old": old_word.to_dict() if old_word else None, "new": new_word.to_dict() if new_word else None},
            )
        )
    return triggers


def plan_v2_revision(
    old: Utterance,
    new: Utterance,
    nodes: Iterable[tuple[str, Utterance]],
    *,
    root_node: str | None = None,
    dependencies: dict[str, set[str]] | None = None,
) -> RefreshPlan:
    """Compute the narrow overlap/word-trigger/dependency invalidation closure."""

    node_list = list(nodes)
    root_node = root_node or f"v2:{new.utterance_id}"
    changed_fields: list[str] = []
    if old.text != new.text:
        changed_fields.append("text")
    if old.words != new.words:
        changed_fields.append("words")
    if (old.start_ms, old.end_ms) != (new.start_ms, new.end_ms):
        changed_fields.append("interval")
    if old.confidence != new.confidence:
        changed_fields.append("confidence")
    if old.revision != new.revision:
        changed_fields.append("revision")

    root_interval = (
        min(old.start_ms, new.start_ms),
        max(old.end_ms, new.end_ms),
    )
    triggers = [Trigger("revision_span", root_interval, {"changed_fields": changed_fields})]
    triggers.extend(_word_triggers(old, new))

    invalidated: set[str] = {root_node}
    reasons: dict[str, list[str]] = {root_node: ["revised_v2_utterance"]}

    for node_key, utterance in node_list:
        if node_key == root_node:
            continue
        candidate_interval = utterance.interval()
        node_reasons: list[str] = []
        if _intervals_overlap(root_interval, candidate_interval):
            node_reasons.append("interval_overlap_with_revision_span")
        for trigger in triggers:
            if trigger.kind.startswith("word") and _intervals_overlap(trigger.interval, candidate_interval):
                node_reasons.append(f"{trigger.kind}_trigger@{trigger.interval[0]}-{trigger.interval[1]}")
                break
        if node_reasons:
            invalidated.add(node_key)
            reasons[node_key] = node_reasons

    dependency_map = dependencies or {}
    reverse: dict[str, set[str]] = {}
    for node_key, upstream_keys in dependency_map.items():
        for upstream in upstream_keys:
            reverse.setdefault(upstream, set()).add(node_key)

    queue = list(invalidated)
    while queue:
        upstream = queue.pop()
        for downstream in reverse.get(upstream, set()):
            if downstream not in invalidated:
                invalidated.add(downstream)
                reasons[downstream] = [f"dependency_on_invalidated:{upstream}"]
                queue.append(downstream)

    all_keys = [key for key, _ in node_list]
    invalidated_ordered = [key for key in all_keys if key in invalidated]
    known_keys = set(all_keys)
    invalidated_ordered.extend(sorted(key for key in invalidated if key not in known_keys))
    reused = [key for key in all_keys if key not in invalidated]
    return RefreshPlan(
        root_node=root_node,
        utterance_id=new.utterance_id,
        changed_fields=changed_fields,
        triggers=triggers,
        invalidated_nodes=invalidated_ordered,
        reused_nodes=reused,
        reasons=reasons,
    )


class RefreshEngine:
    """Reuse committed unit results and recompute only planned nodes."""

    def __init__(self, committed_results: dict[str, Any] | None = None) -> None:
        self.results: dict[str, Any] = dict(committed_results or {})

    def run(self, plan: RefreshPlan, compute: Callable[[str], Any]) -> RefreshRunResult:
        recomputed: list[str] = []
        for node_key in plan.invalidated_nodes:
            self.results[node_key] = compute(node_key)
            recomputed.append(node_key)
        return RefreshRunResult(
            plan=plan,
            recomputed=recomputed,
            reused=list(plan.reused_nodes),
            results=dict(self.results),
        )


def pair_millisecond_state(report: AdjudicationReport, region: Interval) -> dict[int, str]:
    """Hash all active overlap-pair decisions for every millisecond in region."""

    start_ms, end_ms = region
    state: dict[int, str] = {}
    for ms in range(start_ms, end_ms):
        active = [
            pair
            for pair in report.pairs
            if pair.overlap_start_ms <= ms < pair.overlap_end_ms
        ]
        active.sort(key=lambda pair: pair.pair_key)
        state[ms] = digest_json(
            [
                {
                    "pair_key": pair.pair_key,
                    "conclusion": pair.decision.conclusion,
                    "winner_id": pair.decision.winner_id,
                    "priority": pair.decision.priority_applied,
                    "pair_content_digest": digest_json(pair.to_dict(include_utterances=True)),
                }
                for pair in active
            ]
        )
    return state


def millisecond_states_equal(left: dict[int, str], right: dict[int, str]) -> bool:
    if set(left) != set(right):
        return False
    return all(left[ms] == right[ms] for ms in left)


class AdjudicationRefreshService:
    def __init__(
        self,
        v1: Iterable[Utterance],
        v2: Iterable[Utterance],
        *,
        region: Interval | None = None,
        dependencies: dict[str, set[str]] | None = None,
    ) -> None:
        self.v1 = list(v1)
        self.v2 = list(v2)
        self.dependencies = dependencies or {}
        self.region = region or self._default_region()
        self.full_report = adjudicate(self.v1, self.v2)
        self.committed_pairs: dict[str, AdjudicationPair] = {
            pair.pair_key: pair for pair in self.full_report.pairs
        }
        self.last_plan: RefreshPlan | None = None
        self.last_incremental_report: AdjudicationReport | None = None
        self.last_recomputed_pair_keys: list[str] = []
        self.last_reused_pair_keys: list[str] = []

    def _default_region(self) -> Interval:
        items = self.v1 + self.v2
        if not items:
            return (0, 0)
        return min(item.start_ms for item in items), max(item.end_ms for item in items)

    def revise_v2(
        self,
        utterance_id: str,
        *,
        text: str | None = None,
        words: tuple[Any, ...] | list[Any] | None = None,
        confidence: float | None = None,
        revision: int | None = None,
    ) -> tuple[RefreshPlan, AdjudicationReport, bool]:
        old_index = next(index for index, item in enumerate(self.v2) if item.utterance_id == utterance_id)
        old = self.v2[old_index]
        changes: dict[str, Any] = {}
        if text is not None:
            changes["text"] = text
        if words is not None:
            changes["words"] = tuple(words)
        if confidence is not None:
            changes["confidence"] = confidence
        if revision is not None:
            changes["revision"] = revision
        new = replace(old, **changes)

        nodes = versioned_nodes(self.v1, self.v2)
        plan = plan_v2_revision(
            old,
            new,
            nodes,
            dependencies=self.dependencies,
        )
        invalidated_nodes = set(plan.invalidated_nodes)

        self.v2[old_index] = new
        new_full_report = adjudicate(self.v1, self.v2)
        new_pairs = {pair.pair_key: pair for pair in new_full_report.pairs}

        recomputed_pair_keys: list[str] = []
        reused_pair_keys: list[str] = []
        for pair_key, old_pair in list(self.committed_pairs.items()):
            involved_nodes = {f"v1:{old_pair.v1_id}", f"v2:{old_pair.v2_id}"}
            if involved_nodes & invalidated_nodes:
                if pair_key in new_pairs:
                    self.committed_pairs[pair_key] = new_pairs[pair_key]
                    recomputed_pair_keys.append(pair_key)
                else:
                    del self.committed_pairs[pair_key]
            else:
                reused_pair_keys.append(pair_key)

        for pair_key, pair in new_pairs.items():
            if pair_key not in self.committed_pairs:
                self.committed_pairs[pair_key] = pair
                if pair_key not in recomputed_pair_keys:
                    recomputed_pair_keys.append(pair_key)

        incremental_report = AdjudicationReport(
            pairs=list(self.committed_pairs.values()),
            unmatched_v1=new_full_report.unmatched_v1,
            unmatched_v2=new_full_report.unmatched_v2,
        )
        incremental_state = pair_millisecond_state(incremental_report, self.region)
        full_state = pair_millisecond_state(new_full_report, self.region)
        consistent = millisecond_states_equal(incremental_state, full_state)

        plan.reasons["_pair_refresh"] = [
            f"recomputed_pairs={','.join(recomputed_pair_keys)}",
            f"reused_pairs={','.join(reused_pair_keys)}",
            f"millisecond_consistent={consistent}",
        ]
        self.last_plan = plan
        self.last_incremental_report = incremental_report
        self.last_recomputed_pair_keys = recomputed_pair_keys
        self.last_reused_pair_keys = reused_pair_keys
        return plan, incremental_report, consistent

    def millisecond_states(self) -> tuple[dict[int, str], dict[int, str]]:
        if self.last_incremental_report is None:
            state = pair_millisecond_state(self.full_report, self.region)
            return state, state
        full_report = adjudicate(self.v1, self.v2)
        return (
            pair_millisecond_state(self.last_incremental_report, self.region),
            pair_millisecond_state(full_report, self.region),
        )
