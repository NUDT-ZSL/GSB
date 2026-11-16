"""Stage load driver: concurrent worker threads + fixed-interval sampling.

Design:
  * One persistent TCP connection per worker thread (`concurrency` workers).
  * A shared token bucket enforces the stage throughput_limit_rps cap; workers
    block on tokens instead of overshooting the target.
  * A dedicated sampler thread wakes every `sample_interval_ms` (monotonic
    clock) and freezes per-interval counters/latencies into a Sample row.
  * Success is declared only on a parseable server receipt status=="ok".
  * A stop event supports interruption; interrupted stages return no
    Observation and are persisted as "unknown" progress markers only.
"""
from __future__ import annotations

import json
import socket
import threading
import time
from dataclasses import dataclass

from .clock import CLOCK_BASIS, MonotonicClock
from .errors import ClockError
from .models import Observation, Sample, StageSpec
from .mock_server import RECEIPT_PROTO

TRUTH_SOURCE = "mock_server_receipt(server_log_auditable)"


class TokenBucket:
    def __init__(self, rate_per_sec: int) -> None:
        self.rate = float(rate_per_sec)
        self._tokens = float(rate_per_sec)
        self._updated = time.monotonic()
        self._lock = threading.Lock()

    def take(self, stop_event: threading.Event | None = None) -> None:
        while True:
            if stop_event is not None and stop_event.is_set():
                return
            with self._lock:
                now = time.monotonic()
                self._tokens = min(self.rate,
                                   self._tokens + (now - self._updated) * self.rate)
                self._updated = now
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                deficit = (1.0 - self._tokens) / self.rate
            if stop_event is not None:
                if stop_event.wait(timeout=min(deficit, 0.02)):
                    return
            else:
                time.sleep(min(deficit, 0.01))


@dataclass
class IntervalCounters:
    success: int = 0
    failure: int = 0
    latency_ms: list[int] | None = None

    def __post_init__(self) -> None:
        if self.latency_ms is None:
            self.latency_ms = []


