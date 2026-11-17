"""Connection declaration manifest: loading and strong validation.

All validation failures raise DiagError(E_DECL) with the offending field
path; the whole manifest is rejected on the first violation.
"""

import json

from .errors import DiagError, E_DECL

# Protocol limits (RFC 9113)
MAX_WINDOW = 2**31 - 1
MIN_MAX_FRAME_SIZE = 16384
MAX_MAX_FRAME_SIZE = 2**24 - 1
MIN_WEIGHT = 1
MAX_WEIGHT = 256
INITIAL_CONNECTION_WINDOW = 65535

# Local workbench constraints (deterministic bounds, not protocol limits)
LOCAL_MAX_CONCURRENT_STREAMS = 256
LOCAL_MAX_STREAMS = 512
LOCAL_MAX_BODY_BYTES = 16 * 1024 * 1024
LOCAL_MAX_RESPONSE_BYTES = 1024 * 1024
LOCAL_MAX_GRANT_LATENCY_MS = 1000

_STRATEGIES = ("weighted_rr", "window_first", "arrival")


def _fail(message, path):
    raise DiagError(E_DECL, message, path=path)


def _require(cond, message, path):
    if not cond:
        _fail(message, path)


def _int_field(obj, key, path, lo, hi, required=True, default=None):
    if key not in obj:
        if required:
            _fail(f"missing required field '{key}'", path)
        return default
    value = obj[key]
    _require(isinstance(value, int) and not isinstance(value, bool),
             f"field '{key}' must be an integer", f"{path}.{key}")
    _require(lo <= value <= hi,
             f"field '{key}' must be in [{lo}, {hi}], got {value}",
             f"{path}.{key}")
    return value


def _validate_window_pair(obj, key, path):
    pair = obj.get(key)
    _require(isinstance(pair, dict), f"field '{key}' must be an object",
             f"{path}.{key}")
    stream_window = _int_field(pair, "stream_window", f"{path}.{key}",
                               0, MAX_WINDOW)
    connection_window = _int_field(pair, "connection_window",
                                   f"{path}.{key}",
                                   INITIAL_CONNECTION_WINDOW, MAX_WINDOW)
    return {"stream_window": stream_window,
            "connection_window": connection_window}


def load_manifest(path):
    """Load and validate a declaration manifest from a local JSON file."""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            raw = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise DiagError(E_DECL, f"cannot read manifest: {exc}", path="$")
    return validate_manifest(raw)


def validate_manifest(raw):
    _require(isinstance(raw, dict), "manifest root must be an object", "$")

    version = _int_field(raw, "version", "$", 1, 2**31 - 1)
    name = raw.get("name", f"manifest-v{version}")
    _require(isinstance(name, str), "field 'name' must be a string", "$.name")

    conn = raw.get("connection")
    _require(isinstance(conn, dict), "field 'connection' must be an object",
             "$.connection")
    max_frame_size = _int_field(conn, "max_frame_size", "$.connection",
                                MIN_MAX_FRAME_SIZE, MAX_MAX_FRAME_SIZE)
    max_concurrent = _int_field(conn, "max_concurrent_streams",
                                "$.connection",
                                1, LOCAL_MAX_CONCURRENT_STREAMS)
    client_send = _validate_window_pair(conn, "client_send", "$.connection")
    server_send = _validate_window_pair(conn, "server_send", "$.connection")

    peer = raw.get("peer")
    _require(isinstance(peer, dict), "field 'peer' must be an object",
             "$.peer")
    grant_increment = _int_field(peer, "grant_increment", "$.peer",
                                 1, MAX_WINDOW)
    grant_latency = _int_field(peer, "grant_latency_ms", "$.peer",
                               0, LOCAL_MAX_GRANT_LATENCY_MS)
    response_bytes = _int_field(peer, "response_bytes", "$.peer",
                                0, LOCAL_MAX_RESPONSE_BYTES)

    policy = raw.get("policy", {})
    _require(isinstance(policy, dict), "field 'policy' must be an object",
             "$.policy")
    tie_threshold = _int_field(policy, "tie_threshold_ms", "$.policy",
                               0, 10**9, required=False, default=0)

    streams_raw = raw.get("streams")
    _require(isinstance(streams_raw, list) and len(streams_raw) > 0,
             "field 'streams' must be a non-empty array", "$.streams")
    _require(len(streams_raw) <= LOCAL_MAX_STREAMS,
             f"stream count must not exceed {LOCAL_MAX_STREAMS}",
             "$.streams")

    seen_ids = set()
    streams = []
    for index, entry in enumerate(streams_raw):
        path = f"$.streams[{index}]"
        _require(isinstance(entry, dict), "stream entry must be an object",
                 path)
        stream_id = _int_field(entry, "id", path, 1, 2**31 - 1)
        _require(stream_id not in seen_ids,
                 f"duplicate stream id {stream_id}", f"{path}.id")
        seen_ids.add(stream_id)
        weight = _int_field(entry, "weight", path, MIN_WEIGHT, MAX_WEIGHT)
        depends_on = _int_field(entry, "depends_on", path, 0, 2**31 - 1)
        body_bytes = _int_field(entry, "body_bytes", path,
                                0, LOCAL_MAX_BODY_BYTES)
        stream_path = entry.get("path", f"/stream-{stream_id}")
        _require(isinstance(stream_path, str),
                 "field 'path' must be a string", f"{path}.path")
        streams.append({
            "id": stream_id,
            "weight": weight,
            "depends_on": depends_on,
            "body_bytes": body_bytes,
            "path": stream_path,
            "arrival_index": index,
        })

    # Dependency rules: parent must exist, be declared earlier (so arrival
    # order is already a topological order), and the graph must be acyclic.
    for index, stream in enumerate(streams):
        parent = stream["depends_on"]
        path = f"$.streams[{index}].depends_on"
        if parent == 0:
            continue
        _require(parent in seen_ids,
                 f"dependency parent {parent} is not a declared stream",
                 path)
        _require(parent != stream["id"],
                 "stream must not depend on itself", path)
        parent_index = next(s["arrival_index"] for s in streams
                            if s["id"] == parent)
        _require(parent_index < index,
                 f"dependency parent {parent} must be declared before "
                 f"stream {stream['id']} (arrival order must be a "
                 "topological order)", path)
    _check_acyclic(streams)

    return {
        "version": version,
        "name": name,
        "connection": {
            "max_frame_size": max_frame_size,
            "max_concurrent_streams": max_concurrent,
            "client_send": client_send,
            "server_send": server_send,
        },
        "peer": {
            "grant_increment": grant_increment,
            "grant_latency_ms": grant_latency,
            "response_bytes": response_bytes,
        },
        "policy": {"tie_threshold_ms": tie_threshold},
        "streams": streams,
    }


def _check_acyclic(streams):
    parent_of = {s["id"]: s["depends_on"] for s in streams}
    for stream in streams:
        visited = set()
        node = stream["id"]
        while node != 0:
            if node in visited:
                _fail(f"dependency cycle detected at stream {node}",
                      "$.streams")
            visited.add(node)
            node = parent_of.get(node, 0)


def strategies():
    """Fixed candidate strategy order used for deterministic tie-breaks."""
    return _STRATEGIES
