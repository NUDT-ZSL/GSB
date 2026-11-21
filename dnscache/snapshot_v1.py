"""v1 snapshot: single JSON file with remaining-seconds TTLs."""
import json

from .errors import (
    BizError, E_V1_SCHEMA, E_V1_DUPLICATE_DOMAIN, E_V1_TTL_INVALID,
    E_V1_CLOCK_INVALID,
)
from .normalize import canon

FORMAT = "v1"


def _is_nonneg_int(value):
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


class V1Snapshot:
    def __init__(self, logical_clock_us, generation, entries):
        self.format = FORMAT
        self.logical_clock_us = logical_clock_us
        self.generation = generation
        self.entries = entries  # list[dict], validated & canonicalized


def load_v1(path):
    """Parse and validate a v1 snapshot. Raises BizError on any violation;
    callers must not persist anything when this raises."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        raise BizError(E_V1_SCHEMA, "v1 snapshot unreadable", {"error": str(exc)})
    if not isinstance(doc, dict) or doc.get("format") != FORMAT:
        raise BizError(E_V1_SCHEMA, "missing format=v1")
    clock_us = doc.get("logical_clock_us")
    if not _is_nonneg_int(clock_us):
        raise BizError(E_V1_CLOCK_INVALID, "logical_clock_us must be a non-negative integer")
    generation = doc.get("generation", 0)
    if not _is_nonneg_int(generation):
        raise BizError(E_V1_SCHEMA, "generation must be a non-negative integer")
    raw_entries = doc.get("entries")
    if not isinstance(raw_entries, list):
        raise BizError(E_V1_SCHEMA, "entries must be a list")

    seen = set()
    entries = []
    for idx, raw in enumerate(raw_entries):
        ctx = {"index": idx}
        if not isinstance(raw, dict):
            raise BizError(E_V1_SCHEMA, "entry must be an object", ctx)
        name = canon(raw.get("name"))
        rtype = raw.get("type")
        if not isinstance(rtype, str) or not rtype.isalpha() or not rtype.isupper():
            raise BizError(E_V1_SCHEMA, "type must be an upper-case letters string",
                           {**ctx, "type": rtype})
        ttl = raw.get("ttl_remaining_s")
        if not _is_nonneg_int(ttl):
            raise BizError(E_V1_TTL_INVALID,
                           "ttl_remaining_s must be a non-negative integer",
                           {**ctx, "name": name, "type": rtype, "value": repr(ttl)})
        records = raw.get("records", [])
        if not isinstance(records, list):
            raise BizError(E_V1_SCHEMA, "records must be a list", ctx)
        authoritative = bool(raw.get("authoritative", False))
        key = (name, rtype)
        if key in seen:
            raise BizError(E_V1_DUPLICATE_DOMAIN,
                           "duplicate (name, type) after canonicalization",
                           {"name": name, "type": rtype})
        seen.add(key)
        entries.append({
            "name": name,
            "type": rtype,
            "ttl_remaining_s": ttl,
            "records": records,
            "authoritative": authoritative,
            # v1 authoritative flag maps onto the multi-level index.
            "level": "authoritative" if authoritative else "local",
        })
    return V1Snapshot(clock_us, generation, entries)
