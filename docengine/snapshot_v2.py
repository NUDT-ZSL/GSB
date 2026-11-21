"""v2 snapshot: sharded JSON files plus a SQLite metadata database.

The meta DB carries document versions, tombstones, fragment checksums and the
snapshot generation. Validation checks shard content hashes, version
monotonicity, fragment checksums against the fragment directory, and that
tombstones reference existing documents."""

import json
import os
import sqlite3

from .errors import BusinessError, ErrorCode
from .stablejson import digest, file_sha256

SNAPSHOT_ID = "v2"


def _read_meta(meta_db_path):
    con = sqlite3.connect("file:%s?mode=ro" % meta_db_path, uri=True)
    try:
        documents = {}
        for doc_id, version, clock_ms in con.execute(
            "SELECT doc_id, version, clock_ms FROM documents ORDER BY doc_id"
        ):
            documents[doc_id] = {"version": version, "clock_ms": clock_ms}
        tombstones = {}
        for doc_id, version, clock_ms in con.execute(
            "SELECT doc_id, version, clock_ms FROM tombstones ORDER BY doc_id"
        ):
            tombstones[doc_id] = {"version": version, "clock_ms": clock_ms}
        fragments = {}
        for checksum, size in con.execute(
            "SELECT checksum, size FROM fragments ORDER BY checksum"
        ):
            fragments[checksum] = {"size": size}
        meta = {}
        for key, value in con.execute("SELECT key, value FROM meta ORDER BY key"):
            meta[key] = value
    finally:
        con.close()
    return {
        "documents": documents,
        "tombstones": tombstones,
        "fragments": fragments,
        "meta": meta,
    }


def load_v2(shard_dir, meta_db_path, manifest_shards, fragment_store):
    """Parse and fully validate a v2 snapshot (shards + SQLite metadata)."""
    meta = _read_meta(meta_db_path)

    # 1. Shard content hashes must match the manifest.
    shards = []
    for entry in sorted(manifest_shards, key=lambda e: e["path"]):
        path = os.path.join(shard_dir, os.path.basename(entry["path"]))
        actual = file_sha256(path)
        if actual != entry["sha256"]:
            raise BusinessError(
                ErrorCode.E_V2_SHARD_HASH,
                "shard content hash mismatch",
                snapshot_version=SNAPSHOT_ID,
                shard=entry["path"],
                expected_sha256=entry["sha256"],
                actual_sha256=actual,
            )
        with open(path, "r", encoding="utf-8") as fh:
            shards.append(json.load(fh))

    # 2. Document versions: shard-declared versions must match the metadata
    #    and never regress below it.
    documents = {}
    for shard in shards:
        for doc in shard.get("documents", []):
            doc_id = doc["doc_id"]
            recorded = meta["documents"].get(doc_id)
            if recorded is None or doc["version"] != recorded["version"]:
                raise BusinessError(
                    ErrorCode.E_V2_VERSION_REGRESS,
                    "shard document version does not match metadata "
                    "(regression or divergence)",
                    snapshot_version=SNAPSHOT_ID,
                    doc_id=doc_id,
                    shard_version=doc["version"],
                    meta_version=None if recorded is None else recorded["version"],
                )
            documents[doc_id] = doc

    # 3. Fragment checksums declared in metadata must match the directory.
    for checksum, frag in sorted(meta["fragments"].items()):
        try:
            data = fragment_store.read_verified(checksum)
        except BusinessError as exc:
            raise BusinessError(
                ErrorCode.E_V2_FRAGMENT_CHECKSUM,
                "v2 metadata fragment fails checksum verification",
                snapshot_version=SNAPSHOT_ID,
                checksum=checksum,
                reason=exc.details.get("reason"),
            )
        if len(data) != frag["size"]:
            raise BusinessError(
                ErrorCode.E_V2_FRAGMENT_CHECKSUM,
                "v2 metadata fragment size mismatch",
                snapshot_version=SNAPSHOT_ID,
                checksum=checksum,
                meta_size=frag["size"],
                actual_size=len(data),
            )

    # 4. Tombstones must reference existing documents.
    for doc_id in sorted(meta["tombstones"]):
        if doc_id not in meta["documents"]:
            raise BusinessError(
                ErrorCode.E_V2_TOMBSTONE_ORPHAN,
                "tombstone references a document absent from metadata",
                snapshot_version=SNAPSHOT_ID,
                doc_id=doc_id,
            )

    return {
        "snapshot_id": meta["meta"].get("snapshot_id", SNAPSHOT_ID),
        "format": SNAPSHOT_ID,
        "clock_ms": int(meta["meta"].get("clock_ms", "0")),
        "generation": int(meta["meta"].get("generation", "0")),
        "documents": documents,
        "meta_documents": meta["documents"],
        "tombstones": meta["tombstones"],
        "fragments": meta["fragments"],
        "meta_digest": digest(meta),
        "shard_digests": {e["path"]: e["sha256"] for e in manifest_shards},
    }
