"""Derived throughput/latency estimates, truth comparison, and error chains."""

from __future__ import annotations

import bisect
from collections import deque
from fractions import Fraction
from typing import Any, Iterable, Mapping

from .fixed import FIXED_SCALE, conversion_ledger, fraction_text
from .models import Sample


def _nearest_rank(values: list[int], numerator: int, denominator: int) -> int | None:
    if not values:
        return None
    rank = (len(values) * numerator + denominator - 1) // denominator
    index = max(0, rank - 1)
    return values[index]


def _metric_tuple(
    passed_bytes: int,
    waits: list[int],
    window_ms: int,
) -> dict[str, Any]:
    ordered = sorted(waits)
    count = len(waits)
    throughput = Fraction(passed_bytes * 8 * 1000, window_ms)
    mean_wait = Fraction(sum(waits), count) if count else None
    return {
        "passed_bytes": passed_bytes,
        "passed_events": count,
        "throughput_bps": throughput,
        "p50_wait_ms": _nearest_rank(ordered, 1, 2),
        "p95_wait_ms": _nearest_rank(ordered, 95, 100),
        "max_wait_ms": ordered[-1] if ordered else None,
        "mean_wait_ms": mean_wait,
    }


class StreamingEstimator:
    """One-pass sliding-window estimator over committed step payloads."""

    def __init__(self, sample: Sample) -> None:
        self.sample = sample
        self.passes: dict[str, deque[dict[str, Any]]] = {
            item.name: deque() for item in sample.classes
        }
        self.sorted_waits: dict[str, list[int]] = {
            item.name: [] for item in sample.classes
        }
        self.bytes: dict[str, int] = {item.name: 0 for item in sample.classes}
        self.end_ms = 0
        self.start_ms = 0

    def advance(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        self.end_ms = int(payload["step_end_ms"])
        self.start_ms = self.end_ms - self.sample.window_ms
        for name in self.passes:
            queue = self.passes[name]
            while queue and queue[0]["pass_ms"] < self.start_ms:
                removed = queue.popleft()
                self.bytes[name] -= removed["bytes"]
                waits = self.sorted_waits[name]
                index = bisect.bisect_left(waits, removed["wait_ms"])
                if index < len(waits) and waits[index] == removed["wait_ms"]:
                    waits.pop(index)
        for item in payload["passes"]:
            name = item["class"]
            self.passes[name].append(item)
            self.bytes[name] += item["bytes"]
            bisect.insort(self.sorted_waits[name], item["wait_ms"])
        return self.snapshot()

    def snapshot(self) -> dict[str, Any]:
        return {
            "window_start_ms": self.start_ms,
            "window_end_ms": self.end_ms,
            "classes": {
                name: _metric_tuple(
                    self.bytes[name], self.sorted_waits[name], self.sample.window_ms
                )
                for name in self.bytes
            },
        }


def _all_passes(payloads: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for payload in payloads:
        result.extend(payload["passes"])
    return result


def truth_snapshot(
    sample: Sample,
    payloads: list[Mapping[str, Any]],
    end_step: int | None = None,
) -> dict[str, Any]:
    if end_step is None:
        end_step = len(payloads) - 1
    end_ms = (end_step + 1) * sample.step_ms
    start_ms = end_ms - sample.window_ms
    prefix = payloads[: end_step + 1]
    all_events = _all_passes(prefix)
    classes: dict[str, Any] = {}
    for class_config in sample.classes:
        selected = [
            event
            for event in all_events
            if event["class"] == class_config.name
            and start_ms <= event["pass_ms"] < end_ms
        ]
        selected.sort(key=lambda event: (event["pass_ms"], event["seq"]))
        classes[class_config.name] = _metric_tuple(
            sum(event["bytes"] for event in selected),
            [event["wait_ms"] for event in selected],
            sample.window_ms,
        )
    return {
        "window_start_ms": start_ms,
        "window_end_ms": end_ms,
        "classes": classes,
    }


def _comparable(metrics: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "passed_bytes": metrics["passed_bytes"],
        "passed_events": metrics["passed_events"],
        "throughput_bps": metrics["throughput_bps"],
        "p50_wait_ms": metrics["p50_wait_ms"],
        "p95_wait_ms": metrics["p95_wait_ms"],
        "max_wait_ms": metrics["max_wait_ms"],
        "mean_wait_ms": metrics["mean_wait_ms"],
    }


def compare_estimators(
    sample: Sample, payloads: list[Mapping[str, Any]]
) -> dict[str, Any]:
    streaming = StreamingEstimator(sample)
    comparisons = 0
    first: dict[str, Any] | None = None
    for step_index, payload in enumerate(payloads):
        stream = streaming.advance(payload)
        truth = truth_snapshot(sample, payloads, step_index)
        for class_config in sample.classes:
            name = class_config.name
            stream_metrics = _comparable(stream["classes"][name])
            truth_metrics = _comparable(truth["classes"][name])
            for field in (
                "passed_bytes",
                "passed_events",
                "throughput_bps",
                "p50_wait_ms",
                "p95_wait_ms",
                "max_wait_ms",
                "mean_wait_ms",
            ):
                comparisons += 1
                left = stream_metrics[field]
                right = truth_metrics[field]
                if left != right and first is None:
                    if any(
                        row.get("rate_remainder", 0) or row.get("error_upper_units") not in (None, "0.000000000000")
                        for row in conversion_ledger(sample)
                        if row["class"] == name
                    ):
                        classification = "fixed_point_conversion"
                    elif field == "throughput_bps":
                        classification = "token_accumulation_error"
                    else:
                        classification = "scheduling_order_difference"
                    first = {
                        "step_index": step_index,
                        "class": name,
                        "field": field,
                        "streaming_value": _json_metric(left),
                        "truth_value": _json_metric(right),
                        "classification": classification,
                        "intermediate": {
                            "window_start_ms": stream["window_start_ms"],
                            "window_end_ms": stream["window_end_ms"],
                            "streaming": _metrics_to_json(stream_metrics),
                            "truth": _metrics_to_json(truth_metrics),
                            "pass_event_sequences": [
                                item["seq"]
                                for item in payloads[step_index]["passes"]
                                if item["class"] == name
                            ],
                        },
                    }
    return {
        "consistent": first is None,
        "comparisons": comparisons,
        "first_divergence": first,
        "aggregation_order": "steps ascending; passes in scheduler order; wait quantiles nearest-rank",
    }


def _json_metric(value: Any) -> Any:
    if isinstance(value, Fraction):
        return fraction_text(value)
    return value


def _metrics_to_json(metrics: Mapping[str, Any]) -> dict[str, Any]:
    return {key: _json_metric(value) for key, value in metrics.items()}


def _latency_bound(sample: Sample) -> Fraction:
    _ = sample
    return Fraction(0, 1)


def error_ledger(sample: Sample, payloads: list[Mapping[str, Any]]) -> dict[str, Any]:
    conversions = conversion_ledger(sample)
    by_class: dict[str, list[dict[str, Any]]] = {
        item.name: [] for item in sample.classes
    }
    for row in conversions:
        by_class[row["class"]].append(row)
    passed_seqs: dict[str, list[int]] = {item.name: [] for item in sample.classes}
    for payload in payloads:
        for event in payload["passes"]:
            passed_seqs[event["class"]].append(event["seq"])
    token_bound = Fraction(2, FIXED_SCALE)
    propagation: dict[str, Any] = {}
    for class_config in sample.classes:
        name = class_config.name
        throughput_bound = token_bound * 8 * 1000 / sample.window_ms
        propagation[name] = {
            "token_balance_bound_bytes": fraction_text(token_bound),
            "queue_occupancy_bound_bytes": "0.000000",
            "throughput_bound_bps": fraction_text(throughput_bound),
            "latency_bound_ms": fraction_text(_latency_bound(sample)),
            "reason": (
                "Each bucket is fixed-point integer state with its own saved "
                "1/8000-byte accumulated remainder; exact additions, cap checks, "
                "FIFO operations, and scheduler quotas therefore add no float drift."
            ),
            "conversion_chain": by_class[name],
            "passed_event_sequences_sample": passed_seqs[name][:8],
            "passed_event_sequence_count": len(passed_seqs[name]),
        }
    return {
        "fixed_scale": FIXED_SCALE,
        "token_remainder_denominator": 8000,
        "rounding_direction": "floor for rate increments; saved remainder carries the subunits",
        "conversions": conversions,
        "propagation": propagation,
        "arithmetic_note": (
            "Token, queue, quota, byte, and wait arithmetic is integer/Fraction "
            "exact. The only represented conversion uncertainty is the sub-unit "
            "fraction below one fixed token, and it is retained instead of dropped."
        ),
    }


def _event_sequences_for_class(
    payloads: list[Mapping[str, Any]], class_names: set[str], limit: int = 8
) -> list[int]:
    result: list[int] = []
    for payload in payloads:
        for event in payload["passes"]:
            if event["class"] in class_names:
                result.append(event["seq"])
                if len(result) >= limit:
                    return result
    return result


def _conversion_sources(sample: Sample, class_names: set[str]) -> list[dict[str, Any]]:
    rows = [
        row
        for row in conversion_ledger(sample)
        if row["class"] in class_names
        and str(row["field"])
        in {"committed_rate", "peak_rate", "guaranteed_bandwidth"}
    ]
    return sorted(rows, key=lambda row: (str(row["class"]), str(row["field"])))


def sensitivity_markers(
    sample: Sample,
    payloads: list[Mapping[str, Any]],
    derived: Mapping[str, Any],
) -> list[dict[str, Any]]:
    markers: list[dict[str, Any]] = []
    names = [item.name for item in sample.classes if item.is_leaf]
    for index, left in enumerate(names):
        for right in names[index + 1 :]:
            left_metrics = derived["classes"][left]
            right_metrics = derived["classes"][right]
            for kind, field, tolerance in (
                (
                    "observed_throughput",
                    "observed_throughput_bps",
                    sample.throughput_tolerance_bps,
                ),
                (
                    "achievable_throughput",
                    "achievable_throughput_bps",
                    sample.throughput_tolerance_bps,
                ),
            ):
                left_value = Fraction(str(left_metrics[field]))
                right_value = Fraction(str(right_metrics[field]))
                difference = abs(left_value - right_value)
                if difference < tolerance:
                    markers.append(
                        {
                            "kind": kind,
                            "participants": [left, right],
                            "difference_value": fraction_text(difference, 6),
                            "tolerance": fraction_text(tolerance, 6),
                            "tolerance_unit": "bps",
                            "sensitive": True,
                            "depends_on_difference": True,
                            "basis": "pairwise throughput comparison falls inside the declared tolerance",
                            "event_sequences": _event_sequences_for_class(
                                payloads, {left, right}
                            ),
                            "error_sources": _conversion_sources(
                                sample, {left, right}
                            ),
                        }
                    )

    parents = [item for item in sample.classes if item.children]
    for parent in parents:
        children = list(parent.children)
        pair_evidence: dict[tuple[str, str], list[dict[str, Any]]] = {
            (children[i], children[j]): []
            for i in range(len(children))
            for j in range(i + 1, len(children))
        }
        for payload in payloads:
            parent_report = payload["scheduler"].get(parent.name)
            if parent_report is None:
                continue
            for left, right in pair_evidence:
                left_quota = Fraction(
                    parent_report["children"][left]["quota_bytes"]
                )
                right_quota = Fraction(
                    parent_report["children"][right]["quota_bytes"]
                )
                difference = abs(left_quota - right_quota)
                if difference < sample.schedule_tolerance_bytes:
                    pair_evidence[(left, right)].append(
                        {
                            "step_index": payload["step_index"],
                            "logical_time_ms": payload["logical_time_ms"],
                            "quota_difference_bytes": fraction_text(difference, 3),
                            "left_quota_bytes": parent_report["children"][left][
                                "quota_bytes"
                            ],
                            "right_quota_bytes": parent_report["children"][right][
                                "quota_bytes"
                            ],
                        }
                    )
        for (left, right), evidence in pair_evidence.items():
            if not evidence:
                continue
            first = evidence[0]
            relevant_events = _event_sequences_for_class(
                payloads[: first["step_index"] + 1], {left, right}
            )
            markers.append(
                {
                    "kind": "sibling_scheduling_quota",
                    "parent": parent.name,
                    "participants": [left, right],
                    "difference_value": first["quota_difference_bytes"],
                    "tolerance": fraction_text(
                        sample.schedule_tolerance_bytes, 3
                    ),
                    "tolerance_unit": "bytes",
                    "sensitive": True,
                    "depends_on_difference": True,
                    "basis": "at least one per-step sibling quota difference is inside the declared tolerance",
                    "first_step_index": first["step_index"],
                    "sensitive_step_count": len(evidence),
                    "first_evidence": evidence[0],
                    "evidence": evidence,
                    "event_sequences": relevant_events,
                    "error_sources": _conversion_sources(
                        sample, {parent.name, left, right}
                    ),
                }
            )
    markers.sort(
        key=lambda item: (
            str(item["kind"]),
            str(item.get("parent", "")),
            tuple(item["participants"]),
        )
    )
    return markers


def derive_metrics(
    sample: Sample,
    payloads: list[Mapping[str, Any]],
    final_state: Mapping[str, Any],
) -> dict[str, Any]:
    window_series: list[dict[str, Any]] = []
    if payloads:
        end_step = len(payloads) - 1
        streaming = StreamingEstimator(sample)
        for payload in payloads:
            snapshot = streaming.advance(payload)
            window_series.append(
                {
                    "step_index": payload["step_index"],
                    "window_start_ms": snapshot["window_start_ms"],
                    "window_end_ms": snapshot["window_end_ms"],
                    "classes": {
                        name: {
                            "passed_bytes": metrics["passed_bytes"],
                            "throughput_bps": fraction_text(
                                metrics["throughput_bps"], 6
                            ),
                            "p50_wait_ms": metrics["p50_wait_ms"],
                            "p95_wait_ms": metrics["p95_wait_ms"],
                        }
                        for name, metrics in snapshot["classes"].items()
                    },
                }
            )
        final_metrics = streaming.snapshot()
        truth = truth_snapshot(sample, payloads, end_step)
    else:
        final_metrics = {
            "window_start_ms": 0,
            "window_end_ms": 0,
            "classes": {
                item.name: _metric_tuple(0, [], sample.window_ms)
                for item in sample.classes
            },
        }
        truth = final_metrics
    classes: dict[str, Any] = {}
    passed_total = {item.name: 0 for item in sample.classes}
    dropped_total = {item.name: 0 for item in sample.classes}
    for payload in payloads:
        for event in payload["passes"]:
            passed_total[event["class"]] += event["bytes"]
        for event in payload["drops"]:
            dropped_total[event["class"]] += event["bytes"]
    for class_config in sample.classes:
        name = class_config.name
        metrics = final_metrics["classes"][name]
        path = sample.path(name)
        bottleneck = min(path, key=lambda item: sample.class_map[item].pir_bps)
        queue_bytes = sum(
            item["bytes"] for item in final_state["queues"][name]
        )
        classes[name] = {
            "window_start_ms": final_metrics["window_start_ms"],
            "window_end_ms": final_metrics["window_end_ms"],
            "window_passed_bytes": metrics["passed_bytes"],
            "window_passed_events": metrics["passed_events"],
            "observed_throughput_bps": fraction_text(
                metrics["throughput_bps"], 6
            ),
            "estimated_p50_wait_ms": metrics["p50_wait_ms"],
            "estimated_p95_wait_ms": metrics["p95_wait_ms"],
            "estimated_max_wait_ms": metrics["max_wait_ms"],
            "estimated_mean_wait_ms": (
                None
                if metrics["mean_wait_ms"] is None
                else fraction_text(metrics["mean_wait_ms"], 6)
            ),
            "guaranteed_bandwidth_bps": class_config.guarantee_bps,
            "achievable_throughput_bps": sample.class_map[bottleneck].pir_bps,
            "achievable_bottleneck_path": list(path),
            "passed_bytes_total": passed_total[name],
            "dropped_bytes_total": dropped_total[name],
            "queue_occupancy_bytes": queue_bytes,
            "token_error_bound_bytes": fraction_text(
                Fraction(2, FIXED_SCALE), 6
            ),
        }
    comparison = compare_estimators(sample, payloads)
    errors = error_ledger(sample, payloads)
    markers = sensitivity_markers(sample, payloads, {"classes": classes})
    return {
        "window": {
            "start_ms": final_metrics["window_start_ms"],
            "end_ms": final_metrics["window_end_ms"],
            "truth_start_ms": truth["window_start_ms"],
            "truth_end_ms": truth["window_end_ms"],
        },
        "classes": classes,
        "window_series": window_series,
        "estimator_comparison": comparison,
        "error_ledger": errors,
        "precision_sensitive": markers,
        "precision_summary": {
            "sensitive_marker_count": len(markers),
            "throughput_tolerance_bps": fraction_text(
                sample.throughput_tolerance_bps, 6
            ),
            "schedule_tolerance_bytes": fraction_text(
                sample.schedule_tolerance_bytes, 3
            ),
        },
    }

