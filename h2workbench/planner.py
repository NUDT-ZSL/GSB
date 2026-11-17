"""Deterministic send-plan simulator.

Simulates the client send side of one connection against the deterministic
peer rule, producing a byte-exact frame plan, predicted WINDOW_UPDATE
increments, stall (window-exhaustion) segments and per-round scheduling
statistics. The runtime client executes the same algorithm against the
real peer; both must produce identical frame sequences.

Simulated clock: 1 tick (ms) per outbound frame; a predicted grant becomes
visible ``grant_latency_ms`` ticks after the frame that triggered it.

Deterministic peer rule (pure function of the received DATA sequence):
  * track consumed-since-grant per stream and at connection level;
  * after each DATA frame, emit WINDOW_UPDATE(stream, consumed) when
    consumed >= grant_increment OR the mirrored client stream window is
    exhausted; then the same check at connection level;
  * on stream end, flush any remaining consumed amount for that stream.
"""

from .ledger import WindowLedger
from .scheduler import Scheduler

RESPONSE_CLOSE_LATENCY_TICKS = 1


class Plan:
    def __init__(self, strategy):
        self.strategy = strategy
        self.frames = []            # HEADERS/DATA frame records
        self.stalls = []            # window-exhaustion stall segments
        self.predicted_grants = []  # ordered (stream_id, increment)
        self.total_data_bytes = 0
        self.rounds = 0
        self.max_starve = {}        # stream_id -> max consecutive rounds skipped
        self.parent_overlap = set()  # streams eligible together with parent
        self.finish_tick = 0

    def to_dict(self):
        return {
            "strategy": self.strategy,
            "frames": self.frames,
            "stalls": self.stalls,
            "predicted_grants": [list(g) for g in self.predicted_grants],
            "total_data_bytes": self.total_data_bytes,
            "rounds": self.rounds,
            "max_starve": {str(k): v for k, v in
                           sorted(self.max_starve.items())},
            "parent_overlap": sorted(self.parent_overlap),
            "finish_tick": self.finish_tick,
        }

    @staticmethod
    def frame_signature(frames):
        """Byte-exact comparable signature of a frame sequence."""
        return [(f["type"], f["stream_id"], f["bytes"], f["end_stream"])
                for f in frames]


