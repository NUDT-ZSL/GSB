"""Cross-version snapshot merge and the unified in-memory index.

Adjudication for the same (doc_id, field) present in both generations:
  1. higher document version wins;
  2. tie -> higher declared logical clock wins;
  3. tie -> source format priority (v2 > v1).
Tombstoned documents are excluded entirely. The result is independent of
load order because candidates are compared, not applied sequentially.
"""

import bisect

import numpy as np

from .stablejson import digest

SOURCE_PRIORITY = {"v2": 2, "v1": 1}


def _sort_key(value):
    """Total, deterministic ordering across ints/floats/strings."""
    if isinstance(value, str):
        return (1, value)
    return (0, float(value))


def adjudicate(candidates):
    """Pick the winning candidate; candidates are dicts with version,
    clock_ms and source. Returns (winner, rule)."""
    best = None
    rule = "unique"
    for cand in candidates:
        if best is None:
            best = cand
            continue
        if cand["version"] != best["version"]:
            if cand["version"] > best["version"]:
                best = cand
            rule = "version"
        elif cand["clock_ms"] != best["clock_ms"]:
            if cand["clock_ms"] > best["clock_ms"]:
                best = cand
            rule = "clock"
        else:
            if SOURCE_PRIORITY[cand["source"]] > SOURCE_PRIORITY[best["source"]]:
                best = cand
            rule = "priority"
    return best, rule


class FieldIndex:
    """Per-field inverted index with bidirectional access: exact key lookup
    and value-range scan. The sorted key projection is held in a NumPy
    object array; range bounds come from binary search over that order."""

    def __init__(self):
        self._entries = []
        self._sorted = []
        self._keys = None

    def add(self, value, doc_id):
        self._entries.append((_sort_key(value), value, doc_id))

    def freeze(self):
        self._sorted = sorted(self._entries, key=lambda e: (e[0], e[2]))
        keys = [k for k, _, _ in self._sorted]
        self._keys = np.empty(len(keys), dtype=object)
        self._keys[:] = keys

    def _bounds(self, lo_key, hi_key):
        keys = self._keys.tolist()
        return bisect.bisect_left(keys, lo_key), bisect.bisect_right(keys, hi_key)

    def eq(self, value):
        key = _sort_key(value)
        lo, hi = self._bounds(key, key)
        return sorted({doc for _, _, doc in self._sorted[lo:hi]})

    def range(self, lo_value, hi_value):
        lo, hi = self._bounds(_sort_key(lo_value), _sort_key(hi_value))
        return sorted({doc for _, _, doc in self._sorted[lo:hi]})

    def value_type(self):
        for _, value, _ in self._sorted:
            return "string" if isinstance(value, str) else "number"
        return None

    def interval_digest(self):
        """Digest of the sorted (key, doc) interval, recorded per batch."""
        return digest(
            [{"key": list(k), "doc_id": d} for k, _, d in self._sorted]
        )


class UnifiedIndex:
    def __init__(self):
        self.docs = {}
        self.fields = {}
        self.tombstoned = []

    def freeze(self):
        for fi in self.fields.values():
            fi.freeze()

    def field_index(self, field):
        return self.fields.get(field)

    def provenance(self, doc_id):
        return self.docs.get(doc_id, {})


def build_unified_index(v1, v2):
    """Merge validated v1/v2 snapshots into a UnifiedIndex. Deterministic
    and independent of the order the snapshots were loaded in."""
    tombstoned = set(v2["tombstones"])
    candidates = {}

    def add_candidate(source, snap_clock, doc, field, value, spans):
        span = next((s for s in spans if s["field"] == field), None)
        cand = {
            "source": source,
            "version": doc["version"],
            "clock_ms": doc.get("clock_ms", snap_clock),
            "value": value,
            "span": span,
        }
        candidates.setdefault((doc["doc_id"], field), []).append(cand)

    for doc in v1["documents"].values():
        if doc["doc_id"] in tombstoned:
            continue
        for field, value in doc.get("fields", {}).items():
            add_candidate(
                "v1", v1["clock_ms"], doc, field, value, doc.get("spans", [])
            )
    for doc in v2["documents"].values():
        if doc["doc_id"] in tombstoned:
            continue
        for field, value in doc.get("fields", {}).items():
            add_candidate(
                "v2", v2["clock_ms"], doc, field, value, doc.get("spans", [])
            )

    index = UnifiedIndex()
    index.tombstoned = sorted(tombstoned)
    for doc_id, field in sorted(candidates):
        winner, rule = adjudicate(candidates[(doc_id, field)])
        record = {
            "value": winner["value"],
            "source": winner["source"],
            "version": winner["version"],
            "clock_ms": winner["clock_ms"],
            "rule": rule,
            "span": winner["span"],
        }
        index.docs.setdefault(doc_id, {})[field] = record
        index.fields.setdefault(field, FieldIndex()).add(winner["value"], doc_id)
    index.freeze()
    return index


def merge_report(index):
    """Deterministic, JSON-serializable view of adjudication decisions."""
    decisions = []
    for doc_id in sorted(index.docs):
        for field in sorted(index.docs[doc_id]):
            rec = index.docs[doc_id][field]
            decisions.append(
                {
                    "doc_id": doc_id,
                    "field": field,
                    "value": rec["value"],
                    "source": rec["source"],
                    "version": rec["version"],
                    "clock_ms": rec["clock_ms"],
                    "rule": rec["rule"],
                    "span": rec["span"],
                }
            )
    return {
        "decisions": decisions,
        "tombstoned": list(index.tombstoned),
        "doc_count": len(index.docs),
        "field_count": len(index.fields),
    }
