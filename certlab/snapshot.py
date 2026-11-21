from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from cryptography import x509
from cryptography.hazmat.primitives import serialization

from .clock import LogicalClock, require_int
from .errors import CertLabError, ErrorCode
from .util import (
    canonical_alias,
    load_json_file,
    sha256_bytes,
    sha256_file,
    stable_json,
)

TOMBSTONE_VALUES = frozenset({"none", "warning", "tombstoned"})
REVOCATION_VALUES = frozenset({"unknown", "good", "revoked"})
SOURCE_PRIORITY = {"v1": 1, "v2": 2}


@dataclass(frozen=True)
class SnapshotAnchor:
    alias: str
    canonical_alias: str
    certificate_pem: str
    fingerprint_sha256: str
    enabled: bool
    tombstone: str
    revocation: str
    trust_generation: int
    logical_clock_ms: int
    source_version: str
    source_format: str
    raw_time: dict[str, Any]
    effective_at_ms: int | None
    subject: str

    @property
    def source_priority(self) -> int:
        return SOURCE_PRIORITY[self.source_version]

    def arbitration_key(self) -> tuple[int, int, int, str]:
        return (
            self.logical_clock_ms,
            self.trust_generation,
            self.source_priority,
            self.fingerprint_sha256,
        )


@dataclass
class Snapshot:
    version: str
    path: str
    logical_clock_ms: int
    trust_generation: int
    digest: str
    anchors: list[SnapshotAnchor] = field(default_factory=list)
    source_meta: dict[str, Any] = field(default_factory=dict)


def _bad(message: str, **details: Any) -> CertLabError:
    return CertLabError(ErrorCode.BAD_SNAPSHOT, message, details)


def _parse_certificate(pem: str, *, alias: str, source: str) -> x509.Certificate:
    if not isinstance(pem, str) or "BEGIN CERTIFICATE" not in pem:
        raise _bad("invalid PEM certificate", alias=alias, source=source)
    try:
        return x509.load_pem_x509_certificate(pem.encode("utf-8"))
    except Exception as exc:
        raise _bad(
            "PEM certificate cannot be parsed",
            alias=alias,
            source=source,
            cause=type(exc).__name__,
        ) from exc


def _certificate_fingerprint(cert: x509.Certificate) -> str:
    der = cert.public_bytes(serialization.Encoding.DER)
    return sha256_bytes(der)


def _validate_fingerprint(
    cert: x509.Certificate, expected: object, *, alias: str, source: str
) -> str:
    actual = _certificate_fingerprint(cert)
    if not isinstance(expected, str) or expected.lower() != actual:
        raise CertLabError(
            ErrorCode.HASH_MISMATCH,
            "certificate fingerprint does not match PEM bytes",
            {
                "alias": alias,
                "source": source,
                "expected": expected,
                "actual": actual,
                "digest": "sha256",
            },
        )
    return actual


def _subject_name(cert: x509.Certificate) -> str:
    return cert.subject.rfc4514_string()


