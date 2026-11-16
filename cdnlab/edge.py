"""Edge node simulator.

Caches origin responses per the configured policy (TTL, key composition,
capacity, eviction rule) on a single logical clock supplied by the
orchestrator (`tick`), so every run is reproducible.
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
from collections import OrderedDict

from .config import load_policy, RoutePolicy
from .errors import E_UPSTREAM
from .protocol import JsonLineServer, send_request

# Hit-attribution outcomes
HIT_FRESH = "HIT_FRESH"
HIT_STALE = "HIT_STALE"
MISS_COLD = "MISS_COLD"
MISS_EXPIRED = "MISS_EXPIRED"
MISS_EVICTED = "MISS_EVICTED"
MISS_ERROR = "MISS_ERROR"
REVALIDATED = "REVALIDATED"

# Consistency verdicts
CONSISTENT = "CONSISTENT"
STALE_SERVED = "STALE_SERVED"
UPSTREAM_FAILED = "UPSTREAM_FAILED"


class CacheEntry:
    __slots__ = (
        "key", "path", "fields", "body", "etag", "last_modified",
        "entry_version", "write_tick", "ttl", "last_access_tick", "freq", "stale",
    )

    def __init__(self, key, path, fields, body, etag, last_modified, entry_version, write_tick, ttl):
        self.key = key
        self.path = path
        self.fields = dict(fields)
        self.body = body
        self.etag = etag
        self.last_modified = last_modified
        self.entry_version = entry_version
        self.write_tick = write_tick
        self.ttl = ttl
        self.last_access_tick = write_tick
        self.freq = 0
        self.stale = False

    def remaining_ttl(self, tick):
        return max(0, self.ttl - (tick - self.write_tick))

    def is_fresh(self, tick):
        return not self.stale and (tick - self.write_tick) < self.ttl

    def to_dict(self, tick):
        return {
            "key": RoutePolicy.key_to_string(self.key),
            "key_tuple": [list(pair) for pair in self.key],
            "path": self.path,
            "fields": dict(self.fields),
            "body": self.body,
            "etag": self.etag,
            "last_modified": self.last_modified,
            "entry_version": self.entry_version,
            "write_tick": self.write_tick,
            "ttl": self.ttl,
            "remaining_ttl": self.remaining_ttl(tick),
            "stale": self.stale,
            "freq": self.freq,
            "last_access_tick": self.last_access_tick,
        }

    @classmethod
    def from_dict(cls, data, key_tuple):
        entry = cls(
            key_tuple, data["path"], data.get("fields", {}), data.get("body", ""),
            data["etag"], data["last_modified"], data["entry_version"],
            data["write_tick"], data["ttl"],
        )
        entry.stale = data.get("stale", False)
        entry.freq = data.get("freq", 0)
        entry.last_access_tick = data.get("last_access_tick", data["write_tick"])
        return entry


class CacheStore:
    """Capacity-bounded cache with lru/lfu/fifo eviction."""

    def __init__(self, capacity, rule):
        self.capacity = capacity
        self.rule = rule
        self.entries = OrderedDict()  # key_string -> CacheEntry (insertion order = fifo order)
        self.evicted_history = []     # list of {key, rule, tick}
        self._evicted_keys = set()

    def _record_eviction(self, key_string, tick):
        event = {"key": key_string, "rule": self.rule, "tick": tick}
        self.evicted_history.append(event)
        self._evicted_keys.add(key_string)
        return event

    def was_evicted(self, key_string):
        return key_string in self._evicted_keys

    def get(self, key_string, tick):
        entry = self.entries.get(key_string)
        if entry is not None:
            entry.freq += 1
            entry.last_access_tick = tick
        return entry

    def put(self, key_string, entry, tick):
        evictions = []
        if key_string in self.entries:
            del self.entries[key_string]
        while len(self.entries) >= self.capacity:
            victim = self._pick_victim()
            del self.entries[victim]
            evictions.append(self._record_eviction(victim, tick))
        self.entries[key_string] = entry
        return evictions

    def _pick_victim(self):
        if self.rule == "fifo":
            return next(iter(self.entries))
        if self.rule == "lru":
            return min(self.entries.items(), key=lambda kv: kv[1].last_access_tick)[0]
        # lfu: lowest frequency, ties broken by oldest access
        return min(self.entries.items(), key=lambda kv: (kv[1].freq, kv[1].last_access_tick))[0]

    def clear(self):
        self.entries.clear()
        self.evicted_history.clear()
        self._evicted_keys.clear()


class EdgeNode:
    def __init__(self, node_id, policy, origin_port):
        self.node_id = node_id
        self.policy = policy
        self.origin_port = origin_port
        self.cache = CacheStore(policy.node_capacity, policy.eviction)
        self._lock = threading.Lock()

    # ---- origin communication ------------------------------------------------
    def _origin_call(self, payload):
        return send_request(self.origin_port, payload)

    def _report(self, report):
        try:
            self._origin_call({"op": "report", "report": report})
        except Exception:
            pass  # reporting is best-effort and must not affect the data path

    # ---- request handling ----------------------------------------------------
    def handle(self, request):
        op = request.get("op")
        if op == "ping":
            return {"ok": True, "role": "edge", "node_id": self.node_id}
        if op == "request":
            with self._lock:
                return self._serve_request(request)
        if op == "cache_state":
            tick = request.get("tick", 0)
            with self._lock:
                return {
                    "ok": True,
                    "node_id": self.node_id,
                    "entries": [e.to_dict(tick) for e in self.cache.entries.values()],
                    "evictions": list(self.cache.evicted_history),
                }
        if op == "reset":
            with self._lock:
                self.cache.clear()
            return {"ok": True}
        if op == "restore":
            with self._lock:
                self.cache.clear()
                for item in request.get("entries", []):
                    key_tuple = tuple(tuple(pair) for pair in item["key_tuple"])
                    entry = CacheEntry.from_dict(item["entry"], key_tuple)
                    self.cache.entries[item["key"]] = entry
                self.cache._evicted_keys = set(request.get("evicted_keys", []))
                self.cache.evicted_history = list(request.get("evictions", []))
            return {"ok": True}
        return {"ok": False, "code": "E_OP", "message": f"unknown op {op!r}"}

    def _serve_request(self, request):
        path = request.get("path")
        fields = request.get("fields", {})
        tick = int(request.get("tick", 0))
        force_validate = bool(request.get("force_validate", False))

        route = self.policy.route_for(path)
        if route is None:
            return {"ok": False, "code": "E_ROUTE", "message": f"no route for path {path!r}"}

        key = route.make_key({**fields, "path": path})
        key_string = RoutePolicy.key_to_string(key)
        entry = self.cache.get(key_string, tick)
        evictions = []

        warnings = []
        if entry is not None and entry.fields != fields:
            # Same cache key but different unkeyed request fields: key collision.
            warnings.append("KEY_COLLISION")

        if entry is None:
            revalidate_reason = "cold"
        elif entry.stale:
            revalidate_reason = "stale"
        elif force_validate:
            revalidate_reason = "forced"
        elif not entry.is_fresh(tick):
            revalidate_reason = "expired"
        else:
            revalidate_reason = None
        need_validate = revalidate_reason is not None

        if not need_validate:
            result = {
                "ok": True, "outcome": HIT_FRESH, "node_id": self.node_id,
                "key": key_string, "etag": entry.etag,
                "last_modified": entry.last_modified,
                "entry_version": entry.entry_version,
                "remaining_ttl": entry.remaining_ttl(tick),
                "stale": False, "consistency": CONSISTENT,
                "warnings": warnings, "evictions": [], "tick": tick,
            }
            self._report({"node": self.node_id, "outcome": HIT_FRESH, "key": key_string, "tick": tick})
            return result

        # Validation / fetch path.
        prior_entry = entry
        had_evicted = self.cache.was_evicted(key_string)
        try:
            if prior_entry is not None:
                origin_resp = self._origin_call(
                    {"op": "validate", "path": path, "etag": prior_entry.etag}
                )
            else:
                origin_resp = self._origin_call({"op": "get", "path": path})
        except (ConnectionError, OSError, json.JSONDecodeError):
            origin_resp = None

        if origin_resp is None or not origin_resp.get("ok"):
            # Upstream unreachable (injected E_UPSTREAM).
            if prior_entry is not None and self.policy.stale_on_upstream_error:
                prior_entry.stale = True
                result = {
                    "ok": True, "outcome": HIT_STALE, "node_id": self.node_id,
                    "key": key_string, "etag": prior_entry.etag,
                    "last_modified": prior_entry.last_modified,
                    "entry_version": prior_entry.entry_version,
                    "remaining_ttl": 0, "stale": True,
                    "consistency": STALE_SERVED, "code": E_UPSTREAM,
                    "warnings": warnings, "evictions": [], "tick": tick,
                }
                self._report({"node": self.node_id, "outcome": HIT_STALE, "key": key_string, "tick": tick})
                return result
            return {
                "ok": False, "code": E_UPSTREAM, "outcome": MISS_ERROR,
                "node_id": self.node_id, "key": key_string,
                "consistency": UPSTREAM_FAILED, "warnings": warnings,
                "evictions": [], "tick": tick,
            }

        status = origin_resp.get("status")
        if status == 304 and prior_entry is not None:
            # Version unchanged: refresh the write time base on the single clock.
            prior_entry.write_tick = tick
            prior_entry.stale = False
            outcome = REVALIDATED
            entry = prior_entry
        elif status == 200:
            body = origin_resp["body"]
            etag = origin_resp["etag"]
            if prior_entry is not None and prior_entry.etag != etag:
                entry_version = prior_entry.entry_version + 1
                outcome = REVALIDATED
            elif prior_entry is not None:
                entry_version = prior_entry.entry_version
                outcome = REVALIDATED
            else:
                entry_version = 1
                outcome = MISS_EVICTED if had_evicted else MISS_COLD
            entry = CacheEntry(
                key, path, fields, body, etag, origin_resp["last_modified"],
                entry_version, tick, route.ttl,
            )
            evictions = self.cache.put(key_string, entry, tick)
        else:
            return {
                "ok": False, "code": "E_ROUTE", "outcome": MISS_ERROR,
                "node_id": self.node_id, "key": key_string,
                "message": f"origin status {status}", "warnings": warnings,
                "evictions": [], "tick": tick,
            }

        result = {
            "ok": True, "outcome": outcome, "node_id": self.node_id,
            "key": key_string, "etag": entry.etag,
            "last_modified": entry.last_modified,
            "entry_version": entry.entry_version,
            "remaining_ttl": entry.remaining_ttl(tick),
            "stale": False, "consistency": CONSISTENT,
            "origin_status": status, "revalidate_reason": revalidate_reason,
            "warnings": warnings, "evictions": evictions, "tick": tick,
        }
        self._report({"node": self.node_id, "outcome": outcome, "key": key_string, "tick": tick})
        return result


def main(argv=None):
    parser = argparse.ArgumentParser(description="cdnlab edge node simulator")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--origin-port", type=int, required=True)
    parser.add_argument("--config", required=True)
    parser.add_argument("--node-id", required=True)
    args = parser.parse_args(argv)

    try:
        policy = load_policy(args.config)
    except Exception as exc:
        code = getattr(exc, "code", "E_CONFIG")
        print(json.dumps({"ok": False, "code": code, "message": str(exc)}), flush=True)
        return 2

    node = EdgeNode(args.node_id, policy, args.origin_port)
    server = JsonLineServer(args.port, node.handle, name=f"edge-{args.node_id}")
    try:
        port = server.start()
    except OSError as exc:
        print(json.dumps({"ok": False, "code": "E_PORT", "message": str(exc)}), flush=True)
        return 2
    print(json.dumps({"ok": True, "role": "edge", "node_id": args.node_id, "port": port}), flush=True)
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        pass
    finally:
        server.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
