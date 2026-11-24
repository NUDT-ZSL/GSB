from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Dict, List

from .digests import derive_pbkdf2_digest, file_sha256, sha256_hex

SEED = "account-verify-fixed-seed-2026"
NOW_MS = 1_767_225_600_000
MINUTE = 60_000
HOUR = 60 * MINUTE
DAY = 24 * HOUR


def _salt(user_id: str, generation: int) -> str:
    return sha256_hex(f"{SEED}:salt:{user_id}:gen{generation}".encode("utf-8"))[:32]


def _credential(
    user_id: str,
    generation: int,
    algorithm: str,
    iterations: int,
    *,
    effective_delta_ms: int,
) -> Dict[str, Any]:
    salt = _salt(user_id, generation)
    password = f"{SEED}:password:{user_id}:gen{generation}"
    digest = derive_pbkdf2_digest(
        algorithm=algorithm,
        password=password,
        salt_hex=salt,
        iterations=iterations,
    )
    return {
        "record_id": f"cred-{user_id}-g{generation}",
        "user_id": user_id,
        "credential_generation": generation,
        "salt_hex": salt,
        "salt_length_bytes": 16,
        "algorithm_tier": algorithm,
        "iterations": iterations,
        "digest_value": digest,
        "effective_at_ms": NOW_MS + effective_delta_ms,
    }


def _token(
    token_id: str,
    user_id: str,
    credential: Dict[str, Any],
    *,
    issued_delta_ms: int,
    expires_delta_ms: int,
    use_count: int = 0,
    one_time: bool = True,
    signature: str | None = None,
    signing_algorithm: str | None = None,
) -> Dict[str, Any]:
    return {
        "token_id": token_id,
        "user_id": user_id,
        "issued_at_ms": NOW_MS + issued_delta_ms,
        "expires_at_ms": NOW_MS + expires_delta_ms,
        "use_count": use_count,
        "one_time": one_time,
        "old_digest_signature": credential["digest_value"] if signature is None else signature,
        "signing_algorithm": credential["algorithm_tier"] if signing_algorithm is None else signing_algorithm,
    }