def load_v1(path: Path, clock: LogicalClock) -> Snapshot:
    try:
        payload = load_json_file(path)
    except CertLabError:
        raise
    except Exception as exc:
        raise _bad("v1 snapshot is not valid JSON", path=str(path), cause=str(exc)) from exc

    if not isinstance(payload, dict) or payload.get("schema") != "trust-v1":
        raise _bad("v1 snapshot schema must be trust-v1", path=str(path))
    logical_clock = require_int(payload.get("logical_clock_ms"), "logical_clock_ms")
    certificates = payload.get("certificates")
    if not isinstance(certificates, list):
        raise _bad("certificates must be an array", path=str(path))

    seen: set[str] = set()
    anchors: list[SnapshotAnchor] = []
    for index, item in enumerate(certificates):
        if not isinstance(item, dict):
            raise _bad("certificate entry must be an object", index=index)
        alias = item.get("alias")
        if not isinstance(alias, str) or not alias.strip():
            raise _bad("alias must be a non-empty string", index=index)
        alias_key = canonical_alias(alias)
        if alias_key in seen:
            raise _bad("duplicate v1 alias", alias=alias, index=index)
        seen.add(alias_key)
        pem = item.get("pem")
        cert = _parse_certificate(pem, alias=alias, source="v1")
        fingerprint = _validate_fingerprint(
            cert, item.get("fingerprint_sha256"), alias=alias, source="v1"
        )
        trust_seconds = require_int(item.get("trust_seconds"), "trust_seconds")
        if trust_seconds < 0:
            raise _bad("trust_seconds must be non-negative", alias=alias)
        enabled = item.get("enabled")
        if not isinstance(enabled, bool):
            raise _bad("enabled must be a boolean", alias=alias)
        anchors.append(
            SnapshotAnchor(
                alias=alias,
                canonical_alias=alias_key,
                certificate_pem=pem,
                fingerprint_sha256=fingerprint,
                enabled=enabled,
                tombstone="none",
                revocation="unknown",
                trust_generation=0,
                logical_clock_ms=logical_clock,
                source_version="v1",
                source_format="single-json",
                raw_time={
                    "kind": "relative_seconds",
                    "value": trust_seconds,
                    "reference": "injected_logical_clock",
                },
                effective_at_ms=clock.now_ms - trust_seconds * 1000,
                subject=_subject_name(cert),
            )
        )

    semantic = {
        "schema": "trust-v1",
        "logical_clock_ms": logical_clock,
        "certificates": [
            {
                "alias": item.alias,
                "fingerprint_sha256": item.fingerprint_sha256,
                "trust_seconds": item.raw_time["value"],
                "enabled": item.enabled,
            }
            for item in sorted(anchors, key=lambda entry: entry.canonical_alias)
        ],
    }
    return Snapshot(
        version="v1",
        path=str(path),
        logical_clock_ms=logical_clock,
        trust_generation=0,
        digest=sha256_bytes(stable_json(semantic).encode("utf-8")),
        anchors=anchors,
        source_meta={"file": path.name, "certificate_count": len(anchors)},
    )


def _safe_child(root: Path, relative: object) -> Path:
    if not isinstance(relative, str) or not relative:
        raise _bad("v2 shard path must be a non-empty string")
    candidate = (root / relative).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError as exc:
        raise _bad("v2 shard path escapes snapshot directory", path=relative) from exc
    return candidate


def _load_v2_metadata(path: Path) -> tuple[dict[str, str], list[tuple[int, int]], dict[str, dict[str, Any]]]:
    if not path.is_file():
        raise _bad("v2 metadata database is missing", path=str(path))
    try:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        try:
            meta_rows = connection.execute("SELECT key, value FROM snapshot_meta").fetchall()
            history_rows = connection.execute(
                "SELECT generation, logical_clock_ms FROM generation_history ORDER BY generation"
            ).fetchall()
            anchor_rows = connection.execute(
                "SELECT alias, shard_path, effective_at_ms, trust_generation, "
                "tombstone, revocation, enabled, fingerprint_sha256 FROM anchors ORDER BY alias"
            ).fetchall()
        finally:
            connection.close()
    except sqlite3.Error as exc:
        raise _bad("v2 metadata database cannot be read", path=str(path), cause=str(exc)) from exc

    metadata = {str(row["key"]): str(row["value"]) for row in meta_rows}
    history = [(int(row["generation"]), int(row["logical_clock_ms"])) for row in history_rows]
    anchors = {
        str(row["alias"]): {
            "shard_path": str(row["shard_path"]),
            "effective_at_ms": row["effective_at_ms"],
            "trust_generation": row["trust_generation"],
            "tombstone": str(row["tombstone"]),
            "revocation": str(row["revocation"]),
            "enabled": row["enabled"],
            "fingerprint_sha256": str(row["fingerprint_sha256"]),
        }
        for row in anchor_rows
    }
    return metadata, history, anchors


