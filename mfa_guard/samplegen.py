"""Deterministic local sample generation."""

from __future__ import annotations

import hashlib
import json
import os
import random
import sqlite3
import tempfile
from pathlib import Path
from typing import Any

from .atomic import atomic_write_bytes, atomic_write_json, atomic_write_json_lines, read_json
from .otp import derive_code
from .util import canonical_json_bytes, sha256_canonical

OBSERVATION_CLOCK_MS = 1_767_225_600_000
WINDOW_MS = 30_000
THRESHOLD_MICRO = 5_000


def _digest(seed: int, purpose: str) -> str:
    return "sha256:" + hashlib.sha256(
        f"{purpose}|{seed}".encode("utf-8")
    ).hexdigest()


def _secret(seed: int, factor_id: str) -> str:
    return _digest(seed, f"factor-secret|{factor_id}")


def _registration(
    seed: int,
    user_id: str,
    factors: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "user_id": user_id,
        "password_digest": _digest(seed, f"password|{user_id}"),
        "factors": factors,
    }


def _factor(
    seed: int,
    factor_id: str,
    factor_type: str,
    components: list[int],
    scale: int,
) -> dict[str, Any]:
    return {
        "factor_id": factor_id,
        "factor_type": factor_type,
        "shared_secret_digest": _secret(seed, factor_id),
        "bound_device_components": components,
        "platform_scale": scale,
    }


def _attempt(
    *,
    attempt_no: int,
    user_id: str,
    factor_id: str,
    code: int,
    components: list[int],
    scale: int,
    risk: int,
    submitted_at_ms: int,
    timestamp_error_ms: float = 0.5,
) -> dict[str, Any]:
    return {
        "attempt_no": attempt_no,
        "user_id": user_id,
        "factor_id": factor_id,
        "totp_code": code,
        "device_components": components,
        "fingerprint_scale": scale,
        "risk_score_millis": risk,
        "submitted_at_ms": submitted_at_ms,
        "timestamp_error_ms": timestamp_error_ms,
    }