def simulate(manifest, strategy):
    """Run the deterministic plan simulation for ``strategy``."""
    conn = manifest["connection"]
    peer = manifest["peer"]
    max_frame = conn["max_frame_size"]
    max_concurrent = conn["max_concurrent_streams"]
    grant_increment = peer["grant_increment"]
    grant_latency = peer["grant_latency_ms"]

    streams = manifest["streams"]
    plan = Plan(strategy)
    scheduler = Scheduler(strategy, streams)
    ledger = WindowLedger(
        [s["id"] for s in streams],
        conn["client_send"]["stream_window"],
        conn["client_send"]["connection_window"],
    )
    # The peer raises the client connection window above 65535 with an
    # initial connection-level WINDOW_UPDATE right after SETTINGS.
    setup_increment = (conn["client_send"]["connection_window"]
                       - 65535)
    if setup_increment > 0:
        plan.predicted_grants.append((0, setup_increment))

    remaining = {s["id"]: s["body_bytes"] for s in streams}
    by_id = {s["id"]: s for s in streams}
    # Peer-side mirror of the client send windows: decremented as the peer
    # consumes DATA and incremented when the peer *emits* a grant. The
    # deterministic grant rule is evaluated against this mirror, exactly
    # like the real peer does; the client ledger instead only grows when a
    # grant arrives (grant_latency ticks later).
    peer_stream_window = {s["id"]: conn["client_send"]["stream_window"]
                          for s in streams}
    peer_conn_window = [conn["client_send"]["connection_window"]]
    parent_of = {s["id"]: s["depends_on"] for s in streams}
    consumed_since_grant = {s["id"]: 0 for s in streams}
    conn_consumed_since_grant = 0
    pending_grants = []   # (apply_tick, stream_id, increment), FIFO
    open_queue = [s["id"] for s in streams]  # arrival order
    opened = set()
    closed_count = 0
    pending_closes = []   # (close_tick, stream_id)
    current_starve = {s["id"]: 0 for s in streams}
    plan.max_starve = {s["id"]: 0 for s in streams}
    now = 0

    def apply_due_grants():
        nonlocal conn_consumed_since_grant
        due = [g for g in pending_grants if g[0] <= now]
        pending_grants[:] = [g for g in pending_grants if g[0] > now]
        for _, stream_id, increment in due:
            ledger.grant(stream_id, increment)
            plan.predicted_grants.append((stream_id, increment))
            if stream_id != 0:
                state = scheduler.state(stream_id)
                state.window = ledger.window(stream_id)

    def apply_due_closes():
        nonlocal closed_count
        due = [c for c in pending_closes if c[0] <= now]
        pending_closes[:] = [c for c in pending_closes if c[0] > now]
        for _, stream_id in due:
            opened.discard(stream_id)
            closed_count += 1

    def schedule_grant(stream_id, amount):
        pending_grants.append((now + grant_latency, stream_id, amount))
        if stream_id == 0:
            peer_conn_window[0] += amount
        else:
            peer_stream_window[stream_id] += amount

    while closed_count < len(streams):
        apply_due_grants()
        apply_due_closes()
        # Open as many streams as the concurrency limit allows, in
        # arrival (declaration) order; parents are always declared first.
        while open_queue and len(opened) < max_concurrent:
            stream_id = open_queue.pop(0)
            stream = by_id[stream_id]
            opened.add(stream_id)
            end_stream = remaining[stream_id] == 0
            plan.frames.append({
                "seq": len(plan.frames), "type": "HEADERS",
                "stream_id": stream_id, "bytes": 0,
                "end_stream": end_stream,
                "weight": stream["weight"],
                "depends_on": stream["depends_on"], "tick": now,
            })
            now += 1
            if end_stream:
                pending_closes.append(
                    (now + RESPONSE_CLOSE_LATENCY_TICKS, stream_id))
        for stream_id in opened:
            state = scheduler.state(stream_id)
            state.remaining = remaining[stream_id]
            state.window = ledger.window(stream_id)
        candidates = scheduler.eligible(ledger.connection.window)
        eligible_ids = {s.stream_id for s in candidates}
        picked = scheduler.pick_from(candidates)
        if picked is None:
            if any(remaining[s] > 0 for s in opened):
                if not pending_grants:
                    raise RuntimeError(
                        "deterministic peer rule deadlocked: no window "
                        "and no pending grant")
                grant_tick = min(g[0] for g in pending_grants)
                blocked = sorted(s for s in opened if remaining[s] > 0)
                plan.stalls.append({
                    "start_ms": now, "end_ms": grant_tick,
                    "streams": blocked,
                    "connection_blocked": ledger.connection.window <= 0,
                })
                now = grant_tick
                continue
            if closed_count >= len(streams):
                break
            # All open streams finished; wait for their closes.
            now += RESPONSE_CLOSE_LATENCY_TICKS
            apply_due_closes()
            continue

        state = picked
        nbytes = scheduler.frame_size(state, ledger.connection.window,
                                      max_frame)
        ledger.send(picked.stream_id, nbytes)
        remaining[picked.stream_id] -= nbytes
        end_stream = remaining[picked.stream_id] == 0
        plan.frames.append({
            "seq": len(plan.frames), "type": "DATA",
            "stream_id": picked.stream_id, "bytes": nbytes,
            "end_stream": end_stream, "tick": now,
        })
        plan.total_data_bytes += nbytes
        now += 1

        # Per-round starvation statistics: only streams that were
        # eligible at pick time but skipped count as starved rounds.
        plan.rounds += 1
        for stream_id in eligible_ids:
            if stream_id == picked.stream_id:
                current_starve[stream_id] = 0
            else:
                current_starve[stream_id] += 1
                plan.max_starve[stream_id] = max(
                    plan.max_starve[stream_id], current_starve[stream_id])
            parent = parent_of[stream_id]
            if parent in eligible_ids:
                plan.parent_overlap.add(stream_id)

        # Deterministic peer rule: evaluate grants after each DATA frame
        # against the peer-side window mirror (emission view).
        sid = picked.stream_id
        consumed_since_grant[sid] += nbytes
        conn_consumed_since_grant += nbytes
        peer_stream_window[sid] -= nbytes
        peer_conn_window[0] -= nbytes
        if (consumed_since_grant[sid] >= grant_increment
                or peer_stream_window[sid] == 0):
            amount = consumed_since_grant[sid]
            consumed_since_grant[sid] = 0
            schedule_grant(sid, amount)
        if (conn_consumed_since_grant >= grant_increment
                or peer_conn_window[0] == 0):
            amount = conn_consumed_since_grant
            conn_consumed_since_grant = 0
            schedule_grant(0, amount)
        if end_stream:
            if consumed_since_grant[sid] > 0:
                amount = consumed_since_grant[sid]
                consumed_since_grant[sid] = 0
                schedule_grant(sid, amount)
            pending_closes.append(
                (now + RESPONSE_CLOSE_LATENCY_TICKS, sid))

    # Flush grants that are still pending after the last frame.
    if pending_grants:
        now = max(g[0] for g in pending_grants)
        apply_due_grants()
    plan.finish_tick = now
    return plan
