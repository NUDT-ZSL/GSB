"""Deterministic fixed-seed sample generator for the TSA audit prototype."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .canon import sha256_obj


DEFAULT_SEED = 402
OBSERVATION_CLOCK_MS = 1_730_000_402_000
V2_REGISTERED_MS = OBSERVATION_CLOCK_MS + 60_000
TOKEN_ISSUED_MS = OBSERVATION_CLOCK_MS + 120_000
SIGNED_MS = OBSERVATION_CLOCK_MS + 180_000
VALID_FROM_MS = OBSERVATION_CLOCK_MS + 50_000
VALID_TO_MS = OBSERVATION_CLOCK_MS + 3_600_000


def digest(label: str, seed: int = DEFAULT_SEED) -> str:
    return hashlib.sha256(f"tsaudit:{seed}:{label}".encode("utf-8")).hexdigest()


def iso_ms(value_ms: int) -> str:
    return datetime.fromtimestamp(value_ms / 1000, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def sync_marker(seed: int, kind: str, number: int, label: str) -> str:
    raw = f"{seed}|{kind}|{number}|{label}"
    return f"sync-{seed}-{kind}-{number:03d}-{hashlib.sha256(raw.encode()).hexdigest()[:16]}"


def _signature(
    number: int,
    artifact_id: str,
    label: str,
    signed_at: int,
    *,
    bound_token_id: str | None = None,
    seed: int = DEFAULT_SEED,
    override_digest: str | None = None,
) -> dict[str, Any]:
    return {
        "artifact_id": artifact_id,
        "signature_digest": override_digest if override_digest is not None else digest(label, seed),
        "digest_algorithm": "sha256",
        "local_signed_at_ms": signed_at,
        "bound_token_id": bound_token_id,
        "sender_time_repr": iso_ms(signed_at),
        "sync_marker": sync_marker(seed, "sig", number, artifact_id),
    }


def _token(
    number: int,
    token_id: str,
    label: str,
    issuer_id: str,
    *,
    issued_at: int = TOKEN_ISSUED_MS,
    generation: int = 2,
    valid_from: int = VALID_FROM_MS,
    valid_to: int = VALID_TO_MS,
    seed: int = DEFAULT_SEED,
    digest_override: str | None = None,
) -> dict[str, Any]:
    return {
        "token_id": token_id,
        "issuer_id": issuer_id,
        "covered_signature_digest": digest_override if digest_override is not None else digest(label, seed),
        "issued_at_ms": issued_at,
        "token_generation": generation,
        "valid_from_ms": valid_from,
        "valid_to_ms": valid_to,
        "sender_time_repr": iso_ms(issued_at),
        "sync_marker": sync_marker(seed, "tok", number, token_id),
    }


def build_sample(seed: int = DEFAULT_SEED) -> dict[str, Any]:
    signatures: list[dict[str, Any]] = [
        _signature(1, "art-ok", "ok", SIGNED_MS, bound_token_id="tok-ok", seed=seed),
        _signature(2, "art-multi", "multi", OBSERVATION_CLOCK_MS + 500_000, seed=seed),
        _signature(3, "art-reanchor", "reanchor", OBSERVATION_CLOCK_MS + 500_000, seed=seed),
        _signature(4, "art-orphan-revoked-before", "orphan-revoked", SIGNED_MS, seed=seed),
        _signature(5, "art-late-issuer", "late-issuer", SIGNED_MS, seed=seed),
        _signature(6, "art-generation-mismatch", "mismatch", SIGNED_MS, seed=seed),
        _signature(7, "art-tombstoned", "tombstone", SIGNED_MS, seed=seed),
        _signature(8, "art-trust-conflict", "conflict", SIGNED_MS, seed=seed),
        _signature(9, "art-token-future", "future", OBSERVATION_CLOCK_MS + 300_000, seed=seed),
        _signature(10, "art-expired-token", "expired", SIGNED_MS, seed=seed),
        _signature(11, "art-not-yet-valid", "not-valid", SIGNED_MS, seed=seed),
        _signature(12, "art-no-coverage", "no-coverage", SIGNED_MS, seed=seed),
    ]

    # Deliberately invalid rows. The parser must reject and continue.
    signatures.extend([
        {
            "artifact_id": "art-invalid-hex",
            "signature_digest": "zz" * 32,
            "local_signed_at_ms": SIGNED_MS,
        },
        {
            "artifact_id": "art-invalid-length",
            "signature_digest": "abcdef",
            "local_signed_at_ms": SIGNED_MS,
        },
        {
            "artifact_id": "art-invalid-time",
            "signature_digest": digest("bad-time", seed),
            "local_signed_at_ms": "not-an-integer",
        },
        {
            "artifact_id": "art-ok",
            "signature_digest": digest("duplicate-id", seed),
            "local_signed_at_ms": SIGNED_MS,
        },
        {
            "signature_digest": digest("missing-id", seed),
            "local_signed_at_ms": SIGNED_MS,
        },
    ])
    invalid_signature_codes = [
        "E_DIGEST_HEX",
        "E_DIGEST_LENGTH",
        "E_TIME_INTEGER",
        "E_DUP_ARTIFACT_ID",
        "E_ID_EMPTY",
    ]
    for offset, record in enumerate(signatures[12:], start=1):
        record["_expected_rejection_code"] = invalid_signature_codes[offset - 1]
        signed_at = record.get("local_signed_at_ms")
        record.setdefault(
            "sender_time_repr",
            iso_ms(signed_at) if isinstance(signed_at, int) and not isinstance(signed_at, bool)
            else "sender-original:unparseable-local-time",
        )
        record.setdefault(
            "sync_marker",
            sync_marker(seed, "badsig", offset, str(record.get("artifact_id", "missing-id"))),
        )

    tokens: list[dict[str, Any]] = [
        _token(1, "tok-ok", "ok", "TSA-Alpha", seed=seed),

        _token(2, "tok-multi-a", "multi", "TSA-Alpha",
               issued_at=OBSERVATION_CLOCK_MS + 240_000, generation=2, seed=seed),
        _token(3, "tok-multi-b", "multi", "TSA-Beta",
               issued_at=OBSERVATION_CLOCK_MS + 270_000, generation=3, seed=seed),
        _token(4, "tok-multi-c", "multi", "TSA-Alpha",
               issued_at=OBSERVATION_CLOCK_MS + 300_000, generation=2, seed=seed),

        _token(5, "tok-revoked-primary", "reanchor", "TSA-Revoked-After", seed=seed),
        _token(6, "tok-backup", "reanchor", "TSA-Backup",
               issued_at=OBSERVATION_CLOCK_MS + 330_000, generation=2, seed=seed),

        _token(7, "tok-revoked-before", "orphan-revoked", "TSA-Revoked-Before",
               generation=1, seed=seed),
        _token(8, "tok-late-issuer", "late-issuer", "TSA-Late", seed=seed),
        _token(9, "tok-generation-mismatch", "mismatch", "TSA-Mismatch", generation=9, seed=seed),
        _token(10, "tok-tombstoned", "tombstone", "TSA-Tombstone", seed=seed),
        _token(11, "tok-conflict", "conflict", "TSA-Conflict", generation=1, seed=seed),
        _token(12, "tok-future", "future", "TSA-Alpha",
               issued_at=OBSERVATION_CLOCK_MS + 420_000, seed=seed),
        _token(13, "tok-expired", "expired", "TSA-Alpha",
               valid_from=OBSERVATION_CLOCK_MS + 60_000,
               valid_to=OBSERVATION_CLOCK_MS + 100_000, seed=seed),
        _token(14, "tok-not-yet-valid", "not-valid", "TSA-Alpha",
               valid_from=OBSERVATION_CLOCK_MS + 200_000,
               valid_to=VALID_TO_MS, seed=seed),
    ]
    tokens.extend([
        {
            "token_id": "tok-invalid-hex",
            "issuer_id": "TSA-Alpha",
            "covered_signature_digest": "00ffzz",
            "issued_at_ms": TOKEN_ISSUED_MS,
            "token_generation": 2,
            "valid_from_ms": VALID_FROM_MS,
            "valid_to_ms": VALID_TO_MS,
        },
        {
            "token_id": "tok-invalid-window",
            "issuer_id": "TSA-Alpha",
            "covered_signature_digest": digest("bad-window", seed),
            "issued_at_ms": TOKEN_ISSUED_MS,
            "token_generation": 2,
            "valid_from_ms": 200,
            "valid_to_ms": 100,
        },
        {
            "token_id": "tok-negative-generation",
            "issuer_id": "TSA-Alpha",
            "covered_signature_digest": digest("negative-gen", seed),
            "issued_at_ms": TOKEN_ISSUED_MS,
            "token_generation": -1,
            "valid_from_ms": VALID_FROM_MS,
            "valid_to_ms": VALID_TO_MS,
        },
        {
            "token_id": "tok-ok",
            "issuer_id": "TSA-Alpha",
            "covered_signature_digest": digest("duplicate-token", seed),
            "issued_at_ms": TOKEN_ISSUED_MS,
            "token_generation": 2,
            "valid_from_ms": VALID_FROM_MS,
            "valid_to_ms": VALID_TO_MS,
        },
    ])
    invalid_token_codes = [
        "E_DIGEST_HEX",
        "E_TIME_WINDOW",
        "E_GENERATION_NON_NEGATIVE",
        "E_DUP_TOKEN_ID",
    ]
    for offset, record in enumerate(tokens[14:], start=1):
        record["_expected_rejection_code"] = invalid_token_codes[offset - 1]
        issued_at = record.get("issued_at_ms")
        record.setdefault(
            "sender_time_repr",
            iso_ms(issued_at) if isinstance(issued_at, int) and not isinstance(issued_at, bool)
            else "sender-original:unparseable-issue-time",
        )
        record.setdefault(
            "sync_marker",
            sync_marker(seed, "badtok", offset, str(record.get("token_id", "missing-token"))),
        )

    v1 = {
        "format": "tsaudit.trust-v1",
        "schema_version": 1,
        "snapshot_id": "trust-v1-baseline",
        "trust_generation": 1,
        "relative_effective_seconds": 0,
        "issuers": [
            "TSA-Alpha", "TSA-Backup", "TSA-Beta", "TSA-Mismatch",
            "TSA-Revoked-After", "TSA-Tombstone", "TSA-Conflict",
        ],
        "revocations": ["TSA-Revoked-Before"],
    }

    def v2_record(
        issuer: str,
        tsa_generation: int,
        *,
        token_generation: int | None = None,
        trusted: bool = True,
        revoked: bool = False,
        revoked_at: int | None = None,
        reason: str | None = None,
        tombstone: bool = False,
        registered_at: int = V2_REGISTERED_MS,
    ) -> dict[str, Any]:
        return {
            "issuer_id": issuer,
            "canonical_issuer_id": issuer.lower(),
            "registered_at_ms": registered_at,
            "effective_at_ms": registered_at,
            "tsa_generation": tsa_generation,
            "token_generation": token_generation if token_generation is not None else tsa_generation,
            "trusted": trusted,
            "revoked": revoked,
            "revoked_at_ms": revoked_at,
            "revocation_reason_code": reason,
            "tombstone": tombstone,
        }

    v2_records = [
        v2_record("TSA-Alpha", 2, token_generation=2),
        v2_record("TSA-Backup", 2, token_generation=2),
        v2_record("TSA-Beta", 3, token_generation=3),
        v2_record("TSA-Mismatch", 2, token_generation=2),
        v2_record("TSA-Gen-Nine", 9, token_generation=9),
        v2_record("TSA-Revoked-After", 2, token_generation=2, revoked=True,
                   revoked_at=OBSERVATION_CLOCK_MS + 240_000, reason="key_compromise"),
        v2_record("TSA-Late", 2, token_generation=2,
                  registered_at=OBSERVATION_CLOCK_MS + 400_000),
        v2_record("TSA-Tombstone", 2, token_generation=2, tombstone=True, trusted=False,
                  reason="generation_superseded"),
        v2_record("TSA-Conflict", 1, token_generation=1, trusted=False, revoked=True,
                  revoked_at=OBSERVATION_CLOCK_MS, reason="policy_disagreement",
                  registered_at=OBSERVATION_CLOCK_MS),
    ]

    alpha_revocation_update = {
        "format": "tsaudit.trust-update",
        "snapshot_id": "trust-update-revoke-alpha",
        "basis_generation": 3,
        "effective_at_ms": OBSERVATION_CLOCK_MS + 600_000,
        "records": [
            v2_record(
                "TSA-Alpha", 3, token_generation=2, trusted=False, revoked=True,
                revoked_at=OBSERVATION_CLOCK_MS + 600_000, reason="key_compromise",
                registered_at=OBSERVATION_CLOCK_MS + 600_000,
            )
        ],
    }
    generation_tombstone_update = {
        "format": "tsaudit.trust-update",
        "snapshot_id": "trust-update-tombstone-gen-3",
        "basis_generation": 4,
        "effective_at_ms": OBSERVATION_CLOCK_MS + 610_000,
        "records": [
            v2_record(
                "TSA-Beta", 4, token_generation=3, tombstone=True, trusted=False,
                reason="generation_tombstone",
                registered_at=OBSERVATION_CLOCK_MS + 610_000,
            )
        ],
    }
    cycle_fixture = {
        "format": "tsaudit.trust-update",
        "snapshot_id": "trust-update-cycle",
        "basis_generation": 9,
        "effective_at_ms": OBSERVATION_CLOCK_MS + 620_000,
        "records": [
            {
                **v2_record("TSA-Cycle-A", 9, token_generation=9),
                "references": ["token_generation:9"],
            },
            {
                **v2_record("TSA-Cycle-B", 9, token_generation=9),
                "references": ["issuer:tsa-cycle-a"],
            },
        ],
        "generation_reference_edges": [
            {"from": "token_generation:9", "to": "issuer:tsa-cycle-b"}
        ],
    }

    truth = _build_truth(signatures, tokens, seed)
    for record in signatures + tokens:
        record.pop("_expected_rejection_code", None)
    return {
        "seed": seed,
        "observation_clock_ms": OBSERVATION_CLOCK_MS,
        "signatures": signatures,
        "tokens": tokens,
        "v1": v1,
        "v2_records": v2_records,
        "alpha_revocation_update": alpha_revocation_update,
        "generation_tombstone_update": generation_tombstone_update,
        "cycle_fixture": cycle_fixture,
        "truth": truth,
    }


def _expected_for_signature(artifact: dict[str, Any]) -> dict[str, Any]:
    artifact_id = artifact["artifact_id"]
    mapping = {
        "art-ok": {
            "status": "anchored", "primary_token_id": "tok-ok",
            "codes": ["ANCHORED"], "basis_generation": 2,
        },
        "art-multi": {
            "status": "anchored", "primary_token_id": "tok-multi-a",
            "alternative_token_ids": ["tok-multi-b", "tok-multi-c"],
            "codes_include": ["W_MULTIPLE_TOKEN_COVERAGE"],
        },
        "art-reanchor": {
            "status": "anchored", "primary_token_id": "tok-backup",
            "codes": ["ANCHORED"],
        },
        "art-orphan-revoked-before": {
            "status": "unanchored", "codes_include": ["E_REVOCATION_BEFORE_ISSUANCE"],
        },
        "art-late-issuer": {
            "status": "unanchored", "codes_include": ["E_ISSUER_NOT_REGISTERED"],
        },
        "art-generation-mismatch": {
            "status": "unanchored", "codes_include": ["E_GENERATION_MISMATCH"],
        },
        "art-tombstoned": {
            "status": "unanchored", "codes_include": ["E_ISSUER_TOMBSTONED"],
        },
        "art-trust-conflict": {
            "status": "unanchored", "codes_include": ["E_TRUST_CONFLICT"],
        },
        "art-token-future": {
            "status": "unanchored", "codes_include": ["E_TOKEN_AFTER_SIGNING"],
        },
        "art-expired-token": {
            "status": "unanchored", "codes_include": ["E_TOKEN_EXPIRED"],
        },
        "art-not-yet-valid": {
            "status": "unanchored", "codes_include": ["E_TOKEN_NOT_YET_VALID"],
        },
        "art-no-coverage": {
            "status": "unanchored", "codes": ["UNANCHORED", "E_NO_TOKEN_COVERAGE"],
        },
    }
    return mapping[artifact_id]


def _build_truth(signatures: list[dict[str, Any]], tokens: list[dict[str, Any]], seed: int) -> dict[str, Any]:
    sync_checks: list[dict[str, Any]] = []
    for signature in signatures:
        if "sync_marker" not in signature:
            continue
        expected_code = signature.get("_expected_rejection_code")
        expected = (
            {"rejected": True, "code": expected_code}
            if expected_code else _expected_for_signature(signature)
        )
        sync_checks.append({
            "kind": "signature",
            "sync_marker": signature["sync_marker"],
            "source_id": signature.get("artifact_id", "<missing-artifact-id>"),
            "sender_original_time_repr": signature["sender_time_repr"],
            "sender_original_time_ms": signature.get("local_signed_at_ms"),
            "expected": expected,
        })
    for token in tokens:
        if "sync_marker" not in token:
            continue
        expected_code = token.get("_expected_rejection_code")
        sync_checks.append({
            "kind": "token",
            "sync_marker": token["sync_marker"],
            "source_id": token["token_id"],
            "sender_original_time_repr": token["sender_time_repr"],
            "sender_original_time_ms": token.get("issued_at_ms"),
            "expected": {"rejected": True, "code": expected_code}
            if expected_code else {
                "issued_at_ms": token["issued_at_ms"],
                "token_generation": token["token_generation"],
                "issuer_id": token["issuer_id"],
            },
        })
    marker_by_source_id = {
        case["source_id"]: case["sync_marker"]
        for case in sync_checks
        if case["kind"] == "signature" and not case["expected"].get("rejected")
    }
    expected_after = {
        "art-ok": {"status": "unanchored", "primary_token_id": None},
        "art-multi": {"status": "anchored", "primary_token_id": "tok-multi-b"},
        "art-reanchor": {"status": "anchored", "primary_token_id": "tok-backup"},
    }
    return {
        "seed": seed,
        "observation_clock_ms": OBSERVATION_CLOCK_MS,
        "validation_basis": "sync_marker_only",
        "sync_checks": sync_checks,
        "expected_rejection_codes": [
            "E_DIGEST_HEX",
            "E_DIGEST_LENGTH",
            "E_TIME_INTEGER",
            "E_DUP_ARTIFACT_ID",
            "E_ID_EMPTY",
            "E_DIGEST_HEX",
            "E_TIME_WINDOW",
            "E_GENERATION_NON_NEGATIVE",
            "E_DUP_TOKEN_ID",
        ],
        "revocation_update": {
            "file": "updates/revoke-alpha.json",
            "affected_artifact_keys_include": ["art-ok"],
            "expected_after": expected_after,
            "expected_after_by_marker": {
                marker_by_source_id[source_id]: expectation
                for source_id, expectation in expected_after.items()
            },
        },
    }


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")


def _write_v2_database(v2_dir: Path, records: list[dict[str, Any]], shards: list[dict[str, Any]]) -> None:
    db_path = v2_dir / "meta.db"
    if db_path.exists():
        db_path.unlink()
    connection = sqlite3.connect(db_path)
    try:
        connection.executescript(
            """
            CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE shards (
                shard_id TEXT PRIMARY KEY, path TEXT NOT NULL,
                digest TEXT NOT NULL, record_count INTEGER NOT NULL
            );
            CREATE TABLE trust_records (
                issuer_id TEXT NOT NULL,
                canonical_issuer_id TEXT NOT NULL,
                registered_at_ms INTEGER NOT NULL,
                tsa_generation INTEGER NOT NULL,
                token_generation INTEGER,
                trusted INTEGER NOT NULL,
                tombstone INTEGER NOT NULL,
                revoked INTEGER NOT NULL,
                revoked_at_ms INTEGER,
                revocation_reason_code TEXT
            );
            """
        )
        connection.executemany("INSERT INTO meta(key,value) VALUES(?,?)", [
            ("snapshot_id", "trust-v2-sharded"),
            ("basis_generation", "2"),
            ("effective_at_ms", str(V2_REGISTERED_MS)),
            ("format_version", "1"),
        ])
        for shard in shards:
            connection.execute(
                "INSERT INTO shards(shard_id,path,digest,record_count) VALUES(?,?,?,?)",
                (shard["shard_id"], shard["path"], shard["digest"], shard["record_count"]),
            )
        connection.executemany(
            "INSERT INTO trust_records VALUES(?,?,?,?,?,?,?,?,?,?)",
            [
                (r["issuer_id"], r["canonical_issuer_id"], r["registered_at_ms"],
                 r["tsa_generation"], r["token_generation"], int(r["trusted"]),
                 int(r["tombstone"]), int(r["revoked"]), r["revoked_at_ms"],
                 r["revocation_reason_code"])
                for r in records
            ],
        )
        connection.commit()
    finally:
        connection.close()


def generate_sample(out: str | Path, seed: int = DEFAULT_SEED) -> dict[str, Any]:
    root = Path(out)
    root.mkdir(parents=True, exist_ok=True)
    sample = build_sample(seed)

    _write_json(root / "manifest.json", {
        "format": "tsaudit.sample-manifest",
        "version": 1,
        "seed": seed,
        "observation_clock_ms": sample["observation_clock_ms"],
        "digest_algorithm": "sha256",
        "files": {
            "signatures": "signatures.json",
            "tokens": "tokens.json",
            "v1": "snapshots/v1/trust_v1.json",
            "v2": "snapshots/v2",
        },
    })
    _write_json(root / "signatures.json", {
        "format": "tsaudit.signature-batch", "records": sample["signatures"],
    })
    _write_json(root / "tokens.json", {
        "format": "tsaudit.token-batch", "records": sample["tokens"],
    })
    _write_json(root / "snapshots" / "v1" / "trust_v1.json", sample["v1"])

    records = sample["v2_records"]
    midpoint = 5
    shard_specs = [
        ("shard-0", "shards/shard_0.json", records[:midpoint]),
        ("shard-1", "shards/shard_1.json", records[midpoint:]),
    ]
    shards_for_db: list[dict[str, Any]] = []
    for shard_id, relative_path, shard_records in shard_specs:
        payload = {"shard_id": shard_id, "records": shard_records}
        _write_json(root / "snapshots" / "v2" / relative_path, payload)
        shards_for_db.append({
            "shard_id": shard_id,
            "path": relative_path,
            "digest": sha256_obj(shard_records),
            "record_count": len(shard_records),
        })
    _write_v2_database(root / "snapshots" / "v2", records, shards_for_db)

    _write_json(root / "updates" / "revoke-alpha.json", sample["alpha_revocation_update"])
    _write_json(root / "updates" / "tombstone-generation.json", sample["generation_tombstone_update"])
    _write_json(root / "invalid" / "cycle-reference.json", sample["cycle_fixture"])
    _write_json(root / "truth.json", sample["truth"])

    marker_files = [
        "manifest.json", "signatures.json", "tokens.json", "truth.json",
        "snapshots/v1/trust_v1.json", "snapshots/v2/meta.db",
        "snapshots/v2/shards/shard_0.json", "snapshots/v2/shards/shard_1.json",
        "updates/revoke-alpha.json", "updates/tombstone-generation.json",
        "invalid/cycle-reference.json",
    ]
    file_markers: dict[str, str] = {}
    for relative in marker_files:
        path = root / relative
        if path.is_file():
            import hashlib
            file_markers[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    record_markers = [
        item["sync_marker"] for item in sample["truth"]["sync_checks"]
        if item.get("sync_marker")
    ]
    total_marker = sha256_obj({"files": file_markers, "records": sorted(record_markers)})
    marker_payload = {
        "format": "tsaudit.sync-marker",
        "version": 1,
        "seed": seed,
        "observation_clock_ms": sample["observation_clock_ms"],
        "verification": "compare_by_sync_marker_only",
        "files": file_markers,
        "record_markers": sorted(record_markers),
        "total_marker": total_marker,
    }
    _write_json(root / "sync.marker.json", marker_payload)
    return {
        "status": "generated",
        "out": str(root),
        "seed": seed,
        "total_marker": total_marker,
        "signature_records": len(sample["signatures"]),
        "token_records": len(sample["tokens"]),
        "sync_checks": len(sample["truth"]["sync_checks"]),
    }
