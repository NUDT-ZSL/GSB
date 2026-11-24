"""Unified rule view, authority arbitration and version binding."""

from __future__ import annotations

import bisect
from dataclasses import dataclass
from typing import Iterable

from .codes import Code
from .models import PatchBatch, RulePatch, RuleVersion, VersionResolution
from .util import digest_json, stable_json


_SOURCE_RANK = {"v1": 0, "v2": 1, "patch": 2}


def rule_version_as_dict(version: RuleVersion) -> dict:
    return {
        "rule_key": version.rule_key,
        "source_rule_key": version.source_rule_key,
        "source": version.source,
        "effective_ms": version.effective_ms,
        "effective_error_ms": version.effective_error_ms,
        "generation": version.generation,
        "dedup_window_ms": version.dedup_window_ms,
        "window_error_ms": version.window_error_ms,
        "match_strings": list(version.match_strings),
        "enabled": version.enabled,
        "tombstone": version.tombstone,
        "parent_rule_keys": list(version.parent_rule_keys),
        "record_index": version.record_index,
        "shard_id": version.shard_id,
        "batch_id": version.batch_id,
        "logical_ts_ms": version.logical_ts_ms,
    }


def _version_rank(version: RuleVersion) -> tuple:
    return (
        version.generation,
        1 if version.tombstone else 0,
        _SOURCE_RANK.get(version.source, -1),
        -1 if version.logical_ts_ms is None else version.logical_ts_ms,
        version.batch_id or "",
        version.record_index,
    )


def patch_to_version(batch: PatchBatch, rule: RulePatch) -> RuleVersion:
    return RuleVersion(
        rule_key=rule.rule_key,
        source_rule_key=rule.source_rule_key,
        source="patch",
        effective_ms=rule.effective_ms,
        effective_error_ms=0,
        generation=rule.generation,
        dedup_window_ms=rule.dedup_window_ms,
        window_error_ms=0,
        match_strings=rule.match_strings,
        enabled=rule.enabled,
        tombstone=rule.tombstone,
        parent_rule_keys=rule.parent_rule_keys,
        record_index=rule.record_index,
        batch_id=batch.batch_id,
        logical_ts_ms=batch.logical_ts_ms,
    )


def versions_from_patches(
    batches: Iterable[PatchBatch],
) -> list[RuleVersion]:
    return [
        patch_to_version(batch, rule)
        for batch in batches
        for rule in batch.rules
    ]


def validate_generation_chain(
    versions: Iterable[RuleVersion],
) -> list[dict]:
    """Validate monotonic generations for absolute-time sources (v2/patch).

    v1 has an implicit generation 1 and a different second-based time basis;
    the cross-generation authority handling is documented separately.
    """

    by_key: dict[str, list[RuleVersion]] = {}
    for version in versions:
        if version.source == "v1":
            continue
        by_key.setdefault(version.rule_key, []).append(version)

    issues: list[dict] = []
    for key in sorted(by_key):
        groups: dict[int, list[RuleVersion]] = {}
        for version in by_key[key]:
            groups.setdefault(version.effective_ms, []).append(version)
        previous_max: int | None = None
        for effective_ms in sorted(groups):
            group = groups[effective_ms]
            minimum = min(item.generation for item in group)
            if previous_max is not None and minimum <= previous_max:
                for version in group:
                    issues.append(
                        {
                            "rule_key": key,
                            "effective_ms": effective_ms,
                            "generation": version.generation,
                            "previous_generation": previous_max,
                            "source": version.source,
                            "batch_id": version.batch_id,
                            "record_index": version.record_index,
                        }
                    )
            previous_max = max(
                previous_max if previous_max is not None else -1,
                max(item.generation for item in group),
            )
    issues.sort(
        key=lambda item: (
            item["rule_key"],
            item["effective_ms"],
            item["generation"],
            item.get("batch_id") or "",
        )
    )
    return issues


@dataclass(frozen=True)
class _ConflictSide:
    source: str
    generation: int
    effective_ms: int
    source_rule_key: str
    value: object

    def as_dict(self) -> dict:
        return {
            "source": self.source,
            "generation": self.generation,
            "effective_ms": self.effective_ms,
            "source_rule_key": self.source_rule_key,
            "value": self.value,
        }


def _latest_version(versions: Iterable[RuleVersion]) -> RuleVersion | None:
    values = list(versions)
    if not values:
        return None
    return max(values, key=lambda item: (item.effective_ms, _version_rank(item)))