class StageDriver:
    def __init__(self, host: str, port: int, spec: StageSpec,
                 sample_interval_ms: int, clock: MonotonicClock,
                 stop_event: threading.Event | None = None) -> None:
        self.host = host
        self.port = port
        self.spec = spec
        self.sample_interval_ms = sample_interval_ms
        self.clock = clock
        self.external_stop = stop_event
        self.stop_event = threading.Event()  # internal end-of-drive event
        self._bucket = TokenBucket(spec.throughput_limit_rps)
        self._counter_lock = threading.Lock()
        self._current = IntervalCounters()
        self._samples: list[Sample] = []
        self._req_id = 0
        self._req_lock = threading.Lock()
        self.interrupted = False
        self.fatal_error: Exception | None = None

    def run(self) -> Observation | None:
        start_ms = self.clock.now_ms()
        deadline_ns = time.monotonic_ns() + self.spec.duration_s * 1_000_000_000

        sampler = threading.Thread(target=self._sample_loop,
                                   args=(start_ms,), name="sampler", daemon=True)
        workers = [
            threading.Thread(target=self._worker_loop,
                             args=(worker_id, deadline_ns),
                             name=f"load-worker-{worker_id}", daemon=True)
            for worker_id in range(self.spec.concurrency)
        ]
        sampler.start()
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join()
        if self.fatal_error is not None:
            self.external_stop.set()
            sampler.join(timeout=2)
            raise self.fatal_error
        # Distinguish forced termination (deadline not reached) from normal end.
        deadline_reached = time.monotonic_ns() >= deadline_ns - 5_000_000
        was_signalled = self.external_stop.is_set()
        self.stop_event.set()      # release sampler at normal end too
        sampler.join(timeout=2)
        if was_signalled and not deadline_reached:
            self.interrupted = True

        # Flush only a non-empty trailing partial interval.
        with self._counter_lock:
            has_trailing = self._current.success > 0 or self._current.failure > 0
        if has_trailing:
            self._freeze_sample(tick=len(self._samples), flush=True)
        if self.interrupted:
            return None
        end_ms = self.clock.now_ms()
        success = sum(s.success for s in self._samples)
        failure = sum(s.failure for s in self._samples)
        return Observation(
            stage_index=self.spec.index,
            started_at_ms=start_ms,
            ended_at_ms=end_ms,
            samples=list(self._samples),
            server_confirmed_success=success,
            server_confirmed_failure=failure,
            clock_basis=CLOCK_BASIS,
            truth_source=TRUTH_SOURCE,
        )

    # ------------------------------------------------------------------ sampler
    def _sample_loop(self, start_ms: int) -> None:
        tick = 0
        next_wake = time.monotonic_ns()
        while not self.stop_event.is_set():
            next_wake += self.sample_interval_ms * 1_000_000
            remaining = max(0.0, (next_wake - time.monotonic_ns()) / 1e9)
            if self.stop_event.wait(remaining):
                break
            tick += 1
            self._freeze_sample(tick)

    def _freeze_sample(self, tick: int, flush: bool = False) -> None:
        with self._counter_lock:
            counters = self._current
            if not flush and counters.success == 0 and counters.failure == 0:
                # still record an empty interval so sampling cadence is explicit
                pass
            self._current = IntervalCounters()
        ts_ms = self.clock.now_ms()   # raises ClockError on backwards movement
        self._samples.append(Sample(
            stage_index=self.spec.index,
            tick=tick,
            ts_ms=ts_ms,
            success=counters.success,
            failure=counters.failure,
            latency_ms=counters.latency_ms or [],
        ))

    # ------------------------------------------------------------------ workers
    def _next_req_id(self) -> int:
        with self._req_lock:
            self._req_id += 1
            return self._req_id

    def _worker_loop(self, worker_id: int, deadline_ns: int) -> None:
        try:
            return self._worker_body(worker_id, deadline_ns)
        except Exception as exc:  # propagate clock/business errors to driver
            self.fatal_error = exc
            self.external_stop.set()

    def _worker_body(self, worker_id: int, deadline_ns: int) -> None:
        try:
            conn = socket.create_connection((self.host, self.port), timeout=5)
        except OSError:
            return
        conn.settimeout(5)
        buffer = b""
        try:
            while time.monotonic_ns() < deadline_ns:
                if self.external_stop.is_set():
                    self.interrupted = True
                    break
                self._bucket.take(self.external_stop)
                if self.external_stop.is_set():
                    self.interrupted = True
                    break
                if time.monotonic_ns() >= deadline_ns:
                    break
                req_id = self._next_req_id()
                sent_ms = self.clock.now_ms()
                payload = json.dumps({"cmd": "run", "req_id": req_id,
                                      "seed": worker_id}) + "\n"
                try:
                    conn.sendall(payload.encode("utf-8"))
                    receipt, buffer = self._readline(conn, buffer)
                except (OSError, ValueError):
                    self._record(False, self.clock.now_ms() - sent_ms)
                    continue
                recv_ms = self.clock.now_ms()
                ok = isinstance(receipt, dict) and receipt.get("proto") == \
                    RECEIPT_PROTO and receipt.get("status") == "ok"
                self._record(ok, recv_ms - sent_ms)
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def _readline(self, conn: socket.socket, buffer: bytes) -> tuple[dict, bytes]:
        while b"\n" not in buffer:
            chunk = conn.recv(65536)
            if not chunk:
                raise ConnectionError("server closed connection")
            buffer += chunk
        line, buffer = buffer.split(b"\n", 1)
        return json.loads(line.decode("utf-8")), buffer

    def _record(self, ok: bool, latency_ms: int) -> None:
        latency_ms = max(0, int(latency_ms))
        with self._counter_lock:
            if ok:
                self._current.success += 1
            else:
                self._current.failure += 1
            self._current.latency_ms.append(latency_ms)


def request_stop_after(stop_event: threading.Event, delay_s: float) -> threading.Timer:
    """Test/demo helper: emulate forced termination after `delay_s` seconds."""
    timer = threading.Timer(delay_s, stop_event.set)
    timer.daemon = True
    timer.start()
    return timer
