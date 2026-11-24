from __future__ import annotations

import argparse
import hashlib
import json
import random
import shutil
import sqlite3
from pathlib import Path
from typing import Any, Optional

from .io_utils import atomic_write_json
from .pkce import s256_challenge


DEFAULT_SEED = 20261125
CLOCK_MS = 1795000000000
REDIRECT_URI = "https://interop.client.invalid/callback"

S256_VERIFIER = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
PLAIN_VERIFIER = "plain-deterministic-verifier-20261125"
LEGACY_VERIFIER = "legacy-replacement-verifier-20261125-0123456789abcdef"


JsonDict = dict[str, Any]


def _write_json(path: Path, payload: JsonDict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(path, payload)


def _v1_code(
    record_id: str,
    code: str,
    session_id: str,
    *,
    generation: int = 1,
    offset_seconds: int = -60,
    ttl_seconds: int = 600,
    client_id: str = "client-demo",
    scope: Optional[list[str]] = None,
    used: bool = False,
    redirect_uri: str = REDIRECT_URI,
) -> JsonDict:
    return {
        "id": record_id,
        "type": "authorization_code",
        "generation": generation,
        "code": code,
        "client_id": client_id,
        "session_id": session_id,
        "redirect_uri": redirect_uri,
        "scope": scope or ["openid", "profile"],
        "issued_at_offset_seconds": offset_seconds,
        "expires_in_seconds": ttl_seconds,
        "used": used,
    }


def _v2_window(offset_seconds: int, ttl_seconds: int) -> tuple[int, int]:
    issued = CLOCK_MS + offset_seconds * 1000
    return issued, issued + ttl_seconds * 1000


def _v2_code(
    record_id: str,
    code: str,
    session_id: str,
    *,
    generation: int = 2,
    offset_seconds: int = -60,
    ttl_seconds: int = 600,
    client_id: str = "client-demo",
    scope: Optional[list[str]] = None,
    used: bool = False,
    tombstoned: bool = False,
    challenge: Optional[str] = None,
    method: Optional[str] = None,
    expires_at_ms: Optional[int] = None,
    issued_at_ms: Optional[int] = None,
) -> JsonDict:
    default_issued, default_expires = _v2_window(offset_seconds, ttl_seconds)
    return {
        "id": record_id,
        "type": "authorization_code",
        "generation": generation,
        "code": code,
        "client_id": client_id,
        "session_id": session_id,
        "redirect_uri": REDIRECT_URI,
        "scope": scope or ["openid", "profile"],
        "issued_at_ms": issued_at_ms if issued_at_ms is not None else default_issued,
        "expires_at_ms": expires_at_ms if expires_at_ms is not None else default_expires,
        "used": used,
        "tombstoned": tombstoned,
        "code_challenge": challenge,
        "code_challenge_method": method,
    }


def _session(version: str, session_id: str, generation: int = 1) -> JsonDict:
    if version == "v1":
        return {
            "id": session_id,
            "type": "session",
            "generation": generation,
            "session_id": session_id,
            "issued_at_offset_seconds": -300,
            "expires_in_seconds": 3600,
            "subject": "deterministic-local-subject",
        }
    issued, expires = _v2_window(-300, 3600)
    return {
        "id": session_id,
        "type": "session",
        "generation": generation,
        "session_id": session_id,
        "issued_at_ms": issued,
        "expires_at_ms": expires,
        "tombstoned": False,
        "subject": "deterministic-local-subject",
    }


def _write_v2_metadata(v2_dir: Path, shard_payloads: dict[str, JsonDict]) -> None:
    db_path = v2_dir / "metadata.sqlite"
    if db_path.exists():
        db_path.unlink()
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute(
            """
            CREATE TABLE shards (
                shard_name TEXT PRIMARY KEY,
                sha256 TEXT NOT NULL,
                record_count INTEGER NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE record_metadata (
                source_shard TEXT NOT NULL,
                record_no INTEGER NOT NULL,
                record_type TEXT,
                record_id TEXT,
                entity_key TEXT,
                client_id TEXT,
                code TEXT,
                session_id TEXT,
                generation INTEGER,
                tombstoned INTEGER,
                issued_at_ms INTEGER,
                expires_at_ms INTEGER
            )
            """
        )
        for shard_name, payload in shard_payloads.items():
            shard_path = v2_dir / shard_name
            raw = shard_path.read_bytes()
            conn.execute(
                "INSERT INTO shards VALUES (?, ?, ?)",
                (shard_name, hashlib.sha256(raw).hexdigest(), len(payload.get("records", []))),
            )
            for index, record in enumerate(payload.get("records", []), start=1):
                conn.execute(
                    """
                    INSERT INTO record_metadata VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        shard_name,
                        index,
                        record.get("type") or record.get("record_type"),
                        record.get("id") or record.get("record_id"),
                        record.get("entity_key"),
                        record.get("client_id"),
                        record.get("code"),
                        record.get("session_id"),
                        record.get("generation", 0),
                        1 if record.get("tombstoned") else 0,
                        record.get("issued_at_ms"),
                        record.get("expires_at_ms"),
                    ),
                )
        conn.commit()


def _attempt(
    attempt_id: str,
    code: str,
    *,
    sequence: int,
    client_id: str = "client-demo",
    redirect_uri: Optional[str] = REDIRECT_URI,
    verifier: Optional[str] = None,
) -> JsonDict:
    return {
        "attempt_id": attempt_id,
        "sequence": sequence,
        "code": code,
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "code_verifier": verifier,
    }


def generate_samples(output: str | Path = "samples", seed: int = DEFAULT_SEED) -> Path:
    root = Path(output)
    if root.exists() and (root / "seed.json").exists():
        shutil.rmtree(root)
    root.mkdir(parents=True, exist_ok=True)
    rng = random.Random(seed)
    nonce = rng.randrange(16**10, 16**11)

    _write_json(
        root / "clock.json",
        {
            "format": "auth-interop/observation-clock/v1",
            "observed_at_ms": CLOCK_MS,
            "declared_at": "2026-11-18T11:06:40Z",
            "truth": "This declared observation clock is the only adjudication now-source.",
            "disable_system_clock": True,
        },
    )
    _write_json(
        root / "seed.json",
        {"seed": seed, "generator": "auth_interop.samplegen", "rng_nonce": nonce},
    )

    sessions = [
        "sess-valid-v1",
        "sess-valid-s256",
        "sess-valid-plain",
        "sess-expired",
        "sess-used",
        "sess-replay",
        "sess-client-mismatch",
        "sess-redirect-mismatch",
        "sess-pkce-mismatch",
        "sess-pkce-missing",
        "sess-legacy-replaced",
        "sess-conflict",
        "sess-tombstone",
        "sess-revoke",
        "sess-cycle",
    ]
    v1_sessions = [_session("v1", sid) for sid in sessions]

    v1_codes = [
        _v1_code("code-valid-v1", "CODE_VALID_V1", "sess-valid-v1"),
        _v1_code("code-expired", "CODE_EXPIRED", "sess-expired", offset_seconds=-7200, ttl_seconds=300),
        _v1_code("code-replay", "CODE_REPLAY", "sess-replay"),
        _v1_code("code-client-mismatch", "CODE_CLIENT_MISMATCH", "sess-client-mismatch"),
        _v1_code("code-redirect-mismatch", "CODE_REDIRECT_MISMATCH", "sess-redirect-mismatch"),
        _v1_code(
            "code-legacy-replaced",
            "CODE_LEGACY_PKCE_REPLACED",
            "sess-legacy-replaced",
            offset_seconds=-120,
            ttl_seconds=900,
        ),
        _v1_code(
            "code-conflict",
            "CODE_CONFLICT",
            "sess-conflict",
            scope=["openid"],
            offset_seconds=-120,
            ttl_seconds=720,
        ),
        _v1_code("code-tombstone-old", "CODE_TOMBSTONE", "sess-tombstone"),
        _v1_code("code-revoke", "CODE_REVOKE", "sess-revoke"),
        _v1_code("code-cycle", "CODE_CYCLE", "sess-cycle"),
        _v1_code("code-duplicate-1", "CODE_DUPLICATE", "sess-duplicate"),
        _v1_code("code-duplicate-2", "CODE_DUPLICATE", "sess-duplicate"),
        {
            "id": "code-invalid-missing-value",
            "type": "authorization_code",
            "client_id": "client-demo",
            "session_id": "sess-invalid",
            "issued_at_offset_seconds": 0,
            "expires_in_seconds": 60,
        },
        _v1_code(
            "code-bad-time",
            "CODE_BAD_TIME",
            "sess-bad-time",
            ttl_seconds=-5,
        ),
    ]

    v1_payload = {
        "format": "auth-interop/v1-flat-json",
        "source_version": "v1",
        "relative_expiry": True,
        "pkce": "absent_by_design",
        "clients": [
            {"id": "client-demo", "type": "client", "client_id": "client-demo", "generation": 1},
            {
                "id": "client-conflict",
                "type": "client",
                "client_id": "client-conflict",
                "generation": 1,
            },
        ],
        "sessions": v1_sessions,
        "authorization_codes": v1_codes,
        "access_tokens": [],
        "refresh_tokens": [],
    }
    _write_json(root / "v1" / "flat.json", v1_payload)

    legacy_expires = CLOCK_MS + (-120 + 900) * 1000
    conflict_expires_v2 = CLOCK_MS + 1800 * 1000
    v2_core_records = [
        {
            "id": "client-demo-v2",
            "type": "client",
            "client_id": "client-demo",
            "generation": 2,
        },
        *[_session("v2", sid, generation=2) for sid in sessions],
        _v2_code(
            "code-valid-s256",
            "CODE_VALID_S256",
            "sess-valid-s256",
            challenge=s256_challenge(S256_VERIFIER),
            method="S256",
        ),
        _v2_code(
            "code-valid-plain",
            "CODE_VALID_PLAIN",
            "sess-valid-plain",
            generation=1,
            challenge=PLAIN_VERIFIER,
            method="plain",
        ),
        _v2_code(
            "code-used",
            "CODE_USED",
            "sess-used",
            used=True,
        ),
        _v2_code(
            "code-pkce-mismatch",
            "CODE_PKCE_MISMATCH",
            "sess-pkce-mismatch",
            challenge=s256_challenge(S256_VERIFIER),
            method="S256",
        ),
        _v2_code(
            "code-pkce-missing",
            "CODE_PKCE_MISSING",
            "sess-pkce-missing",
            challenge=s256_challenge(S256_VERIFIER),
            method="S256",
        ),
        _v2_code(
            "code-legacy-replaced-v2",
            "CODE_LEGACY_PKCE_REPLACED",
            "sess-legacy-replaced",
            issued_at_ms=CLOCK_MS - 60_000,
            expires_at_ms=legacy_expires,
            challenge=s256_challenge(LEGACY_VERIFIER),
            method="S256",
        ),
        _v2_code(
            "code-conflict-v2",
            "CODE_CONFLICT",
            "sess-conflict",
            scope=["openid", "profile", "email"],
            expires_at_ms=conflict_expires_v2,
        ),
        _v2_code(
            "code-tombstone-v2",
            "CODE_TOMBSTONE",
            "sess-tombstone",
            tombstoned=True,
        ),
    ]

    token_records = [
        {
            "id": "at-revoke-1",
            "type": "access_token",
            "token_id": "at-revoke-1",
            "client_id": "client-demo",
            "code": "CODE_REVOKE",
            "session_id": "sess-revoke",
            "generation": 1,
            "chain_id": "access-revoke",
            "issued_at_ms": CLOCK_MS - 120_000,
            "expires_at_ms": CLOCK_MS + 3600_000,
            "refresh_token_id": "rt-revoke-1",
            "tombstoned": False,
        },
        {
            "id": "rt-revoke-1",
            "type": "refresh_token",
            "token_id": "rt-revoke-1",
            "client_id": "client-demo",
            "session_id": "sess-revoke",
            "generation": 1,
            "chain_id": "chain-revoke",
            "issued_at_ms": CLOCK_MS - 120_000,
            "expires_at_ms": CLOCK_MS + 7 * 86400_000,
            "access_token_id": "at-revoke-1",
            "successor_id": "rt-revoke-2",
            "tombstoned": False,
        },
        {
            "id": "rt-revoke-2",
            "type": "refresh_token",
            "token_id": "rt-revoke-2",
            "client_id": "client-demo",
            "session_id": "sess-revoke",
            "generation": 2,
            "chain_id": "chain-revoke",
            "issued_at_ms": CLOCK_MS - 60_000,
            "expires_at_ms": CLOCK_MS + 7 * 86400_000,
            "access_token_id": "at-revoke-1",
            "rotates_from_id": "rt-revoke-1",
            "tombstoned": False,
        },
        {
            "id": "at-cycle",
            "type": "access_token",
            "token_id": "at-cycle",
            "client_id": "client-demo",
            "code": "CODE_CYCLE",
            "session_id": "sess-cycle",
            "generation": 1,
            "issued_at_ms": CLOCK_MS - 120_000,
            "expires_at_ms": CLOCK_MS + 3600_000,
            "refresh_token_id": "rt-cycle-a",
        },
        {
            "id": "rt-cycle-a",
            "type": "refresh_token",
            "token_id": "rt-cycle-a",
            "client_id": "client-demo",
            "session_id": "sess-cycle",
            "generation": 1,
            "chain_id": "chain-cycle",
            "issued_at_ms": CLOCK_MS - 120_000,
            "expires_at_ms": CLOCK_MS + 7 * 86400_000,
            "access_token_id": "at-cycle",
            "successor_id": "rt-cycle-b",
        },
        {
            "id": "rt-cycle-b",
            "type": "refresh_token",
            "token_id": "rt-cycle-b",
            "client_id": "client-demo",
            "session_id": "sess-cycle",
            "generation": 2,
            "chain_id": "chain-cycle",
            "issued_at_ms": CLOCK_MS - 60_000,
            "expires_at_ms": CLOCK_MS + 7 * 86400_000,
            "access_token_id": "at-cycle",
            "successor_id": "rt-cycle-a",
        },
        {
            "id": "rt-bad-monotonic-1",
            "type": "refresh_token",
            "token_id": "rt-bad-monotonic-1",
            "generation": 1,
            "chain_id": "chain-bad-time",
            "issued_at_ms": CLOCK_MS - 200_000,
            "expires_at_ms": CLOCK_MS + 3600_000,
        },
        {
            "id": "rt-bad-monotonic-2",
            "type": "refresh_token",
            "token_id": "rt-bad-monotonic-2",
            "generation": 2,
            "chain_id": "chain-bad-time",
            "issued_at_ms": CLOCK_MS - 300_000,
            "expires_at_ms": CLOCK_MS + 3600_000,
        },
    ]

    v2_dir = root / "v2"
    shard_payloads = {
        "shard-000.json": {
            "format": "auth-interop/v2-shard-json",
            "source_version": "v2",
            "shard": 0,
            "absolute_timestamps": "epoch_milliseconds",
            "records": v2_core_records,
        },
        "shard-001.json": {
            "format": "auth-interop/v2-shard-json",
            "source_version": "v2",
            "shard": 1,
            "absolute_timestamps": "epoch_milliseconds",
            "records": token_records,
        },
    }
    for name, payload in shard_payloads.items():
        _write_json(v2_dir / name, payload)
    _write_v2_metadata(v2_dir, shard_payloads)

    attempts = [
        _attempt("att-valid-v1", "CODE_VALID_V1", sequence=1),
        _attempt("att-valid-s256", "CODE_VALID_S256", sequence=2, verifier=S256_VERIFIER),
        _attempt("att-valid-plain", "CODE_VALID_PLAIN", sequence=3, verifier=PLAIN_VERIFIER),
        _attempt("att-expired", "CODE_EXPIRED", sequence=4),
        _attempt("att-used", "CODE_USED", sequence=5),
        _attempt("att-replay-1", "CODE_REPLAY", sequence=6),
        _attempt("att-replay-2", "CODE_REPLAY", sequence=7),
        _attempt("att-client-mismatch", "CODE_CLIENT_MISMATCH", sequence=8, client_id="client-wrong"),
        _attempt("att-redirect-mismatch", "CODE_REDIRECT_MISMATCH", sequence=9, redirect_uri="https://wrong.example.invalid/cb"),
        _attempt("att-pkce-mismatch", "CODE_PKCE_MISMATCH", sequence=10, verifier="definitely-wrong-verifier"),
        _attempt("att-pkce-missing", "CODE_PKCE_MISSING", sequence=11),
        _attempt("att-legacy-replaced", "CODE_LEGACY_PKCE_REPLACED", sequence=12, verifier=LEGACY_VERIFIER),
        _attempt("att-conflict", "CODE_CONFLICT", sequence=13),
        _attempt("att-tombstone", "CODE_TOMBSTONE", sequence=14),
    ]
    _write_json(root / "redemption_attempts.json", {"attempts": attempts})

    happy_attempts = [attempts[0], attempts[1], attempts[2]]
    _write_json(root / "redemption_attempts_success.json", {"attempts": happy_attempts})

    revocations = {
        "revocations": [
            {
                "request_id": "rev-session-revoke",
                "entity_type": "session",
                "entity_id": "sess-revoke",
            },
            {
                "request_id": "rev-code-revoke",
                "entity_type": "authorization_code",
                "entity_id": "CODE_REVOKE",
                "client_id": "client-demo",
            },
            {
                "request_id": "rev-code-cycle",
                "entity_type": "authorization_code",
                "entity_id": "CODE_CYCLE",
                "client_id": "client-demo",
            },
        ]
    }
    _write_json(root / "revocation_requests.json", revocations)

    _generate_batch_samples(root / "batch", seed)
    return root


def _generate_batch_samples(batch_root: Path, seed: int) -> None:
    for name, code in [("sample-valid-a", "BATCH_CODE_A"), ("sample-valid-c", "BATCH_CODE_C")]:
        sample_root = batch_root / name
        _write_json(sample_root / "clock.json", {"observed_at_ms": CLOCK_MS})
        _write_json(
            sample_root / "v1" / "flat.json",
            {
                "source_version": "v1",
                "sessions": [_session("v1", f"sess-{name}")],
                "authorization_codes": [_v1_code("code-1", code, f"sess-{name}")],
            },
        )
        v2_records = [
            _v2_code(
                "code-1-v2",
                code,
                f"sess-{name}",
                generation=2,
            )
        ]
        payload = {"records": v2_records}
        _write_json(sample_root / "v2" / "shard-000.json", payload)
        _write_v2_metadata(sample_root / "v2", {"shard-000.json": payload})

    bad_root = batch_root / "sample-bad-json"
    bad_root.mkdir(parents=True, exist_ok=True)
    _write_json(bad_root / "clock.json", {"observed_at_ms": CLOCK_MS})
    (bad_root / "v1").mkdir(parents=True, exist_ok=True)
    (bad_root / "v1" / "flat.json").write_text('{"records": [\n', encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default="samples", help="deterministic sample output directory")
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    args = parser.parse_args()
    path = generate_samples(args.output, args.seed)
    print(f"generated deterministic samples in {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
