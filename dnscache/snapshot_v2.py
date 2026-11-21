"""v2 snapshot: sharded JSON files + SQLite metadata.

Layout of a v2 snapshot directory:
  manifest.json   {"format":"v2","version":N,"min_generation":G,
                   "logical_clock_us":U,"shards":[{"file","sha256"}]}
  meta.sqlite     table shard_meta(shard TEXT PRIMARY KEY, generation INTEGER)
  <shard>.json    {"entries":[{"name","type","level","expiry_ms","negative",
                               "records","generation"}]}
"""
import hashlib
import json
import os
import sqlite3

from .errors import (
    BizError, E_V2_SCHEMA, E_V2_SHARD_HASH_MISMATCH,
    E_V2_GENERATION_REGRESSION, E_V2_DUPLICATE_ENTRY, E_V2_EXPIRY_INVALID,
)
from .normalize import canon

FORMAT = "v2"
LEVELS = ("authoritative", "recursive", "local")


def _is_nonneg_int(value):
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


class V2Snapshot:
    def __init__(self, version, min_generation, logical_clock_us, entries,
                 shard_hashes, manifest_digest):
        self.format = FORMAT
        self.version = version
        self.min_generation = min_generation
        self.logical_clock_us = logical_clock_us
        self.entries = entries
        self.shard_hashes = shard_hashes          # {file: sha256}
        self.manifest_digest = manifest_digest    # digest of raw manifest bytes


def load_v2(directory, generation_high_water=0):
    """Parse and validate a v2 snapshot directory.

    generation_high_water: highest cache generation already accepted by this
    store; shard generations must be >= to it (monotonicity check).
    """
    manifest_path = os.path.join(directory, "manifest.json")
    try:
        with open(manifest_path, "rb") as fh:
            manifest_raw = fh.read()
        manifest = json.loads(manifest_raw.decode("utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise BizError(E_V2_SCHEMA, "manifest unreadable", {"error": str(exc)})
    if not isinstance(manifest, dict) or manifest.get("format") != FORMAT:
        raise BizError(E_V2_SCHEMA, "missing format=v2")
    version = manifest.get("version")
    min_generation = manifest.get("min_generation")
    clock_us = manifest.get("logical_clock_us")
    shards = manifest.get("shards")
    for field_value, field_name in ((version, "version"),
                                    (min_generation, "min_generation"),
                                    (clock_us, "logical_clock_us")):
        if not _is_nonneg_int(field_value):
            raise BizError(E_V2_SCHEMA, f"{field_name} must be a non-negative integer")
    if not isinstance(shards, list) or not shards:
        raise BizError(E_V2_SCHEMA, "shards must be a non-empty list")
    meta_path = os.path.join(directory, "meta.sqlite")
    try:
        conn = sqlite3.connect(meta_path)
        try:
            rows = conn.execute(
                "SELECT shard, generation FROM shard_meta").fetchall()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        raise BizError(E_V2_SCHEMA, "meta.sqlite unreadable", {"error": str(exc)})
    shard_generations = {shard: gen for shard, gen in rows}
    shard_hashes = {}
    entries = []
    seen = set()
    for shard in shards:
        if not isinstance(shard, dict):
            raise BizError(E_V2_SCHEMA, "shard entry must be an object")
        fname = shard.get("file")
        expected = shard.get("sha256")
        if not isinstance(fname, str) or not isinstance(expected, str):
            raise BizError(E_V2_SCHEMA, "shard needs file and sha256")
        shard_path = os.path.join(directory, fname)
        actual = _sha256_file(shard_path)
        if actual != expected:
            raise BizError(E_V2_SHARD_HASH_MISMATCH, "shard hash mismatch",
                           {"file": fname, "expected": expected, "actual": actual})
        shard_hashes[fname] = actual
        generation = shard_generations.get(fname)
        if not _is_nonneg_int(generation):
            raise BizError(E_V2_SCHEMA, "shard missing from meta.sqlite",
                           {"file": fname})
        if generation < generation_high_water:
            raise BizError(E_V2_GENERATION_REGRESSION,
                           "shard generation below high-water mark",
                           {"file": fname, "generation": generation,
                            "high_water": generation_high_water})
        with open(shard_path, "r", encoding="utf-8") as fh:
            doc = json.load(fh)
        for idx, raw in enumerate(doc.get("entries", [])):
            ctx = {"file": fname, "index": idx}
            name = canon(raw.get("name"))
            rtype = raw.get("type")
            level = raw.get("level")
            expiry_ms = raw.get("expiry_ms")
            if level not in LEVELS:
                raise BizError(E_V2_SCHEMA, "invalid level", {**ctx, "level": level})
            if not _is_nonneg_int(expiry_ms):
                raise BizError(E_V2_EXPIRY_INVALID,
                               "expiry_ms must be a non-negative integer",
                               {**ctx, "name": name, "value": repr(expiry_ms)})
            negative = bool(raw.get("negative", False))
            records = raw.get("records", [])
            if negative and records:
                raise BizError(E_V2_SCHEMA, "negative entry must not carry records",
                               {**ctx, "name": name})
            negative_kind = raw.get("negative_kind")
            if negative and negative_kind not in ("NXDOMAIN", "NODATA"):
                raise BizError(E_V2_SCHEMA,
                               "negative entry needs negative_kind",
                               {**ctx, "name": name})
            key = (name, rtype, level)
            if key in seen:
                raise BizError(E_V2_DUPLICATE_ENTRY,
                               "duplicate (name, type, level) after canonicalization",
                               {"name": name, "type": rtype, "level": level})
            seen.add(key)
            entries.append({
                "name": name, "type": rtype, "level": level,
                "expiry_ms": expiry_ms, "negative": negative,
                "records": records, "generation": generation,
                "negative_kind": negative_kind,
            })
    manifest_digest = hashlib.sha256(manifest_raw).hexdigest()
    return V2Snapshot(version, min_generation, clock_us, entries,
                      shard_hashes, manifest_digest)