def build_samples() -> Dict[str, Any]:
    users = [
        "u-alpha",
        "u-beta",
        "u-gamma",
        "u-delta",
        "u-epsilon",
        "u-zeta",
        "u-eta",
        "u-conflict",
        "u-tomb",
        "u-direct",
        "u-strong",
    ]

    credentials = [
        _credential("u-alpha", 1, "legacy-pbkdf2-sha1", 12_000, effective_delta_ms=-40 * DAY),
        _credential("u-beta", 1, "pbkdf2-sha256", 240_000, effective_delta_ms=-60 * DAY),
        _credential("u-beta", 2, "legacy-pbkdf2-sha1", 18_000, effective_delta_ms=-6 * DAY),
        _credential("u-gamma", 1, "legacy-pbkdf2-sha1", 20_000, effective_delta_ms=-45 * DAY),
        _credential("u-gamma", 2, "pbkdf2-sha256", 220_000, effective_delta_ms=-20 * DAY),
        _credential("u-delta", 1, "legacy-pbkdf2-sha1", 16_000, effective_delta_ms=-40 * DAY),
        _credential("u-epsilon", 1, "legacy-pbkdf2-sha1", 16_000, effective_delta_ms=-40 * DAY),
        _credential("u-zeta", 1, "legacy-pbkdf2-sha1", 16_000, effective_delta_ms=-40 * DAY),
        _credential("u-eta", 1, "legacy-pbkdf2-sha1", 16_000, effective_delta_ms=-40 * DAY),
        _credential("u-conflict", 1, "pbkdf2-sha256", 210_000, effective_delta_ms=-12 * DAY),
        _credential("u-tomb", 1, "pbkdf2-sha256", 210_000, effective_delta_ms=-12 * DAY),
        _credential("u-direct", 1, "pbkdf2-sha256", 550_000, effective_delta_ms=-12 * DAY),
        _credential("u-strong", 1, "pbkdf2-sha256", 350_000, effective_delta_ms=-12 * DAY),
    ]
    cred_by_id = {item["record_id"]: item for item in credentials}

    tokens = [
        _token(
            "tok-alpha-valid",
            "u-alpha",
            cred_by_id["cred-u-alpha-g1"],
            issued_delta_ms=-2 * DAY,
            expires_delta_ms=DAY,
        ),
        _token(
            "tok-beta-bad-signature",
            "u-beta",
            cred_by_id["cred-u-beta-g2"],
            issued_delta_ms=-2 * DAY,
            expires_delta_ms=DAY,
            signature="0" * 64,
        ),
        _token(
            "tok-gamma-old-after-migration",
            "u-gamma",
            cred_by_id["cred-u-gamma-g1"],
            issued_delta_ms=-10 * DAY,
            expires_delta_ms=5 * DAY,
            signing_algorithm="legacy-pbkdf2-sha1",
        ),
        _token(
            "tok-gamma-legacy-window",
            "u-gamma",
            cred_by_id["cred-u-gamma-g1"],
            issued_delta_ms=-25 * DAY,
            expires_delta_ms=15 * DAY,
            signing_algorithm="legacy-pbkdf2-sha1",
        ),
        _token(
            "tok-delta-expired",
            "u-delta",
            cred_by_id["cred-u-delta-g1"],
            issued_delta_ms=-3 * DAY,
            expires_delta_ms=-HOUR,
        ),
        _token(
            "tok-epsilon-reused",
            "u-epsilon",
            cred_by_id["cred-u-epsilon-g1"],
            issued_delta_ms=-2 * DAY,
            expires_delta_ms=DAY,
            use_count=1,
        ),
        _token(
            "tok-zeta-outside-window",
            "u-zeta",
            cred_by_id["cred-u-zeta-g1"],
            issued_delta_ms=-40 * DAY,
            expires_delta_ms=2 * DAY,
        ),
        _token(
            "tok-eta-not-one-time",
            "u-eta",
            cred_by_id["cred-u-eta-g1"],
            issued_delta_ms=-2 * DAY,
            expires_delta_ms=DAY,
            one_time=False,
        ),
        _token(
            "tok-conflict-policy",
            "u-conflict",
            cred_by_id["cred-u-conflict-g1"],
            issued_delta_ms=-2 * DAY,
            expires_delta_ms=DAY,
        ),
    ]

    user_payload = {
        "schema": "account-verify/credentials/v1",
        "seed": SEED,
        "users": [{"user_id": uid, "display_name": uid.replace("-", " ").title()} for uid in users],
        "credentials": credentials,
    }
    token_payload = {
        "schema": "account-verify/reset-tokens/v1",
        "logical_clock": "manifest.observed_clock_ms",
        "tokens": tokens,
    }
    policy_v1 = {
        "schema": "account-verify/policy-v1",
        "snapshot_generation": 1,
        "time_reference": "manifest.observed_clock_ms",
        "effective_formula": "observed_clock_ms + effective_relative_seconds * 1000",
        "policies": [
            {
                "policy_key": "password/default",
                "policy_generation": 1,
                "effective_relative_seconds": 0,
                "algorithm_whitelist": ["legacy-pbkdf2-sha1", "pbkdf2-sha256"],
                "iteration_floor": 10_000,
            },
            {
                "policy_key": "password/user/u-conflict",
                "policy_generation": 2,
                "effective_relative_seconds": 0,
                "algorithm_whitelist": ["legacy-pbkdf2-sha1"],
                "iteration_floor": 50_000,
            },
            {
                "policy_key": "password/user/u-tomb",
                "policy_generation": 1,
                "effective_relative_seconds": 0,
                "algorithm_whitelist": ["legacy-pbkdf2-sha1", "pbkdf2-sha256"],
                "iteration_floor": 20_000,
            },
        ],
    }
    v2_shard_0 = {
        "schema": "account-verify/policy-v2-shard",
        "shard_id": "shard-000",
        "policies": [
            {
                "policy_key": "password/default",
                "policy_generation": 2,
                "absolute_effective_ms": NOW_MS - 30 * DAY,
                "allowed_algorithms": ["pbkdf2-sha256", "argon2id"],
                "min_iterations": 200_000,
                "tombstone": False,
            },
            {
                "policy_key": "password/user/u-direct",
                "policy_generation": 2,
                "absolute_effective_ms": NOW_MS - 25 * DAY,
                "allowed_algorithms": ["pbkdf2-sha256"],
                "min_iterations": 500_000,
                "tombstone": False,
            },
            {
                "policy_key": "password/user/u-tomb",
                "policy_generation": 2,
                "absolute_effective_ms": NOW_MS - 20 * DAY,
                "allowed_algorithms": ["legacy-pbkdf2-sha1", "pbkdf2-sha256"],
                "min_iterations": 20_000,
                "tombstone": False,
            },
        ],
    }
    v2_shard_1 = {
        "schema": "account-verify/policy-v2-shard",
        "shard_id": "shard-001",
        "policies": [
            {
                "policy_key": "password/user/u-tomb",
                "policy_generation": 3,
                "absolute_effective_ms": NOW_MS - 10 * DAY,
                "tombstone": True,
            },
            {
                "policy_key": "password/user/u-conflict",
                "policy_generation": 2,
                "absolute_effective_ms": NOW_MS,
                "allowed_algorithms": ["pbkdf2-sha256", "argon2id"],
                "min_iterations": 220_000,
                "tombstone": False,
            },
        ],
    }
    upgrade_v3 = {
        "schema": "account-verify/policy-v2-upgrade",
        "shard_id": "upgrade-v3",
        "policies": [
            {
                "policy_key": "password/default",
                "policy_generation": 3,
                "absolute_effective_ms": NOW_MS - 2 * DAY,
                "allowed_algorithms": ["pbkdf2-sha256", "argon2id"],
                "min_iterations": 300_000,
                "tombstone": False,
            },
            {
                "policy_key": "password/user/u-direct",
                "policy_generation": 3,
                "absolute_effective_ms": NOW_MS - 1 * DAY,
                "tombstone": True,
            },
        ],
    }
    invalid_records = {
        "schema": "account-verify/invalid-fixtures/v1",
        "credentials": [
            {"user_id": "", "credential_generation": 1, "salt_hex": "00", "iterations": 10, "algorithm_tier": "pbkdf2-sha256", "digest_value": "x"},
            {"user_id": "u-bad-salt", "credential_generation": 1, "salt_hex": "", "iterations": 10, "algorithm_tier": "pbkdf2-sha256", "digest_value": "x"},
            {"user_id": "u-bad-iter", "credential_generation": 1, "salt_hex": "00", "iterations": 0, "algorithm_tier": "pbkdf2-sha256", "digest_value": "x"},
            {"user_id": "u-bad-alg", "credential_generation": 1, "salt_hex": "00", "iterations": 10, "algorithm_tier": "md5", "digest_value": "x"},
            {"user_id": "u-dup", "credential_generation": 1, "salt_hex": "00", "iterations": 10, "algorithm_tier": "pbkdf2-sha256", "digest_value": "x"},
            {"user_id": "u-dup", "credential_generation": 1, "salt_hex": "11", "iterations": 11, "algorithm_tier": "pbkdf2-sha256", "digest_value": "y"},
        ],
        "tokens": [
            {"token_id": "tok-bad-time", "user_id": "u-x", "issued_at_ms": 20, "expires_at_ms": 10, "old_digest_signature": "x"},
            {"token_id": "tok-dup", "user_id": "u-x", "issued_at_ms": 1, "expires_at_ms": 2, "old_digest_signature": "x"},
            {"token_id": "tok-dup", "user_id": "u-x", "issued_at_ms": 1, "expires_at_ms": 3, "old_digest_signature": "y"},
        ],
        "policy_v1": [
            {"policy_key": "password/default", "policy_generation": 1, "effective_relative_seconds": -1, "allowed_algorithms": ["pbkdf2-sha256"], "min_iterations": 1},
        ],
        "policy_v2": [
            {"policy_key": "password/default", "policy_generation": -1, "absolute_effective_ms": 1, "allowed_algorithms": ["pbkdf2-sha256"], "min_iterations": 1},
        ],
    }

    truth = {
        "schema": "account-verify/truth-marker/v1",
        "seed": SEED,
        "observed_clock_ms": NOW_MS,
        "expected_user_status": {
            "u-alpha": "PENDING_MIGRATION",
            "u-beta": "PENDING_MIGRATION",
            "u-gamma": "UP_TO_DATE",
            "u-delta": "PENDING_MIGRATION",
            "u-epsilon": "PENDING_MIGRATION",
            "u-zeta": "PENDING_MIGRATION",
            "u-eta": "PENDING_MIGRATION",
            "u-conflict": "POLICY_CONFLICT",
            "u-tomb": "UP_TO_DATE",
            "u-direct": "UP_TO_DATE",
            "u-strong": "UP_TO_DATE",
        },
        "expected_token_codes": {
            "tok-alpha-valid": "OK",
            "tok-beta-bad-signature": "E_SIGNATURE_MISMATCH",
            "tok-gamma-old-after-migration": "E_OLD_TIER_AFTER_MIGRATION",
            "tok-gamma-legacy-window": "OK",
            "tok-delta-expired": "E_TOKEN_EXPIRED",
            "tok-epsilon-reused": "E_TOKEN_REUSED",
            "tok-zeta-outside-window": "E_OUTSIDE_POLICY_WINDOW",
            "tok-eta-not-one-time": "E_NOT_ONE_TIME",
            "tok-conflict-policy": "E_POLICY_CONFLICT",
        },
        "consume_token_id": "tok-alpha-valid",
        "expected_second_consume_code": "E_TOKEN_REUSED",
        "expected_upgrade_trigger_keys": [
            "password/default",
            "password/user/u-direct",
        ],
        "expected_upgrade_user_status": {
            "u-gamma": "PENDING_MIGRATION",
            "u-strong": "UP_TO_DATE",
            "u-direct": "UP_TO_DATE",
            "u-tomb": "PENDING_MIGRATION",
        },
        "expected_invalid_codes": [
            "E_INVALID_USER_ID",
            "E_INVALID_SALT",
            "E_INVALID_ITERATIONS",
            "E_UNKNOWN_ALGORITHM",
            "E_DUPLICATE_USER",
            "E_INVALID_TIME_RANGE",
            "E_DUPLICATE_TOKEN",
            "E_INVALID_EFFECTIVE_TIME",
            "E_INVALID_POLICY_GENERATION",
        ],
    }

    return {
        "manifest": {
            "schema": "account-verify/manifest/v1",
            "seed": SEED,
            "observed_clock_ms": NOW_MS,
            "observation_clock_source": "fixed sample declaration; never use system clock",
            "default_batch_size": 2,
        },
        "credentials": user_payload,
        "tokens": token_payload,
        "policy_v1": policy_v1,
        "v2_shard_0": v2_shard_0,
        "v2_shard_1": v2_shard_1,
        "upgrade_v3": upgrade_v3,
        "invalid_records": invalid_records,
        "truth": truth,
    }


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")


