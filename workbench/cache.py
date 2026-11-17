"""Content-fingerprint trajectory cache with invalidation classification."""

from __future__ import annotations

import hashlib
import json
import os

from .decoders import DECODER_IMPL_VERSION

INVALIDATION_POLICY = "policy"
INVALIDATION_REQUEST = "request"
INVALIDATION_SCORETABLE = "scoretable"
INVALIDATION_MISSING = "missing"


def cache_key(context_fp, policy_fp, score_fp):
    blob = f"{context_fp}|{policy_fp}|{score_fp}|{DECODER_IMPL_VERSION}"
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def atomic_write_json(path, data):
    """Write JSON via temp file + atomic replace."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2, sort_keys=True)
        fh.write("\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


class TrajectoryStore:
    """On-disk trajectory artifacts keyed by content fingerprint cache key."""

    def __init__(self, directory):
        self.directory = directory
        os.makedirs(directory, exist_ok=True)

    def _path(self, key):
        return os.path.join(self.directory, f"{key}.json")

    def has(self, key):
        return os.path.exists(self._path(key))

    def save(self, key, trace):
        atomic_write_json(self._path(key), trace)

    def load(self, key):
        with open(self._path(key), "r", encoding="utf-8") as fh:
            return json.load(fh)


def classify_invalidation(request_id, prev_state, score_fp, policy_fp, override_fps):
    """Return the invalidation source for a request, or None if reusable.

    Precedence: scoretable-level > request-level > policy-level.
    """
    if prev_state is None:
        return INVALIDATION_MISSING
    if prev_state.get("score_fp") != score_fp:
        return INVALIDATION_SCORETABLE
    prev_override = prev_state.get("override_fps", {}).get(request_id)
    cur_override = override_fps.get(request_id)
    if prev_override != cur_override:
        return INVALIDATION_REQUEST
    if cur_override is None and prev_state.get("policy_fp") != policy_fp:
        return INVALIDATION_POLICY
    return None
