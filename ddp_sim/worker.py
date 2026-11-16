"""Worker process: ring all-reduce over TCP loopback.

Ring topology: rank r listens on base_port + r, sends to rank (r+1) % N.
Chunk c starts its reduce phase at rank c % N and circulates with a fixed
accumulation order (start rank, then ring order), so the floating point
addition sequence never depends on network arrival timing.  A chunk only
enters the all-gather (broadcast) phase after its reduce phase completed
a full ring turn; violating that order raises E_PHASE.
"""

import base64
import json
import os
import queue
import signal
import socket
import threading
import time

import numpy as np

from .errors import E_PHASE, E_PORT, SimError
from .model import digest_array, digest_bytes, local_gradient
from .protocol import recv_message, send_message

PHASE_INIT = "init"
PHASE_REDUCE = "reduce-scatter"
PHASE_GATHER = "all-gather"
PHASE_DONE = "done"


class RingReducer:
    """Per-chunk phase state machine for one rank.

    Pure logic, no sockets: unit tests drive it directly.  `on_send` is
    called with (header, payload) for every message this rank must forward
    to its ring successor.
    """

    def __init__(self, rank, workers, chunk_sizes, local_grad, on_send):
        self.rank = rank
        self.workers = workers
        self.chunk_sizes = chunk_sizes
        self.local_grad = local_grad
        self.on_send = on_send
        n = len(chunk_sizes)
        self.reduce_seen = [False] * n
        self.results = [None] * n          # bytes of the fully reduced chunk
        self.orders = [None] * n           # accumulation order (reducer only)
        self.own_completed = set()         # gather returned to origin

    def owned_chunks(self):
        return [c for c in range(len(self.chunk_sizes)) if c % self.workers == self.rank]

    def _local_chunk(self, c):
        start = sum(self.chunk_sizes[:c])
        return self.local_grad[start:start + self.chunk_sizes[c]]

    def start_chunk(self, c):
        """Inject chunk c into the ring (only valid on its start rank)."""
        if c % self.workers != self.rank:
            raise SimError(E_PHASE, "chunk injected from wrong rank",
                           chunk=c, rank=self.rank)
        if self.reduce_seen[c]:
            raise SimError(E_PHASE, "chunk reduce phase restarted", chunk=c)
        self.reduce_seen[c] = True
        header = {"type": "chunk", "phase": "reduce", "chunk": c,
                  "contrib": 1, "order": [self.rank]}
        self.on_send(header, self._local_chunk(c).tobytes())

    def handle(self, header, payload):
        """Handle one inbound message; returns 'reduce'|'gather'|None."""
        c = int(header["chunk"])
        phase = header["phase"]
        if phase == "reduce":
            if self.reduce_seen[c]:
                raise SimError(E_PHASE, "duplicate reduce for chunk",
                               chunk=c, rank=self.rank)
            self.reduce_seen[c] = True
            incoming = np.frombuffer(payload, dtype=np.float64)
            # Fixed accumulation order: existing partial sum first, then the
            # local contribution -- never reordered by arrival timing.
            acc = incoming + self._local_chunk(c)
            order = list(header["order"]) + [self.rank]
            contrib = int(header["contrib"]) + 1
            if contrib == self.workers:
                self.results[c] = acc.tobytes()
                self.orders[c] = order
                out = {"type": "chunk", "phase": "gather", "chunk": c,
                       "origin": self.rank}
                self.on_send(out, self.results[c])
            else:
                out = {"type": "chunk", "phase": "reduce", "chunk": c,
                       "contrib": contrib, "order": order}
                self.on_send(out, acc.tobytes())
            return "reduce"
        if phase == "gather":
            if not self.reduce_seen[c]:
                raise SimError(
                    E_PHASE,
                    "chunk entered broadcast phase before reduction completed",
                    chunk=c, rank=self.rank)
            self.results[c] = bytes(payload)
            if int(header["origin"]) == self.rank:
                self.own_completed.add(c)
            else:
                self.on_send(header, payload)
            return "gather"
        raise SimError(E_PHASE, "unknown message phase", phase=phase)

    @property
    def done(self):
        return all(r is not None for r in self.results)

    def global_gradient(self):
        return np.concatenate(
            [np.frombuffer(r, dtype=np.float64) for r in self.results])


class _JsonlLogger:
    def __init__(self, path):
        self._fh = open(path, "a", encoding="utf-8") if path else None

    def write(self, record):
        if self._fh:
            self._fh.write(json.dumps(record, sort_keys=True) + "\n")
            self._fh.flush()

    def close(self):
        if self._fh:
            self._fh.close()


def _connect_with_retry(host, port, timeout_s):
    deadline = time.monotonic() + timeout_s
    while True:
        try:
            return socket.create_connection((host, port), timeout=5.0)
        except OSError:
            if time.monotonic() > deadline:
                raise
            time.sleep(0.05)


