"""Multiplexed send scheduling strategies.

Each strategy deterministically picks the next stream to send one DATA
frame for, given the set of eligible streams (open, body remaining, stream
window available). The frame size is always
``min(max_frame_size, stream window, connection window, remaining body)``
so a frame never exceeds the frame size limit or any available window.

All tie-breaks use the stream arrival index (declaration order); no dict
or hash iteration order is ever relied upon.
"""

WEIGHTED_RR = "weighted_rr"
WINDOW_FIRST = "window_first"
ARRIVAL = "arrival"

STRATEGIES = (WEIGHTED_RR, WINDOW_FIRST, ARRIVAL)


class StreamSchedState:
    """Per-stream scheduling view used by strategies."""

    __slots__ = ("stream_id", "arrival_index", "weight", "remaining",
                 "window", "smooth_weight")

    def __init__(self, stream_id, arrival_index, weight):
        self.stream_id = stream_id
        self.arrival_index = arrival_index
        self.weight = weight
        self.remaining = 0
        self.window = 0
        self.smooth_weight = 0  # smooth weighted round-robin cursor


class Scheduler:
    """Deterministic frame scheduler for one connection."""

    def __init__(self, strategy, streams):
        if strategy not in STRATEGIES:
            raise ValueError(f"unknown strategy: {strategy}")
        self.strategy = strategy
        # Fixed iteration order: arrival (declaration) order.
        self._states = [
            StreamSchedState(s["id"], s["arrival_index"], s["weight"])
            for s in sorted(streams, key=lambda s: s["arrival_index"])
        ]
        self._by_id = {s.stream_id: s for s in self._states}
        self._total_weight = sum(s.weight for s in self._states)

    def state(self, stream_id):
        return self._by_id[stream_id]

    def eligible(self, connection_window):
        """Open streams that can send at least one byte right now."""
        if connection_window <= 0:
            return []
        return [s for s in self._states
                if s.remaining > 0 and s.window > 0]

    def pick(self, connection_window):
        """Pick the next stream to send one DATA frame for, or None."""
        return self.pick_from(self.eligible(connection_window))

    def pick_from(self, candidates):
        """Pick from a precomputed candidate list (deterministic)."""
        if not candidates:
            return None
        if self.strategy == ARRIVAL:
            return candidates[0]  # lowest arrival index
        if self.strategy == WINDOW_FIRST:
            # Largest remaining window first; ties by arrival index.
            best = candidates[0]
            for state in candidates[1:]:
                if state.window > best.window:
                    best = state
            return best
        # Smooth weighted round robin (deterministic, stable ties).
        best = None
        for state in candidates:
            state.smooth_weight += state.weight
            if best is None or state.smooth_weight > best.smooth_weight:
                best = state
        best.smooth_weight -= self._total_weight
        return best

    def frame_size(self, state, connection_window, max_frame_size):
        """Bytes to allocate to the next frame for ``state``."""
        return min(max_frame_size, state.window, connection_window,
                   state.remaining)
