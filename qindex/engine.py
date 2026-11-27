"""Layered immutable-segment index engine with deterministic maintenance."""

from __future__ import annotations

from dataclasses import dataclass, field
import heapq
from typing import Any, Iterable, Iterator

from .model import (
    IndexConfig,
    IndexEntry,
    QualityEvent,
    Segment,
    segment_payload_size,
)


@dataclass
class _PartitionState:
    process: str
    shift: str
    levels: list[list[Segment]]
    memtable_id: str | None = None
    memtable_entries: list[IndexEntry] = field(default_factory=list)

    @property
    def partition(self) -> tuple[str, str]:
        return (self.process, self.shift)


class IndexEngine:
    """Partitioned, tiered immutable-segment index.

    A partition is exactly one (process, shift) group. Each partition owns one
    active memory table. A logical-time group is appended atomically before the
    flush threshold is checked, so same-time events cannot be split by a flush.
    """

    POLICY = {
        "partitioning": "(process, shift)",
        "key_order": [
            "process",
            "shift",
            "batch_prefix",
            "logical_time",
            "event_id(tie_breaker)",
        ],
        "flush_barrier": (
            "append the complete logical-time group, then flush partitions "
            "whose memtable reaches the threshold"
        ),
        "maintenance_priority": [
            "promote_overfull_non_top_level",
            "compact_invalid_ratio_at_or_above_threshold",
            "compact_partition_or_top_level_capacity",
        ],
        "invalid_ratio_comparison": ">=",
        "invalid_ratio_candidate_set": (
            "all segments in the triggering level; rewritten to the same level"
        ),
        "capacity_candidate_set": (
            "prefer the overfull top level, then the level with most bytes; "
            "merge all segments in that level, or all partition segments into "
            "the top level when no level-local rewrite can reduce bytes"
        ),
        "deletion_order": "write replacement, delete old segments, update ledger",
        "ledger_equation": (
            "partition_bytes_before = partition_bytes_after + reclaimed_bytes; "
            "deleted_segment_bytes = rewritten_segment_bytes + reclaimed_bytes"
        ),
    }

    def __init__(self, config: IndexConfig) -> None:
        self.config = config
        self.partitions: dict[tuple[str, str], _PartitionState] = {}
        self.records: dict[str, QualityEvent] = {}
        self.invalid_event_ids: set[str] = set()
        self.live_segments: dict[str, Segment] = {}
        self.next_memtable_seq = 1
        self.next_segment_seq = 1
        self.action_sequence = 0

    # ------------------------------------------------------------------
    # construction helpers
    # ------------------------------------------------------------------
    def _action(self, action: str, time: int, **payload: Any) -> dict[str, Any]:
        self.action_sequence += 1
        result = {
            "sequence": self.action_sequence,
            "action": action,
            "time": int(time),
        }
        result.update(payload)
        return result

    def _partition(self, process: str, shift: str) -> _PartitionState:
        partition_key = (process, shift)
        state = self.partitions.get(partition_key)
        if state is None:
            state = _PartitionState(
                process=process,
                shift=shift,
                levels=[[] for _ in self.config.levels],
            )
            self.partitions[partition_key] = state
        return state

    def _new_segment(
        self,
        partition: _PartitionState,
        level: int,
        entries: Iterable[IndexEntry],
    ) -> Segment:
        segment_seq = self.next_segment_seq
        self.next_segment_seq += 1
        segment = Segment.create(
            segment_id=f"SEG-{segment_seq:06d}",
            partition=partition.partition,
            level=level,
            entries=entries,
            created_order=segment_seq,
        )
        self.live_segments[segment.segment_id] = segment
        partition.levels[level].append(segment)
        partition.levels[level].sort(
            key=lambda item: (item.created_order, item.segment_id)
        )
        return segment

    # ------------------------------------------------------------------
    # ingestion and flushing
    # ------------------------------------------------------------------
    def ingest_time_group(
        self, time: int, events: list[QualityEvent]
    ) -> list[dict[str, Any]]:
        touched: set[tuple[str, str]] = set()
        for event in events:
            if event.logical_time != time:
                raise ValueError(
                    f"事件 {event.event_id} 的 logical_time 不属于该时刻"
                )
            if event.event_id in self.records:
                raise ValueError(f"重复事件: {event.event_id}")
            partition = self._partition(event.process, event.shift)
            if partition.memtable_id is None:
                partition.memtable_id = f"MT-{self.next_memtable_seq:05d}"
                self.next_memtable_seq += 1
            offset = len(partition.memtable_entries)
            entry = IndexEntry.from_event(event, partition.memtable_id, offset)
            partition.memtable_entries.append(entry)
            partition.memtable_entries.sort(
                key=lambda item: item.key
            )
            self.records[event.event_id] = event
            touched.add(partition.partition)

        actions: list[dict[str, Any]] = []
        ready = sorted(
            partition_key
            for partition_key in touched
            if len(self.partitions[partition_key].memtable_entries)
            >= self.config.flush_threshold
        )
        for partition_key in ready:
            partition = self.partitions[partition_key]
            flush_action = self._flush_partition(partition, time)
            actions.append(flush_action)
            actions.extend(self._maintain_partition(partition, time, "flush"))
        return actions

    def _flush_partition(
        self, partition: _PartitionState, time: int
    ) -> dict[str, Any]:
        if not partition.memtable_entries:
            raise AssertionError("空内存表不应触发刷写")
        memtable_id = partition.memtable_id
        entries = sorted(partition.memtable_entries, key=lambda entry: entry.key)
        entry_count = len(entries)
        segment = self._new_segment(partition, 0, entries)
        partition.memtable_entries.clear()
        partition.memtable_id = None
        return self._action(
            "flush",
            time,
            partition=list(partition.partition),
            memtable_id=memtable_id,
            segment_id=segment.segment_id,
            target_level=0,
            threshold=self.config.flush_threshold,
            entry_count=entry_count,
            threshold_reached=entry_count >= self.config.flush_threshold,
            threshold_overshoot=max(
                0, entry_count - self.config.flush_threshold
            ),
            timestamp_barrier=True,
            segment_size_bytes=segment.size_bytes,
            segment=segment.to_dict(include_entries=False),
            entry_locations=[
                {
                    "segment_id": segment.segment_id,
                    "segment_offset": offset,
                    "event_id": entry.event_id,
                    "key": list(entry.key),
                    "memtable_id": entry.memtable_id,
                    "logical_offset": entry.logical_offset,
                }
                for offset, entry in enumerate(entries)
            ],
        )

    # ------------------------------------------------------------------
    # invalidation
    # ------------------------------------------------------------------
    @staticmethod
    def _predicate_matches(
        entry: IndexEntry, station: str | None, batch_prefix: str | None
    ) -> bool:
        if station is not None and entry.station != station:
            return False
        if batch_prefix is not None and not entry.key[2].startswith(
            batch_prefix
        ):
            return False
        return True

    def _all_entries_with_source(
        self,
    ) -> list[tuple[IndexEntry, dict[str, Any]]]:
        rows: list[tuple[IndexEntry, dict[str, Any]]] = []
        for partition in self.partitions.values():
            for level_index, segments in enumerate(partition.levels):
                for segment in segments:
                    for offset, entry in enumerate(segment.entries):
                        rows.append(
                            (
                                entry,
                                {
                                    "kind": "segment",
                                    "segment_id": segment.segment_id,
                                    "segment_offset": offset,
                                    "level": level_index,
                                },
                            )
                        )
            if partition.memtable_id is not None:
                for entry in partition.memtable_entries:
                    rows.append(
                        (
                            entry,
                            {
                                "kind": "memtable",
                                "memtable_id": partition.memtable_id,
                                "logical_offset": entry.logical_offset,
                            },
                        )
                    )
        rows.sort(key=lambda row: row[0].key)
        return rows

    def _active_partition_entries(
        self, partition: _PartitionState
    ) -> list[IndexEntry]:
        entries: list[IndexEntry] = []
        for segments in partition.levels:
            for segment in segments:
                entries.extend(segment.entries)
        entries.extend(partition.memtable_entries)
        entries = [
            entry
            for entry in entries
            if entry.event_id not in self.invalid_event_ids
        ]
        entries.sort(key=lambda entry: entry.key)
        return entries

    def invalidate(
        self, spec: dict[str, Any], time: int
    ) -> dict[str, Any]:
        station_raw = spec.get("station")
        prefix_raw = spec.get("batch_prefix")
        station = str(station_raw) if station_raw else None
        batch_prefix = str(prefix_raw) if prefix_raw else None
        if station is None and batch_prefix is None:
            raise ValueError("失效事件必须指定 station 或 batch_prefix")

        matched: list[tuple[IndexEntry, dict[str, Any]]] = [
            row
            for row in self._all_entries_with_source()
            if self._predicate_matches(row[0], station, batch_prefix)
        ]
        matched_ids = {entry.event_id for entry, _ in matched}
        newly_invalid = {
            event_id
            for event_id in matched_ids
            if event_id not in self.invalid_event_ids
        }
        self.invalid_event_ids.update(newly_invalid)

        affected_partitions = sorted(
            {
                (self.records[event_id].process, self.records[event_id].shift)
                for event_id in matched_ids
            }
        )
        involved_segments = sorted(
            {
                source["segment_id"]
                for entry, source in matched
                if entry.event_id in newly_invalid
                and source["kind"] == "segment"
            }
        )
        involved_memtables = sorted(
            {
                source["memtable_id"]
                for entry, source in matched
                if entry.event_id in newly_invalid
                and source["kind"] == "memtable"
            }
        )

        surviving_ranges: list[dict[str, Any]] = []
        for partition_key in affected_partitions:
            partition = self.partitions[partition_key]
            active = self._active_partition_entries(partition)
            surviving_ranges.append(
                {
                    "partition": list(partition_key),
                    "entry_count": len(active),
                    "first_key": list(active[0].key) if active else None,
                    "last_key": list(active[-1].key) if active else None,
                }
            )

        active_matching = [
            (entry, source)
            for entry, source in self._all_entries_with_source()
            if entry.event_id not in self.invalid_event_ids
            and self._predicate_matches(entry, station, batch_prefix)
        ]
        first_inconsistency: dict[str, Any] | None = None
        if active_matching:
            entry, source = active_matching[0]
            first_inconsistency = {
                "event_id": entry.event_id,
                "key": list(entry.key),
                "source": source,
            }

        return self._action(
            "invalidate",
            time,
            invalidation_id=spec.get("invalidation_id"),
            kind=spec.get("kind"),
            reason=spec.get("reason"),
            predicate={"station": station, "batch_prefix": batch_prefix},
            matched_entry_count=len(matched),
            newly_invalidated_entry_count=len(newly_invalid),
            newly_invalidated_event_ids=sorted(newly_invalid),
            involved_segments=involved_segments,
            involved_memtables=involved_memtables,
            affected_partitions=[list(item) for item in affected_partitions],
            surviving_ranges=surviving_ranges,
            diagnostic={
                "definition": (
                    "first active indexed entry still matching the invalidation "
                    "predicate; null means no stale hit remains"
                ),
                "active_matching_count": len(active_matching),
                "first_inconsistency": first_inconsistency,
            },
        )

    def maintain_after_invalidation(
        self, time: int, action: dict[str, Any]
    ) -> list[dict[str, Any]]:
        affected = {
            (str(item[0]), str(item[1]))
            for item in action.get("affected_partitions", [])
        }
        actions: list[dict[str, Any]] = []
        for partition_key in sorted(affected):
            partition = self.partitions[partition_key]
            if not any(partition.levels):
                continue
            actions.extend(
                self._maintain_partition(partition, time, "invalidation")
            )
        return actions

    # ------------------------------------------------------------------
    # maintenance: tiering, ratio compaction, capacity compaction
    # ------------------------------------------------------------------
    def _usage(self, segments: list[Segment]) -> dict[str, int]:
        return {
            "segment_count": len(segments),
            "entry_count": sum(segment.entry_count for segment in segments),
            "bytes": sum(segment.size_bytes for segment in segments),
        }

    def _usage_over(
        self, segments: list[Segment], level_index: int
    ) -> bool:
        config = self.config.levels[level_index]
        return (
            len(segments) > config.max_segments
            or sum(segment.size_bytes for segment in segments) > config.max_bytes
        )

    def _partition_bytes(self, partition: _PartitionState) -> int:
        return sum(
            segment.size_bytes
            for segments in partition.levels
            for segment in segments
        )

    def _promote_once(
        self, partition: _PartitionState, time: int
    ) -> dict[str, Any] | None:
        top_index = len(partition.levels) - 1
        for level_index in range(top_index):
            segments = list(partition.levels[level_index])
            if not self._usage_over(segments, level_index):
                continue
            source_before = self._usage(segments)
            remaining = list(segments)
            selected: list[Segment] = []
            while remaining and self._usage_over(remaining, level_index):
                selected.append(remaining.pop(0))
            if not selected:
                raise AssertionError("超限层未选出晋升候选")
            selected_ids = {segment.segment_id for segment in selected}
            partition.levels[level_index] = [
                segment
                for segment in partition.levels[level_index]
                if segment.segment_id not in selected_ids
            ]
            target_before = self._usage(partition.levels[level_index + 1])
            for segment in selected:
                segment.level = level_index + 1
                partition.levels[level_index + 1].append(segment)
            partition.levels[level_index + 1].sort(
                key=lambda item: (item.created_order, item.segment_id)
            )
            source_after = self._usage(partition.levels[level_index])
            target_after = self._usage(partition.levels[level_index + 1])
            source_config = self.config.levels[level_index]
            reasons: list[str] = []
            if source_before["segment_count"] > source_config.max_segments:
                reasons.append("segment_count_over_cap")
            if source_before["bytes"] > source_config.max_bytes:
                reasons.append("bytes_over_cap")
            return self._action(
                "promote",
                time,
                partition=list(partition.partition),
                from_level=level_index,
                from_level_name=source_config.name,
                to_level=level_index + 1,
                to_level_name=self.config.levels[level_index + 1].name,
                reasons=reasons,
                segment_ids=[segment.segment_id for segment in selected],
                moved_entry_count=sum(
                    segment.entry_count for segment in selected
                ),
                moved_bytes=sum(segment.size_bytes for segment in selected),
                source_level_before=source_before,
                source_level_after=source_after,
                target_level_before=target_before,
                target_level_after=target_after,
                source_capacity=source_config.to_dict(),
            )
        return None

    def _ratio_candidates_for_partition(
        self, partition: _PartitionState
    ) -> list[dict[str, Any]]:
        candidates: list[dict[str, Any]] = []
        for level_index, segments in enumerate(partition.levels):
            if not segments:
                continue
            triggering = []
            for segment in segments:
                invalid_count = sum(
                    1
                    for entry in segment.entries
                    if entry.event_id in self.invalid_event_ids
                )
                ratio = invalid_count / segment.entry_count
                if ratio >= self.config.merge_invalid_ratio:
                    triggering.append(
                        {
                            "segment_id": segment.segment_id,
                            "entry_count": segment.entry_count,
                            "invalid_entry_count": invalid_count,
                            "invalid_ratio": ratio,
                            "threshold": self.config.merge_invalid_ratio,
                            "comparison": ">=",
                        }
                    )
            if triggering:
                candidates.append(
                    {
                        "level": level_index,
                        "trigger_segments": triggering,
                        "max_invalid_ratio": max(
                            item["invalid_ratio"] for item in triggering
                        ),
                    }
                )
        return candidates

    def _capacity_pressure(
        self, partition: _PartitionState
    ) -> dict[str, Any]:
        total_bytes = self._partition_bytes(partition)
        levels_over = [
            level_index
            for level_index, segments in enumerate(partition.levels)
            if self._usage_over(segments, level_index)
        ]
        return {
            "partition_bytes": total_bytes,
            "partition_max_bytes": self.config.partition_max_bytes,
            "partition_over_capacity": total_bytes
            > self.config.partition_max_bytes,
            "levels_over_capacity": levels_over,
            "capacity_triggered": bool(levels_over)
            or total_bytes > self.config.partition_max_bytes,
        }

    def _valid_entries_from_segments(
        self, segments: list[Segment]
    ) -> list[IndexEntry]:
        merged = heapq.merge(
            *(segment.entries for segment in segments),
            key=lambda entry: entry.key,
        )
        return [
            entry
            for entry in merged
            if entry.event_id not in self.invalid_event_ids
        ]

    def _compact_segments(
        self,
        partition: _PartitionState,
        sources: list[Segment],
        target_level: int,
        time: int,
        trigger: str,
        conditions: dict[str, Any],
    ) -> dict[str, Any]:
        if not sources:
            raise AssertionError("合并候选不能为空")
        before = self._partition_bytes(partition)
        source_ids = {segment.segment_id for segment in sources}
        source_levels = sorted({segment.level for segment in sources})
        old_bytes = sum(segment.size_bytes for segment in sources)
        source_entry_count = sum(segment.entry_count for segment in sources)
        valid_entries = self._valid_entries_from_segments(sources)
        valid_ids = [entry.event_id for entry in valid_entries]
        if len(valid_ids) != len(set(valid_ids)):
            raise AssertionError("合并时发现重复的有效事件")
        new_size = (
            segment_payload_size(partition.partition, valid_entries)
            if valid_entries
            else 0
        )

        deleted = [segment.to_dict(include_entries=False) for segment in sources]
        for segment in sources:
            self.live_segments.pop(segment.segment_id, None)
        for level_index, segments in enumerate(partition.levels):
            partition.levels[level_index] = [
                segment
                for segment in segments
                if segment.segment_id not in source_ids
            ]

        target_segment: Segment | None = None
        if valid_entries:
            target_segment = self._new_segment(
                partition, target_level, valid_entries
            )
        if target_segment is not None:
            if [entry.event_id for entry in target_segment.entries] != valid_ids:
                raise AssertionError("合并后有效条目集合或顺序发生变化")
        after = self._partition_bytes(partition)
        reclaimed = before - after
        expected_after = before - old_bytes + new_size
        ledger_ok = (
            after == expected_after
            and reclaimed == old_bytes - new_size
            and before == after + reclaimed
            and old_bytes == new_size + reclaimed
        )
        if not ledger_ok:
            raise AssertionError(
                "空间回收账目不平衡: "
                f"before={before}, after={after}, deleted={old_bytes}, "
                f"new={new_size}, reclaimed={reclaimed}"
            )

        discarded_ids = [
            entry.event_id
            for segment in sources
            for entry in segment.entries
            if entry.event_id in self.invalid_event_ids
        ]
        action = self._action(
            "compact",
            time,
            partition=list(partition.partition),
            trigger=trigger,
            policy_priority=self.POLICY["maintenance_priority"],
            conditions=conditions,
            source_levels=source_levels,
            target_level=target_level,
            source_segments=deleted,
            source_segment_ids=[segment.segment_id for segment in sources],
            source_entry_count=source_entry_count,
            discarded_invalid_entry_count=len(discarded_ids),
            discarded_invalid_event_ids=discarded_ids,
            valid_entry_count=len(valid_entries),
            valid_sequence_preserved=True,
            valid_event_ids_before=valid_ids,
            valid_event_ids_after=[entry.event_id for entry in valid_entries],
            target_segment=(
                target_segment.to_dict(include_entries=False)
                if target_segment is not None
                else None
            ),
            target_key_event_id_mapping=(
                target_segment.key_event_id_mapping()
                if target_segment is not None
                else {}
            ),
            target_entry_locations=[
                {
                    "segment_id": target_segment.segment_id,
                    "segment_offset": offset,
                    "event_id": entry.event_id,
                    "key": list(entry.key),
                    "memtable_id": entry.memtable_id,
                    "logical_offset": entry.logical_offset,
                }
                for offset, entry in enumerate(target_segment.entries)
            ]
            if target_segment is not None
            else [],
            partition_bytes_before=before,
            partition_bytes_after=after,
            deleted_segment_bytes=old_bytes,
            rewritten_segment_bytes=new_size,
            reclaimed_bytes=reclaimed,
            ledger={
                "before": before,
                "after": after,
                "deleted_segment_bytes": old_bytes,
                "rewritten_segment_bytes": new_size,
                "reclaimed_bytes": reclaimed,
                "equation_before_after_reclaimed": before
                == after + reclaimed,
                "equation_deleted_rewritten_reclaimed": old_bytes
                == new_size + reclaimed,
                "ok": ledger_ok,
            },
            steps=[
                {
                    "action": "rewrite_new_segment",
                    "target_segment": (
                        target_segment.to_dict(include_entries=False)
                        if target_segment is not None
                        else None
                    ),
                    "entry_count": len(valid_entries),
                    "bytes": new_size,
                },
                {
                    "action": "delete_old_segments",
                    "segment_ids": [segment.segment_id for segment in sources],
                    "bytes": old_bytes,
                },
                {
                    "action": "space_reclaim",
                    "partition_bytes_before": before,
                    "partition_bytes_after": after,
                    "reclaimed_bytes": reclaimed,
                },
            ],
        )
        return action

    def _capacity_conditions(
        self, partition: _PartitionState
    ) -> dict[str, Any]:
        pressure = self._capacity_pressure(partition)
        return {
            "partition": pressure,
            "level_capacities": [
                {
                    "level": level_index,
                    **self._usage(segments),
                    **self.config.levels[level_index].to_dict(),
                }
                for level_index, segments in enumerate(partition.levels)
            ],
        }

    def _capacity_compact_once(
        self, partition: _PartitionState, time: int
    ) -> dict[str, Any] | None:
        pressure = self._capacity_pressure(partition)
        if not pressure["capacity_triggered"]:
            return None

        overfull = sorted(
            pressure["levels_over_capacity"], reverse=True
        )
        by_bytes = sorted(
            range(len(partition.levels)),
            key=lambda level_index: (
                -sum(
                    segment.size_bytes
                    for segment in partition.levels[level_index]
                ),
                level_index,
            ),
        )
        level_order = overfull + [
            level_index
            for level_index in by_bytes
            if level_index not in overfull
        ]
        for level_index in level_order:
            segments = list(partition.levels[level_index])
            if len(segments) < 1:
                continue
            valid_entries = self._valid_entries_from_segments(segments)
            estimated_size = (
                segment_payload_size(partition.partition, valid_entries)
                if valid_entries
                else 0
            )
            old_size = sum(segment.size_bytes for segment in segments)
            if estimated_size >= old_size:
                continue
            trigger = (
                "level_capacity"
                if level_index in pressure["levels_over_capacity"]
                else "partition_capacity"
            )
            return self._compact_segments(
                partition,
                segments,
                level_index,
                time,
                trigger,
                self._capacity_conditions(partition),
            )

        # Cross-level fallback: no single-level rewrite can shrink bytes.
        all_segments = [
            segment
            for segments in partition.levels
            for segment in segments
        ]
        if len(all_segments) > 1:
            valid_entries = self._valid_entries_from_segments(all_segments)
            estimated_size = (
                segment_payload_size(partition.partition, valid_entries)
                if valid_entries
                else 0
            )
            old_size = sum(segment.size_bytes for segment in all_segments)
            if estimated_size < old_size:
                return self._compact_segments(
                    partition,
                    all_segments,
                    len(partition.levels) - 1,
                    time,
                    "partition_capacity",
                    self._capacity_conditions(partition),
                )
        return None

    def _maintain_partition(
        self, partition: _PartitionState, time: int, reason: str
    ) -> list[dict[str, Any]]:
        actions: list[dict[str, Any]] = []
        for _ in range(128):
            promoted = self._promote_once(partition, time)
            if promoted is not None:
                promoted["maintenance_reason"] = reason
                actions.append(promoted)
                continue

            ratio_candidates = self._ratio_candidates_for_partition(partition)
            capacity = self._capacity_pressure(partition)
            if ratio_candidates:
                candidate = ratio_candidates[0]
                level_index = candidate["level"]
                both = bool(capacity["capacity_triggered"])
                trigger = "invalid_ratio+capacity" if both else "invalid_ratio"
                conditions = {
                    "invalid_ratio": candidate,
                    "capacity": capacity,
                    "priority_decision": (
                        "invalid_ratio_before_capacity"
                        if both
                        else "invalid_ratio_only"
                    ),
                }
                action = self._compact_segments(
                    partition,
                    list(partition.levels[level_index]),
                    level_index,
                    time,
                    trigger,
                    conditions,
                )
                action["maintenance_reason"] = reason
                actions.append(action)
                continue

            if capacity["capacity_triggered"]:
                action = self._capacity_compact_once(partition, time)
                if action is not None:
                    action["maintenance_reason"] = reason
                    actions.append(action)
                    continue
            break
        else:
            raise AssertionError("维护循环超过安全上限")
        return actions

    # ------------------------------------------------------------------
    # queries and views
    # ------------------------------------------------------------------
    def query(
        self,
        query_id: str,
        process: str,
        shift: str,
        batch_prefix: str,
    ) -> dict[str, Any]:
        partition = self.partitions.get((process, shift))
        if partition is None:
            return {
                "query_id": query_id,
                "process": process,
                "shift": shift,
                "batch_prefix": batch_prefix,
                "hits": [],
                "filtered_invalid": [],
                "segments_scanned": 0,
                "candidate_entries_scanned": 0,
                "invalid_filtered_entries": 0,
                "returned_entries": 0,
            }

        streams: list[Iterator[tuple[IndexEntry, dict[str, Any]]]] = []
        segments_scanned = 0
        candidate_entries_scanned = 0
        filtered_invalid: list[dict[str, Any]] = []
        for segments in partition.levels:
            for segment in sorted(
                segments, key=lambda item: (item.level, item.created_order)
            ):
                segments_scanned += 1

                def segment_stream(
                    current: Segment = segment,
                ) -> Iterator[tuple[IndexEntry, dict[str, Any]]]:
                    for offset, entry in enumerate(current.entries):
                        if not entry.key[2].startswith(batch_prefix):
                            continue
                        if entry.event_id in self.invalid_event_ids:
                            continue
                        yield (
                            entry,
                            {
                                "kind": "segment",
                                "segment_id": current.segment_id,
                                "segment_offset": offset,
                                "level": current.level,
                            },
                        )

                streams.append(segment_stream())

        if partition.memtable_id is not None:
            memtable_id = partition.memtable_id
            sorted_memtable = sorted(
                partition.memtable_entries, key=lambda entry: entry.key
            )

            def memtable_stream() -> Iterator[
                tuple[IndexEntry, dict[str, Any]]
            ]:
                for entry in sorted_memtable:
                    if not entry.key[2].startswith(batch_prefix):
                        continue
                    if entry.event_id in self.invalid_event_ids:
                        continue
                    yield (
                        entry,
                        {
                            "kind": "memtable",
                            "memtable_id": memtable_id,
                            "logical_offset": entry.logical_offset,
                        },
                    )

            streams.append(memtable_stream())

        merged = (
            heapq.merge(*streams, key=lambda row: row[0].key)
            if streams
            else iter(())
        )
        hits: list[dict[str, Any]] = []
        for rank, (entry, source) in enumerate(merged, start=1):
            hits.append(
                {
                    "rank": rank,
                    "event_id": entry.event_id,
                    "key": list(entry.key),
                    "logical_time": entry.key[3],
                    "source": source,
                    "memtable_id": entry.memtable_id,
                    "logical_offset": entry.logical_offset,
                    "live": True,
                    "metric": entry.metric,
                    "value": entry.value,
                    "result": entry.result,
                }
            )

        for entry, source in self._all_entries_with_source():
            if entry.key[0] != process or entry.key[1] != shift:
                continue
            if not entry.key[2].startswith(batch_prefix):
                continue
            candidate_entries_scanned += 1
            if entry.event_id in self.invalid_event_ids:
                filtered_invalid.append(
                    {
                        "event_id": entry.event_id,
                        "key": list(entry.key),
                        "source": source,
                        "live": False,
                    }
                )

        return {
            "query_id": query_id,
            "process": process,
            "shift": shift,
            "batch_prefix": batch_prefix,
            "hits": hits,
            "filtered_invalid": filtered_invalid,
            "segments_scanned": segments_scanned,
            "candidate_entries_scanned": candidate_entries_scanned,
            "invalid_filtered_entries": len(filtered_invalid),
            "returned_entries": len(hits),
            "scan_amplification": (
                candidate_entries_scanned / len(hits)
                if hits
                else float(candidate_entries_scanned)
            ),
        }

    def partition_views(self) -> list[dict[str, Any]]:
        views: list[dict[str, Any]] = []
        for partition_key in sorted(self.partitions):
            partition = self.partitions[partition_key]
            level_views: list[dict[str, Any]] = []
            segment_bytes = 0
            for level_index, segments in enumerate(partition.levels):
                segments = sorted(
                    segments,
                    key=lambda item: (item.created_order, item.segment_id),
                )
                level_bytes = sum(segment.size_bytes for segment in segments)
                segment_bytes += level_bytes
                invalids = sum(
                    1
                    for segment in segments
                    for entry in segment.entries
                    if entry.event_id in self.invalid_event_ids
                )
                level_views.append(
                    {
                        "level": level_index,
                        **self.config.levels[level_index].to_dict(),
                        "segment_count": len(segments),
                        "entry_count": sum(
                            segment.entry_count for segment in segments
                        ),
                        "invalid_entry_count": invalids,
                        "bytes": level_bytes,
                        "segments": [
                            {
                                "segment_id": segment.segment_id,
                                "entry_count": segment.entry_count,
                                "invalid_entry_count": sum(
                                    1
                                    for entry in segment.entries
                                    if entry.event_id
                                    in self.invalid_event_ids
                                ),
                                "size_bytes": segment.size_bytes,
                            }
                            for segment in segments
                        ],
                    }
                )
            level_bytes_sum = sum(level["bytes"] for level in level_views)
            manifest_segment_bytes = sum(
                segment.size_bytes
                for segments in partition.levels
                for segment in segments
            )
            memtable_bytes = (
                segment_payload_size(
                    partition.partition, partition.memtable_entries
                )
                if partition.memtable_entries
                else 0
            )
            pressure = self._capacity_pressure(partition)
            views.append(
                {
                    "partition": list(partition_key),
                    "levels": level_views,
                    "segment_count": sum(
                        level["segment_count"] for level in level_views
                    ),
                    "segment_entry_count": sum(
                        level["entry_count"] for level in level_views
                    ),
                    "segment_bytes": segment_bytes,
                    "level_bytes_sum": level_bytes_sum,
                    "manifest_segment_bytes": manifest_segment_bytes,
                    "memtable": {
                        "memtable_id": partition.memtable_id,
                        "entry_count": len(partition.memtable_entries),
                        "logical_bytes": memtable_bytes,
                    },
                    "capacity": pressure,
                    "accounting_ok": (
                        segment_bytes == level_bytes_sum
                        == manifest_segment_bytes
                    ),
                }
            )
        return views

    def segment_manifest(
        self, include_entries: bool = False
    ) -> list[dict[str, Any]]:
        return [
            self.live_segments[segment_id].to_dict(
                include_entries=include_entries
            )
            for segment_id in sorted(self.live_segments)
        ]