def run_worker(cfg, rank, events):
    """Entry point of one worker process.  `events` is a mp.Queue."""
    workers = cfg["workers"]
    dim = cfg["dim"]
    step = int(cfg["step"])
    delay_s = float(cfg["delays_ms"].get(str(rank), cfg["delays_ms"].get(rank, 0.0))) / 1000.0
    log_path = os.path.join(cfg["log_dir"], f"worker-{rank}.jsonl") if cfg.get("log_dir") else None
    logger = _JsonlLogger(log_path)
    stop = threading.Event()

    def _graceful(signum, frame):
        # Best-effort exit report on catchable signals; never promised for
        # every worker (SIGKILL cannot be handled).
        stop.set()
        logger.write({"event": "exit_report", "rank": rank, "signal": signum,
                      "ts": time.time()})

    signal.signal(signal.SIGTERM, _graceful)
    signal.signal(signal.SIGINT, _graceful)

    started = time.monotonic()
    try:
        params = np.frombuffer(base64.b64decode(cfg["params_b64"]), dtype=np.float64).copy()
        local_grad = local_gradient(cfg["seed"], rank, step, dim)
        logger.write({"event": "init", "rank": rank, "step": step,
                      "shard_digest": digest_array(local_grad),
                      "param_digest": digest_array(params), "ts": time.time()})

        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            srv.bind(("127.0.0.1", cfg["base_port"] + rank))
        except OSError as exc:
            raise SimError(E_PORT, "loopback port already in use",
                           port=cfg["base_port"] + rank, rank=rank, error=str(exc))
        srv.listen(1)
        srv.settimeout(cfg["recv_timeout_s"])

        out_sock = _connect_with_retry("127.0.0.1",
                                       cfg["base_port"] + (rank + 1) % workers,
                                       cfg["recv_timeout_s"])
        conn, _ = srv.accept()
        conn.settimeout(cfg["recv_timeout_s"])
        srv.close()

        send_q = queue.Queue()
        counters = {"sent": 0, "recv": 0}
        timing = []

        def emit_status(phase):
            events.put({"kind": "status", "rank": rank, "phase": phase,
                        "sent": counters["sent"], "recv": counters["recv"],
                        "elapsed": time.monotonic() - started})

        def sender():
            while True:
                item = send_q.get()
                if item is None:
                    return
                header, payload = item
                if delay_s:
                    time.sleep(delay_s)  # injected scheduling delay only
                send_message(out_sock, header, payload)
                counters["sent"] += 1
                timing.append({"chunk": header.get("chunk"), "phase": header["phase"],
                               "send_ts": time.time(), "recv_ts": None})
                emit_status(reducer_phase())

        reducer_holder = {}

        def reducer_phase():
            red = reducer_holder.get("red")
            if red is None:
                return PHASE_INIT
            if red.done:
                return PHASE_DONE
            if all(red.reduce_seen):
                return PHASE_GATHER
            return PHASE_REDUCE

        send_thread = threading.Thread(target=sender, daemon=True)
        send_thread.start()

        reducer = RingReducer(rank, workers, cfg["chunk_sizes"], local_grad,
                              lambda h, p: send_q.put((h, p)))
        reducer_holder["red"] = reducer

        inflight = threading.Semaphore(int(cfg["interleave"]))

        def injector():
            for c in reducer.owned_chunks():
                inflight.acquire()
                if stop.is_set():
                    inflight.release()
                    return
                reducer.start_chunk(c)

        inj_thread = threading.Thread(target=injector, daemon=True)
        inj_thread.start()

        while not reducer.done:
            if stop.is_set():
                raise SimError("E_STOP", "worker stopped by signal", rank=rank)
            header, payload = recv_message(conn)
            counters["recv"] += 1
            kind = reducer.handle(header, payload)
            timing.append({"chunk": header.get("chunk"), "phase": kind,
                           "send_ts": None, "recv_ts": time.time()})
            if kind == "gather" and int(header.get("origin", -1)) == rank:
                inflight.release()
            emit_status(reducer_phase())

        # Let the injector finish so permits stay consistent.
        inj_thread.join(timeout=5.0)
        send_q.put(None)
        send_thread.join(timeout=5.0)

        global_grad = reducer.global_gradient()
        mean_grad = global_grad / workers
        new_params = params - float(cfg["lr"]) * mean_grad
        version = step + 1
        elapsed = time.monotonic() - started

        done_record = {
            "kind": "done", "rank": rank, "version": version,
            "grad_digest": digest_array(global_grad),
            "param_digest": digest_array(new_params),
            "chunk_digests": [digest_bytes(r) for r in reducer.results],
            "acc_orders": reducer.orders,
            "global_grad_b64": base64.b64encode(global_grad.tobytes()).decode("ascii"),
            "sent": counters["sent"], "recv": counters["recv"],
            "elapsed": elapsed, "timing": timing,
        }
        logger.write({"event": "done", **{k: v for k, v in done_record.items()
                                          if k not in ("global_grad_b64", "timing")}})
        logger.close()
        events.put(done_record)
        conn.close()
        out_sock.close()
    except SimError as exc:
        logger.write({"event": "error", "rank": rank, **exc.to_dict()})
        logger.close()
        events.put({"kind": "error", "rank": rank, **exc.to_dict()})
    except Exception as exc:  # noqa: BLE001 - report anything to controller
        logger.write({"event": "error", "rank": rank, "code": "E_INTERNAL",
                      "message": str(exc)})
        logger.close()
        events.put({"kind": "error", "rank": rank, "code": "E_INTERNAL",
                    "message": str(exc), "context": {}})
