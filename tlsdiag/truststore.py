"""Parsing and strict validation of v1 and v2 trust store snapshots.

Loaders are side-effect free with respect to engine persistence: they only read
sample files and return in-memory records, raising :class:`DiagError` with a
business code on the first illegal entry.  Nothing is written to SQLite here,
so an invalid snapshot can never pollute persistent state.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes

from .errors import DiagError


@dataclass
class RawAnchor:
    alias: str
    pem_path: str
    cert: x509.Certificate
    fingerprint: str
    source_version: str
    logical_clock_tick: int
    enabled: bool
    # v1: relative seconds; v2: absolute integer milliseconds
    relative_trust_seconds: int | None = None
    effective_ms: int | None = None
    generation: int | None = None
    alert_tombstone: dict | None = None
    revoked: bool = False


@dataclass
class Snapshot:
    version: str
    logical_clock_tick: int
    anchors: list[RawAnchor] = field(default_factory=list)


def _load_cert(base: Path, rel_path: str) -> x509.Certificate:
    cert_path = base / rel_path
    if not cert_path.is_file():
        raise DiagError("E1005", f"PEM file missing: {rel_path}")
    try:
        return x509.load_pem_x509_certificate(cert_path.read_bytes())
    except Exception as exc:  # cryptography raises various exception types
        raise DiagError("E1005", f"unparseable PEM {rel_path}: {exc}") from exc


def _actual_fingerprint(cert: x509.Certificate) -> str:
    return cert.fingerprint(hashes.SHA256()).hex()


def load_v1(path: str | Path) -> Snapshot:
    path = Path(path)
    base = path.parent.parent if path.parent.name == "truststore_v1" else path.parent
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("format") != "v1":
        raise DiagError("E1004", "missing format=v1 marker")
    tick = data.get("logical_clock_tick")
    if not isinstance(tick, int) or isinstance(tick, bool) or tick < 0:
        raise DiagError("E1004", "v1 logical_clock_tick must be a non-negative integer")

    snapshot = Snapshot(version="v1", logical_clock_tick=tick)
    seen: set[str] = set()
    for entry in data.get("entries", []):
        for required in ("alias", "pem", "fingerprint_sha256", "relative_trust_seconds", "enabled"):
            if required not in entry:
                raise DiagError("E1004", f"entry missing field {required}")
        alias = entry["alias"]
        if not isinstance(alias, str) or not alias:
            raise DiagError("E1004", "alias must be a non-empty string")
        if alias in seen:
            raise DiagError("E1001", f"duplicate alias {alias}")
        seen.add(alias)

        seconds = entry["relative_trust_seconds"]
        if not isinstance(seconds, int) or isinstance(seconds, bool) or seconds < 0:
            raise DiagError("E1003", f"alias {alias} relative_trust_seconds={seconds!r}")

        enabled = entry["enabled"]
        if not isinstance(enabled, bool):
            raise DiagError("E1006", f"alias {alias} enabled={enabled!r}")

        cert = _load_cert(base, entry["pem"])
        actual = _actual_fingerprint(cert)
        if actual != entry["fingerprint_sha256"].lower():
            raise DiagError("E1002", f"alias {alias}: stored {entry['fingerprint_sha256']} actual {actual}")

        snapshot.anchors.append(
            RawAnchor(
                alias=alias,
                pem_path=entry["pem"],
                cert=cert,
                fingerprint=actual,
                source_version="v1",
                logical_clock_tick=tick,
                enabled=enabled,
                relative_trust_seconds=seconds,
            )
        )
    return snapshot


def _validate_tombstone(value) -> dict | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise DiagError("E2005", f"tombstone must be object or null, got {type(value).__name__}")
    for required in ("code", "reason", "tick"):
        if required not in value:
            raise DiagError("E2005", f"tombstone missing field {required}")
    if not isinstance(value["code"], str) or not isinstance(value["reason"], str):
        raise DiagError("E2005", "tombstone code/reason must be strings")
    if not isinstance(value["tick"], int) or isinstance(value["tick"], bool) or value["tick"] < 0:
        raise DiagError("E2005", "tombstone tick must be a non-negative integer")
    return value


def load_v2(manifest_path: str | Path) -> Snapshot:
    manifest_path = Path(manifest_path)
    base = manifest_path.parent.parent if manifest_path.parent.name == "truststore_v2" else manifest_path.parent
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("format") != "v2":
        raise DiagError("E2009", "missing format=v2 marker")
    tick = manifest.get("logical_clock_tick")
    if not isinstance(tick, int) or isinstance(tick, bool) or tick < 0:
        raise DiagError("E2009", "v2 logical_clock_tick must be a non-negative integer")

    db_path = manifest_path.parent / "meta.sqlite3"
    if not db_path.is_file():
        raise DiagError("E2007", f"metadata DB missing: {db_path}")
    try:
        db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        meta_rows = db.execute(
            "SELECT alias, generation, effective_ms, alert_tombstone, revoked FROM anchor_meta ORDER BY rowid"
        ).fetchall()
        db.close()
    except sqlite3.DatabaseError as exc:
        raise DiagError("E2007", f"metadata DB unreadable: {exc}") from exc

    # Per-alias generation must be strictly monotonic across persisted rows.
    history: dict[str, list[int]] = {}
    meta_by_alias: dict[str, dict] = {}
    for alias, generation, effective_ms, tombstone_json, revoked in meta_rows:
        if not isinstance(generation, int) or generation < 0:
            raise DiagError("E2003", f"alias {alias} generation {generation!r}")
        if not isinstance(effective_ms, int) or isinstance(effective_ms, bool):
            raise DiagError("E2004", f"alias {alias} effective_ms {effective_ms!r}")
        if revoked not in (0, 1):
            raise DiagError("E2006", f"alias {alias} revoked flag {revoked!r}")
        tombstone = _validate_tombstone(json.loads(tombstone_json) if tombstone_json is not None else None)
        history.setdefault(alias, []).append(generation)
        meta_by_alias[alias] = {
            "generation": generation,
            "effective_ms": effective_ms,
            "alert_tombstone": tombstone,
            "revoked": bool(revoked),
        }
    for alias, generations in history.items():
        if generations != sorted(generations) or len(set(generations)) != len(generations):
            raise DiagError("E2003", f"alias {alias} generations not strictly monotonic: {generations}")

    snapshot = Snapshot(version="v2", logical_clock_tick=tick)
    shard_aliases: set[str] = set()
    for shard_info in manifest.get("shards", []):
        rel_file = shard_info.get("file")
        if not rel_file:
            raise DiagError("E2009", "manifest shard missing file")
        shard_path = base / rel_file
        if not shard_path.is_file():
            raise DiagError("E2002", rel_file)
        body = shard_path.read_bytes()
        if hashlib.sha256(body).hexdigest() != shard_info.get("sha256"):
            raise DiagError("E2001", rel_file)
        shard = json.loads(body.decode("utf-8"))
        for required in ("alias", "pem", "fingerprint_sha256", "effective_ms", "generation", "revoked"):
            if required not in shard:
                raise DiagError("E2009", f"{rel_file} missing field {required}")
        alias = shard["alias"]
        if alias in shard_aliases:
            raise DiagError("E2009", f"duplicate shard alias {alias}")
        shard_aliases.add(alias)

        effective_ms = shard["effective_ms"]
        if not isinstance(effective_ms, int) or isinstance(effective_ms, bool):
            raise DiagError("E2004", f"alias {alias} effective_ms {effective_ms!r}")
        generation = shard["generation"]
        if not isinstance(generation, int) or isinstance(generation, bool) or generation < 0:
            raise DiagError("E2003", f"alias {alias} generation {generation!r}")
        if not isinstance(shard["revoked"], bool):
            raise DiagError("E2006", f"alias {alias} revoked {shard['revoked']!r}")
        tombstone = _validate_tombstone(shard.get("alert_tombstone"))

        if alias not in meta_by_alias:
            raise DiagError("E2008", alias)
        meta = meta_by_alias[alias]
        if (
            meta["generation"] != generation
            or meta["effective_ms"] != effective_ms
            or meta["revoked"] != shard["revoked"]
            or meta["alert_tombstone"] != tombstone
        ):
            raise DiagError("E2007", f"shard/SQLite metadata mismatch for alias {alias}")

        cert = _load_cert(base, shard["pem"])
        actual = _actual_fingerprint(cert)
        if actual != shard["fingerprint_sha256"].lower():
            raise DiagError("E1002", f"v2 alias {alias} fingerprint mismatch")

        snapshot.anchors.append(
            RawAnchor(
                alias=alias,
                pem_path=shard["pem"],
                cert=cert,
                fingerprint=actual,
                source_version="v2",
                logical_clock_tick=tick,
                enabled=True,
                effective_ms=effective_ms,
                generation=generation,
                alert_tombstone=tombstone,
                revoked=shard["revoked"],
            )
        )
    return snapshot