def _required_meta(metadata: dict[str, str], key: str, cast: type) -> Any:
    if key not in metadata:
        raise _bad("v2 metadata key is missing", key=key)
    try:
        return cast(metadata[key])
    except (TypeError, ValueError) as exc:
        raise _bad("v2 metadata value has wrong type", key=key, value=metadata[key]) from exc


def load_v2(path: Path, clock: LogicalClock) -> Snapshot:
    root = path if path.is_dir() else path.parent
    manifest_path = path / "manifest.json" if path.is_dir() else path
    if manifest_path.name != "manifest.json":
        raise _bad("v2 snapshot must be a directory or manifest.json", path=str(path))
    try:
        manifest = load_json_file(manifest_path)
    except CertLabError:
        raise
    except Exception as exc:
        raise _bad("v2 manifest is not valid JSON", path=str(manifest_path), cause=str(exc)) from exc
    if not isinstance(manifest, dict) or manifest.get("schema") != "trust-v2":
        raise _bad("v2 manifest schema must be trust-v2", path=str(manifest_path))

    logical_clock = require_int(manifest.get("logical_clock_ms"), "logical_clock_ms")
    trust_generation = require_int(manifest.get("trust_generation"), "trust_generation")
    if trust_generation < 0:
        raise _bad("trust_generation must be non-negative")
    shard_specs = manifest.get("shards")
    if not isinstance(shard_specs, list) or not shard_specs:
        raise _bad("v2 shards must be a non-empty array")
    database_name = manifest.get("database", "metadata.sqlite")
    database_path = _safe_child(root, database_name)
    metadata, history, db_anchors = _load_v2_metadata(database_path)

    if metadata.get("schema_version") != "trust-v2":
        raise _bad("v2 metadata schema_version must be trust-v2")
    meta_clock = _required_meta(metadata, "logical_clock_ms", int)
    meta_generation = _required_meta(metadata, "trust_generation", int)
    if meta_clock != logical_clock:
        raise _bad(
            "manifest and metadata logical clocks differ",
            manifest=logical_clock,
            metadata=meta_clock,
        )
    if meta_generation != trust_generation:
        raise _bad(
            "manifest and metadata generations differ",
            manifest=trust_generation,
            metadata=meta_generation,
        )
    if not history:
        raise _bad("v2 generation_history is empty")
    previous_generation = -1
    previous_clock = -1
    for generation, item_clock in history:
        if generation <= previous_generation:
            raise _bad(
                "v2 trust generations must be strictly monotonic",
                previous=previous_generation,
                current=generation,
            )
        if item_clock < previous_clock:
            raise _bad("v2 generation-history clocks must be monotonic")
        previous_generation = generation
        previous_clock = item_clock
    if history[-1][0] != trust_generation:
        raise _bad(
            "metadata generation does not match latest history",
            metadata=trust_generation,
            latest=history[-1][0],
        )
    valid_generations = {item[0] for item in history}

    anchors: list[SnapshotAnchor] = []
    seen: set[str] = set()
    files_by_name: dict[str, str] = {}
    previous_shard_generation = -1
    for shard_index, spec in enumerate(shard_specs):
        if not isinstance(spec, dict):
            raise _bad("v2 shard spec must be an object", index=shard_index)
        shard_path = _safe_child(root, spec.get("path"))
        expected_hash = spec.get("sha256")
        if not isinstance(expected_hash, str):
            raise _bad("v2 shard sha256 must be a string", path=str(shard_path))
        if not shard_path.is_file():
            raise _bad("v2 shard file is missing", path=str(shard_path))
        actual_hash = sha256_file(shard_path)
        if actual_hash.lower() != expected_hash.lower():
            raise CertLabError(
                ErrorCode.HASH_MISMATCH,
                "v2 shard hash does not match manifest",
                {
                    "path": str(shard_path),
                    "expected": expected_hash,
                    "actual": actual_hash,
                    "digest": "sha256",
                },
            )
        files_by_name[shard_path.name] = actual_hash
        try:
            shard = load_json_file(shard_path)
        except Exception as exc:
            raise _bad("v2 shard is not valid JSON", path=str(shard_path), cause=str(exc)) from exc
        if not isinstance(shard, dict) or shard.get("schema") != "trust-v2-shard":
            raise _bad("v2 shard schema must be trust-v2-shard", path=str(shard_path))
        if "logical_clock_ms" in shard:
            shard_clock = require_int(shard["logical_clock_ms"], "logical_clock_ms")
            if shard_clock != logical_clock:
                raise _bad(
                    "v2 shard logical clock differs from manifest",
                    path=str(shard_path),
                    shard=shard_clock,
                    manifest=logical_clock,
                )
        if "generation" in shard:
            shard_generation = require_int(shard["generation"], "generation")
            if shard_generation != trust_generation:
                raise _bad(
                    "v2 shard generation differs from manifest",
                    path=str(shard_path),
                    shard=shard_generation,
                    manifest=trust_generation,
                )
        entries = shard.get("entries")
        if not isinstance(entries, list):
            raise _bad("v2 shard entries must be an array", path=str(shard_path))
        shard_generations: list[int] = []
        for index, item in enumerate(entries):
            if not isinstance(item, dict):
                raise _bad("v2 entry must be an object", path=str(shard_path), index=index)
            alias = item.get("alias")
            canonical = item.get("canonical_alias")
            if not isinstance(alias, str) or not alias.strip():
                raise _bad("v2 alias must be a non-empty string", index=index)
            if not isinstance(canonical, str) or not canonical.strip():
                raise _bad("v2 canonical_alias must be a non-empty string", alias=alias)
            alias_key = canonical_alias(canonical)
            if alias_key in seen:
                raise _bad("duplicate v2 canonical alias", alias=alias)
            seen.add(alias_key)
            pem = item.get("pem")
            cert = _parse_certificate(pem, alias=alias, source="v2")
            fingerprint = _validate_fingerprint(
                cert, item.get("fingerprint_sha256"), alias=alias, source="v2"
            )
            effective_at = require_int(item.get("effective_at_ms"), "effective_at_ms")
            generation = require_int(item.get("trust_generation"), "trust_generation")
            if generation not in valid_generations:
                raise _bad(
                    "v2 anchor generation is absent from generation_history",
                    alias=alias,
                    generation=generation,
                )
            shard_generations.append(generation)
            enabled = item.get("enabled")
            if not isinstance(enabled, bool):
                raise _bad("v2 enabled must be a boolean", alias=alias)
            tombstone = item.get("tombstone")
            if tombstone not in TOMBSTONE_VALUES:
                raise _bad(
                    "invalid v2 tombstone enum",
                    alias=alias,
                    value=tombstone,
                    allowed=sorted(TOMBSTONE_VALUES),
                )
            revocation = item.get("revocation")
            if revocation not in REVOCATION_VALUES:
                raise _bad(
                    "invalid v2 revocation enum",
                    alias=alias,
                    value=revocation,
                    allowed=sorted(REVOCATION_VALUES),
                )
            db_row = db_anchors.get(alias)
            if db_row is None:
                raise _bad("v2 shard anchor is absent from metadata database", alias=alias)
            expected_row = {
                "shard_path": shard_path.name,
                "effective_at_ms": effective_at,
                "trust_generation": generation,
                "tombstone": tombstone,
                "revocation": revocation,
                "enabled": 1 if enabled else 0,
                "fingerprint_sha256": fingerprint,
            }
            for field_name, expected in expected_row.items():
                if db_row[field_name] != expected:
                    raise _bad(
                        "v2 shard and metadata disagree",
                        alias=alias,
                        field=field_name,
                        shard=expected,
                        metadata=db_row[field_name],
                    )
            anchors.append(
                SnapshotAnchor(
                    alias=alias,
                    canonical_alias=alias_key,
                    certificate_pem=pem,
                    fingerprint_sha256=fingerprint,
                    enabled=enabled,
                    tombstone=tombstone,
                    revocation=revocation,
                    trust_generation=generation,
                    logical_clock_ms=logical_clock,
                    source_version="v2",
                    source_format="shards+sqlite",
                    raw_time={"kind": "absolute_ms", "value": effective_at},
                    effective_at_ms=effective_at,
                    subject=_subject_name(cert),
                )
            )
        if shard_generations:
            shard_max = max(shard_generations)
            if shard_max < previous_shard_generation:
                raise _bad(
                    "v2 shard generations must be monotonic in manifest order",
                    path=str(shard_path),
                    previous=previous_shard_generation,
                    current=shard_max,
                )
            previous_shard_generation = shard_max

    if set(db_anchors) != {entry.alias for entry in anchors}:
        raise _bad(
            "v2 metadata anchors and shard entries differ",
            metadata_only=sorted(set(db_anchors) - {entry.alias for entry in anchors}),
            shard_only=sorted({entry.alias for entry in anchors} - set(db_anchors)),
        )

    semantic = {
        "schema": "trust-v2",
        "logical_clock_ms": logical_clock,
        "trust_generation": trust_generation,
        "history": history,
        "anchors": [
            {
                "alias": item.alias,
                "canonical_alias": item.canonical_alias,
                "fingerprint_sha256": item.fingerprint_sha256,
                "effective_at_ms": item.effective_at_ms,
                "trust_generation": item.trust_generation,
                "enabled": item.enabled,
                "tombstone": item.tombstone,
                "revocation": item.revocation,
            }
            for item in sorted(anchors, key=lambda entry: entry.canonical_alias)
        ],
    }
    return Snapshot(
        version="v2",
        path=str(manifest_path),
        logical_clock_ms=logical_clock,
        trust_generation=trust_generation,
        digest=sha256_bytes(stable_json(semantic).encode("utf-8")),
        anchors=anchors,
        source_meta={
            "manifest": manifest_path.name,
            "database": database_path.name,
            "shards": files_by_name,
            "generation_history": history,
        },
    )


