from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from .canonical import (
    canonical_fingerprint,
    sha256_file,
    sha256_json,
)
from .certs import CertPool, cert_fingerprint
from .errors import E_BAD_SNAPSHOT, E_FP_MISMATCH, PipelineError
from .models import Anchor, Snapshot


def _bad(message: str, **details: Any) -> PipelineError:
    return PipelineError(E_BAD_SNAPSHOT, message, details or None)


def _load_json(path: Path, label: str) -> dict:
    if not path.is_file():
        raise _bad(f"{label} does not exist", path=str(path))
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise _bad(f"{label} is not valid JSON", path=str(path)) from exc
    if not isinstance(value, dict):
        raise _bad(f"{label} root must be an object", path=str(path))
    return value


def _integer(value: Any, field: str, *, minimum: int | None = None) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise _bad(f"{field} must be an integer")
    if minimum is not None and value < minimum:
        raise _bad(f"{field} must be >= {minimum}", value=value)
    return value


def _boolean(value: Any, field: str) -> bool:
    if not isinstance(value, bool):
        raise _bad(f"{field} must be a boolean")
    return value


def _string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise _bad(f"{field} must be a non-empty string")
    return value


def _basename(value: Any, field: str) -> str:
    text = _string(value, field)
    if Path(text).name != text:
        raise _bad(f"{field} must be a basename", value=text)
    return text


def _fingerprint(value: Any, field: str) -> str:
    text = _string(value, field)
    try:
        return canonical_fingerprint(text)
    except ValueError as exc:
        raise _bad(f"{field} is not a SHA-256 fingerprint", value=text) from exc


def _check_cert_fingerprint(
    pool: CertPool,
    fingerprint: str,
    cert_file: str,
    alias: str,
) -> None:
    cert = pool.get_file(cert_file)
    actual = cert_fingerprint(cert)
    if actual != fingerprint:
        raise PipelineError(
            E_FP_MISMATCH,
            "snapshot fingerprint does not match certificate PEM",
            {"alias": alias, "declared": fingerprint, "actual": actual, "cert_file": cert_file},
        )


def parse_v1_snapshot(path: Path, cert_dir: Path, logical_clock_ms: int) -> Snapshot:
    document = _load_json(path, "v1 snapshot")
    if document.get("schema") != 1:
        raise _bad("v1 snapshot schema must be 1")
    version = _string(document.get("snapshot_version"), "snapshot_version")
    generation = _integer(document.get("generation"), "generation", minimum=1)
    raw_anchors = document.get("anchors")
    if not isinstance(raw_anchors, list) or not raw_anchors:
        raise _bad("v1 anchors must be a non-empty array")
    pool = CertPool.load(cert_dir)
    aliases: set[str] = set()
    fingerprints: set[str] = set()
    anchors: list[Anchor] = []
    for index, item in enumerate(raw_anchors):
        if not isinstance(item, dict):
            raise _bad("v1 anchor must be an object", index=index)
        alias = _string(item.get("alias"), f"anchors[{index}].alias")
        if alias in aliases:
            raise _bad("v1 alias must be unique", alias=alias)
        aliases.add(alias)
        fingerprint = _fingerprint(item.get("fingerprint"), f"anchors[{index}].fingerprint")
        if fingerprint in fingerprints:
            raise _bad("v1 anchor fingerprints must be unique", fingerprint=fingerprint)
        fingerprints.add(fingerprint)
        cert_file = _basename(item.get("cert_file"), f"anchors[{index}].cert_file")
        _check_cert_fingerprint(pool, fingerprint, cert_file, alias)
        trust_seconds = _integer(
            item.get("trust_seconds"), f"anchors[{index}].trust_seconds", minimum=0
        )
        not_before_ms = logical_clock_ms
        not_after_ms = logical_clock_ms + trust_seconds * 1000
        conversion = {
            "source": "v1.trust_seconds",
            "clock_ms": logical_clock_ms,
            "scale_ms": 1000,
            "rounding": "exact",
            "input_seconds": trust_seconds,
            "remainder_ms": 0,
            "not_before_ms": not_before_ms,
            "not_after_ms": not_after_ms,
        }
        anchors.append(
            Anchor(
                alias=alias,
                fingerprint=fingerprint,
                cert_file=cert_file,
                not_before_ms=not_before_ms,
                not_after_ms=not_after_ms,
                revoked=False,
                can_issue_intermediates=True,
                trust_generation=generation,
                conversion=conversion,
            )
        )
    anchors.sort(key=lambda item: item.alias)
    return Snapshot(
        snapshot_version=version,
        generation=generation,
        created_clock_ms=logical_clock_ms,
        source_digest=sha256_json({"kind": "v1", "document": document}),
        kind="v1",
        anchors=tuple(anchors),
    )


