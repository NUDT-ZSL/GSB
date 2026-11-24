"""Fixed-seed deterministic sample and truth generator."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import random
import sqlite3
from pathlib import Path
from typing import Any

from .factors import build_proof_digest
from .fingerprint import normalize_fingerprint
from .jsonio import atomic_write_json, sha256_text


def digest_for(identifier: str) -> str:
    return "sha256:" + hashlib.sha256(identifier.encode("utf-8")).hexdigest()


def write_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n" for record in records),
        encoding="utf-8",
    )


def make_factor(factor_id: str, user_id: str, factor_type: str, device_id: str) -> dict[str, Any]:
    return {
        "record_seq": 0,
        "factor_id": factor_id,
        "user_id": user_id,
        "device_id": device_id,
        "factor_type": factor_type,
        "secret_placeholder_digest": digest_for("secret:" + factor_id),
        "status": "BOUND",
        "bound_at_ms": 1767225600000,
    }


def proof(factor: dict[str, Any], challenge: str, **extra: Any) -> dict[str, Any]:
    payload = {
        "factor_id": factor["factor_id"],
        "factor_type": factor["factor_type"],
        "challenge": challenge,
        "proof_digest": build_proof_digest(factor, challenge, **extra),
    }
    payload.update(extra)
    return payload


def totp_proof(factor: dict[str, Any], challenge: str = "ch-totp") -> dict[str, Any]:
    return proof(factor, challenge, code="123456", time_step=1)


def webauthn_proof(factor: dict[str, Any], challenge: str = "ch-webauthn") -> dict[str, Any]:
    return proof(
        factor,
        challenge,
        client_data_digest=digest_for("client:" + factor["factor_id"]),
        signature_placeholder="sig-placeholder",
    )


def push_proof(factor: dict[str, Any], challenge: str = "ch-push") -> dict[str, Any]:
    return proof(factor, challenge, push_response="APPROVED", responded_at_ms=1767226200000)


def modified_bound_vector(fingerprint: dict[str, Any], delta: int, coordinate: tuple[Any, str] = (1, "cpu_score")) -> list[dict[str, Any]]:
    points = copy.deepcopy(fingerprint["points"])
    point_seq, name = coordinate
    for point in points:
        if point["seq"] != point_seq:
            continue
        for component in point["components"]:
            if component["name"] == name:
                component["canonical_value"] += delta
                component["lower"] = component["canonical_value"]
                component["upper"] = component["canonical_value"]
                component["error_bound"] = 0
    return points


def key_for_points(platform: str, decimals: int, points: list[dict[str, Any]]) -> str:
    payload = {
        "algorithm": "fixed-point-v1",
        "canonical_decimals": decimals,
        "platform": platform,
        "points": points,
    }
    return f"fpv1:sha256:{sha256_text(payload)}"


def generate(params_path: Path, output_dir: Path | None = None) -> Path:
    params = json.loads(params_path.read_text(encoding="utf-8"))
    seed = int(params["seed"])
    rng = random.Random(seed)
    t0 = int(params["observation_clock_ms"])
    minute = 60_000
    hour = 3_600_000
    k = int(params["canonical_fingerprint_decimals"])
    threshold = int(params["fingerprint_match_threshold_canonical_units"])
    output = output_dir or Path(params.get("output_dir", "samples/generated"))
    output.mkdir(parents=True, exist_ok=True)
    (output / "policy_v1").mkdir(exist_ok=True)
    (output / "policy_v2" / "shards").mkdir(parents=True, exist_ok=True)
    (output / "truth").mkdir(exist_ok=True)

    snapshot_version = params["snapshot_version"]
    manifest = {
        "sample_id": params["sample_id"],
        "seed": seed,
        "snapshot_version": snapshot_version,
        "layout_version": 2,
        "observation_clock_ms": t0,
        "canonical_fingerprint_decimals": k,
        "fingerprint_match_threshold_canonical_units": threshold,
        "platform_scales": params["platform_scales"],
        "files": {
            "users": "user_credentials.jsonl",
            "devices": "device_fingerprints.jsonl",
            "factors": "factor_bindings.jsonl",
            "sessions": "sessions.jsonl",
            "edges": "dependency_edges.jsonl",
            "attempts": "login_attempts.jsonl",
            "revocations": "revocation_events.jsonl",
            "policy_v1": "policy_v1/policy_snapshot.json",
            "policy_v2_dir": "policy_v2",
            "policy_v2_meta": "policy_v2/policy_meta.sqlite",
        },
    }
    atomic_write_json(output / "bundle_manifest.json", manifest)

    def user(user_id: str, tier: int, iterations: int) -> dict[str, Any]:
        return {
            "user_id": user_id,
            "salt": rng.randbytes(12).hex(),
            "password_digest": digest_for("password:" + user_id),
            "digest_tier": tier,
            "iterations": iterations,
        }

    users = [
        user("u-alice", 3, 200_000),
        user("u-bob", 3, 200_000),
        user("u-carol", 1, 50_000),
        user("u-dave", 3, 200_000),
        user("u-erin", 2, 150_000),
        user("u-frank", 3, 200_000),
        user("u-grace", 3, 200_000),
        user("u-henry", 3, 200_000),
        user("u-ivy", 3, 200_000),
        user("u-jack", 3, 200_000),
        user("u-nate", 2, 120_000),
        user("u-cycle", 3, 200_000),
        {"user_id": "u-alice", "salt": "duplicate", "password_digest": digest_for("dup"), "digest_tier": 3, "iterations": 200_000},
        {"user_id": "u-bad-tier", "salt": "s", "password_digest": digest_for("bad-tier"), "digest_tier": 9, "iterations": 100_000},
        {"user_id": "u-bad-iter", "salt": "s", "password_digest": digest_for("bad-iter"), "digest_tier": 2, "iterations": -1},
    ]
    write_jsonl(output / "user_credentials.jsonl", users)

    def device(device_id: str, user_id: str, platform: str, raw_value: int) -> dict[str, Any]:
        decimals = params["platform_scales"][platform]
        return {
            "device_id": device_id,
            "user_id": user_id,
            "platform": platform,
            "series": [
                {"seq": 1, "components": {"cpu_score": raw_value, "stable_trait": 4200}},
                {"seq": 2, "components": {"cpu_score": raw_value + 4, "stable_trait": 4204}},
            ],
        }

    devices = [
        device("dev-alice", "u-alice", "WINDOWS", 123456),
        device("dev-bob", "u-bob", "LINUX", 123456789),
        device("dev-carol", "u-carol", "MACOS", 987654),
        device("dev-dave", "u-dave", "ANDROID", 555123),
        device("dev-erin", "u-erin", "IOS", 77712),
        device("dev-frank", "u-frank", "WINDOWS", 222333),
        device("dev-grace", "u-grace", "WINDOWS", 333444),
        device("dev-henry", "u-henry", "ANDROID", 444555),
        device("dev-ivy", "u-ivy", "IOS", 66677),
        device("dev-jack", "u-jack", "WINDOWS", 888999),
        device("dev-nate", "u-nate", "WINDOWS", 111222),
        device("dev-cycle", "u-cycle", "LINUX", 999888777),
        {"device_id": "dev-bad-platform", "user_id": "u-x", "platform": "CHROMEBOOK", "series": [{"seq": 1, "components": {"a": 1}}]},
        {"device_id": "dev-bad-component", "user_id": "u-x", "platform": "WINDOWS", "series": [{"seq": 1, "components": {"a": -2}}]},
        device("dev-alice", "u-alice", "WINDOWS", 123456),
    ]
    write_jsonl(output / "device_fingerprints.jsonl", devices)

    factor_records: list[dict[str, Any]] = []
    factor_specs = [
        ("fac-alice-totp", "u-alice", "TOTP", "dev-alice"),
        ("fac-alice-webauthn", "u-alice", "WEBAUTHN", "dev-alice"),
        ("fac-bob-totp", "u-bob", "TOTP", "dev-bob"),
        ("fac-carol-totp", "u-carol", "TOTP", "dev-carol"),
        ("fac-carol-webauthn", "u-carol", "WEBAUTHN", "dev-carol"),
        ("fac-dave-totp", "u-dave", "TOTP", "dev-dave"),
        ("fac-erin-totp", "u-erin", "TOTP", "dev-erin"),
        ("fac-frank-totp", "u-frank", "TOTP", "dev-frank"),
        ("fac-frank-push", "u-frank", "PUSH", "dev-frank"),
        ("fac-grace-totp", "u-grace", "TOTP", "dev-grace"),
        ("fac-henry-push", "u-henry", "PUSH", "dev-henry"),
        ("fac-ivy-totp", "u-ivy", "TOTP", "dev-ivy"),
        ("fac-jack-totp", "u-jack", "TOTP", "dev-jack"),
        ("fac-nate-totp", "u-nate", "TOTP", "dev-nate"),
        ("fac-nate-webauthn", "u-nate", "WEBAUTHN", "dev-nate"),
        ("fac-cycle-a", "u-cycle", "TOTP", "dev-cycle"),
        ("fac-cycle-b", "u-cycle", "WEBAUTHN", "dev-cycle"),
        ("fac-cycle-c", "u-cycle", "PUSH", "dev-cycle"),
    ]
    for spec in factor_specs:
        factor_records.append(make_factor(*spec))
    factor_records.append(make_factor("fac-alice-totp", "u-alice", "TOTP", "dev-alice"))
    factor_records.append(make_factor("fac-bad-type", "u-x", "SMS", "dev-x"))
    write_jsonl(output / "factor_bindings.jsonl", factor_records)

    factor_map = {item["factor_id"]: item for item in factor_records if "factor_id" in item}

    def fp_for(device_id: str) -> dict[str, Any]:
        raw = next(item for item in devices if item.get("device_id") == device_id and item.get("platform") in params["platform_scales"])
        return normalize_fingerprint(raw["platform"], params["platform_scales"][raw["platform"]], raw["series"], k)

    alice_fp = fp_for("dev-alice")
    bob_fp = fp_for("dev-bob")
    carol_fp = fp_for("dev-carol")
    dave_fp = fp_for("dev-dave")
    erin_fp = fp_for("dev-erin")
    frank_fp = fp_for("dev-frank")
    grace_fp = fp_for("dev-grace")
    henry_fp = fp_for("dev-henry")
    ivy_fp = fp_for("dev-ivy")
    jack_fp = fp_for("dev-jack")
    nate_fp = fp_for("dev-nate")

    bob_vector = modified_bound_vector(bob_fp, 11)
    bob_key = key_for_points("LINUX", k, bob_vector)
    carol_vector = modified_bound_vector(carol_fp, 1000)
    carol_key = key_for_points("MACOS", k, carol_vector)

    sessions = [
        {"session_id": "sess-alice-1", "user_id": "u-alice", "device_id": "dev-alice", "factor_ids": ["fac-alice-totp", "fac-alice-webauthn"], "status": "ACTIVE", "created_at_ms": t0 + 10 * minute},
        {"session_id": "sess-henry-1", "user_id": "u-henry", "device_id": "dev-henry", "factor_ids": ["fac-henry-push"], "status": "ACTIVE", "created_at_ms": t0 + 10 * minute},
        {"session_id": "sess-carol-old", "user_id": "u-carol", "device_id": "dev-carol", "factor_ids": ["fac-carol-totp", "fac-carol-webauthn"], "status": "ACTIVE", "created_at_ms": t0 - hour},
        {"session_id": "sess-nate-1", "user_id": "u-nate", "device_id": "dev-nate", "factor_ids": ["fac-nate-totp", "fac-nate-webauthn"], "status": "ACTIVE", "created_at_ms": t0 + 4 * hour},
    ]
    write_jsonl(output / "sessions.jsonl", sessions)

    edges = [
        {"edge_id": "factor-cycle-1", "from_node": "factor:fac-cycle-a", "to_node": "factor:fac-cycle-b", "edge_type": "FACTOR_DEPENDENCY"},
        {"edge_id": "factor-cycle-2", "from_node": "factor:fac-cycle-b", "to_node": "factor:fac-cycle-c", "edge_type": "FACTOR_DEPENDENCY"},
        {"edge_id": "factor-cycle-3", "from_node": "factor:fac-cycle-c", "to_node": "factor:fac-cycle-a", "edge_type": "FACTOR_DEPENDENCY"},
        {"edge_id": "binding-cycle-1", "from_node": "binding:bind-cycle-a", "to_node": "binding:bind-cycle-b", "edge_type": "DEVICE_BINDING_CHAIN"},
        {"edge_id": "binding-cycle-2", "from_node": "binding:bind-cycle-b", "to_node": "binding:bind-cycle-c", "edge_type": "DEVICE_BINDING_CHAIN"},
        {"edge_id": "binding-cycle-3", "from_node": "binding:bind-cycle-c", "to_node": "binding:bind-cycle-a", "edge_type": "DEVICE_BINDING_CHAIN"},
    ]
    write_jsonl(output / "dependency_edges.jsonl", edges)

    def attempt(seq: int, user: str, device: str, policy: str, submitted_factors: list[dict[str, Any]], session: str | None = None, watermark: int = 1, when: int | None = None) -> dict[str, Any]:
        return {
            "attempt_seq": seq,
            "user_id": user,
            "device_id": device,
            "policy_key": policy,
            "observed_at_ms": when if when is not None else t0 + 10 * minute,
            "logical_clock": watermark * 1000 + seq,
            "batch_watermark": watermark,
            "session_id": session,
            "factors": submitted_factors,
        }

    attempts = [
        attempt(1, "u-alice", "dev-alice", "mfa-default", [totp_proof(factor_map["fac-alice-totp"]), webauthn_proof(factor_map["fac-alice-webauthn"])], "sess-alice-1"),
        attempt(2, "u-bob", "dev-bob", "mfa-default", [totp_proof(factor_map["fac-bob-totp"])]),
        attempt(3, "u-carol", "dev-carol", "mfa-default", [totp_proof(factor_map["fac-carol-totp"]), webauthn_proof(factor_map["fac-carol-webauthn"])], "sess-carol-old"),
        attempt(4, "u-dave", "dev-dave", "mfa-webauthn", [{**totp_proof(factor_map["fac-dave-totp"]), "factor_type": "TOTP"}]),
        attempt(5, "u-erin", "dev-erin", "mfa-high", [totp_proof(factor_map["fac-erin-totp"])]),
        attempt(6, "u-frank", "dev-frank", "mfa-legacy", [totp_proof(factor_map["fac-frank-totp"]), push_proof(factor_map["fac-frank-push"])]),
        attempt(7, "u-grace", "dev-grace", "mfa-expired", [totp_proof(factor_map["fac-grace-totp"])]),
        attempt(8, "u-henry", "dev-henry", "mfa-push", [push_proof(factor_map["fac-henry-push"])], "sess-henry-1"),
        attempt(9, "u-ivy", "dev-ivy", "mfa-default", [{**totp_proof(factor_map["fac-ivy-totp"]), "proof_digest": "proof-sha256:corrupt"}]),
        attempt(10, "u-jack", "dev-jack", "mfa-tombstoned", [totp_proof(factor_map["fac-jack-totp"])]),
        attempt(11, "u-alice", "dev-alice", "mfa-default", [totp_proof(factor_map["fac-alice-totp"]), webauthn_proof(factor_map["fac-alice-webauthn"])], "sess-alice-1", watermark=2, when=t0 + 4 * hour),
        attempt(12, "u-carol", "dev-carol", "mfa-default", [totp_proof(factor_map["fac-carol-totp"]), webauthn_proof(factor_map["fac-carol-webauthn"])], "sess-carol-old", watermark=2, when=t0 + 4 * hour),
        attempt(1, "u-nate", "dev-nate", "mfa-default", [totp_proof(factor_map["fac-nate-totp"]), webauthn_proof(factor_map["fac-nate-webauthn"])], "sess-nate-1", watermark=2, when=t0 + 4 * hour),
    ]
    attempts.append({
        "attempt_seq": 1,
        "user_id": "u-alice",
        "device_id": "dev-alice",
        "policy_key": "mfa-default",
        "observed_at_ms": t0 + 11 * minute,
        "logical_clock": 999,
        "batch_watermark": 1,
        "factors": [],
    })
    write_jsonl(output / "login_attempts.jsonl", attempts)

    v1_policies = {
        "snapshot_kind": "v1",
        "snapshot_version": snapshot_version + "-v1",
        "policies": [
            {"policy_key": "mfa-default", "generation": 1, "expires_in_seconds": 2 * hour // 1000, "required_factors": ["TOTP", "WEBAUTHN"], "binding_requirement": "CANONICAL_FINGERPRINT", "min_digest_tier": 2, "min_iterations": 120_000},
            {"policy_key": "mfa-legacy", "generation": 1, "expires_in_seconds": hour // 1000, "required_factors": ["TOTP"], "binding_requirement": "NONE", "min_digest_tier": 2, "min_iterations": 120_000},
            {"policy_key": "mfa-expired", "generation": 1, "expires_in_seconds": 0, "required_factors": ["TOTP"], "binding_requirement": "CANONICAL_FINGERPRINT", "min_digest_tier": 1, "min_iterations": 1},
            {"policy_key": "mfa-push", "generation": 1, "expires_in_seconds": 8 * hour // 1000, "required_factors": ["PUSH"], "binding_requirement": "CANONICAL_FINGERPRINT", "min_digest_tier": 1, "min_iterations": 1},
            {"policy_key": "mfa-invalid-v1", "expires_in_seconds": -5, "required_factors": ["TOTP"]},
        ],
    }
    atomic_write_json(output / "policy_v1" / "policy_snapshot.json", v1_policies)

    def policy_record(key: str, generation: int, effective: int, expires: int | None, factors: list[str], binding: str = "CANONICAL_FINGERPRINT", tier: int = 2, iterations: int = 120_000, watermark: int = 1) -> dict[str, Any]:
        return {
            "record_kind": "policy",
            "policy_key": key,
            "generation": generation,
            "effective_at_ms": effective,
            "expires_at_ms": expires,
            "required_factors": factors,
            "binding_requirement": binding,
            "min_digest_tier": tier,
            "min_iterations": iterations,
            "tombstoned": False,
            "batch_watermark": watermark,
        }

    shard0 = {
        "shard": "policy-records-000",
        "records": [
            policy_record("mfa-default", 2, t0, t0 + 2 * hour, ["TOTP", "WEBAUTHN"]),
            policy_record("mfa-legacy", 1, t0, t0 + 2 * hour, ["TOTP", "PUSH"]),
            policy_record("mfa-expired", 1, t0, t0, ["TOTP"], tier=1, iterations=1),
            policy_record("mfa-high", 2, t0, t0 + 8 * hour, ["TOTP"], tier=3, iterations=300_000),
            policy_record("mfa-webauthn", 1, t0, t0 + 8 * hour, ["WEBAUTHN"], tier=1, iterations=1),
            policy_record("mfa-tombstoned", 1, t0, t0 + 8 * hour, ["TOTP"]),
            {"record_kind": "policy", "policy_key": "mfa-bad-v2", "generation": -1, "effective_at_ms": t0, "expires_at_ms": t0 + hour},
        ],
    }
    atomic_write_json(output / "policy_v2" / "shards" / "policy-records-000.json", shard0)
    shard1 = {
        "shard": "policy-records-001",
        "records": [
            policy_record("mfa-default", 3, t0 + 90 * minute, t0 + 8 * hour, ["TOTP", "WEBAUTHN"], tier=3, iterations=200_000, watermark=2),
        ],
    }
    atomic_write_json(output / "policy_v2" / "shards" / "policy-records-001.json", shard1)

    def binding_record(binding_id: str, device_id: str, user_id: str, fp: dict[str, Any] | None = None, key: str | None = None, vector: list[dict[str, Any]] | None = None, generation: int = 2, tombstoned: bool = False, watermark: int = 1) -> dict[str, Any]:
        fp_key = key or fp["canonical_fingerprint_key"]
        fp_vector = vector if vector is not None else (fp["points"] if fp else [])
        return {
            "record_kind": "binding",
            "binding_id": binding_id,
            "policy_key": "mfa-default",
            "user_id": user_id,
            "device_id": device_id,
            "canonical_fingerprint_key": fp_key,
            "canonical_vector": fp_vector,
            "generation": generation,
            "effective_at_ms": t0,
            "tombstoned": tombstoned,
            "batch_watermark": watermark,
        }

    bindings0 = {
        "shard": "binding-records-000",
        "records": [
            binding_record("bind-alice", "dev-alice", "u-alice", alice_fp),
            binding_record("bind-bob", "dev-bob", "u-bob", key=bob_key, vector=bob_vector),
            binding_record("bind-carol", "dev-carol", "u-carol", key=carol_key, vector=carol_vector),
            binding_record("bind-dave", "dev-dave", "u-dave", dave_fp),
            binding_record("bind-erin", "dev-erin", "u-erin", erin_fp),
            binding_record("bind-frank", "dev-frank", "u-frank", frank_fp),
            binding_record("bind-grace", "dev-grace", "u-grace", grace_fp),
            binding_record("bind-henry", "dev-henry", "u-henry", henry_fp),
            binding_record("bind-ivy", "dev-ivy", "u-ivy", ivy_fp),
            binding_record("bind-jack", "dev-jack", "u-jack", jack_fp),
            binding_record("bind-nate", "dev-nate", "u-nate", nate_fp),
        ],
    }
    atomic_write_json(output / "policy_v2" / "shards" / "binding-records-000.json", bindings0)
    bindings1 = {
        "shard": "binding-records-001",
        "records": [
            binding_record("bind-carol", "dev-carol", "u-carol", key=carol_key, vector=carol_vector, generation=3, tombstoned=True, watermark=2),
        ],
    }
    bindings1["records"][0]["effective_at_ms"] = t0 + 3 * hour
    atomic_write_json(output / "policy_v2" / "shards" / "binding-records-001.json", bindings1)

    revocations = [
        {"event_id": "evt-unbind-alice-webauthn", "event_type": "FACTOR_UNBIND", "factor_id": "fac-alice-webauthn", "effective_at_ms": t0 + 3 * hour, "batch_watermark": 2},
        {"event_id": "evt-tombstone-carol-device", "event_type": "DEVICE_BINDING_TOMBSTONE", "device_id": "dev-carol", "binding_id": "bind-carol", "effective_at_ms": t0 + 3 * hour, "batch_watermark": 2},
        {"event_id": "evt-upgrade-default-gen3", "event_type": "POLICY_UPGRADE", "policy_key": "mfa-default", "new_min_digest_tier": 3, "new_min_iterations": 200_000, "effective_at_ms": t0 + 90 * minute, "batch_watermark": 2},
    ]
    write_jsonl(output / "revocation_events.jsonl", revocations)

    db_path = output / "policy_v2" / "policy_meta.sqlite"
    if db_path.exists():
        db_path.unlink()
    connection = sqlite3.connect(db_path)
    connection.executescript(
        """
        CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE shards(shard_name TEXT PRIMARY KEY, kind TEXT, record_count INTEGER, watermark INTEGER);
        CREATE TABLE policy_meta(
            policy_key TEXT,
            generation INTEGER,
            source_shard TEXT,
            record_seq INTEGER,
            tombstoned INTEGER,
            batch_watermark INTEGER,
            effective_at_ms INTEGER,
            expires_at_ms INTEGER
        );
        """
    )
    connection.executemany("INSERT INTO meta(key,value) VALUES(?,?)", [
        ("snapshot_version", snapshot_version + "-v2"),
        ("layout_version", "2"),
        ("observation_clock_ms", str(t0)),
    ])
    connection.executemany("INSERT INTO shards VALUES(?,?,?,?)", [
        ("policy-records-000", "policy", 7, 1),
        ("policy-records-001", "policy", 1, 2),
        ("binding-records-000", "binding", 11, 1),
        ("binding-records-001", "binding", 1, 2),
    ])
    connection.executemany("INSERT INTO policy_meta VALUES(?,?,?,?,?,?,?,?)", [
        ("mfa-default", 2, "policy-records-000", 1, 0, 1, t0, t0 + 2 * hour),
        ("mfa-default", 3, "policy-records-001", 1, 0, 2, t0 + 90 * minute, t0 + 8 * hour),
        ("mfa-tombstoned", 2, "policy_meta", 2, 1, 1, t0, t0 + 8 * hour),
        ("mfa-invalid-sqlite", -1, "policy_meta", 99, 0, 1, t0, t0 + hour),
    ])
    connection.commit()
    connection.close()

    expected_decisions = [
        {"user_id": "u-alice", "attempt_seq": 1, "decision": "ALLOW", "codes": []},
        {"user_id": "u-bob", "attempt_seq": 2, "decision": "DENY", "codes": ["E_FINGERPRINT_AMBIGUOUS", "E_MISSING_FACTOR"]},
        {"user_id": "u-carol", "attempt_seq": 3, "decision": "DENY", "codes": ["E_DEVICE_MISMATCH", "E_DIGEST_TIER_TOO_LOW", "E_ITERATIONS_TOO_LOW"]},
        {"user_id": "u-dave", "attempt_seq": 4, "decision": "DENY", "codes": ["E_FACTOR_TYPE_MISMATCH", "E_MISSING_FACTOR"]},
        {"user_id": "u-erin", "attempt_seq": 5, "decision": "DENY", "codes": ["E_DIGEST_TIER_TOO_LOW", "E_ITERATIONS_TOO_LOW"]},
        {"user_id": "u-frank", "attempt_seq": 6, "decision": "DENY", "codes": ["E_POLICY_CONFLICT"]},
        {"user_id": "u-grace", "attempt_seq": 7, "decision": "DENY", "codes": ["E_POLICY_EXPIRED"]},
        {"user_id": "u-henry", "attempt_seq": 8, "decision": "ALLOW", "codes": []},
        {"user_id": "u-ivy", "attempt_seq": 9, "decision": "DENY", "codes": ["E_FACTOR_PROOF_INVALID", "E_MISSING_FACTOR"]},
        {"user_id": "u-jack", "attempt_seq": 10, "decision": "DENY", "codes": ["E_POLICY_TOMBSTONED"]},
        {"user_id": "u-alice", "attempt_seq": 11, "batch_watermark": 2, "decision": "DENY", "codes": ["E_FACTOR_REVOKED", "E_INVALIDATED_BY_CLOSURE"]},
        {"user_id": "u-carol", "attempt_seq": 12, "batch_watermark": 2, "decision": "DENY", "codes": ["E_DEVICE_BINDING_REVOKED", "E_DIGEST_TIER_TOO_LOW", "E_INVALIDATED_BY_CLOSURE", "E_ITERATIONS_TOO_LOW"]},
        {"user_id": "u-nate", "attempt_seq": 1, "batch_watermark": 2, "decision": "DENY", "codes": ["E_DIGEST_TIER_TOO_LOW", "E_INVALIDATED_BY_CLOSURE", "E_ITERATIONS_TOO_LOW"]},
    ]
    atomic_write_json(output / "truth" / "expected_decisions.json", {"decisions": expected_decisions})

    expected_rejections = [
        {"source_category": "user_credential", "record_seq": 13, "code": "E_DUPLICATE_USER"},
        {"source_category": "user_credential", "record_seq": 14, "code": "E_INVALID_DIGEST_TIER"},
        {"source_category": "user_credential", "record_seq": 15, "code": "E_INVALID_ITERATIONS"},
        {"source_category": "device_fingerprint", "record_seq": 13, "code": "E_INVALID_PLATFORM"},
        {"source_category": "device_fingerprint", "record_seq": 14, "code": "E_INVALID_FINGERPRINT_COMPONENT"},
        {"source_category": "device_fingerprint", "record_seq": 15, "code": "E_DUPLICATE_DEVICE"},
        {"source_category": "factor_binding", "record_seq": 19, "code": "E_DUPLICATE_FACTOR"},
        {"source_category": "factor_binding", "record_seq": 20, "code": "E_INVALID_FACTOR_TYPE"},
        {"source_category": "login_attempt", "record_seq": 14, "code": "E_DUPLICATE_ATTEMPT_KEY"},
        {"source_category": "policy_v1", "record_seq": 5, "code": "E_INVALID_EXPIRY_SECONDS"},
        {"source_category": "policy_v2", "record_seq": 7, "code": "E_INVALID_POLICY_GENERATION"},
        {"source_category": "policy_v2_sqlite", "record_seq": 99, "code": "E_INVALID_POLICY_GENERATION"},
    ]
    atomic_write_json(output / "truth" / "expected_rejections.json", {"rejections": expected_rejections})
    atomic_write_json(
        output / "truth" / "expected_closure.json",
        {
            "must_contain_nodes": [
                "factor:fac-alice-webauthn",
                "session:sess-alice-1",
                "device:dev-carol",
                "session:sess-carol-old",
                "user:u-nate",
                "session:sess-nate-1",
            ],
            "must_contain_cycle_types": ["FACTOR_DEPENDENCY_CYCLE", "DEVICE_BINDING_CHAIN_CYCLE"],
            "factor_cycle_nodes": ["factor:fac-cycle-a", "factor:fac-cycle-b", "factor:fac-cycle-c"],
            "binding_cycle_nodes": ["binding:bind-cycle-a", "binding:bind-cycle-b", "binding:bind-cycle-c"],
        },
    )
    return output


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate deterministic offline-authz sample")
    parser.add_argument("--params", type=Path, default=Path("sample_params.json"))
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    generate(args.params, args.output)
    print(json.dumps({"status": "success", "generated": str(args.output or "samples/generated")}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
