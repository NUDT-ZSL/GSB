"""In-memory scheduling cores: precise min-heap and hierarchical timing wheel.

Both cores expose the same interface over plain (fire_ms, task_id, version)
entries and, fed with identical entries and identical logical clock steps,
emit byte-identical expiration sequences ordered by (fire_ms, task_id).
"""

import heapq
import math


class HeapScheduler:
    """Precise min-heap ordered strictly by (fire_ms, task_id)."""

    name = "heap"

    def __init__(self, wheel_config=None):
        self._heap = []

    def add(self, fire_ms, task_id, version=1):
        heapq.heappush(self._heap, (int(fire_ms), task_id, int(version)))

    def remove(self, task_id):
        # The queue drives removals through rebuild; kept for interface parity.
        self._heap = [item for item in self._heap if item[1] != task_id]
        heapq.heapify(self._heap)

    def advance(self, now_ms):
        due = []
        while self._heap and self._heap[0][0] <= now_ms:
            fire_ms, task_id, version = heapq.heappop(self._heap)
            due.append((fire_ms, task_id, version))
        due.sort()
        return due

    def peek_ms(self):
        return self._heap[0][0] if self._heap else None

    def __len__(self):
        return len(self._heap)


class WheelScheduler:
    """Hierarchical timing wheel with configurable ticks and level count."""

    name = "wheel"

    def __init__(self, wheel_config, start_ms):
        self.cfg = wheel_config.validate()
        ticks = self.cfg.ticks_per_wheel
        self._slots = [[[] for _ in range(ticks)]
                       for _ in range(self.cfg.level_count)]
        self._now = int(start_ms)
        self._overdue = []

    def _slot_index(self, level, fire_ms):
        width = self.cfg.level_width(level)
        return (fire_ms // width) % self.cfg.ticks_per_wheel

    def _pick_level(self, fire_ms):
        if fire_ms <= self._now:
            return -1
        for level in range(self.cfg.level_count):
            upper = self._now + self.cfg.level_width(level) * \
                self.cfg.ticks_per_wheel
            if fire_ms < upper:
                return level
        return self.cfg.level_count - 1

    def add(self, fire_ms, task_id, version=1):
        entry = (int(fire_ms), task_id, int(version))
        level = self._pick_level(fire_ms)
        if level < 0:
            self._overdue.append(entry)
            return
        idx = self._slot_index(level, fire_ms)
        self._slots[level][idx].append(entry)

    def advance(self, now_ms):
        if now_ms <= self._now:
            due = list(self._overdue)
            self._overdue.clear()
            due.sort()
            return due

        due = list(self._overdue)
        self._overdue.clear()
        tick = self.cfg.tick_ms
        # Slot boundaries are aligned to absolute logical time.  Drain the
        # slot holding the starting instant first, then every crossed slot.
        boundaries = list(range(self._now // tick * tick,
                                now_ms // tick * tick + tick, tick))
        for boundary in boundaries:
            due.extend(self._drain_level0(boundary, now_ms))
            for level in range(1, self.cfg.level_count):
                width = self.cfg.level_width(level)
                if boundary % width == 0:
                    self._cascade(level, boundary, now_ms, due)

        # Anything already overdue while sitting in a high-level slot because
        # it exceeded the wheel horizon gets flushed here.
        self._flush_overflow(now_ms, due)
        self._now = int(now_ms)
        due.sort()
        return due

    def _drain_level0(self, boundary, now_ms):
        idx = (boundary // self.cfg.tick_ms) % self.cfg.ticks_per_wheel
        bucket = self._slots[0][idx]
        due = []
        remaining = []
        for entry in bucket:
            if entry[0] <= now_ms:
                due.append(entry)
            else:
                remaining.append(entry)
        self._slots[0][idx] = remaining
        return due

    def _cascade(self, level, boundary, now_ms, due):
        width = self.cfg.level_width(level)
        idx = (boundary // width) % self.cfg.ticks_per_wheel
        bucket = self._slots[level][idx]
        self._slots[level][idx] = []
        for entry in bucket:
            fire_ms = entry[0]
            if fire_ms <= now_ms:
                due.append(entry)
                continue
            target = level - 1
            while target > 0 and fire_ms >= boundary + \
                    self.cfg.level_width(target) * self.cfg.ticks_per_wheel:
                target -= 1
            if target < 0:
                target = 0
            self._slots[target][
                self._slot_index(target, fire_ms)].append(entry)

    def _flush_overflow(self, now_ms, due):
        for level in range(1, self.cfg.level_count):
            for idx, bucket in enumerate(self._slots[level]):
                keep = []
                for entry in bucket:
                    if entry[0] <= now_ms:
                        due.append(entry)
                    else:
                        keep.append(entry)
                self._slots[level][idx] = keep

    def peek_ms(self):
        values = [entry[0] for entry in self._overdue]
        for level in self._slots:
            for bucket in level:
                values.extend(entry[0] for entry in bucket)
        return min(values) if values else None

    def __len__(self):
        total = len(self._overdue)
        for level in self._slots:
            total += sum(len(bucket) for bucket in level)
        return total


def estimate_cost(mode, pending_count, span_ms, wheel_config):
    """Deterministic scan-cost estimate used by the adaptive switch."""
    n = max(0, int(pending_count))
    if mode == "heap":
        return n * math.log2(n + 1)
    if mode == "wheel":
        # Only crossed slots are scanned, plus one bucketing op per entry.
        # Sparse, wide spans waste scans even when few entries are due.
        slots_scanned = max(1.0, span_ms / max(1, wheel_config.tick_ms))
        return slots_scanned + n
    raise ValueError(f"unknown scheduler mode: {mode}")


def choose_mode(n, span_ms, wheel_config, adaptive, current_mode):
    """Apply the explicit switching rule with hysteresis.

    Returns (mode, reason, costs).  The wheel is selected only above the size
    threshold and when its estimate beats the heap estimate by the hysteresis
    margin; the reverse switch is symmetric, preventing flapping.
    """
    heap_cost = estimate_cost("heap", n, span_ms, wheel_config)
    wheel_cost = estimate_cost("wheel", n, span_ms, wheel_config)
    threshold = adaptive.switch_threshold
    margin = adaptive.hysteresis_ratio

    if not adaptive.enabled:
        return current_mode, "adaptive_disabled", \
            {"heap": heap_cost, "wheel": wheel_cost}
    if n < threshold:
        # Down-sizing below the threshold always returns to the precise heap;
        # the margin keeps the mode stable while n hovers at the boundary.
        return "heap", f"pending_below_threshold({n}<{threshold})", \
            {"heap": heap_cost, "wheel": wheel_cost}
    if current_mode == "heap" and wheel_cost < heap_cost * margin:
        return "wheel", "wheel_cost_below_heap_with_margin", \
            {"heap": heap_cost, "wheel": wheel_cost}
    return current_mode, "keep_current_mode", \
        {"heap": heap_cost, "wheel": wheel_cost}