def _safe_child(base: Path, relative: str, field: str) -> Path:
    if not isinstance(relative, str) or not relative:
        raise _bad(f"{field} must be a relative path")
    candidate = Path(relative)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise _bad(f"{field} escapes snapshot directory", value=relative)
    resolved = (base / candidate).resolve()
    if base.resolve() not in resolved.parents and resolved != base.resolve():
        raise _bad(f"{field} escapes snapshot directory", value=relative)
    return resolved


def _parse_metadata_db(
    metadata_path: Path,
    version: str,
    generation: int,
    created_clock_ms: int,
    shard_anchors: list[dict],
) -> None:
    if not metadata_path.is_file():
        raise _bad("v2 metadata database does not exist", path=str(metadata_path))
    try:
        metadata_uri = metadata_path.resolve().as_uri() + "?mode=ro"
        connection = sqlite3.connect(
            metadata_uri,
            uri=True,
        )
    except sqlite3.Error as exc:
        raise _bad("cannot open v2 metadata database") from exc
    try:
        try:
            meta_rows = connection.execute(
                "SELECT version, generation, created_clock_ms FROM snapshot_meta"
            ).fetchall()
            anchor_rows = connection.execute(
                """
                SELECT seq, alias, fingerprint, not_before_ms, not_after_ms,
                       trust_generation, tombstone, can_issue_intermediates
                FROM anchors ORDER BY seq
                """
            ).fetchall()
        except sqlite3.Error as exc:
            raise _bad("v2 metadata database schema is invalid") from exc
    finally:
        connection.close()
    if len(meta_rows) != 1:
        raise _bad("v2 metadata must contain exactly one snapshot_meta row")
    db_version, db_generation, db_clock = meta_rows[0]
    if db_version != version or db_generation != generation or db_clock != created_clock_ms:
        raise _bad(
            "v2 metadata snapshot_meta does not match manifest",
            metadata={
                "version": db_version,
                "generation": db_generation,
                "created_clock_ms": db_clock,
            },
        )
    if not isinstance(db_generation, int) or isinstance(db_generation, bool):
        raise _bad("v2 metadata generation must be an integer")
    if not isinstance(db_clock, int) or isinstance(db_clock, bool):
        raise _bad("v2 metadata clock must be an integer")
    previous_generation = 0
    db_by_alias: dict[str, dict] = {}
    for row in anchor_rows:
        (
            seq,
            alias,
            fingerprint,
            not_before_ms,
            not_after_ms,
            trust_generation,
            tombstone,
            can_issue,
        ) = row
        if not all(
            isinstance(value, int) and not isinstance(value, bool)
            for value in (seq, not_before_ms, not_after_ms, trust_generation, tombstone, can_issue)
        ):
            raise _bad("v2 metadata anchor fields must be integers", alias=alias)
        if trust_generation < previous_generation:
            raise _bad("v2 metadata trust generations are not monotonic", alias=alias)
        if trust_generation < 1 or trust_generation > generation:
            raise _bad("v2 metadata trust generation is out of range", alias=alias)
        previous_generation = trust_generation
        if tombstone not in (0, 1) or can_issue not in (0, 1):
            raise _bad("v2 metadata flags must be 0 or 1", alias=alias)
        if tombstone == 1 and can_issue == 1:
            raise _bad("tombstoned anchor cannot issue intermediates", alias=alias)
        try:
            normalized_fp = canonical_fingerprint(fingerprint)
        except (TypeError, ValueError) as exc:
            raise _bad("v2 metadata fingerprint is invalid", alias=alias) from exc
        if alias in db_by_alias:
            raise _bad("v2 metadata aliases must be unique", alias=alias)
        db_by_alias[alias] = {
            "seq": seq,
            "alias": alias,
            "fingerprint": normalized_fp,
            "not_before_ms": not_before_ms,
            "not_after_ms": not_after_ms,
            "trust_generation": trust_generation,
            "tombstone": bool(tombstone),
            "can_issue_intermediates": bool(can_issue),
        }
    shard_by_alias = {anchor["alias"]: anchor for anchor in shard_anchors}
    if set(db_by_alias) != set(shard_by_alias):
        raise _bad(
            "v2 metadata aliases differ from shard aliases",
            metadata=sorted(db_by_alias),
            shards=sorted(shard_by_alias),
        )
    compared_fields = (
        "fingerprint",
        "not_before_ms",
        "not_after_ms",
        "trust_generation",
        "tombstone",
        "can_issue_intermediates",
    )
    for alias, db_anchor in db_by_alias.items():
        shard_anchor = shard_by_alias[alias]
        for field in compared_fields:
            if db_anchor[field] != shard_anchor[field]:
                raise _bad(
                    "v2 metadata disagrees with sharded anchor",
                    alias=alias,
                    field=field,
                    metadata=db_anchor[field],
                    shard=shard_anchor[field],
                )