class SnapshotLoader:
    @staticmethod
    def load_v1(path: str | Path, clock: LogicalClock) -> Snapshot:
        return load_v1(Path(path), clock)

    @staticmethod
    def load_v2(path: str | Path, clock: LogicalClock) -> Snapshot:
        return load_v2(Path(path), clock)

    @staticmethod
    def load(
        *,
        v1_path: str | Path | None,
        v2_path: str | Path | None,
        clock: LogicalClock,
    ) -> list[Snapshot]:
        snapshots: list[Snapshot] = []
        if v1_path:
            snapshots.append(load_v1(Path(v1_path), clock))
        if v2_path:
            snapshots.append(load_v2(Path(v2_path), clock))
        if not snapshots:
            raise CertLabError(
                ErrorCode.INVALID_ARGUMENT,
                "at least one of v1_path or v2_path is required",
            )
        return snapshots


def _anchor_view(
    anchor: SnapshotAnchor, decision: dict[str, Any], reference_clock_ms: int
) -> dict[str, Any]:
    if anchor.source_version == "v1":
        conversion = {
            "reference_clock_ms": reference_clock_ms,
            "scale_ms": 1000,
            "operator": "subtract",
            "rounding": "exact",
        }
    else:
        conversion = {
            "reference_clock_ms": anchor.logical_clock_ms,
            "scale_ms": 1,
            "operator": "identity",
            "rounding": "none",
        }
    return {
        "alias": anchor.alias,
        "canonical_alias": anchor.canonical_alias,
        "certificate_pem": anchor.certificate_pem,
        "fingerprint_sha256": anchor.fingerprint_sha256,
        "subject": anchor.subject,
        "enabled": anchor.enabled,
        "tombstone": anchor.tombstone,
        "revocation": anchor.revocation,
        "effective_at_ms": anchor.effective_at_ms,
        "trust_generation": anchor.trust_generation,
        "source": {
            "version": anchor.source_version,
            "format": anchor.source_format,
            "priority": anchor.source_priority,
            "logical_clock_ms": anchor.logical_clock_ms,
        },
        "raw_time": anchor.raw_time,
        "time_conversion": conversion,
        "decision": decision,
    }