class RuleCatalog:
    """Merged view with a deterministic authoritative timeline per rule key."""

    def __init__(
        self,
        *,
        v1_versions: Iterable[RuleVersion],
        v2_versions: Iterable[RuleVersion],
        patch_batches: Iterable[PatchBatch],
        observation_clock_ms: int,
    ):
        self.observation_clock_ms = observation_clock_ms
        self.v1_versions = tuple(v1_versions)
        self.v2_versions = tuple(v2_versions)
        self.patch_batches = tuple(patch_batches)
        self.patch_versions = tuple(versions_from_patches(self.patch_batches))
        all_versions = (
            self.v1_versions + self.v2_versions + self.patch_versions
        )
        self.versions = tuple(
            sorted(
                all_versions,
                key=lambda item: (
                    item.rule_key,
                    item.effective_ms,
                    item.generation,
                    _SOURCE_RANK.get(item.source, -1),
                    item.logical_ts_ms if item.logical_ts_ms is not None else -1,
                    item.batch_id or "",
                    item.record_index,
                ),
            )
        )
        by_key: dict[str, list[RuleVersion]] = {}
        for version in self.versions:
            by_key.setdefault(version.rule_key, []).append(version)
        self.by_key = {
            key: tuple(values) for key, values in sorted(by_key.items())
        }
        self.chain_issues = validate_generation_chain(self.versions)
        self.timeline = self._build_timeline()
        self.boundaries = {
            key: tuple(sorted(segments)) for key, segments in self.timeline.items()
        }
        self.conflicts = self._detect_conflicts()
        self._digest = digest_json(self.to_report())

    def _build_timeline(self) -> dict[str, dict[int, RuleVersion]]:
        timeline: dict[str, dict[int, RuleVersion]] = {}
        for key, versions in self.by_key.items():
            by_effective: dict[int, list[RuleVersion]] = {}
            for version in versions:
                by_effective.setdefault(version.effective_ms, []).append(version)
            winners = {
                effective_ms: max(group, key=_version_rank)
                for effective_ms, group in by_effective.items()
            }
            timeline[key] = dict(sorted(winners.items()))
        return timeline

    def _detect_conflicts(self) -> list[dict]:
        conflicts: list[dict] = []
        seen: set[str] = set()

        def add(
            key: str,
            field: str,
            left: RuleVersion,
            right: RuleVersion,
        ) -> None:
            signature = stable_json(
                {
                    "key": key,
                    "field": field,
                    "left": (
                        left.source,
                        left.generation,
                        left.effective_ms,
                        left.source_rule_key,
                    ),
                    "right": (
                        right.source,
                        right.generation,
                        right.effective_ms,
                        right.source_rule_key,
                    ),
                }
            )
            if signature in seen:
                return
            seen.add(signature)
            conflicts.append(
                {
                    "code": Code.E_RULE_CONFLICT,
                    "rule_key": key,
                    "field": field,
                    "left_generation": left.generation,
                    "right_generation": right.generation,
                    "left": _ConflictSide(
                        left.source,
                        left.generation,
                        left.effective_ms,
                        left.source_rule_key,
                        _field_value(left, field),
                    ).as_dict(),
                    "right": _ConflictSide(
                        right.source,
                        right.generation,
                        right.effective_ms,
                        right.source_rule_key,
                        _field_value(right, field),
                    ).as_dict(),
                    "message": (
                        f"{key}: {field} differs between rule generations"
                    ),
                }
            )

        for key in sorted(self.by_key):
            versions = self.by_key[key]
            v1_latest = _latest_version(
                item for item in versions if item.source == "v1"
            )
            absolute_latest = _latest_version(
                item for item in versions if item.source in ("v2", "patch")
            )
            if v1_latest is not None and absolute_latest is not None:
                pairs = [(v1_latest, absolute_latest)]
            else:
                pairs = []

            same_version_groups: dict[tuple[int, int], list[RuleVersion]] = {}
            for version in versions:
                same_version_groups.setdefault(
                    (version.effective_ms, version.generation), []
                ).append(version)
            for group in same_version_groups.values():
                if len(group) > 1:
                    ordered = sorted(group, key=_version_rank)
                    for left_index in range(len(ordered)):
                        for right_index in range(left_index + 1, len(ordered)):
                            pairs.append(
                                (ordered[left_index], ordered[right_index])
                            )

            for left, right in pairs:
                for field in (
                    "dedup_window_ms",
                    "match_strings",
                    "enabled",
                    "tombstone",
                ):
                    if _field_value(left, field) != _field_value(right, field):
                        add(key, field, left, right)

        conflicts.sort(
            key=lambda item: (
                item["rule_key"],
                item["field"],
                item["left"]["source"],
                item["left"]["generation"],
                item["right"]["source"],
                item["right"]["generation"],
            )
        )
        return conflicts

    def keys(self) -> tuple[str, ...]:
        return tuple(sorted(self.by_key))

    def active_version(self, key: str, timestamp_ms: int) -> RuleVersion | None:
        timeline = self.timeline.get(key)
        if not timeline:
            return None
        times = list(timeline)
        position = bisect.bisect_right(times, timestamp_ms)
        if position == 0:
            return None
        return timeline[times[position - 1]]

    def resolve(self, key: str, timestamp_ms: int) -> VersionResolution:
        versions = self.by_key.get(key, ())
        authoritative = self.active_version(key, timestamp_ms)
        if authoritative is None:
            future = [
                item.effective_ms
                for item in versions
                if item.effective_ms > timestamp_ms
            ]
            margin = min(future) - timestamp_ms if future else None
            return VersionResolution(
                status="not_effective",
                margin_ms=margin,
                near_boundary=margin is not None and margin <= 999,
                reason_code=Code.E_RULE_NOT_EFFECTIVE,
            )

        v1_latest = _latest_version(
            item
            for item in versions
            if item.source == "v1" and item.effective_ms <= timestamp_ms
        )
        absolute_latest = _latest_version(
            item
            for item in versions
            if item.source in ("v2", "patch")
            and item.effective_ms <= timestamp_ms
        )

        margin = self._margin_for(key, timestamp_ms)
        near_boundary = margin is not None and margin <= 999

        if v1_latest is not None and absolute_latest is not None:
            start_gap = abs(
                v1_latest.effective_ms - absolute_latest.effective_ms
            )
            allowed_gap = max(
                v1_latest.effective_error_ms,
                absolute_latest.effective_error_ms,
            )
            overlap_start = max(
                v1_latest.effective_ms, absolute_latest.effective_ms
            )
            overlap_end = max(
                v1_latest.effective_ms + v1_latest.effective_error_ms,
                absolute_latest.effective_ms + absolute_latest.effective_error_ms,
            )
            if (
                start_gap <= allowed_gap
                and overlap_start <= timestamp_ms <= overlap_end
            ):
                candidates = tuple(
                    sorted(
                        (v1_latest, absolute_latest),
                        key=_version_rank,
                    )
                )
                return VersionResolution(
                    status="ambiguous",
                    margin_ms=margin,
                    near_boundary=True,
                    candidates=candidates,
                    reason_code=Code.E_VERSION_AMBIGUOUS,
                )

        _ = authoritative
        return VersionResolution(
            status="resolved",
            version=authoritative,
            margin_ms=margin,
            near_boundary=near_boundary,
        )

    def _margin_for(self, key: str, timestamp_ms: int) -> int | None:
        boundaries = self.boundaries.get(key, ())
        if not boundaries:
            return None
        distances = [abs(timestamp_ms - value) for value in boundaries]
        return min(distances)

    def dependency_closure(self, changed_keys: Iterable[str]) -> set[str]:
        closure = {
            key for key in changed_keys if key in self.by_key
        }
        changed = True
        while changed:
            changed = False
            for key, versions in self.by_key.items():
                if key in closure:
                    continue
                if any(
                    parent in closure
                    for version in versions
                    for parent in version.parent_rule_keys
                ):
                    closure.add(key)
                    changed = True
        return closure

    def diff_keys(self, other: "RuleCatalog") -> set[str]:
        keys = set(self.by_key) | set(other.by_key)
        changed: set[str] = set()
        for key in keys:
            left = {
                "versions": [
                    rule_version_as_dict(item) for item in self.by_key.get(key, ())
                ],
                "timeline": [
                    rule_version_as_dict(item)
                    for item in self.timeline.get(key, {}).values()
                ],
            }
            right = {
                "versions": [
                    rule_version_as_dict(item) for item in other.by_key.get(key, ())
                ],
                "timeline": [
                    rule_version_as_dict(item)
                    for item in other.timeline.get(key, {}).values()
                ],
            }
            if stable_json(left) != stable_json(right):
                changed.add(key)
        return changed

    def to_report(self) -> dict:
        return {
            "observation_clock_ms": self.observation_clock_ms,
            "versions": [rule_version_as_dict(item) for item in self.versions],
            "timeline": [
                {
                    "rule_key": key,
                    "segments": [
                        rule_version_as_dict(version)
                        for _, version in sorted(segments.items())
                    ],
                }
                for key, segments in sorted(self.timeline.items())
            ],
            "conflicts": list(self.conflicts),
            "chain_issues": list(self.chain_issues),
        }

    @property
    def digest(self) -> str:
        return self._digest


def _field_value(version: RuleVersion, field: str) -> object:
    if field == "match_strings":
        return list(version.match_strings)
    return getattr(version, field)
