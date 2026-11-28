from __future__ import annotations

import heapq
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

from .models import (
    CancellationEvent,
    EdgeConfig,
    Scenario,
    ServiceRequest,
    group_cancellations_by_time,
)


TERMINAL_STATUSES = frozenset({"completed", "cancelled"})
ACTIVE_STATUSES = frozenset({"prefill", "decode", "cancel_pending"})


class SchedulerError(RuntimeError):
    pass


@dataclass
class LifecycleEntry:
    sequence: int
    time: int
    request_id: str
    ticket_id: str
    event: str
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "sequence": self.sequence,
            "time": self.time,
            "request_id": self.request_id,
            "ticket_id": self.ticket_id,
            "event": self.event,
            "details": self.details,
        }


@dataclass
class BlockLedgerEntry:
    sequence: int
    time: int
    block_id: int
    event: str
    request_id: str
    ticket_id: str
    reason: str
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "sequence": self.sequence,
            "time": self.time,
            "block_id": self.block_id,
            "event": self.event,
            "request_id": self.request_id,
            "ticket_id": self.ticket_id,
            "reason": self.reason,
            "details": self.details,
        }


@dataclass
class RequestRuntime:
    request_id: str
    status: str = "scheduled"
    admitted_at: int | None = None
    prefill_started_at: int | None = None
    prefill_done_at: int | None = None
    next_decode_at: int | None = None
    tokens_generated: int = 0
    first_token_at: int | None = None
    last_token_at: int | None = None
    blocks: list[int] = field(default_factory=list)
    prefill_blocks_allocated: int = 0
    decode_blocks_allocated: int = 0
    cancel_effective_at: int | None = None
    cancel_reason: str | None = None
    cancel_stage: str | None = None
    cancel_event_id: str | None = None
    cancel_sequence: int | None = None
    cancel_record_index: int | None = None
    would_complete_at: int | None = None
    reclaimed_at: int | None = None
    terminal_at: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "status": self.status,
            "admitted_at": self.admitted_at,
            "prefill_started_at": self.prefill_started_at,
            "prefill_done_at": self.prefill_done_at,
            "next_decode_at": self.next_decode_at,
            "tokens_generated": self.tokens_generated,
            "first_token_at": self.first_token_at,
            "last_token_at": self.last_token_at,
            "blocks": list(self.blocks),
            "prefill_blocks_allocated": self.prefill_blocks_allocated,
            "decode_blocks_allocated": self.decode_blocks_allocated,
            "cancel_effective_at": self.cancel_effective_at,
            "cancel_reason": self.cancel_reason,
            "cancel_stage": self.cancel_stage,
            "cancel_event_id": self.cancel_event_id,
            "cancel_sequence": self.cancel_sequence,
            "cancel_record_index": self.cancel_record_index,
            "would_complete_at": self.would_complete_at,
            "reclaimed_at": self.reclaimed_at,
            "terminal_at": self.terminal_at,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "RequestRuntime":
        return cls(
            request_id=str(data["request_id"]),
            status=str(data["status"]),
            admitted_at=data.get("admitted_at"),
            prefill_started_at=data.get("prefill_started_at"),
            prefill_done_at=data.get("prefill_done_at"),
            next_decode_at=data.get("next_decode_at"),
            tokens_generated=int(data.get("tokens_generated", 0)),
            first_token_at=data.get("first_token_at"),
            last_token_at=data.get("last_token_at"),
            blocks=[int(value) for value in data.get("blocks", ())],
            prefill_blocks_allocated=int(data.get("prefill_blocks_allocated", 0)),
            decode_blocks_allocated=int(data.get("decode_blocks_allocated", 0)),
            cancel_effective_at=data.get("cancel_effective_at"),
            cancel_reason=data.get("cancel_reason"),
            cancel_stage=data.get("cancel_stage"),
            cancel_event_id=data.get("cancel_event_id"),
            cancel_sequence=data.get("cancel_sequence"),
            cancel_record_index=data.get("cancel_record_index"),
            would_complete_at=data.get("would_complete_at"),
            reclaimed_at=data.get("reclaimed_at"),
            terminal_at=data.get("terminal_at"),
        )


@dataclass
class BlockRecord:
    block_id: int
    owner_request_id: str | None = None
    owner_ticket_id: str | None = None
    allocated_at: int | None = None
    allocation_source: dict[str, Any] | None = None
    last_owner_request_id: str | None = None
    last_owner_ticket_id: str | None = None
    last_released_at: int | None = None
    last_release_reason: str | None = None
    allocation_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "block_id": self.block_id,
            "owner_request_id": self.owner_request_id,
            "owner_ticket_id": self.owner_ticket_id,
            "allocated_at": self.allocated_at,
            "allocation_source": self.allocation_source,
            "last_owner_request_id": self.last_owner_request_id,
            "last_owner_ticket_id": self.last_owner_ticket_id,
            "last_released_at": self.last_released_at,
            "last_release_reason": self.last_release_reason,
            "allocation_count": self.allocation_count,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "BlockRecord":
        return cls(
            block_id=int(data["block_id"]),
            owner_request_id=data.get("owner_request_id"),
            owner_ticket_id=data.get("owner_ticket_id"),
            allocated_at=data.get("allocated_at"),
            allocation_source=data.get("allocation_source"),
            last_owner_request_id=data.get("last_owner_request_id"),
            last_owner_ticket_id=data.get("last_owner_ticket_id"),
            last_released_at=data.get("last_released_at"),
            last_release_reason=data.get("last_release_reason"),
            allocation_count=int(data.get("allocation_count", 0)),
        )