def merge_snapshots(
    snapshots: list[Snapshot], clock: LogicalClock
) -> dict[str, Any]:
    if not snapshots:
        raise CertLabError(ErrorCode.INVALID_ARGUMENT, "no snapshots supplied")
    candidates: dict[str, list[SnapshotAnchor]] = {}
    for snapshot in snapshots:
        for anchor in snapshot.anchors:
            candidates.setdefault(anchor.canonical_alias, []).append(anchor)

    merged_anchors: list[dict[str, Any]] = []
    merge_decisions: list[dict[str, Any]] = []
    for alias in sorted(candidates):
        entries = sorted(candidates[alias], key=lambda item: item.arbitration_key())
        winner = entries[-1]
        candidate_records = [
            {
                "source_version": item.source_version,
                "alias": item.alias,
                "fingerprint_sha256": item.fingerprint_sha256,
                "logical_clock_ms": item.logical_clock_ms,
                "trust_generation": item.trust_generation,
                "source_priority": item.source_priority,
                "raw_time": item.raw_time,
                "effective_at_ms": item.effective_at_ms,
            }
            for item in entries
        ]
        if len(entries) == 1:
            reason = "single_source"
        elif entries[-1].logical_clock_ms != entries[-2].logical_clock_ms:
            reason = "higher_snapshot_logical_clock"
        elif entries[-1].trust_generation != entries[-2].trust_generation:
            reason = "higher_trust_generation_at_same_clock"
        else:
            reason = "source_format_priority_at_same_clock_and_generation"
        decision = {
            "winner_source": winner.source_version,
            "winner_fingerprint_sha256": winner.fingerprint_sha256,
            "reason": reason,
            "rule": "logical_clock_then_trust_generation_then_source_priority",
            "candidates": candidate_records,
        }
        merged_anchors.append(_anchor_view(winner, decision, clock.now_ms))
        merge_decisions.append(
            {
                "canonical_alias": alias,
                "winner_source": winner.source_version,
                "winner_fingerprint_sha256": winner.fingerprint_sha256,
                "reason": reason,
            }
        )

    generation = max((item.trust_generation for item in snapshots), default=0)
    tombstone_epoch = sum(
        1
        for item in merged_anchors
        if item["tombstone"] in {"warning", "tombstoned"}
    )
    view = {
        "schema": "certlab-trust-view-v1",
        "logical_clock_ms": clock.now_ms,
        "generation": generation,
        "tombstone_epoch": tombstone_epoch,
        "sources": [
            {
                "version": snapshot.version,
                "path": snapshot.path,
                "logical_clock_ms": snapshot.logical_clock_ms,
                "trust_generation": snapshot.trust_generation,
                "digest": snapshot.digest,
                "meta": snapshot.source_meta,
            }
            for snapshot in sorted(snapshots, key=lambda item: item.version)
        ],
        "anchors": merged_anchors,
        "merge_decisions": merge_decisions,
    }
    view["view_digest"] = sha256_bytes(stable_json(view).encode("utf-8"))
    return view


def active_anchors(view: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        anchor
        for anchor in view["anchors"]
        if anchor["enabled"] and anchor["tombstone"] != "tombstoned"
    ]