def _write_policy_v2_database(path: Path, records: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    os.close(fd)
    temporary = Path(temporary_name)
    try:
        connection = sqlite3.connect(str(temporary))
        try:
            connection.execute(
                """
                CREATE TABLE policy_v2_meta (
                    policy_key TEXT PRIMARY KEY,
                    policy_generation INTEGER NOT NULL,
                    effective_at_ms INTEGER NOT NULL,
                    canonical_scale INTEGER NOT NULL,
                    factor_type_allowlist TEXT NOT NULL,
                    ticket_generation_max INTEGER NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE policy_v2_records (
                    policy_key TEXT NOT NULL,
                    factor_id TEXT NOT NULL,
                    policy_generation INTEGER NOT NULL,
                    effective_at_ms INTEGER NOT NULL,
                    verification_window_ms INTEGER NOT NULL,
                    ticket_generation INTEGER NOT NULL,
                    ticket_generation_max INTEGER NOT NULL,
                    tombstone INTEGER NOT NULL,
                    factor_type TEXT,
                    PRIMARY KEY(
                        policy_key, factor_id, policy_generation, effective_at_ms
                    )
                )
                """
            )
            connection.execute(
                """
                INSERT INTO policy_v2_meta(
                    policy_key, policy_generation, effective_at_ms,
                    canonical_scale, factor_type_allowlist, ticket_generation_max
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    "mfa-policy",
                    3,
                    OBSERVATION_CLOCK_MS + 10_000,
                    6,
                    json.dumps(["PUSH", "TOTP", "WEBAUTHN"]),
                    12,
                ),
            )
            for record in records:
                connection.execute(
                    """
                    INSERT INTO policy_v2_records(
                        policy_key, factor_id, policy_generation, effective_at_ms,
                        verification_window_ms, ticket_generation,
                        ticket_generation_max, tombstone, factor_type
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        record["policy_key"],
                        record["factor_id"],
                        record["policy_generation"],
                        record["effective_at_ms"],
                        record["verification_window_ms"],
                        record["ticket_generation"],
                        record["ticket_generation_max"],
                        int(record["tombstone"]),
                        record.get("factor_type"),
                    ),
                )
            connection.commit()
        finally:
            connection.close()
        os.replace(temporary, path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def generate_samples(
    root: str | Path,
    *,
    seed: int = 20_261_125,
) -> dict[str, Any]:
    root_path = Path(root)
    samples = root_path / "samples"
    work = root_path / "work"
    reports = root_path / "reports"
    shard_dir = samples / "policy_v2"
    for directory in (samples, work, reports, shard_dir):
        directory.mkdir(parents=True, exist_ok=True)

    rng = random.Random(seed)
    base = OBSERVATION_CLOCK_MS + 61_123

    factors = {
        "factor-ok": _factor(
            seed, "factor-ok", "TOTP", [123456, 654321], 4
        ),
        "factor-otp": _factor(
            seed, "factor-otp", "TOTP", [123456, 654321], 4
        ),
        "factor-fpamb": _factor(
            seed, "factor-fpamb", "TOTP", [123456, 654321], 4
        ),
        "factor-mismatch": _factor(
            seed, "factor-mismatch", "TOTP", [123456, 654321], 4
        ),
        "factor-otpamb": _factor(
            seed, "factor-otpamb", "TOTP", [123456, 654321], 4
        ),
        "factor-replay": _factor(
            seed, "factor-replay", "TOTP", [123456789, 987654321], 6
        ),
        "factor-push": _factor(
            seed, "factor-push", "PUSH", [123456, 654321], 6
        ),
        "factor-legacy": _factor(
            seed, "factor-legacy", "TOTP", [1234, 5678], 3
        ),
        "factor-retired": _factor(
            seed, "factor-retired", "TOTP", [1111, 2222], 3
        ),
    }
    registrations = [
        _registration(
            seed,
            "user-alpha",
            [
                factors["factor-ok"],
                factors["factor-otp"],
                factors["factor-fpamb"],
                factors["factor-mismatch"],
                factors["factor-otpamb"],
                factors["factor-replay"],
            ],
        ),
        _registration(
            seed,
            "user-beta",
            [factors["factor-push"], factors["factor-legacy"]],
        ),
        _registration(
            seed,
            "user-retired",
            [factors["factor-retired"]],
        ),
    ]
    atomic_write_json(
        samples / "registrations.json",
        {"schema_version": 1, "registrations": registrations},
    )

    def code(factor_id: str, at_ms: int) -> int:
        return derive_code(_secret(seed, factor_id), at_ms // WINDOW_MS)

    attempt_specs: list[tuple[int, int, dict[str, Any], str, str]] = [
        (
            1001,
            base,
            _attempt(
                attempt_no=1001,
                user_id="user-alpha",
                factor_id="factor-ok",
                code=code("factor-ok", base),
                components=[12346, 65432],
                scale=3,
                risk=rng.randint(1, 100),
                submitted_at_ms=base,
            ),
            "APPROVED",
            "",
        ),
        (
            1002,
            base + 1_000,
            _attempt(
                attempt_no=1002,
                user_id="user-alpha",
                factor_id="factor-otp",
                code=(code("factor-otp", base + 1_000) + 1) % 1_000_000,
                components=[123456, 654321],
                scale=4,
                risk=rng.randint(1, 100),
                submitted_at_ms=base + 1_000,
            ),
            "REJECTED",
            "E_OTP_MISMATCH",
        ),
        (
            1003,
            base + 2_000,
            _attempt(
                attempt_no=1003,
                user_id="user-alpha",
                factor_id="factor-fpamb",
                code=code("factor-fpamb", base + 2_000),
                components=[1235, 6543],
                scale=2,
                risk=rng.randint(1, 100),
                submitted_at_ms=base + 2_000,
            ),
            "INDETERMINATE",
            "E_FP_AMBIGUOUS",
        ),
        (
            1004,
            OBSERVATION_CLOCK_MS + 90_000,
            _attempt(
                attempt_no=1004,
                user_id="user-alpha",
                factor_id="factor-otpamb",
                code=code("factor-otpamb", OBSERVATION_CLOCK_MS + 90_000),
                components=[12346, 65432],
                scale=3,
                risk=rng.randint(1, 100),
                submitted_at_ms=OBSERVATION_CLOCK_MS + 90_000,
                timestamp_error_ms=1.0,
            ),
            "INDETERMINATE",
            "E_OTP_AMBIGUOUS",
        ),
        (
            1005,
            base + 3_000,
            _attempt(
                attempt_no=1005,
                user_id="user-alpha",
                factor_id="factor-mismatch",
                code=code("factor-mismatch", base + 3_000),
                components=[1300, 6543],
                scale=2,
                risk=rng.randint(1, 100),
                submitted_at_ms=base + 3_000,
            ),
            "REJECTED",
            "E_DEVICE_MISMATCH",
        ),
        (
            1006,
            base + 4_000,
            _attempt(
                attempt_no=1006,
                user_id="user-alpha",
                factor_id="factor-replay",
                code=code("factor-replay", base + 4_000),
                components=[123456789, 987654321],
                scale=6,
                risk=rng.randint(1, 100),
                submitted_at_ms=base + 4_000,
            ),
            "REJECTED",
            "E_TICKET_REPLAY",
        ),
        (
            1007,
            base + 5_000,
            _attempt(
                attempt_no=1007,
                user_id="user-beta",
                factor_id="factor-push",
                code=code("factor-push", base + 5_000),
                components=[123456, 654321],
                scale=6,
                risk=rng.randint(1, 100),
                submitted_at_ms=base + 5_000,
            ),
            "APPROVED",
            "",
        ),
        (
            1008,
            base + 6_000,
            _attempt(
                attempt_no=1008,
                user_id="user-beta",
                factor_id="factor-legacy",
                code=code("factor-legacy", base + 6_000),
                components=[1234, 5678],
                scale=3,
                risk=rng.randint(1, 100),
                submitted_at_ms=base + 6_000,
            ),
            "APPROVED",
            "",
        ),
        (
            1009,
            base + 7_000,
            _attempt(
                attempt_no=1009,
                user_id="user-alpha",
                factor_id="factor-unknown",
                code=123456,
                components=[1, 2],
                scale=2,
                risk=rng.randint(1, 100),
                submitted_at_ms=base + 7_000,
            ),
            "PREVALIDATION_REJECTED",
            "E_UNKNOWN_FACTOR",
        ),
        (
            1010,
            base + 8_000,
            _attempt(
                attempt_no=1010,
                user_id="user-retired",
                factor_id="factor-retired",
                code=code("factor-retired", base + 8_000),
                components=[1111, 2222],
                scale=3,
                risk=rng.randint(1, 100),
                submitted_at_ms=base + 8_000,
            ),
            "PREVALIDATION_REJECTED",
            "E_FACTOR_NOT_TRUSTED",
        ),
    ]
    valid_lines = [
        json.dumps(record, ensure_ascii=False, sort_keys=True)
        for _, _, record, _, _ in attempt_specs
    ]
    invalid_lines = [
        json.dumps(
            _attempt(
                attempt_no=2001,
                user_id="user-alpha",
                factor_id="factor-ok",
                code=1_000_000,
                components=[12346, 65432],
                scale=3,
                risk=1,
                submitted_at_ms=base + 9_000,
            ),
            ensure_ascii=False,
            sort_keys=True,
        ),
        json.dumps(
            _attempt(
                attempt_no=2002,
                user_id="user-alpha",
                factor_id="factor-ok",
                code=123456,
                components=[12346, 65432],
                scale=7,
                risk=1,
                submitted_at_ms=base + 10_000,
            ),
            ensure_ascii=False,
            sort_keys=True,
        ),
        '{"attempt_no": 2003, "user_id": "user-alpha",',
    ]
    lines = valid_lines + invalid_lines
    atomic_write_bytes(
        samples / "attempts.jsonl",
        ("\n".join(lines) + "\n").encode("utf-8"),
    )

    v1_document = {
        "schema_version": 1,
        "policy_key": "mfa-policy",
        "observed_at_ms": OBSERVATION_CLOCK_MS,
        "verification_window_seconds": WINDOW_MS / 1000,
        "factor_allowlist": [
            "factor-ok",
            "factor-otp",
            "factor-fpamb",
            "factor-mismatch",
            "factor-otpamb",
            "factor-replay",
            "factor-push",
            "factor-legacy",
            "factor-retired",
        ],
        "stateless_ticket": True,
    }
    atomic_write_json(samples / "policy_v1.json", v1_document)

    v2_effective = OBSERVATION_CLOCK_MS + 10_000
    active_factor_ids = [
        "factor-ok",
        "factor-otp",
        "factor-fpamb",
        "factor-mismatch",
        "factor-otpamb",
        "factor-replay",
        "factor-push",
    ]
    v2_records: list[dict[str, Any]] = []
    for factor_id in active_factor_ids:
        v2_records.append(
            {
                "policy_key": "mfa-policy",
                "factor_id": factor_id,
                "policy_generation": 3,
                "effective_at_ms": v2_effective,
                "verification_window_ms": WINDOW_MS,
                "ticket_generation": 10,
                "ticket_generation_max": 12,
                "tombstone": False,
                "factor_type": factors[factor_id]["factor_type"],
            }
        )
    shard_records = [v2_records[::2], v2_records[1::2]]
    shard_names = ["policy_v2/shard_00.json", "policy_v2/shard_01.json"]
    for name, records in zip(shard_names, shard_records):
        atomic_write_json(
            samples / name,
            {"schema_version": 1, "records": records},
        )
    tombstone = {
        "policy_key": "mfa-policy",
        "factor_id": "factor-retired",
        "policy_generation": 3,
        "effective_at_ms": v2_effective,
        "verification_window_ms": WINDOW_MS,
        "ticket_generation": 10,
        "ticket_generation_max": 12,
        "tombstone": True,
        "factor_type": "TOTP",
    }
    _write_policy_v2_database(samples / "policy_v2.sqlite", [tombstone])

    expectations = {
        str(attempt_no): {"status": status, "reason": reason}
        for attempt_no, _, _, status, reason in attempt_specs
    }
    expectations["parse_rejections"] = [
        {"record_index": 11, "code": "E_TOTP_FORMAT"},
        {"record_index": 12, "code": "E_SCALE_RANGE"},
        {"record_index": 13, "code": "E_INPUT_JSON"},
    ]
    atomic_write_json(
        samples / "expected_scenarios.json",
        {
            "seed": seed,
            "scenario_expectations": expectations,
            "notes": {
                "first_approved_attempt": 1001,
                "replay_attempt": 1006,
                "replay_first_attempt": 1001,
            },
        },
    )

    as_of_ms = max(record["submitted_at_ms"] for _, _, record, _, _ in attempt_specs)
    manifest = {
        "format": "mfa-sample-v1",
        "seed": seed,
        "observation_clock_ms": OBSERVATION_CLOCK_MS,
        "policy_key": "mfa-policy",
        "policy_as_of_ms": as_of_ms,
        "canonical_scale": 6,
        "fingerprint_threshold_micro": THRESHOLD_MICRO,
        "default_timestamp_error_us": 500,
        "default_concurrency": 4,
        "default_timeout_ms": 5_000,
        "default_queue_limit": 64,
        "auto_recover": True,
        "policy_v1_file": "policy_v1.json",
        "policy_v2_db": "policy_v2.sqlite",
        "policy_v2_shards": shard_names,
        "sample_sha256": sha256_canonical(
            {
                "registrations": registrations,
                "attempts": [record for _, _, record, _, _ in attempt_specs],
                "v1": v1_document,
                "v2": v2_records,
                "tombstone": tombstone,
            }
        ),
    }
    atomic_write_json(samples / "manifest.json", manifest)
    return {
        "root": str(root_path),
        "samples_dir": str(samples),
        "manifest": manifest,
        "expectations": expectations,
        "paths": {
            "registrations": str(samples / "registrations.json"),
            "attempts": str(samples / "attempts.jsonl"),
            "policy_v1": str(samples / "policy_v1.json"),
            "policy_v2_db": str(samples / "policy_v2.sqlite"),
            "policy_v2_shards": [str(samples / name) for name in shard_names],
        },
    }

