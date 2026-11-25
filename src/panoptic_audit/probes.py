from __future__ import annotations

from dataclasses import dataclass
from heapq import heappop, heappush
from typing import Any

from .errors import AuditError, ErrorCode


@dataclass(frozen=True)
class Probe:
    uid: str
    probe_id: str
    plan_index: int
    sequence: int
    x: int
    y: int
    expected_category: int
    expected_instance: int
    expected_ignore: bool
    priority: int
    time_budget_ms: float

    def coordinate(self) -> dict[str, int]:
        return {"x": self.x, "y": self.y}

    def to_dict(self) -> dict[str, Any]:
        return {
            "uid": self.uid,
            "probe_id": self.probe_id,
            "plan_index": self.plan_index,
            "sequence": self.sequence,
            "x": self.x,
            "y": self.y,
            "expected_category": self.expected_category,
            "expected_instance": self.expected_instance,
            "expected_ignore": self.expected_ignore,
            "priority": self.priority,
            "time_budget_ms": self.time_budget_ms,
        }


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


class ProbePriorityQueue:
    """Deterministic max-priority queue; ties use original plan order."""

    def __init__(self, probes: list[Probe] | None = None) -> None:
        self._heap: list[tuple[int, int, int, str, Probe]] = []
        for probe in probes or []:
            self.add(probe)

    def add(self, probe: Probe) -> None:
        heappush(self._heap, (-probe.priority, probe.plan_index, probe.sequence, probe.uid, probe))

    def pop(self) -> Probe:
        if not self._heap:
            raise IndexError("probe queue is empty")
        return heappop(self._heap)[-1]

    def __len__(self) -> int:
        return len(self._heap)


def parse_probe_plan(
    payload: dict[str, Any],
    height: int,
    width: int,
) -> tuple[list[Probe], list[dict[str, Any]], list[dict[str, Any]]]:
    if not isinstance(payload, dict):
        raise AuditError(ErrorCode.PLAN_INVALID, "probe plan must be a JSON object")
    if payload.get("format") != "active_probe_plan":
        raise AuditError(ErrorCode.FORMAT_UNSUPPORTED, "unsupported probe plan format", {"format": payload.get("format")})
    if payload.get("version") not in {"plan-v1", "1.0", "v1"}:
        raise AuditError(ErrorCode.FORMAT_UNSUPPORTED, "unsupported probe plan version", {"version": payload.get("version")})
    raw_probes = payload.get("probes")
    if not isinstance(raw_probes, list):
        raise AuditError(ErrorCode.FORMAT_CORRUPT, "probe plan probes must be a list")

    valid_indices: set[int] = set()
    rejected: list[dict[str, Any]] = []
    for plan_index, item in enumerate(raw_probes):
        if not isinstance(item, dict):
            rejected.append({
                "plan_index": plan_index,
                "probe_id": None,
                "code": ErrorCode.PLAN_INVALID,
                "reasons": ["probe entry must be an object"],
            })
            continue

        probe_id = item.get("probe_id") or f"PLAN-{plan_index:03d}"
        reasons: list[str] = []
        x, y = item.get("x"), item.get("y")
        if not _is_int(x) or not 0 <= int(x) < width:
            reasons.append("x is missing or out of bounds")
        if not _is_int(y) or not 0 <= int(y) < height:
            reasons.append("y is missing or out of bounds")
        if not _is_int(item.get("expected_category")) or not 0 <= int(item.get("expected_category")) <= 255:
            reasons.append("expected_category is missing or invalid")
        if not _is_int(item.get("expected_instance")) or not 0 <= int(item.get("expected_instance")) < 1000:
            reasons.append("expected_instance is missing or invalid")
        if not _is_int(item.get("priority")) or not 0 <= int(item.get("priority")) <= 100:
            reasons.append("priority is missing or outside [0, 100]")
        budget = item.get("time_budget_ms")
        if not isinstance(budget, (int, float)) or isinstance(budget, bool) or not 0 < float(budget) <= 10_000:
            reasons.append("time_budget_ms is missing or invalid")

        expected_ignore = item.get("expected_ignore", False)
        if not isinstance(expected_ignore, bool):
            reasons.append("expected_ignore must be boolean when provided")

        if reasons:
            rejected.append({
                "plan_index": plan_index,
                "probe_id": probe_id,
                "coordinate": {"x": x, "y": y} if _is_int(x) and _is_int(y) else None,
                "code": ErrorCode.PLAN_INVALID,
                "reasons": reasons,
            })
        else:
            valid_indices.add(plan_index)

    groups: dict[tuple[int, int], list[dict[str, Any]]] = {}
    for plan_index, item in enumerate(raw_probes):
        if plan_index in valid_indices:
            # Dict membership works because valid_raw retains same object references.
            x, y = int(item["x"]), int(item["y"])
            groups.setdefault((x, y), []).append((plan_index, item))

    winners: list[tuple[int, dict[str, Any]]] = []
    suppressed: list[dict[str, Any]] = []
    for coordinate, entries in groups.items():
        def sort_key(entry: tuple[int, dict[str, Any]]) -> tuple[int, int]:
            plan_index, item = entry
            return (-int(item["priority"]), plan_index)

        ordered = sorted(entries, key=sort_key)
        winner_index, winner = ordered[0]
        winners.append((winner_index, winner))
        for plan_index, item in ordered[1:]:
            suppressed.append({
                "plan_index": plan_index,
                "probe_id": item.get("probe_id") or f"PLAN-{plan_index:03d}",
                "coordinate": {"x": coordinate[0], "y": coordinate[1]},
                "code": "PROBE_SUPPRESSED_DUPLICATE",
                "reason": "duplicate coordinate retained the higher-priority or earlier plan entry",
                "retained_probe_id": winner.get("probe_id") or f"PLAN-{winner_index:03d}",
                "retained_plan_index": winner_index,
                "priority": int(item["priority"]),
            })

    accepted: list[Probe] = []
    execution_order = sorted(winners, key=lambda entry: (-int(entry[1]["priority"]), entry[0]))
    for sequence, (plan_index, item) in enumerate(execution_order):
        x, y = int(item["x"]), int(item["y"])
        accepted.append(Probe(
            uid=f"{x},{y}",
            probe_id=str(item.get("probe_id") or f"PLAN-{plan_index:03d}"),
            plan_index=plan_index,
            sequence=sequence,
            x=x,
            y=y,
            expected_category=int(item["expected_category"]),
            expected_instance=int(item["expected_instance"]),
            expected_ignore=bool(item.get("expected_ignore", int(item["expected_category"]) == 255)),
            priority=int(item["priority"]),
            time_budget_ms=float(item["time_budget_ms"]),
        ))

    suppressed.sort(key=lambda item: int(item["plan_index"]))
    rejected.sort(key=lambda item: int(item["plan_index"]))
    return accepted, rejected, suppressed