class Scheduler:
    """Deterministic edge KV-cache batch scheduler."""

    CHECKPOINT_SCHEMA_VERSION = 1

    def __init__(
        self,
        config: EdgeConfig,
        requests: Iterable[ServiceRequest],
        cancellations: Iterable[CancellationEvent] = (),
    ) -> None:
        config.validate()
        self.config = config
        self.requests = tuple(
            sorted(requests, key=lambda item: (item.arrival_time, item.arrival_seq))
        )
        self.cancellations = tuple(
            sorted(cancellations, key=lambda item: item.order_key)
        )
        self.request_by_id = {request.request_id: request for request in self.requests}
        if len(self.request_by_id) != len(self.requests):
            raise SchedulerError("request ids must be unique")
        if len({event.event_id for event in self.cancellations}) != len(
            self.cancellations
        ):
            raise SchedulerError("cancellation event ids must be unique")
        for event in self.cancellations:
            if event.request_id not in self.request_by_id:
                raise SchedulerError(
                    f"cancellation {event.event_id} refers to unknown request"
                )
        self._arrivals_by_time: dict[int, list[ServiceRequest]] = {}
        for request in self.requests:
            self._arrivals_by_time.setdefault(request.arrival_time, []).append(request)
        for bucket in self._arrivals_by_time.values():
            bucket.sort(key=lambda item: item.arrival_seq)
        self._cancellations_by_time = group_cancellations_by_time(self.cancellations)

        self.current_time = -1
        self.runtimes = {
            request.request_id: RequestRuntime(request_id=request.request_id)
            for request in self.requests
        }
        self.queue: list[str] = []
        self.admission_order: list[str] = []
        self.free_blocks: list[int] = list(range(config.total_blocks))
        heapq.heapify(self.free_blocks)
        self.block_records = {
            block_id: BlockRecord(block_id=block_id)
            for block_id in range(config.total_blocks)
        }
        self.lifecycle: list[LifecycleEntry] = []
        self.block_ledger: list[BlockLedgerEntry] = []
        self.token_events: list[dict[str, Any]] = []
        self.memory_samples: list[dict[str, Any]] = []
        self.cancellation_records: list[dict[str, Any]] = []
        self.competition_records: list[dict[str, Any]] = []
        self.ignored_cancellations: list[dict[str, Any]] = []
        self.reuse_links: list[dict[str, Any]] = []
        self.processed_cancel_ids: set[str] = set()
        self.internal_issues: list[dict[str, Any]] = []
        self._lifecycle_seq = 0
        self._block_seq = 0
        self._last_hard_stop_reason: str | None = None

    @property
    def used_blocks(self) -> int:
        return sum(
            1
            for record in self.block_records.values()
            if record.owner_request_id is not None
        )

    @property
    def free_count(self) -> int:
        return len(self.free_blocks)

    @property
    def used_bytes(self) -> int:
        return self.used_blocks * self.config.block_bytes

    @property
    def active_count(self) -> int:
        return sum(
            1 for runtime in self.runtimes.values() if runtime.status in ACTIVE_STATUSES
        )

    @property
    def all_terminal(self) -> bool:
        return all(
            runtime.status in TERMINAL_STATUSES for runtime in self.runtimes.values()
        )

    def _append_lifecycle(
        self,
        time: int,
        runtime: RequestRuntime,
        event: str,
        **details: Any,
    ) -> None:
        self._lifecycle_seq += 1
        request = self.request_by_id[runtime.request_id]
        self.lifecycle.append(
            LifecycleEntry(
                sequence=self._lifecycle_seq,
                time=time,
                request_id=runtime.request_id,
                ticket_id=request.ticket_id,
                event=event,
                details=details,
            )
        )

    def _append_block_event(
        self,
        time: int,
        block_id: int,
        event: str,
        request: ServiceRequest,
        reason: str,
        **details: Any,
    ) -> None:
        self._block_seq += 1
        self.block_ledger.append(
            BlockLedgerEntry(
                sequence=self._block_seq,
                time=time,
                block_id=block_id,
                event=event,
                request_id=request.request_id,
                ticket_id=request.ticket_id,
                reason=reason,
                details=details,
            )
        )

    def _can_allocate(self, count: int) -> bool:
        if count <= 0:
            return True
        if len(self.free_blocks) < count:
            return False
        projected = (self.used_blocks + count) * self.config.block_bytes
        return projected <= self.config.vram_budget_bytes

    def _allocate_blocks(
        self,
        runtime: RequestRuntime,
        count: int,
        time: int,
        reason: str,
    ) -> list[dict[str, Any]]:
        if count <= 0:
            return []
        request = self.request_by_id[runtime.request_id]
        if not self._can_allocate(count):
            return []
        allocated: list[dict[str, Any]] = []
        for _ in range(count):
            block_id = heapq.heappop(self.free_blocks)
            record = self.block_records[block_id]
            if record.owner_request_id is not None:
                self._record_issue(
                    "duplicate_allocation",
                    time,
                    block_id,
                    runtime.request_id,
                    request.ticket_id,
                    "block was already owned when popped from free list",
                )
                continue
            source = None
            if record.last_owner_request_id is not None:
                source = {
                    "request_id": record.last_owner_request_id,
                    "ticket_id": record.last_owner_ticket_id,
                    "released_at": record.last_released_at,
                    "release_reason": record.last_release_reason,
                    "was_cancelled": record.last_release_reason == "cancelled",
                }
            record.owner_request_id = request.request_id
            record.owner_ticket_id = request.ticket_id
            record.allocated_at = time
            record.allocation_source = source
            record.allocation_count += 1
            runtime.blocks.append(block_id)
            details: dict[str, Any] = {}
            if source is not None:
                details["reused_from_request_id"] = source["request_id"]
                details["reused_from_ticket_id"] = source["ticket_id"]
                details["reused_from_release_reason"] = source["release_reason"]
                link = {
                    "time": time,
                    "block_id": block_id,
                    "to_request_id": request.request_id,
                    "to_ticket_id": request.ticket_id,
                    "from_request_id": source["request_id"],
                    "from_ticket_id": source["ticket_id"],
                    "from_release_reason": source["release_reason"],
                    "from_was_cancelled": source["was_cancelled"],
                }
                self.reuse_links.append(link)
            self._append_block_event(
                time, block_id, "allocate", request, reason, **details
            )
            allocated.append({"block_id": block_id, "source": source})
        return allocated

    def _release_blocks(
        self,
        runtime: RequestRuntime,
        time: int,
        reason: str,
    ) -> list[int]:
        request = self.request_by_id[runtime.request_id]
        released = list(runtime.blocks)
        for block_id in sorted(released):
            record = self.block_records[block_id]
            if record.owner_request_id != request.request_id:
                self._record_issue(
                    "invalid_release",
                    time,
                    block_id,
                    request.request_id,
                    request.ticket_id,
                    f"expected owner {request.request_id}, found {record.owner_request_id}",
                )
                continue
            record.owner_request_id = None
            record.owner_ticket_id = None
            record.last_owner_request_id = request.request_id
            record.last_owner_ticket_id = request.ticket_id
            record.last_released_at = time
            record.last_release_reason = reason
            record.allocated_at = None
            record.allocation_source = None
            heapq.heappush(self.free_blocks, block_id)
            self._append_block_event(time, block_id, "release", request, reason)
        runtime.blocks = []
        return released

    def _record_issue(
        self,
        kind: str,
        time: int,
        block_id: int | None,
        request_id: str | None,
        ticket_id: str | None,
        message: str,
    ) -> None:
        self.internal_issues.append(
            {
                "kind": kind,
                "time": time,
                "block_id": block_id,
                "request_id": request_id,
                "ticket_id": ticket_id,
                "message": message,
            }
        )

    def _invariant_issue(
        self,
        time: int,
        phase: str,
    ) -> None:
        if self.used_blocks + self.free_count != self.config.total_blocks:
            self._record_issue(
                "block_conservation",
                time,
                None,
                None,
                None,
                f"after {phase}: used={self.used_blocks}, free={self.free_count}, "
                f"total={self.config.total_blocks}",
            )
        if self.used_bytes > self.config.vram_budget_bytes:
            self._record_issue(
                "vram_budget",
                time,
                None,
                None,
                None,
                f"after {phase}: used_bytes={self.used_bytes} exceeds "
                f"budget={self.config.vram_budget_bytes}",
            )
        if len(set(self.free_blocks)) != len(self.free_blocks):
            self._record_issue(
                "duplicate_free_entry",
                time,
                None,
                None,
                None,
                f"after {phase}: free list contains duplicate block ids",
            )
        for runtime in self.runtimes.values():
            if runtime.status in TERMINAL_STATUSES and runtime.blocks:
                self._record_issue(
                    "terminal_request_holds_blocks",
                    time,
                    runtime.blocks[0],
                    runtime.request_id,
                    self.request_by_id[runtime.request_id].ticket_id,
                    f"terminal request still owns {len(runtime.blocks)} blocks",
                )

    def _next_step_boundary(self, value: int) -> int:
        step = self.config.decode_step_ticks
        return ((value + step - 1) // step) * step

    def _next_boundary_after(self, value: int) -> int:
        step = self.config.decode_step_ticks
        return ((value // step) + 1) * step

    def _next_token_extra_blocks(self, runtime: RequestRuntime) -> int:
        request = self.request_by_id[runtime.request_id]
        required = request.blocks_after_tokens(
            self.config.block_size_tokens, runtime.tokens_generated + 1
        )
        return max(0, required - len(runtime.blocks))

    def _would_complete_at(self, runtime: RequestRuntime, time: int) -> bool:
        if runtime.status != "decode":
            return False
        if time % self.config.decode_step_ticks != 0:
            return False
        if runtime.next_decode_at is None or runtime.next_decode_at > time:
            return False
        request = self.request_by_id[runtime.request_id]
        if runtime.tokens_generated + 1 < request.max_new_tokens:
            return False
        extra = self._next_token_extra_blocks(runtime)
        return self._can_allocate(extra)

    def _handle_arrivals(self, time: int) -> None:
        for request in self._arrivals_by_time.get(time, ()):
            runtime = self.runtimes[request.request_id]
            if runtime.status != "scheduled":
                continue
            runtime.status = "queued"
            self.queue.append(request.request_id)
            self._append_lifecycle(
                time,
                runtime,
                "arrive",
                room_no=request.room_no,
                ticket_id=request.ticket_id,
                max_new_tokens=request.max_new_tokens,
                prompt_tokens=request.prompt_token_count,
                channel=request.channel,
            )

    def _admit_pending(self, time: int) -> None:
        while self.queue and self.active_count < self.config.max_batch_requests:
            request_id = self.queue[0]
            runtime = self.runtimes[request_id]
            if runtime.status != "queued":
                self.queue.pop(0)
                continue
            request = self.request_by_id[request_id]
            initial_blocks = request.initial_blocks(self.config.block_size_tokens)
            if not self._can_allocate(initial_blocks):
                break
            self.queue.pop(0)
            runtime.status = "prefill"
            runtime.admitted_at = time
            runtime.prefill_started_at = time
            runtime.prefill_done_at = time + self.config.prefill_duration_ticks
            self.admission_order.append(request_id)
            allocated = self._allocate_blocks(runtime, initial_blocks, time, "prefill")
            runtime.prefill_blocks_allocated = len(allocated)
            self._append_lifecycle(
                time,
                runtime,
                "slot_acquired",
                batch_slot=len(self.admission_order),
            )
            self._append_lifecycle(
                time,
                runtime,
                "cache_acquired",
                phase="prefill",
                blocks=[item["block_id"] for item in allocated],
                block_count=len(allocated),
                bytes=len(allocated) * self.config.block_bytes,
                reuse=[
                    {
                        "block_id": item["block_id"],
                        "from_request_id": item["source"]["request_id"],
                        "from_ticket_id": item["source"]["ticket_id"],
                        "release_reason": item["source"]["release_reason"],
                    }
                    for item in allocated
                    if item["source"] is not None
                ],
            )
            self._append_lifecycle(
                time,
                runtime,
                "prefill_started",
                expected_done_at=runtime.prefill_done_at,
            )

    def _record_competition(
        self,
        time: int,
        runtime: RequestRuntime,
        selected: CancellationEvent,
        group: list[CancellationEvent],
        would_complete: bool,
        stage: str,
    ) -> None:
        types: list[str] = []
        if len(group) > 1:
            types.append("multiple_cancellations")
        if would_complete:
            types.append("cancel_vs_completion")
        if not types:
            return
        self.competition_records.append(
            {
                "time": time,
                "request_id": runtime.request_id,
                "ticket_id": self.request_by_id[runtime.request_id].ticket_id,
                "stage": stage,
                "types": types,
                "completion_would_occur_at": time if would_complete else None,
                "selected_event": selected.to_dict(),
                "rejected_events": [
                    event.to_dict()
                    for event in sorted(group, key=lambda item: item.order_key)
                    if event.event_id != selected.event_id
                ],
                "decision": "cancelled",
                "tokens_generated_at_decision": runtime.tokens_generated,
            }
        )

    def _create_cancellation_record(
        self,
        time: int,
        runtime: RequestRuntime,
        selected: CancellationEvent,
        stage: str,
        would_complete: bool,
    ) -> None:
        request = self.request_by_id[runtime.request_id]
        record = {
            "event_id": selected.event_id,
            "request_id": request.request_id,
            "ticket_id": request.ticket_id,
            "room_no": request.room_no,
            "reason": selected.reason,
            "stage": stage,
            "effective_at": time,
            "reclaimed_at": None,
            "latency_ticks": None,
            "reclaimed_blocks": [],
            "reclaimed_block_count": 0,
            "reclaimed_bytes": 0,
            "tokens_generated": runtime.tokens_generated,
            "completion_would_occur_at": time if would_complete else None,
        }
        runtime.cancel_record_index = len(self.cancellation_records)
        self.cancellation_records.append(record)

    def _reclaim_cancelled(self, runtime: RequestRuntime, time: int) -> None:
        if runtime.status != "cancel_pending":
            return
        released = self._release_blocks(runtime, time, "cancelled")
        runtime.reclaimed_at = time
        runtime.terminal_at = time
        runtime.status = "cancelled"
        self._append_lifecycle(
            time,
            runtime,
            "blocks_reclaimed",
            reason="cancelled",
            blocks=released,
            block_count=len(released),
            bytes=len(released) * self.config.block_bytes,
            cancel_effective_at=runtime.cancel_effective_at,
        )
        self._append_lifecycle(
            time,
            runtime,
            "cancelled",
            reason=runtime.cancel_reason,
            stage=runtime.cancel_stage,
            tokens_generated=runtime.tokens_generated,
            cancel_effective_at=runtime.cancel_effective_at,
            reclaimed_at=time,
        )
        if runtime.cancel_record_index is not None:
            record = self.cancellation_records[runtime.cancel_record_index]
            record["reclaimed_at"] = time
            record["latency_ticks"] = time - int(record["effective_at"])
            record["reclaimed_blocks"] = released
            record["reclaimed_block_count"] = len(released)
            record["reclaimed_bytes"] = len(released) * self.config.block_bytes
        self._append_lifecycle(
            time,
            runtime,
            "slot_released",
            reason="cancelled",
        )

    def _handle_cancellations(self, time: int) -> None:
        events = self._cancellations_by_time.get(time, ())
        if not events:
            return
        by_request: dict[str, list[CancellationEvent]] = {}
        for event in events:
            by_request.setdefault(event.request_id, []).append(event)
        ordered_groups = sorted(
            by_request.items(),
            key=lambda item: min(event.order_key for event in item[1]),
        )
        for request_id, group in ordered_groups:
            group = sorted(group, key=lambda item: item.order_key)
            runtime = self.runtimes[request_id]
            for event in group:
                self.processed_cancel_ids.add(event.event_id)
            if runtime.status in TERMINAL_STATUSES:
                for event in group:
                    self.ignored_cancellations.append(
                        {
                            "time": time,
                            "event": event.to_dict(),
                            "status": runtime.status,
                            "reason": "request_already_terminal",
                        }
                    )
                continue
            if runtime.status == "scheduled":
                for event in group:
                    self.ignored_cancellations.append(
                        {
                            "time": time,
                            "event": event.to_dict(),
                            "status": runtime.status,
                            "reason": "before_arrival",
                        }
                    )
                continue
            if runtime.status == "cancel_pending":
                for event in group:
                    self.ignored_cancellations.append(
                        {
                            "time": time,
                            "event": event.to_dict(),
                            "status": runtime.status,
                            "reason": "already_cancel_pending",
                        }
                    )
                continue

            selected = group[0]
            stage = {
                "queued": "queued",
                "prefill": "prefill",
                "decode": "decode",
            }.get(runtime.status, runtime.status)
            would_complete = self._would_complete_at(runtime, time)
            self._record_competition(
                time, runtime, selected, group, would_complete, stage
            )

            runtime.cancel_effective_at = time
            runtime.cancel_reason = selected.reason
            runtime.cancel_stage = stage
            runtime.cancel_event_id = selected.event_id
            runtime.cancel_sequence = selected.sequence
            runtime.would_complete_at = time if would_complete else None
            self._append_lifecycle(
                time,
                runtime,
                "cancel_requested",
                event_id=selected.event_id,
                reason=selected.reason,
                stage=stage,
                tokens_generated=runtime.tokens_generated,
                completion_would_occur_at=time if would_complete else None,
            )
            self._append_lifecycle(
                time,
                runtime,
                "cancel_effective",
                event_id=selected.event_id,
                reason=selected.reason,
                stage=stage,
            )

            if stage == "queued":
                if request_id in self.queue:
                    self.queue.remove(request_id)
                runtime.status = "cancelled"
                runtime.terminal_at = time
                runtime.reclaimed_at = time
                self._create_cancellation_record(
                    time, runtime, selected, stage, would_complete
                )
                if runtime.cancel_record_index is not None:
                    record = self.cancellation_records[runtime.cancel_record_index]
                    record["reclaimed_at"] = time
                    record["latency_ticks"] = 0
                self._append_lifecycle(
                    time,
                    runtime,
                    "cancelled",
                    reason=selected.reason,
                    stage=stage,
                    tokens_generated=0,
                    cancel_effective_at=time,
                    reclaimed_at=time,
                )
            elif stage == "prefill":
                runtime.status = "cancel_pending"
                self._create_cancellation_record(
                    time, runtime, selected, stage, would_complete
                )
                self._reclaim_cancelled(runtime, time)
            else:
                runtime.status = "cancel_pending"
                self._create_cancellation_record(
                    time, runtime, selected, stage, would_complete
                )
                if time % self.config.decode_step_ticks == 0:
                    self._reclaim_cancelled(runtime, time)

    def _reclaim_at_decode_boundaries(self, time: int) -> None:
        if time % self.config.decode_step_ticks != 0:
            return
        for request_id in self.admission_order:
            runtime = self.runtimes[request_id]
            if runtime.status == "cancel_pending":
                self._reclaim_cancelled(runtime, time)

    def _complete_prefills(self, time: int) -> None:
        for request_id in self.admission_order:
            runtime = self.runtimes[request_id]
            if runtime.status != "prefill":
                continue
            if runtime.prefill_done_at is not None and runtime.prefill_done_at <= time:
                runtime.status = "decode"
                runtime.next_decode_at = self._next_step_boundary(
                    max(time, runtime.prefill_done_at)
                )
                self._append_lifecycle(
                    time,
                    runtime,
                    "prefill_completed",
                    blocks=runtime.blocks,
                    block_count=len(runtime.blocks),
                    next_decode_at=runtime.next_decode_at,
                )

    def _decode_step(self, time: int) -> None:
        if time % self.config.decode_step_ticks != 0:
            return
        for request_id in self.admission_order:
            runtime = self.runtimes[request_id]
            if runtime.status != "decode":
                continue
            if runtime.next_decode_at is None or runtime.next_decode_at > time:
                continue
            request = self.request_by_id[request_id]
            extra = self._next_token_extra_blocks(runtime)
            if not self._can_allocate(extra):
                self._append_lifecycle(
                    time,
                    runtime,
                    "decode_stalled",
                    needed_blocks=extra,
                    free_blocks=self.free_count,
                    used_bytes=self.used_bytes,
                    budget=self.config.vram_budget_bytes,
                )
                runtime.next_decode_at = time + self.config.decode_step_ticks
                continue
            allocated = self._allocate_blocks(runtime, extra, time, "decode")
            runtime.decode_blocks_allocated += len(allocated)
            runtime.tokens_generated += 1
            if runtime.first_token_at is None:
                runtime.first_token_at = time
            runtime.last_token_at = time
            self._append_lifecycle(
                time,
                runtime,
                "decode_token",
                token_index=runtime.tokens_generated,
                new_blocks=[item["block_id"] for item in allocated],
                owned_blocks=len(runtime.blocks),
                used_bytes=self.used_bytes,
            )
            self.token_events.append(
                {
                    "time": time,
                    "request_id": request_id,
                    "ticket_id": request.ticket_id,
                    "token_index": runtime.tokens_generated,
                    "allocated_blocks": [
                        item["block_id"] for item in allocated
                    ],
                }
            )
            if runtime.tokens_generated >= request.max_new_tokens:
                runtime.status = "completed"
                runtime.terminal_at = time
                self._append_lifecycle(
                    time,
                    runtime,
                    "completed",
                    tokens_generated=runtime.tokens_generated,
                    max_new_tokens=request.max_new_tokens,
                    blocks=runtime.blocks,
                    block_count=len(runtime.blocks),
                )
                released = self._release_blocks(runtime, time, "completed")
                self._append_lifecycle(
                    time,
                    runtime,
                    "blocks_reclaimed",
                    reason="completed",
                    blocks=released,
                    block_count=len(released),
                    bytes=len(released) * self.config.block_bytes,
                )
                self._append_lifecycle(
                    time,
                    runtime,
                    "slot_released",
                    reason="completed",
                )
            else:
                runtime.next_decode_at = time + self.config.decode_step_ticks

    def _record_memory(self, time: int) -> None:
        owners = [
            {
                "block_id": record.block_id,
                "request_id": record.owner_request_id,
                "ticket_id": record.owner_ticket_id,
            }
            for record in sorted(
                self.block_records.values(), key=lambda item: item.block_id
            )
            if record.owner_request_id is not None
        ]
        sample = {
            "time": time,
            "active_requests": [
                request_id
                for request_id in self.admission_order
                if self.runtimes[request_id].status in ACTIVE_STATUSES
            ],
            "queued_requests": list(self.queue),
            "used_blocks": self.used_blocks,
            "free_blocks": self.free_count,
            "total_blocks": self.config.total_blocks,
            "used_bytes": self.used_bytes,
            "vram_budget_bytes": self.config.vram_budget_bytes,
            "allocatable_free_blocks": max(
                0,
                min(
                    self.free_count,
                    (self.config.vram_budget_bytes - self.used_bytes)
                    // self.config.block_bytes,
                ),
            ),
            "block_owners": owners,
            "conservation_ok": (
                self.used_blocks + self.free_count == self.config.total_blocks
            ),
            "budget_ok": self.used_bytes <= self.config.vram_budget_bytes,
        }
        self.memory_samples.append(sample)

    def _process_time(self, time: int) -> None:
        self.current_time = time
        self._handle_arrivals(time)
        self._admit_pending(time)
        self._handle_cancellations(time)
        self._reclaim_at_decode_boundaries(time)
        self._complete_prefills(time)
        self._decode_step(time)
        self._admit_pending(time)
        self._invariant_issue(time, "tick")
        self._record_memory(time)

    def _next_event_time(self) -> int | None:
        candidates: list[int] = []
        now = self.current_time
        for request in self.requests:
            runtime = self.runtimes[request.request_id]
            if runtime.status == "scheduled" and request.arrival_time > now:
                candidates.append(request.arrival_time)
        for time in self._cancellations_by_time:
            if time > now:
                candidates.append(time)
        for runtime in self.runtimes.values():
            if runtime.status == "prefill" and runtime.prefill_done_at is not None:
                if runtime.prefill_done_at > now:
                    candidates.append(runtime.prefill_done_at)
            elif runtime.status == "decode":
                if runtime.next_decode_at is not None:
                    candidates.append(max(runtime.next_decode_at, now + 1))
            elif runtime.status == "cancel_pending":
                candidates.append(self._next_boundary_after(now))
        return min(candidates) if candidates else None

    def _hard_stop_time(self) -> int:
        if not self.requests:
            return 1
        latest_arrival = max(request.arrival_time for request in self.requests)
        longest_generation = max(request.max_new_tokens for request in self.requests)
        return (
            latest_arrival
            + self.config.prefill_duration_ticks
            + (longest_generation + 3) * self.config.decode_step_ticks
            + self.config.total_blocks * self.config.decode_step_ticks
            + 50
        )

    def run_until(self, end_time: int | None = None) -> None:
        hard_stop = self._hard_stop_time()
        while not self.all_terminal:
            next_time = self._next_event_time()
            if next_time is None:
                self._last_hard_stop_reason = (
                    "no future event exists while requests are non-terminal"
                )
                break
            if end_time is not None and next_time > end_time:
                break
            if next_time > hard_stop:
                self._last_hard_stop_reason = (
                    f"logical time {next_time} exceeded hard stop {hard_stop}"
                )
                break
            self._process_time(next_time)
        if end_time is not None and self.current_time < end_time and not self.all_terminal:
            # The caller may request a checkpoint in an idle interval. Keep the
            # logical clock at the checkpoint boundary without creating events.
            self.current_time = end_time

    def run(self) -> None:
        self.run_until(None)

    def _snapshot_owners(self) -> list[dict[str, Any]]:
        return [
            {
                "block_id": record.block_id,
                "request_id": record.owner_request_id,
                "ticket_id": record.owner_ticket_id,
                "allocated_at": record.allocated_at,
            }
            for record in sorted(
                self.block_records.values(), key=lambda item: item.block_id
            )
            if record.owner_request_id is not None
        ]

    def occupancy_snapshot(self) -> dict[str, Any]:
        active = []
        for request_id in self.admission_order:
            runtime = self.runtimes[request_id]
            if runtime.status not in ACTIVE_STATUSES:
                continue
            active.append(
                {
                    "request_id": request_id,
                    "ticket_id": self.request_by_id[request_id].ticket_id,
                    "status": runtime.status,
                    "tokens_generated": runtime.tokens_generated,
                    "blocks": sorted(runtime.blocks),
                    "next_decode_at": runtime.next_decode_at,
                }
            )
        return {
            "time": self.current_time,
            "used_blocks": self.used_blocks,
            "free_blocks": self.free_count,
            "total_blocks": self.config.total_blocks,
            "free_block_ids": sorted(self.free_blocks),
            "used_bytes": self.used_bytes,
            "vram_budget_bytes": self.config.vram_budget_bytes,
            "queued_request_ids": list(self.queue),
            "active_requests": active,
            "block_owners": self._snapshot_owners(),
            "conservation_ok": (
                self.used_blocks + self.free_count == self.config.total_blocks
            ),
            "budget_ok": self.used_bytes <= self.config.vram_budget_bytes,
        }

    def dense_memory_timeline(self) -> list[dict[str, Any]]:
        if not self.memory_samples:
            return []
        ordered = sorted(self.memory_samples, key=lambda item: item["time"])
        start = min(0, ordered[0]["time"])
        end = max(ordered[-1]["time"], self.current_time)
        result: list[dict[str, Any]] = []
        sample_index = 0
        empty_sample = {
            "time": -1,
            "active_requests": [],
            "queued_requests": [],
            "used_blocks": 0,
            "free_blocks": self.config.total_blocks,
            "total_blocks": self.config.total_blocks,
            "used_bytes": 0,
            "vram_budget_bytes": self.config.vram_budget_bytes,
            "allocatable_free_blocks": self.config.usable_blocks,
            "block_owners": [],
            "conservation_ok": True,
            "budget_ok": True,
        }
        current = empty_sample
        for time in range(start, end + 1):
            while (
                sample_index < len(ordered)
                and ordered[sample_index]["time"] <= time
            ):
                current = ordered[sample_index]
                sample_index += 1
            clone = {
                key: value
                for key, value in current.items()
                if key
                not in {
                    "time",
                    "active_requests",
                    "queued_requests",
                    "block_owners",
                }
            }
            clone["time"] = time
            clone["active_requests"] = list(current["active_requests"])
            clone["queued_requests"] = list(current["queued_requests"])
            clone["block_owners"] = [
                dict(item) for item in current["block_owners"]
            ]
            result.append(clone)
        return result

    def cancellation_summary(self) -> dict[str, Any]:
        records = [dict(item) for item in self.cancellation_records]
        all_latencies = [
            int(record["latency_ticks"])
            for record in records
            if record.get("latency_ticks") is not None
        ]

        def aggregate(subset: list[dict[str, Any]]) -> dict[str, Any]:
            subset = [record for record in subset if record.get("reclaimed_at") is not None]
            latencies = [int(record["latency_ticks"]) for record in subset]
            blocks = sum(int(record["reclaimed_block_count"]) for record in subset)
            bytes_reclaimed = sum(int(record["reclaimed_bytes"]) for record in subset)
            immediate = sum(1 for latency in latencies if latency == 0)
            return {
                "cancelled_requests": len(subset),
                "reclaimed_blocks": blocks,
                "reclaimed_bytes": bytes_reclaimed,
                "latency_ticks": {
                    "min": min(latencies) if latencies else None,
                    "max": max(latencies) if latencies else None,
                    "average": (
                        round(sum(latencies) / len(latencies), 4) if latencies else None
                    ),
                    "total": sum(latencies),
                },
                "immediate_reclaims": immediate,
                "immediate_reclaim_rate": (
                    round(immediate / len(subset), 4) if subset else None
                ),
                "bytes_reclaimed_per_tick": (
                    round(bytes_reclaimed / sum(latencies), 4)
                    if sum(latencies) > 0
                    else None
                ),
            }

        by_reason: dict[str, list[dict[str, Any]]] = {}
        by_stage: dict[str, list[dict[str, Any]]] = {}
        by_reason_stage: dict[str, dict[str, list[dict[str, Any]]]] = {}
        for record in records:
            by_reason.setdefault(record["reason"], []).append(record)
            by_stage.setdefault(record["stage"], []).append(record)
            by_reason_stage.setdefault(record["reason"], {}).setdefault(
                record["stage"], []
            ).append(record)
        return {
            "cancellations": records,
            "overall": aggregate(records),
            "by_reason": {
                reason: aggregate(items) for reason, items in sorted(by_reason.items())
            },
            "by_stage": {
                stage: aggregate(items) for stage, items in sorted(by_stage.items())
            },
            "by_reason_stage": {
                reason: {
                    stage: aggregate(items)
                    for stage, items in sorted(stages.items())
                }
                for reason, stages in sorted(by_reason_stage.items())
            },
            "all_latencies": all_latencies,
        }

    def request_traces(self) -> list[dict[str, Any]]:
        entries_by_request: dict[str, list[dict[str, Any]]] = {}
        for entry in self.lifecycle:
            entries_by_request.setdefault(entry.request_id, []).append(entry.to_dict())
        result = []
        for request in self.requests:
            runtime = self.runtimes[request.request_id]
            result.append(
                {
                    "request_id": request.request_id,
                    "ticket_id": request.ticket_id,
                    "room_no": request.room_no,
                    "service_type": request.service_type,
                    "arrival_time": request.arrival_time,
                    "admitted_at": runtime.admitted_at,
                    "prefill_done_at": runtime.prefill_done_at,
                    "tokens_generated": runtime.tokens_generated,
                    "terminal_status": runtime.status,
                    "terminal_at": runtime.terminal_at,
                    "cancel_reason": runtime.cancel_reason,
                    "cancel_stage": runtime.cancel_stage,
                    "cancel_effective_at": runtime.cancel_effective_at,
                    "reclaimed_at": runtime.reclaimed_at,
                    "trace": entries_by_request.get(request.request_id, []),
                }
            )
        return result

    def result_summary(self) -> dict[str, Any]:
        completed = [
            runtime
            for runtime in self.runtimes.values()
            if runtime.status == "completed"
        ]
        cancelled = [
            runtime
            for runtime in self.runtimes.values()
            if runtime.status == "cancelled"
        ]
        cancelled_reuse = [
            link for link in self.reuse_links if link["from_was_cancelled"]
        ]
        max_sample = max(
            self.memory_samples,
            key=lambda item: item["used_bytes"],
            default={
                "used_blocks": 0,
                "used_bytes": 0,
                "time": None,
            },
        )
        return {
            "status": "ok" if not self.internal_issues else "issues_detected",
            "seed": self.config.seed,
            "current_time": self.current_time,
            "all_terminal": self.all_terminal,
            "requests": {
                "total": len(self.requests),
                "completed": len(completed),
                "cancelled": len(cancelled),
                "pending": len(self.requests)
                - len(completed)
                - len(cancelled),
            },
            "tokens_generated": sum(
                runtime.tokens_generated for runtime in self.runtimes.values()
            ),
            "blocks": {
                "total": self.config.total_blocks,
                "used_at_end": self.used_blocks,
                "free_at_end": self.free_count,
                "reuse_allocations": len(self.reuse_links),
                "reused_blocks_from_cancellations": len(cancelled_reuse),
                "bytes_reused_from_cancellations": (
                    len(cancelled_reuse) * self.config.block_bytes
                ),
            },
            "peak_memory": {
                "time": max_sample.get("time"),
                "used_blocks": max_sample.get("used_blocks", 0),
                "used_bytes": max_sample.get("used_bytes", 0),
                "budget_bytes": self.config.vram_budget_bytes,
            },
            "cancellations": self.cancellation_summary()["overall"],
            "competition_records": len(self.competition_records),
            "ignored_cancellations": len(self.ignored_cancellations),
            "internal_issues": len(self.internal_issues),
            "hard_stop_reason": self._last_hard_stop_reason,
        }

    def to_checkpoint(self) -> dict[str, Any]:
        state = {
            "current_time": self.current_time,
            "runtimes": [
                runtime.to_dict()
                for runtime in sorted(
                    self.runtimes.values(), key=lambda item: item.request_id
                )
            ],
            "queue": list(self.queue),
            "admission_order": list(self.admission_order),
            "free_blocks": sorted(self.free_blocks),
            "block_records": [
                record.to_dict()
                for record in sorted(
                    self.block_records.values(), key=lambda item: item.block_id
                )
            ],
            "lifecycle": [entry.to_dict() for entry in self.lifecycle],
            "block_ledger": [entry.to_dict() for entry in self.block_ledger],
            "token_events": list(self.token_events),
            "memory_samples": list(self.memory_samples),
            "cancellation_records": list(self.cancellation_records),
            "competition_records": list(self.competition_records),
            "ignored_cancellations": list(self.ignored_cancellations),
            "reuse_links": list(self.reuse_links),
            "processed_cancel_ids": sorted(self.processed_cancel_ids),
            "internal_issues": list(self.internal_issues),
            "lifecycle_seq": self._lifecycle_seq,
            "block_seq": self._block_seq,
            "last_hard_stop_reason": self._last_hard_stop_reason,
        }
        return {
            "schema_version": self.CHECKPOINT_SCHEMA_VERSION,
            "config": self.config.to_dict(),
            "requests": [request.to_dict() for request in self.requests],
            "cancellations": [event.to_dict() for event in self.cancellations],
            "state": state,
        }

    @classmethod
    def from_checkpoint(cls, payload: Mapping[str, Any]) -> "Scheduler":
        if int(payload.get("schema_version", 0)) != cls.CHECKPOINT_SCHEMA_VERSION:
            raise SchedulerError("unsupported checkpoint schema version")
        config = EdgeConfig.from_dict(payload["config"])
        requests = tuple(
            ServiceRequest.from_dict(item) for item in payload["requests"]
        )
        cancellations = tuple(
            CancellationEvent.from_dict(item)
            for item in payload.get("cancellations", ())
        )
        scheduler = cls(config, requests, cancellations)
        state = payload["state"]
        scheduler.current_time = int(state["current_time"])
        scheduler.runtimes = {
            item["request_id"]: RequestRuntime.from_dict(item)
            for item in state["runtimes"]
        }
        scheduler.queue = [str(item) for item in state["queue"]]
        scheduler.admission_order = [str(item) for item in state["admission_order"]]
        scheduler.free_blocks = [int(item) for item in state["free_blocks"]]
        heapq.heapify(scheduler.free_blocks)
        scheduler.block_records = {
            int(item["block_id"]): BlockRecord.from_dict(item)
            for item in state["block_records"]
        }
        scheduler.lifecycle = [
            LifecycleEntry(
                sequence=int(item["sequence"]),
                time=int(item["time"]),
                request_id=str(item["request_id"]),
                ticket_id=str(item["ticket_id"]),
                event=str(item["event"]),
                details=dict(item.get("details", {})),
            )
            for item in state["lifecycle"]
        ]
        scheduler.block_ledger = [
            BlockLedgerEntry(
                sequence=int(item["sequence"]),
                time=int(item["time"]),
                block_id=int(item["block_id"]),
                event=str(item["event"]),
                request_id=str(item["request_id"]),
                ticket_id=str(item["ticket_id"]),
                reason=str(item["reason"]),
                details=dict(item.get("details", {})),
            )
            for item in state["block_ledger"]
        ]
        scheduler.token_events = [dict(item) for item in state["token_events"]]
        scheduler.memory_samples = [dict(item) for item in state["memory_samples"]]
        scheduler.cancellation_records = [
            dict(item) for item in state["cancellation_records"]
        ]
        scheduler.competition_records = [
            dict(item) for item in state["competition_records"]
        ]
        scheduler.ignored_cancellations = [
            dict(item) for item in state["ignored_cancellations"]
        ]
        scheduler.reuse_links = [dict(item) for item in state["reuse_links"]]
        scheduler.processed_cancel_ids = {
            str(item) for item in state["processed_cancel_ids"]
        }
        scheduler.internal_issues = [dict(item) for item in state["internal_issues"]]
        scheduler._lifecycle_seq = int(state["lifecycle_seq"])
        scheduler._block_seq = int(state["block_seq"])
        scheduler._last_hard_stop_reason = state.get("last_hard_stop_reason")
        scheduler._validate_restored_state()
        return scheduler

    def _validate_restored_state(self) -> None:
        if set(self.runtimes) != set(self.request_by_id):
            raise SchedulerError("checkpoint runtimes do not match request set")
        if len(self.free_blocks) != len(set(self.free_blocks)):
            raise SchedulerError("checkpoint free block list contains duplicates")
        if self.used_blocks + self.free_count != self.config.total_blocks:
            raise SchedulerError(
                "checkpoint violates block conservation before restore"
            )
        if self.used_bytes > self.config.vram_budget_bytes:
            raise SchedulerError("checkpoint exceeds VRAM budget before restore")
        seen_blocks: set[int] = set()
        for request_id, runtime in self.runtimes.items():
            if runtime.status in TERMINAL_STATUSES and runtime.blocks:
                raise SchedulerError(
                    f"terminal request {request_id} owns blocks in checkpoint"
                )
            for block_id in runtime.blocks:
                if block_id in seen_blocks:
                    raise SchedulerError(
                        f"block {block_id} is owned by multiple requests"
                    )
                seen_blocks.add(block_id)
                record = self.block_records[block_id]
                if record.owner_request_id != request_id:
                    raise SchedulerError(
                        f"block {block_id} owner differs between runtime and ledger"
                    )
        for block_id, record in self.block_records.items():
            if (
                record.owner_request_id is not None
                and block_id not in seen_blocks
            ):
                raise SchedulerError(
                    f"block {block_id} has ledger owner but no runtime owner"
                )
        expected_free = {
            block_id
            for block_id, record in self.block_records.items()
            if record.owner_request_id is None
        }
        if expected_free != set(self.free_blocks):
            raise SchedulerError(
                "checkpoint free list does not match block ownership records"
            )
        if any(request_id not in self.request_by_id for request_id in self.queue):
            raise SchedulerError("checkpoint queue contains an unknown request")


def simulate_scenario(scenario: Scenario) -> Scheduler:
    scenario.validate()
    scheduler = Scheduler(
        scenario.config, scenario.requests, scenario.cancellations
    )
    scheduler.run()
    return scheduler
