"""Cache policy configuration loading and strict validation.

The policy file is a JSON document:

{
  "node_capacity": 3,                 # positive int, per-edge cache capacity
  "eviction": "lru",                  # lru | lfu | fifo
  "stale_on_upstream_error": true,    # serve-marked-stale vs hard error
  "routes": [
    {"path": "/news.txt", "ttl": 4, "key_fields": ["path", "lang"]}
  ]
}

Any violation rejects the whole file with E_CONFIG and a precise location.
"""

from __future__ import annotations

import hashlib
import json

from .errors import E_CONFIG, CdnError

SUPPORTED_KEY_FIELDS = ("path", "query", "header", "cookie", "lang", "device", "host")
EVICTION_RULES = ("lru", "lfu", "fifo")


def _is_positive_int(value):
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


class CachePolicy:
    def __init__(self, node_capacity, eviction, stale_on_upstream_error, routes, raw_text):
        self.node_capacity = node_capacity
        self.eviction = eviction
        self.stale_on_upstream_error = stale_on_upstream_error
        self.routes = routes  # list[RoutePolicy]
        canonical = json.dumps(self.to_dict(), sort_keys=True, ensure_ascii=False)
        self.config_version = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]

    def to_dict(self):
        return {
            "node_capacity": self.node_capacity,
            "eviction": self.eviction,
            "stale_on_upstream_error": self.stale_on_upstream_error,
            "routes": [r.to_dict() for r in self.routes],
        }

    def route_for(self, path):
        for route in self.routes:
            if route.path == path:
                return route
        return None


class RoutePolicy:
    def __init__(self, path, ttl, key_fields):
        self.path = path
        self.ttl = ttl
        self.key_fields = list(key_fields)

    def to_dict(self):
        return {"path": self.path, "ttl": self.ttl, "key_fields": list(self.key_fields)}

    def make_key(self, fields):
        """Build the cache key tuple from request fields per key composition."""
        return tuple((name, fields.get(name, "")) for name in self.key_fields)

    @staticmethod
    def key_to_string(key):
        return "&".join(f"{name}={value}" for name, value in key)


def _fail(location, message):
    raise CdnError(E_CONFIG, message, location=location)


def load_policy(path):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            text = handle.read()
    except OSError as exc:
        _fail("$file", f"cannot read config file: {exc}")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        _fail(f"line {exc.lineno} col {exc.colno}", f"invalid JSON: {exc.msg}")
    return parse_policy(data, text)


def parse_policy(data, raw_text=""):
    if not isinstance(data, dict):
        _fail("$", "top level must be a JSON object")

    capacity = data.get("node_capacity")
    if not _is_positive_int(capacity):
        _fail("node_capacity", f"node_capacity must be a positive integer, got {capacity!r}")

    eviction = data.get("eviction")
    if eviction not in EVICTION_RULES:
        _fail("eviction", f"eviction must be one of {EVICTION_RULES}, got {eviction!r}")

    stale_on_error = data.get("stale_on_upstream_error", True)
    if not isinstance(stale_on_error, bool):
        _fail("stale_on_upstream_error", "stale_on_upstream_error must be a boolean")

    raw_routes = data.get("routes")
    if not isinstance(raw_routes, list) or not raw_routes:
        _fail("routes", "routes must be a non-empty array")

    routes = []
    seen_paths = set()
    for index, raw in enumerate(raw_routes):
        loc = f"routes[{index}]"
        if not isinstance(raw, dict):
            _fail(loc, "route must be an object")
        path = raw.get("path")
        if not isinstance(path, str) or not path.startswith("/"):
            _fail(f"{loc}.path", f"path must be a string starting with '/', got {path!r}")
        if path in seen_paths:
            _fail(f"{loc}.path", f"duplicate path {path!r}")
        seen_paths.add(path)

        ttl = raw.get("ttl")
        if not _is_positive_int(ttl):
            _fail(f"{loc}.ttl", f"ttl must be a positive integer, got {ttl!r}")

        key_fields = raw.get("key_fields")
        if not isinstance(key_fields, list) or not key_fields:
            _fail(f"{loc}.key_fields", "key_fields must be a non-empty array")
        seen_fields = set()
        for field in key_fields:
            if field in seen_fields:
                _fail(f"{loc}.key_fields", f"duplicate key field {field!r}")
            if field not in SUPPORTED_KEY_FIELDS:
                _fail(
                    f"{loc}.key_fields",
                    f"unsupported key field {field!r}; supported: {list(SUPPORTED_KEY_FIELDS)}",
                )
            seen_fields.add(field)
        routes.append(RoutePolicy(path, ttl, key_fields))

    return CachePolicy(capacity, eviction, stale_on_error, routes, raw_text)
