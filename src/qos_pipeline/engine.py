"""Discrete-time dual-token-bucket shaping and hierarchical WRR scheduling."""

from __future__ import annotations

import hashlib
from collections import deque
from typing import Any, Callable, Mapping

from .errors import QosError
from .fixed import (
    TOKEN_DENOMINATOR,
    RateConversion,
    add_refill,
    bytes_to_units,
    consume_tokens,
    convert_rate,
    proportional_allocate,
    units_to_bytes_text,
)
from .models import ClassConfig, Sample
from .serialization import digest_value


_MASK64 = (1 << 64) - 1


def splitmix64(value: int) -> int:
    value = (value + 0x9E3779B97F4A7C15) & _MASK64
    value = ((value ^ (value >> 30)) * 0xBF58476D1CE4E5B9) & _MASK64
    value = ((value ^ (value >> 27)) * 0x94D049BB133111EB) & _MASK64
    return (value ^ (value >> 31)) & _MASK64


def _stable_name_word(name: str) -> int:
    return int.from_bytes(hashlib.sha256(name.encode("utf-8")).digest()[:8], "big")


def _scaled_nonnegative_allocate(total: int, values: list[int]) -> list[int]:
    """Allocate total proportionally to non-negative integer claims."""

    if total < 0 or any(value < 0 for value in values):
        raise ValueError("allocation inputs must be non-negative")
    if total == 0 or not values or sum(values) == 0:
        return [0 for _ in values]
    claim_sum = sum(values)
    raw = [total * value for value in values]
    allocated = [value // claim_sum for value in raw]
    remainders = [value % claim_sum for value in raw]
    remaining = total - sum(allocated)
    order = sorted(range(len(values)), key=lambda index: (-remainders[index], index))
    for index in order[:remaining]:
        allocated[index] += 1
    return allocated


class Engine:
    """Pure deterministic engine. Persistence is handled outside this class."""

    def __init__(
        self,
        sample: Sample,
        crash_hook: Callable[[str, int], None] | None = None,
    ) -> None:
        self.sample = sample
        self.class_map = sample.class_map
        self.crash_hook = crash_hook
        self.rate_conversions: dict[tuple[str, str], RateConversion] = {}
        for class_config in sample.classes:
            self.rate_conversions[(class_config.name, "committed_rate")] = convert_rate(
                class_config.cir_bps,
                sample.step_ms,
                class_config.name,
                "committed_rate",
            )
            self.rate_conversions[(class_config.name, "peak_rate")] = convert_rate(
                class_config.pir_bps,
                sample.step_ms,
                class_config.name,
                "peak_rate",
            )
            if class_config.guarantee_bps is not None:
                self.rate_conversions[(class_config.name, "guaranteed_bandwidth")] = (
                    convert_rate(
                        class_config.guarantee_bps,
                        sample.step_ms,
                        class_config.name,
                        "guaranteed_bandwidth",
                    )
                )
        self._name_words = {item.name: _stable_name_word(item.name) for item in sample.classes}

    def initial_state(self) -> dict[str, Any]:
        buckets: dict[str, dict[str, int]] = {}
        queues: dict[str, list[dict[str, int]]] = {}
        for class_config in self.sample.classes:
            buckets[class_config.name] = {
                "committed_tokens_units": bytes_to_units(
                    class_config.committed_burst_bytes
                ),
                "peak_tokens_units": bytes_to_units(class_config.peak_burst_bytes),
                "committed_remainder": 0,
                "peak_remainder": 0,
            }
            queues[class_config.name] = []
        schedule: dict[str, dict[str, Any]] = {}
        for class_config in self.sample.classes:
            if class_config.children:
                schedule[class_config.name] = {
                    "cursor": 0,
                    "carry_units": {child: 0 for child in class_config.children},
                    "poll_counts": {child: 0 for child in class_config.children},
                    "guarantee_remainders": {
                        child: 0 for child in class_config.children
                    },
                }
        counters = {
            "passed_bytes": {item.name: 0 for item in self.sample.classes},
            "dropped_bytes": {item.name: 0 for item in self.sample.classes},
            "passed_events": {item.name: 0 for item in self.sample.classes},
            "dropped_events": {item.name: 0 for item in self.sample.classes},
        }
        return {
            "schema_version": 1,
            "step_index": -1,
            "logical_time_ms": 0,
            "buckets": buckets,
            "queues": queues,
            "schedule": schedule,
            "arrival_cursor": 0,
            "counters": counters,
        }

    def _crash(self, point: str, step_index: int) -> None:
        if self.crash_hook is not None:
            self.crash_hook(point, step_index)

    def _refill(self, state: dict[str, Any]) -> dict[str, dict[str, Any]]:
        report: dict[str, dict[str, Any]] = {}
        for class_config in self.sample.classes:
            name = class_config.name
            bucket = state["buckets"][name]
            committed_capacity = bytes_to_units(class_config.committed_burst_bytes)
            peak_capacity = bytes_to_units(class_config.peak_burst_bytes)
            c_conv = self.rate_conversions[(name, "committed_rate")]
            p_conv = self.rate_conversions[(name, "peak_rate")]
            committed_before = bucket["committed_tokens_units"]
            peak_before = bucket["peak_tokens_units"]
            c_remainder_before = bucket["committed_remainder"]
            p_remainder_before = bucket["peak_remainder"]

            (
                committed_after,
                committed_remainder,
                committed_added,
                committed_overflow,
            ) = add_refill(
                committed_before,
                committed_capacity,
                c_conv.increment_units,
                c_conv.rate_remainder,
                c_remainder_before,
            )
            peak_with_spill = min(
                peak_capacity, peak_before + committed_overflow
            )
            spill_added = peak_with_spill - peak_before
            spill_discarded = committed_overflow - spill_added
            peak_after, peak_remainder, peak_added, peak_overflow = add_refill(
                peak_with_spill,
                peak_capacity,
                p_conv.increment_units,
                p_conv.rate_remainder,
                p_remainder_before,
            )
            bucket["committed_tokens_units"] = committed_after
            bucket["peak_tokens_units"] = peak_after
            bucket["committed_remainder"] = committed_remainder
            bucket["peak_remainder"] = peak_remainder
            report[name] = {
                "committed_before_units": committed_before,
                "peak_before_units": peak_before,
                "committed_added_units": committed_added,
                "committed_overflow_units": committed_overflow,
                "committed_remainder_before": c_remainder_before,
                "committed_remainder_after": committed_remainder,
                "peak_before_spill_units": peak_before,
                "peak_spill_added_units": spill_added,
                "peak_spill_discarded_units": spill_discarded,
                "peak_added_units": peak_added,
                "peak_overflow_units": peak_overflow,
                "peak_remainder_before": p_remainder_before,
                "peak_remainder_after": peak_remainder,
                "committed_after_units": committed_after,
                "peak_after_units": peak_after,
            }
        return report

    def _queue_bytes(self, state: Mapping[str, Any], class_name: str) -> int:
        return sum(item["bytes"] for item in state["queues"][class_name])

    def _wred_probability(
        self, class_config: ClassConfig, queue_bytes: int
    ) -> int:
        wred = class_config.wred
        if wred is None or queue_bytes < wred.min_bytes:
            return 0
        if queue_bytes >= wred.max_bytes:
            return wred.max_probability_ppm
        span = wred.max_bytes - wred.min_bytes
        return (
            wred.max_probability_ppm * (queue_bytes - wred.min_bytes) // span
        )

    def _admission_decision(
        self,
        state: dict[str, Any],
        event: Any,
        step_index: int,
    ) -> tuple[bool, dict[str, Any]]:
        class_config = self.class_map[event.resolved_class]
        queue_bytes = self._queue_bytes(state, class_config.name)
        full = queue_bytes + event.bytes > class_config.queue_depth_bytes
        p_ppm = 0
        u_ppm: int | None = None
        candidate = False
        basis: str
        if class_config.drop_policy == "tail":
            drop = full
            candidate = full
            basis = "tail_queue_full" if full else "tail_queue_space"
        else:
            p_ppm = self._wred_probability(class_config, queue_bytes)
            candidate = full or p_ppm > 0
            if full:
                drop = True
                basis = "wred_forced_queue_overflow"
            else:
                seed_value = (
                    self.sample.seed
                    ^ self._name_words[class_config.name]
                    ^ (event.seq * 0x9E3779B97F4A7C15)
                    ^ step_index
                )
                u_ppm = splitmix64(seed_value) % 1_000_000
                drop = candidate and u_ppm < p_ppm
                if candidate:
                    basis = (
                        "wred_candidate_selected"
                        if drop
                        else "wred_candidate_not_selected"
                    )
                else:
                    basis = "wred_below_min_threshold"
        decision = {
            "seq": event.seq,
            "class": class_config.name,
            "bytes": event.bytes,
            "arrival_ms": event.arrival_ms,
            "decision": "dropped" if drop else "queued",
            "basis": basis,
            "queue_before_bytes": queue_bytes,
            "queue_after_bytes": queue_bytes if drop else queue_bytes + event.bytes,
            "queue_capacity_bytes": class_config.queue_depth_bytes,
            "drop_candidate": candidate,
            "wred_probability_ppm": p_ppm,
            "wred_random_ppm": u_ppm,
        }
        return drop, decision

    def _process_arrivals(
        self, state: dict[str, Any], step_index: int
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        decisions: list[dict[str, Any]] = []
        drops: list[dict[str, Any]] = []
        cursor = state["arrival_cursor"]
        schedule = self.sample.event_schedule
        while cursor < len(schedule) and schedule[cursor].due_step <= step_index:
            event = schedule[cursor]
            if event.due_step < step_index:
                raise QosError(
                    "E_CONFLICT",
                    "arrival cursor is behind the committed logical clock",
                    {"event_seq": event.seq, "due_step": event.due_step, "step": step_index},
                )
            drop, decision = self._admission_decision(state, event, step_index)
            decisions.append(decision)
            if drop:
                drops.append(decision)
                state["counters"]["dropped_bytes"][event.resolved_class] += event.bytes
                state["counters"]["dropped_events"][event.resolved_class] += 1
            else:
                state["queues"][event.resolved_class].append(
                    {
                        "seq": event.seq,
                        "bytes": event.bytes,
                        "arrival_ms": event.arrival_ms,
                    }
                )
            cursor += 1
        state["arrival_cursor"] = cursor
        return decisions, drops

    def _consumed_bucket(
        self, state: dict[str, Any], class_name: str, units: int
    ) -> tuple[int, int]:
        bucket = state["buckets"][class_name]
        result = consume_tokens(
            bucket["committed_tokens_units"], bucket["peak_tokens_units"], units
        )
        if result is None:
            raise QosError(
                "E_CONFLICT",
                f"attempted to overdraw tokens for class {class_name}",
                {"class": class_name, "requested_units": units},
            )
        bucket["committed_tokens_units"], bucket["peak_tokens_units"] = result
        return result

    def _schedule_node(
        self,
        state: dict[str, Any],
        class_name: str,
        budget_units: int,
        step_index: int,
        pass_records: list[dict[str, Any]],
        scheduler_reports: dict[str, Any],
    ) -> tuple[int, list[dict[str, Any]]]:
        class_config = self.class_map[class_name]
        bucket = state["buckets"][class_name]
        available_tokens = (
            bucket["committed_tokens_units"] + bucket["peak_tokens_units"]
        )
        available = max(0, min(budget_units, available_tokens))
        if not class_config.children:
            return self._schedule_leaf(
                state,
                class_config,
                available,
                step_index,
                pass_records,
            )

        children = list(class_config.children)
        schedule_state = state["schedule"][class_name]
        carry_state: dict[str, int] = schedule_state["carry_units"]
        carry_caps = {
            child: bytes_to_units(
                max(
                    self.class_map[child].committed_burst_bytes,
                    self.class_map[child].queue_depth_bytes,
                )
            )
            for child in children
        }
        carry_before = {
            child: min(carry_state.get(child, 0), carry_caps[child]) for child in children
        }
        carry_claims = {
            child: (
                carry_before[child]
                if state["queues"][child]
                else 0
            )
            for child in children
        }
        claim_values = [carry_claims[child] for child in children]
        if sum(claim_values) > available:
            carry_alloc = _scaled_nonnegative_allocate(available, claim_values)
        else:
            carry_alloc = claim_values
        remaining = available - sum(carry_alloc)

        guarantee_units: dict[str, int] = {}
        guarantee_remainders = schedule_state.setdefault(
            "guarantee_remainders", {child: 0 for child in children}
        )
        for child in children:
            conversion = self.rate_conversions[(child, "guaranteed_bandwidth")]
            carried = (
                guarantee_remainders.get(child, 0) + conversion.rate_remainder
            )
            extra, new_remainder = divmod(carried, TOKEN_DENOMINATOR)
            guarantee_remainders[child] = new_remainder
            guarantee_units[child] = conversion.increment_units + extra
        guarantee_total = sum(guarantee_units.values())
        if guarantee_total <= remaining:
            base = [guarantee_units[child] for child in children]
            extra = remaining - guarantee_total
            weights = [self.class_map[child].weight for child in children]
            weighted = proportional_allocate(extra, weights)
            base = [base[index] + weighted[index] for index in range(len(children))]
        else:
            base = _scaled_nonnegative_allocate(
                remaining, [guarantee_units[child] for child in children]
            )
        quotas = {
            child: carry_alloc[index] + base[index]
            for index, child in enumerate(children)
        }

        cursor = int(schedule_state.get("cursor", 0)) % len(children)
        budget_left = available
        sent_total = 0
        child_reports: dict[str, Any] = {}
        for offset in range(len(children)):
            index = (cursor + offset) % len(children)
            child = children[index]
            quota = min(quotas[child], budget_left)
            child_sent, child_reasons = self._schedule_node(
                state,
                child,
                quota,
                step_index,
                pass_records,
                scheduler_reports,
            )
            if child_sent > budget_left:
                raise QosError(
                    "E_CONFLICT",
                    "child sent more than the parent step budget",
                    {"parent": class_name, "child": child, "sent_units": child_sent},
                )
            if child_sent:
                self._consumed_bucket(state, class_name, child_sent)
            budget_left -= child_sent
            sent_total += child_sent
            polls = 1 if quotas[child] > 0 else 0
            schedule_state["poll_counts"][child] = (
                schedule_state["poll_counts"].get(child, 0) + polls
            )
            unused = max(0, quotas[child] - child_sent)
            unclaimed_carry = max(0, carry_before[child] - carry_alloc[index])
            carry_out = min(carry_caps[child], unclaimed_carry + unused)
            carry_state[child] = carry_out
            reasons = list(child_reasons)
            if child_sent == 0 and quota == 0:
                reasons.append(
                    {
                        "reason": "parent_budget_exhausted",
                        "event_seq": (
                            state["queues"][child][0]["seq"]
                            if state["queues"][child]
                            else None
                        ),
                    }
                )
            child_reports[child] = {
                "quota_units": quotas[child],
                "dynamic_budget_units": quota,
                "sent_units": child_sent,
                "carry_in_units": carry_alloc[index],
                "carry_out_units": carry_out,
                "poll_count_step": polls,
                "poll_count_total": schedule_state["poll_counts"][child],
                "reasons": reasons,
            }
        schedule_state["cursor"] = (cursor + 1) % len(children)
        report = {
            "budget_units": budget_units,
            "available_units": available,
            "allocated_units": sum(quotas.values()),
            "sent_units": sent_total,
            "unused_units": available - sent_total,
            "guarantee_remainders": dict(guarantee_remainders),
            "children": child_reports,
        }
        scheduler_reports[class_name] = report
        return sent_total, []

    def _schedule_leaf(
        self,
        state: dict[str, Any],
        class_config: ClassConfig,
        budget_units: int,
        step_index: int,
        pass_records: list[dict[str, Any]],
    ) -> tuple[int, list[dict[str, Any]]]:
        queue: deque[dict[str, int]] = deque(state["queues"][class_config.name])
        sent = 0
        reasons: list[dict[str, Any]] = []
        bucket = state["buckets"][class_config.name]
        while queue:
            head = queue[0]
            request_units = bytes_to_units(head["bytes"])
            if request_units > budget_units:
                reasons.append(
                    {"reason": "quota_insufficient", "event_seq": head["seq"]}
                )
                break
            tokens = (
                bucket["committed_tokens_units"] + bucket["peak_tokens_units"]
            )
            if request_units > tokens:
                reason = (
                    "packet_exceeds_bucket_capacity"
                    if request_units
                    > bytes_to_units(
                        class_config.committed_burst_bytes
                        + class_config.peak_burst_bytes
                    )
                    else "child_tokens_insufficient"
                )
                reasons.append({"reason": reason, "event_seq": head["seq"]})
                break
            self._consumed_bucket(state, class_config.name, request_units)
            queue.popleft()
            budget_units -= request_units
            sent += request_units
            pass_time = step_index * self.sample.step_ms
            wait_ms = pass_time - head["arrival_ms"]
            pass_records.append(
                {
                    "seq": head["seq"],
                    "class": class_config.name,
                    "bytes": head["bytes"],
                    "arrival_ms": head["arrival_ms"],
                    "pass_ms": pass_time,
                    "wait_ms": wait_ms,
                    "path": list(self.sample.path(class_config.name)),
                }
            )
            state["counters"]["passed_bytes"][class_config.name] += head["bytes"]
            state["counters"]["passed_events"][class_config.name] += 1
        state["queues"][class_config.name] = list(queue)
        if not reasons and not queue:
            reasons.append(
                {
                    "reason": "queue_drained" if sent else "queue_empty",
                    "event_seq": None,
                }
            )
        return sent, reasons

    def _scheduler_public(
        self, reports: Mapping[str, Any], state: Mapping[str, Any]
    ) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for parent in sorted(reports):
            report = reports[parent]
            children: dict[str, Any] = {}
            for child in sorted(report["children"]):
                item = report["children"][child]
                children[child] = {
                    "quota_bytes": units_to_bytes_text(item["quota_units"]),
                    "dynamic_budget_bytes": units_to_bytes_text(
                        item["dynamic_budget_units"]
                    ),
                    "sent_bytes": units_to_bytes_text(item["sent_units"]),
                    "carry_in_bytes": units_to_bytes_text(item["carry_in_units"]),
                    "carry_out_bytes": units_to_bytes_text(item["carry_out_units"]),
                    "poll_count_step": item["poll_count_step"],
                    "poll_count_total": item["poll_count_total"],
                    "reasons": item["reasons"],
                }
            result[parent] = {
                "budget_bytes": units_to_bytes_text(report["budget_units"]),
                "available_bytes": units_to_bytes_text(report["available_units"]),
                "allocated_bytes": units_to_bytes_text(report["allocated_units"]),
                "sent_bytes": units_to_bytes_text(report["sent_units"]),
                "unused_bytes": units_to_bytes_text(report["unused_units"]),
                "guarantee_remainders": report["guarantee_remainders"],
                "children": children,
            }
        return result

    def simulate_step(
        self, state: dict[str, Any], step_index: int
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        if state["step_index"] != step_index - 1:
            raise QosError(
                "E_CONFLICT",
                "state watermark does not precede the requested step",
                {
                    "state_step": state["step_index"],
                    "requested_step": step_index,
                },
            )
        if not 0 <= step_index < self.sample.total_steps:
            raise QosError(
                "E_USAGE",
                "step index is outside the sample horizon",
                {"step": step_index, "total_steps": self.sample.total_steps},
            )
        logical_time_ms = step_index * self.sample.step_ms
        refill_report = self._refill(state)
        self._crash("after_refill", step_index)
        arrivals, drops = self._process_arrivals(state, step_index)
        pass_records: list[dict[str, Any]] = []
        scheduler_reports: dict[str, Any] = {}
        for root in self.sample.roots:
            bucket = state["buckets"][root]
            total_tokens = (
                bucket["committed_tokens_units"] + bucket["peak_tokens_units"]
            )
            self._schedule_node(
                state,
                root,
                total_tokens,
                step_index,
                pass_records,
                scheduler_reports,
            )
        self._crash("after_schedule", step_index)

        class_records: dict[str, Any] = {}
        for class_config in self.sample.classes:
            name = class_config.name
            bucket = state["buckets"][name]
            queue_items = state["queues"][name]
            queue_bytes = sum(item["bytes"] for item in queue_items)
            passed_bytes = sum(
                item["bytes"] for item in pass_records if item["class"] == name
            )
            dropped_bytes = sum(
                item["bytes"] for item in drops if item["class"] == name
            )
            class_records[name] = {
                "tokens": {
                    "committed_units": bucket["committed_tokens_units"],
                    "peak_units": bucket["peak_tokens_units"],
                    "total_units": bucket["committed_tokens_units"]
                    + bucket["peak_tokens_units"],
                    "committed_bytes": units_to_bytes_text(
                        bucket["committed_tokens_units"]
                    ),
                    "peak_bytes": units_to_bytes_text(bucket["peak_tokens_units"]),
                    "total_bytes": units_to_bytes_text(
                        bucket["committed_tokens_units"]
                        + bucket["peak_tokens_units"]
                    ),
                },
                "remainders": {
                    "committed": bucket["committed_remainder"],
                    "peak": bucket["peak_remainder"],
                    "denominator": TOKEN_DENOMINATOR,
                },
                "refill": {
                    "committed_added_bytes": units_to_bytes_text(
                        refill_report[name]["committed_added_units"]
                    ),
                    "peak_added_bytes": units_to_bytes_text(
                        refill_report[name]["peak_added_units"]
                        + refill_report[name]["peak_spill_added_units"]
                    ),
                    "committed_remainder_after": refill_report[name][
                        "committed_remainder_after"
                    ],
                    "peak_remainder_after": refill_report[name][
                        "peak_remainder_after"
                    ],
                },
                "tokens_after_refill": {
                    "committed_units": refill_report[name]["committed_after_units"],
                    "peak_units": refill_report[name]["peak_after_units"],
                    "total_bytes": units_to_bytes_text(
                        refill_report[name]["committed_after_units"]
                        + refill_report[name]["peak_after_units"]
                    ),
                },
                "queue": {
                    "occupancy_bytes": queue_bytes,
                    "event_count": len(queue_items),
                    "capacity_bytes": class_config.queue_depth_bytes,
                    "digest": digest_value(queue_items),
                },
                "passed_bytes": passed_bytes,
                "waiting_bytes": queue_bytes,
                "shaping_wait_bytes": queue_bytes,
                "dropped_bytes": dropped_bytes,
                "error_bound_bytes": units_to_bytes_text(2),
            }

        arrivals_by_class: dict[str, dict[str, int]] = {
            item.name: {"events": 0, "bytes": 0} for item in self.sample.classes
        }
        for item in arrivals:
            arrivals_by_class[item["class"]]["events"] += 1
            arrivals_by_class[item["class"]]["bytes"] += item["bytes"]

        waiting_records: list[dict[str, Any]] = []
        for class_config in self.sample.classes:
            for item in state["queues"][class_config.name]:
                waiting_records.append(
                    {
                        "seq": item["seq"],
                        "class": class_config.name,
                        "bytes": item["bytes"],
                        "arrival_ms": item["arrival_ms"],
                        "wait_so_far_ms": logical_time_ms - item["arrival_ms"],
                    }
                )

        state["step_index"] = step_index
        state["logical_time_ms"] = (step_index + 1) * self.sample.step_ms
        state_digest = digest_value(state)
        payload = {
            "step_index": step_index,
            "logical_time_ms": logical_time_ms,
            "step_end_ms": state["logical_time_ms"],
            "classes": class_records,
            "scheduler": self._scheduler_public(scheduler_reports, state),
            "arrivals": arrivals,
            "passes": pass_records,
            "drops": drops,
            "waiting": waiting_records,
            "totals": {
                "arrival_events": len(arrivals),
                "arrival_bytes": sum(item["bytes"] for item in arrivals),
                "passed_events": len(pass_records),
                "passed_bytes": sum(item["bytes"] for item in pass_records),
                "dropped_events": len(drops),
                "dropped_bytes": sum(item["bytes"] for item in drops),
                "waiting_events": sum(len(queue) for queue in state["queues"].values()),
                "waiting_bytes": sum(
                    item["queue"]["occupancy_bytes"]
                    for item in class_records.values()
                ),
            },
            "arrival_cursor": state["arrival_cursor"],
            "state_digest": state_digest,
        }
        return payload, state

    def replay(self, until_step: int | None = None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        end = self.sample.total_steps if until_step is None else until_step
        if not 0 <= end <= self.sample.total_steps:
            raise QosError(
                "E_USAGE",
                "replay end is outside the sample horizon",
                {"until_step": until_step},
            )
        state = self.initial_state()
        payloads: list[dict[str, Any]] = []
        for step_index in range(end):
            payload, state = self.simulate_step(state, step_index)
            payloads.append(payload)
        return payloads, state