def generate_sample_tree(root: Path) -> Dict[str, str]:
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    samples = build_samples()

    _write_json(root / "manifest.json", samples["manifest"])
    _write_json(root / "credentials.json", samples["credentials"])
    _write_json(root / "tokens.json", samples["tokens"])
    _write_json(root / "policy_v1.json", samples["policy_v1"])
    _write_json(root / "policy_v2" / "shard_000.json", samples["v2_shard_0"])
    _write_json(root / "policy_v2" / "shard_001.json", samples["v2_shard_1"])
    _write_json(root / "policy_upgrade_v3.json", samples["upgrade_v3"])
    _write_json(root / "invalid_records.json", samples["invalid_records"])

    db_path = root / "policy_v2_meta.sqlite"
    if db_path.exists():
        db_path.unlink()
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "CREATE TABLE policy_shards ("
            "shard_id TEXT PRIMARY KEY, path TEXT NOT NULL, sha256 TEXT NOT NULL, record_count INTEGER NOT NULL)"
        )
        shards = [
            ("shard-000", "policy_v2/shard_000.json", root / "policy_v2" / "shard_000.json", samples["v2_shard_0"]),
            ("shard-001", "policy_v2/shard_001.json", root / "policy_v2" / "shard_001.json", samples["v2_shard_1"]),
        ]
        for shard_id, relative_path, absolute_path, payload in shards:
            conn.execute(
                "INSERT INTO policy_shards VALUES (?, ?, ?, ?)",
                (shard_id, relative_path, file_sha256(absolute_path), len(payload["policies"])),
            )
        conn.commit()
    finally:
        conn.close()

    input_files = [
        "manifest.json",
        "credentials.json",
        "tokens.json",
        "policy_v1.json",
        "policy_v2/shard_000.json",
        "policy_v2/shard_001.json",
        "policy_v2_meta.sqlite",
        "policy_upgrade_v3.json",
        "invalid_records.json",
    ]
    input_sha256 = {relative: file_sha256(root / relative) for relative in input_files}
    truth = dict(samples["truth"])
    truth["input_files"] = input_files
    truth["input_sha256"] = input_sha256
    _write_json(root / "truth.json", truth)
    return input_sha256


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Generate deterministic account verification samples")
    parser.add_argument("--output", default="samples", help="sample output directory")
    args = parser.parse_args()
    hashes = generate_sample_tree(Path(args.output))
    print(json.dumps({"generated": args.output, "files": len(hashes)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
