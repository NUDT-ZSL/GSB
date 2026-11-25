"""Timeline consistency checks and millisecond point queries."""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from .codes import Code
from .models import Utterance


Interval = tuple[int, int]


@dataclass(frozen=True)
class ConsistencyIssue:
    code: str
    message: str
    start_ms: int
    end_ms: int
    utterance_ids: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "start_ms": self.start_ms,
            "end_ms": self.end_ms,
            "utterance_ids": list(self.utterance_ids),
        }


@dataclass
class ConsistencyReport:
    ok: bool
    code: str = Code.OK
    issues: list[ConsistencyIssue] = field(default_factory=list)
    declared_regions: list[Interval] = field(default_factory=list)
    covered_regions: list[Interval] = field(default_factory=list)
    checked_ms: int = 0
    covered_ms: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "code": self.code,
            "issue_count": len(self.issues),
            "issues": [issue.to_dict() for issue in self.issues],
            "declared_regions": [list(item) for item in self.declared_regions],
            "covered_regions": [list(item) for item in self.covered_regions],
            "checked_ms": self.checked_ms,
            "covered_ms": self.covered_ms,
        }


def _normalize_regions(
    utterances: list[Utterance],
    declared_regions: Iterable[Interval] | None,
    region_start_ms: int | None,
    region_end_ms: int | None,
) -> list[Interval]:
    regions: list[Interval] = []
    if declared_regions:
        regions.extend((int(start), int(end)) for start, end in declared_regions)
    if region_start_ms is not None and region_end_ms is not None:
        regions.append((int(region_start_ms), int(region_end_ms)))
    if not regions and utterances:
        regions.append(
            (
                min(item.start_ms for item in utterances),
                max(item.end_ms for item in utterances),
            )
        )
    return [(start, end) for start, end in regions if end > start]


def merge_intervals(intervals: Iterable[Interval]) -> list[Interval]:
    ordered = sorted((start, end) for start, end in intervals if end > start)
    merged: list[Interval] = []
    for start, end in ordered:
        if not merged or start > merged[-1][1]:
            merged.append((start, end))
        else:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
    return merged


def check_consistency(
    utterances: Iterable[Utterance],
    *,
    declared_regions: Iterable[Interval] | None = None,
    region_start_ms: int | None = None,
    region_end_ms: int | None = None,
) -> ConsistencyReport:
    """Check half-open ``[start_ms, end_ms)`` millisecond coverage."""

    items = list(utterances)
    issues: list[ConsistencyIssue] = []

    for index, left in enumerate(items):
        for right in items[index + 1 :]:
            overlap_start = max(left.start_ms, right.start_ms)
            overlap_end = min(left.end_ms, right.end_ms)
            if overlap_start < overlap_end:
                issues.append(
                    ConsistencyIssue(
                        code=Code.E_OVERLAP,
                        message="at least two utterances cover the same millisecond points",
                        start_ms=overlap_start,
                        end_ms=overlap_end,
                        utterance_ids=(left.utterance_id, right.utterance_id),
                    )
                )

    regions = _normalize_regions(items, declared_regions, region_start_ms, region_end_ms)
    clipped_intervals: list[Interval] = []
    checked_ms = 0
    for region_start, region_end in regions:
        checked_ms += region_end - region_start
        for item in items:
            start = max(item.start_ms, region_start)
            end = min(item.end_ms, region_end)
            if start < end:
                clipped_intervals.append((start, end))

    covered_regions = merge_intervals(clipped_intervals)
    for region_start, region_end in regions:
        cursor = region_start
        for covered_start, covered_end in covered_regions:
            if covered_end <= region_start or covered_start >= region_end:
                continue
            covered_start = max(covered_start, region_start)
            covered_end = min(covered_end, region_end)
            if covered_start > cursor:
                issues.append(
                    ConsistencyIssue(
                        code=Code.E_GAP,
                        message="declared transcription region has uncovered millisecond points",
                        start_ms=cursor,
                        end_ms=covered_start,
                    )
                )
            cursor = max(cursor, covered_end)
        if cursor < region_end:
            issues.append(
                ConsistencyIssue(
                    code=Code.E_GAP,
                    message="declared transcription region has uncovered millisecond points",
                    start_ms=cursor,
                    end_ms=region_end,
                )
            )

    overlap_issues = [issue for issue in issues if issue.code == Code.E_OVERLAP]
    gap_issues = [issue for issue in issues if issue.code == Code.E_GAP]
    code = Code.E_OVERLAP if overlap_issues else Code.E_GAP if gap_issues else Code.OK
    covered_ms = sum(end - start for start, end in covered_regions)
    return ConsistencyReport(
        ok=not issues,
        code=code,
        issues=issues,
        declared_regions=regions,
        covered_regions=covered_regions,
        checked_ms=checked_ms,
        covered_ms=covered_ms,
    )


