"""Data plane: request forwarding, connection pooling, traffic mirroring.

Key properties:
  - every request binds the rule snapshot current at its start and keeps it
    until the end (read isolation: snapshots are immutable)
  - in-flight mirrors resolve the shadow member from their start snapshot, so
    a rule update can never redirect them to an invalidated shadow upstream
  - mirror failures never affect the main request outcome or decision chain
  - upstream errors map to business error codes and are recorded in the chain
"""

import threading
from concurrent.futures import ThreadPoolExecutor

import httpx

from .errors import ErrorCode
from .hashing import short_digest
from .router import DecisionEngine


class ConnectionPool:
    """httpx clients keyed by (member_id, config digest).

    A member config change yields a new key, so a rebuild can never produce a
    half-updated pool entry: readers either get the complete old client or the
    complete new one. invalidate() retires stale clients instead of closing
    them immediately, so in-flight requests bound to an older snapshot can
    finish on the old connection (connection draining); retired clients are
    closed by reap() / close_all().
    """

    def __init__(self, timeout_ms):
        self._timeout = httpx.Timeout(timeout_ms / 1000.0)
        self._clients = {}
        self._retired = []
        self._lock = threading.Lock()

    @staticmethod
    def key_for(member):
        return (member["id"], short_digest(member["spec"]))

    def client_for(self, member):
        key = self.key_for(member)
        with self._lock:
            client = self._clients.get(key)
            if client is None:
                # trust_env=False: never route loopback upstreams through a
                # proxy configured in the process environment
                client = httpx.Client(timeout=self._timeout, trust_env=False)
                self._clients[key] = client
            return client

    def invalidate(self, member_ids):
        """Retire pool entries for the given member ids (no new users)."""
        with self._lock:
            doomed = [k for k in self._clients if k[0] in member_ids]
            for key in doomed:
                self._retired.append(self._clients.pop(key))
        return sorted(mid for (mid, _) in doomed)

    def reap(self):
        """Close retired clients whose in-flight users have finished."""
        with self._lock:
            retired, self._retired = self._retired, []
        for client in retired:
            try:
                client.close()
            except Exception:
                pass
        return len(retired)

    def close_all(self):
        self.reap()
        with self._lock:
            clients = list(self._clients.values())
            self._clients.clear()
        for client in clients:
            try:
                client.close()
            except Exception:
                pass


def classify_upstream_error(exc):
    """Map an httpx failure to a business error code."""
    if isinstance(exc, httpx.ConnectError):
        return ErrorCode.UPSTREAM_CONN_REFUSED
    if isinstance(exc, httpx.TimeoutException):
        return ErrorCode.UPSTREAM_TIMEOUT
    if isinstance(exc, httpx.RemoteProtocolError):
        message = str(exc).lower()
        if "disconnect" in message or "without sending" in message or "closed" in message:
            return ErrorCode.UPSTREAM_DISCONNECT
        return ErrorCode.UPSTREAM_MALFORMED
    return ErrorCode.UPSTREAM_MALFORMED


class DataPlane:
    def __init__(self, store, clock, timeout_ms=500, max_workers=4):
        self.store = store
        self.clock = clock
        self.pool = ConnectionPool(timeout_ms)
        self.executor = ThreadPoolExecutor(max_workers=max_workers,
                                           thread_name_prefix="mirror")
        self._futures = []
        self._lock = threading.Lock()

    def apply_invalidation(self, event):
        """Rebuild pool entries invalidated by a committed patch."""
        member_ids = [s.split("/", 1)[1]
                      for s in event["invalidated"]["connection_pool"]]
        if member_ids:
            self.pool.invalidate(set(member_ids))

    def handle(self, request):
        """Process one request end to end. Never raises for upstream errors."""
        snapshot = self.store.load_snapshot()  # bound for the whole request
        events = self.store.latest_invalidation(snapshot.version)
        engine = DecisionEngine(snapshot, self.clock, events)
        chain = engine.decide(request)
        if chain.get("error_code"):
            chain["finished_ms"] = self.clock.now_ms()
            self.store.record_decision(chain["request_seq"], chain["version"], chain)
            return chain

        member = snapshot.get("upstream_member", chain["member_id"])
        status, error_code, error_detail = self._send(member, request)
        chain["outcome"] = "ok" if error_code is None else "error"
        if error_code is None:
            chain["status_code"] = status
        else:
            chain["error_code"] = error_code
            chain["error_detail"] = error_detail

        mirror = chain.get("mirror")
        if mirror and mirror.get("sampled"):
            self._start_mirror(snapshot, chain, request)

        chain["finished_ms"] = self.clock.now_ms()
        self.store.record_decision(chain["request_seq"], chain["version"], chain)
        return chain

    def _send(self, member, request):
        """Send to an upstream member. Returns (status, error_code, detail)."""
        spec = member["spec"]
        url = "http://%s:%d%s" % (spec["host"], spec["port"], request.get("path", "/"))
        try:
            client = self.pool.client_for(member)
            response = client.request(
                request.get("method", "GET"), url,
                headers={k: v for k, v in request.get("headers", {}).items()
                         if k.lower() != "host"},
                content=request.get("body", b""),
            )
            return response.status_code, None, None
        except httpx.HTTPError as exc:
            return None, classify_upstream_error(exc), str(exc).split("\n")[0][:200]

    def _start_mirror(self, snapshot, chain, request):
        mirror = chain["mirror"]
        mirror_clock = self.clock.fork()
        mirror["request_seq"] = chain["request_seq"]
        record = {
            "request_seq": chain["request_seq"],
            "version": chain["version"],
            "rules_digest": chain["rules_digest"],
            "policy_id": mirror["policy_id"],
            "shadow_member_id": mirror["shadow_member_id"],
            "started_ms": mirror_clock.now_ms(),
        }
        mirror_seq = self.store.mirror_start(chain["request_seq"], chain["version"], record)
        mirror["mirror_seq"] = mirror_seq
        if mirror["async"]:
            future = self.executor.submit(
                self._run_mirror, mirror_seq, snapshot, mirror, request, mirror_clock)
            with self._lock:
                self._futures.append(future)
        else:
            self._run_mirror(mirror_seq, snapshot, mirror, request, mirror_clock)

    def _run_mirror(self, mirror_seq, snapshot, mirror, request, mirror_clock):
        """Execute one mirror copy against the start-of-request snapshot.

        Exceptions are recorded in the mirror record and never propagate.
        """
        member = snapshot.get("upstream_member", mirror["shadow_member_id"])
        _, error_code, detail = self._send(member, request)
        record = {
            "request_seq": mirror.get("request_seq"),
            "version": snapshot.version,
            "rules_digest": snapshot.digest,
            "policy_id": mirror["policy_id"],
            "shadow_member_id": mirror["shadow_member_id"],
            "outcome": "completed" if error_code is None else "failed",
            "error_code": error_code,
            "error_detail": detail,
            "response_dropped": bool(mirror.get("drop_response")),
            "finished_ms": mirror_clock.now_ms(),
        }
        state = "completed" if error_code is None else "failed"
        self.store.mirror_finish(mirror_seq, state, record)

    def flush_mirrors(self):
        """Wait for all in-flight async mirrors (demo/test determinism)."""
        while True:
            with self._lock:
                futures, self._futures = self._futures, []
            if not futures:
                return
            for future in futures:
                future.result()

    def close(self):
        self.flush_mirrors()
        self.executor.shutdown(wait=True)
        self.pool.close_all()