def parse_v2_snapshot(manifest_path: Path, cert_dir: Path) -> Snapshot:
    manifest = _load_json(manifest_path, "v2 manifest")
    if manifest.get("schema") != 2:
        raise _bad("v2 manifest schema must be 2")
    version = _string(manifest.get("snapshot_version"), "snapshot_version")
    generation = _integer(manifest.get("generation"), "generation", minimum=1)
    created_clock_ms = _integer(manifest.get("created_clock_ms"), "created_clock_ms", minimum=0)
    shard_entries = manifest.get("shards")
    if not isinstance(shard_entries, list) or not shard_entries:
        raise _bad("v2 shards must be a non-empty array")
    pool = CertPool.load(cert_dir)
    aliases: set[str] = set()
    fingerprints: set[str] = set()
    shard_documents: list[dict] = []
    shard_anchors: list[dict] = []
    seen_shards: set[str] = set()
    previous_generation = 0
    for shard_index, shard_entry in enumerate(shard_entries):
        if not isinstance(shard_entry, dict):
            raise _bad("v2 shard entry must be an object", index=shard_index)
        relative = _string(shard_entry.get("path"), f"shards[{shard_index}].path")
        if relative in seen_shards:
            raise _bad("v2 shard paths must be unique", path=relative)
        seen_shards.add(relative)
        shard_path = _safe_child(manifest_path.parent, relative, "shard path")
        if not shard_path.is_file():
            raise _bad("v2 shard file does not exist", path=relative)
        declared_hash = shard_entry.get("sha256")
        try:
            declared_text = str(declared_hash)
            if not declared_text.lower().startswith("sha256:"):
                declared_text = "sha256:" + declared_text
            expected_hash = canonical_fingerprint(declared_text)[7:]
        except ValueError as exc:
            raise _bad("v2 shard sha256 is invalid", path=relative) from exc
        actual_hash = sha256_file(shard_path)
        if actual_hash != expected_hash:
            raise _bad(
                "v2 shard hash mismatch",
                path=relative,
                declared=expected_hash,
                actual=actual_hash,
            )
        document = _load_json(shard_path, "v2 shard")
        if document.get("schema") != 2 or not isinstance(document.get("anchors"), list):
            raise _bad("v2 shard schema is invalid", path=relative)
        shard_documents.append(
            {"path": relative, "sha256": actual_hash, "document": document}
        )
        for anchor_index, item in enumerate(document["anchors"]):
            if not isinstance(item, dict):
                raise _bad("v2 anchor must be an object", path=relative, index=anchor_index)
            alias = _string(item.get("alias"), f"{relative}.anchors[{anchor_index}].alias")
            if alias in aliases:
                raise _bad("v2 alias must be unique", alias=alias)
            aliases.add(alias)
            fingerprint = _fingerprint(
                item.get("fingerprint"), f"{relative}.anchors[{anchor_index}].fingerprint"
            )
            if fingerprint in fingerprints:
                raise _bad("v2 anchor fingerprints must be unique", fingerprint=fingerprint)
            fingerprints.add(fingerprint)
            cert_file = _basename(
                item.get("cert_file"), f"{relative}.anchors[{anchor_index}].cert_file"
            )
            _check_cert_fingerprint(pool, fingerprint, cert_file, alias)
            not_before_ms = _integer(
                item.get("not_before_ms"),
                f"{relative}.anchors[{anchor_index}].not_before_ms",
                minimum=0,
            )
            not_after_ms = _integer(
                item.get("not_after_ms"),
                f"{relative}.anchors[{anchor_index}].not_after_ms",
                minimum=0,
            )
            if not_before_ms > not_after_ms:
                raise _bad("anchor validity interval is inverted", alias=alias)
            trust_generation = _integer(
                item.get("trust_generation"),
                f"{relative}.anchors[{anchor_index}].trust_generation",
                minimum=1,
            )
            if trust_generation < previous_generation:
                raise _bad("v2 trust generations are not monotonic", alias=alias)
            if trust_generation > generation:
                raise _bad("anchor trust generation exceeds snapshot generation", alias=alias)
            previous_generation = trust_generation
            tombstone = _boolean(
                item.get("tombstone"), f"{relative}.anchors[{anchor_index}].tombstone"
            )
            can_issue = _boolean(
                item.get("can_issue_intermediates"),
                f"{relative}.anchors[{anchor_index}].can_issue_intermediates",
            )
            if tombstone and can_issue:
                raise _bad("tombstoned anchor cannot issue intermediates", alias=alias)
            shard_anchors.append(
                {
                    "alias": alias,
                    "fingerprint": fingerprint,
                    "cert_file": cert_file,
                    "not_before_ms": not_before_ms,
                    "not_after_ms": not_after_ms,
                    "trust_generation": trust_generation,
                    "tombstone": tombstone,
                    "can_issue_intermediates": can_issue,
                }
            )
    metadata_rel = _string(manifest.get("metadata_db"), "metadata_db")
    metadata_path = _safe_child(manifest_path.parent, metadata_rel, "metadata_db")
    _parse_metadata_db(
        metadata_path,
        version=version,
        generation=generation,
        created_clock_ms=created_clock_ms,
        shard_anchors=shard_anchors,
    )
    anchors = [
        Anchor(
            alias=item["alias"],
            fingerprint=item["fingerprint"],
            cert_file=item["cert_file"],
            not_before_ms=item["not_before_ms"],
            not_after_ms=item["not_after_ms"],
            revoked=item["tombstone"],
            can_issue_intermediates=item["can_issue_intermediates"],
            trust_generation=item["trust_generation"],
            conversion={"source": "v2.absolute_ms", "rounding": "none"},
        )
        for item in sorted(shard_anchors, key=lambda value: value["alias"])
    ]
    return Snapshot(
        snapshot_version=version,
        generation=generation,
        created_clock_ms=created_clock_ms,
        source_digest=sha256_json(
            {"kind": "v2", "manifest": manifest, "shards": shard_documents}
        ),
        kind="v2",
        anchors=tuple(anchors),
    )