@dataclass(frozen=True)
class PointQueryResult:
    ms: int
    ok: bool
    code: str
    utterance: Utterance | None = None
    cache_key: str | None = None
    dependency_chain: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "ms": self.ms,
            "ok": self.ok,
            "code": self.code,
            "utterance_id": self.utterance.utterance_id if self.utterance else None,
            "text": self.utterance.text if self.utterance else None,
            "cache_key": self.cache_key,
            "dependency_chain": list(self.dependency_chain),
        }

    def replay(self, cache: Any) -> dict[str, Any]:
        if self.cache_key is None:
            return {"ok": False, "code": Code.E_CACHE_MISS, "chain": []}
        return cache.replay_chain(self.cache_key)


class TimelineIndex:
    """Sorted interval index for :math:`O(log n)` point queries."""

    def __init__(
        self,
        utterances: Iterable[Utterance],
        *,
        declared_regions: Iterable[Interval] | None = None,
        cache_keys: dict[str, str] | None = None,
        dependency_chains: dict[str, tuple[str, ...]] | None = None,
    ) -> None:
        self.utterances = sorted(utterances, key=lambda item: (item.start_ms, item.end_ms, item.utterance_id))
        self.starts = [item.start_ms for item in self.utterances]
        self.cache_keys = cache_keys or {}
        self.dependency_chains = dependency_chains or {}
        self.consistency = check_consistency(self.utterances, declared_regions=declared_regions)

    def at(self, ms: int) -> PointQueryResult:
        position = bisect_right(self.starts, ms) - 1
        candidates: list[Utterance] = []
        search = position
        while search >= 0:
            item = self.utterances[search]
            if item.end_ms <= ms:
                search -= 1
                continue
            if item.start_ms > ms:
                break
            candidates.append(item)
            search -= 1

        if not candidates:
            return PointQueryResult(ms=ms, ok=False, code=Code.E_GAP)
        if len(candidates) > 1:
            return PointQueryResult(
                ms=ms,
                ok=False,
                code=Code.E_OVERLAP,
                utterance=candidates[0],
            )
        item = candidates[0]
        key = self.cache_keys.get(item.utterance_id)
        chain = self.dependency_chains.get(item.utterance_id, ())
        return PointQueryResult(
            ms=ms,
            ok=True,
            code=Code.OK,
            utterance=item,
            cache_key=key,
            dependency_chain=tuple(chain),
        )

    def millisecond_ids(self, region: Interval | None = None) -> dict[int, str]:
        if region is None:
            if not self.utterances:
                return {}
            region = (self.utterances[0].start_ms, max(item.end_ms for item in self.utterances))
        result: dict[int, str] = {}
        for ms in range(region[0], region[1]):
            query = self.at(ms)
            result[ms] = query.utterance.utterance_id if query.ok else query.code
        return result

    def millisecond_fingerprint(self, region: Interval | None = None) -> str:
        from .hashing import digest_json

        if region is None and self.utterances:
            region = (
                min(item.start_ms for item in self.utterances),
                max(item.end_ms for item in self.utterances),
            )
        states = []
        for ms, utterance_id in sorted(self.millisecond_ids(region).items()):
            item = next((u for u in self.utterances if u.utterance_id == utterance_id), None)
            states.append(
                [
                    ms,
                    utterance_id,
                    digest_json(item.to_dict()) if item is not None else utterance_id,
                ]
            )
        return digest_json(states)


def replay_with_resolver(
    cache_key: str,
    resolver: Callable[[str], tuple[Any, list[str]] | None],
) -> list[Any]:
    """Replay a key and its upstream dependency keys using a resolver."""

    seen: set[str] = set()
    ordered: list[Any] = []

    def visit(key: str, stack: set[str]) -> None:
        if key in stack:
            from .codes import GovernanceError

            raise GovernanceError(Code.E_DEPENDENCY_CYCLE, "dependency chain contains a cycle", key=key)
        if key in seen:
            return
        resolved = resolver(key)
        if resolved is None:
            from .codes import GovernanceError

            raise GovernanceError(Code.E_CACHE_MISS, "cache key cannot be replayed", key=key)
        value, upstream = resolved
        stack.add(key)
        for parent in upstream:
            visit(parent, stack)
        stack.remove(key)
        seen.add(key)
        ordered.append({"key": key, "value": value, "upstream": list(upstream)})

    visit(cache_key, set())
    return ordered
