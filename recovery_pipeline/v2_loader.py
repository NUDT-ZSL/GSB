"""v2 sharded-JSON + SQLite-metadata config loader and validator.

Pure loader: verifies shard hashes, generation monotonicity in the metadata
database, effective-time sanity and dependency closure. Raises BizError on
the first violation; nothing is persisted on failure.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

from .errors import BizError, Err
from .models import V2Config, V2Service


def _check_meta_db(meta_path: Path) -> None:
    # Recovery generations per service must be monotonic over update seq.
    conn = sqlite3.connect(str(meta_path))
    try:
        rows = conn.execute(
            "SELECT service, generation, effective_ms FROM service_meta"
            " ORDER BY service, seq"
        ).fetchall()
    finally:
        conn.close()
    last_gen: dict[str, int] = {}
    for service, generation, effective_ms in rows:
        if service in last_gen and generation < last_gen[service]:
            raise BizError(Err.V2_GENERATION_REGRESSION,
                           "recovery generation regressed in metadata db",
                           service=service, previous=last_gen[service],
                           current=generation)
        last_gen[service] = generation
        if not isinstance(effective_ms, int) or effective_ms < 0:
            raise BizError(Err.V2_INVALID_EFFECTIVE_MS,
                           "effective_ms must be a non-negative integer",
                           service=service, effective_ms=effective_ms)


def load_v2(package_dir) -> V2Config:
    base = Path(package_dir)
    manifest = json.loads((base / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("format") != "v2":
        raise BizError(Err.V2_SHARD_HASH_MISMATCH, "not a v2 package",
                       path=str(base), format=manifest.get("format"))

    _check_meta_db(base / manifest["meta_db"])

    shard_digests: dict[str, str] = {}
    services: list[V2Service] = []
    for shard in manifest["shards"]:
        rel = shard["path"]
        blob = (base / rel).read_bytes()
        actual = hashlib.sha256(blob).hexdigest()
        if actual != shard["sha256"]:
            raise BizError(Err.V2_SHARD_HASH_MISMATCH,
                           "shard file hash mismatch",
                           shard=rel, expected=shard["sha256"], actual=actual)
        shard_digests[rel] = actual
        payload = json.loads(blob.decode("utf-8"))
        for entry in payload.get("services", []):
            eff = entry.get("effective_ms")
            if not isinstance(eff, int) or isinstance(eff, bool) or eff < 0:
                raise BizError(Err.V2_INVALID_EFFECTIVE_MS,
                               "effective_ms must be a non-negative integer",
                               service=entry.get("name"), effective_ms=eff)
            services.append(V2Service(
                name=entry["name"],
                effective_ms=eff,
                generation=int(entry["generation"]),
                priority=entry.get("priority", "P3"),
                upstream=tuple(entry.get("upstream", [])),
                tombstone=bool(entry.get("tombstone", False)),
            ))

    live = {s.name for s in services if not s.tombstone}
    for svc in services:
        if svc.tombstone:
            continue
        for up in svc.upstream:
            if up not in live:
                raise BizError(Err.V2_DANGLING_DEPENDENCY,
                               "upstream dependency is not closed at both ends",
                               service=svc.name, upstream=up)

    return V2Config(
        manifest_version=int(manifest["manifest_version"]),
        generation_floor=int(manifest["generation_floor"]),
        services=tuple(services),
        shard_digests=shard_digests,
    )
