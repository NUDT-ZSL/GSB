"""Deterministic, offline-only sample fixture generator."""

from __future__ import annotations

import hashlib
import json
import random
from pathlib import Path
from typing import Any

from .constants import (
    BATCH_SIZE,
    CANONICAL_DECIMALS,
    COUNT_QUOTA_VERSION,
    INTERVAL_QUOTA_VERSION,
    QUOTA_PROFILE_VERSION,
    SCHEMA_VERSION,
)
from .io_utils import write_json_atomic

DEFAULT_SEED = 20261125
BASE_MS = 1_767_225_600_000
DAY_MS = 86_400_000
HOUR_MS = 3_600_000
MIN_INTERVAL_MS = 60_000


def _fingerprint(platform: str, components: list[int]) -> dict[str, Any]:
    return {"platform": platform, "components": components}


def _request(
    *,
    request_id: str,
    ticket_id: str,
    device_id: str,
    observed_at_ms: int,
    platform: str,
    components: list[int],
    successor_ticket_id: str | None = None,
) -> dict[str, Any]:
    return {
        "request_id": request_id,
        "ticket_id": ticket_id,
        "device_id": device_id,
        "observed_at_ms": observed_at_ms,
        "fingerprint": _fingerprint(platform, components),
        "successor_ticket_id": successor_ticket_id,
    }


def _ticket(
    *,
    ticket_id: str,
    device_id: str,
    generation: int,
    renewal_limit: int,
    issued_at_ms: int,
    expires_at_ms: int,
    parent_ticket_id: str | None = None,
    usage_count: int = 0,
) -> dict[str, Any]:
    return {
        "ticket_id": ticket_id,
        "device_id": device_id,
        "generation": generation,
        "renewal_limit": renewal_limit,
        "issued_at_ms": issued_at_ms,
        "expires_at_ms": expires_at_ms,
        "usage_count": usage_count,
        "parent_ticket_id": parent_ticket_id,
    }


def build_sample_payload(seed: int = DEFAULT_SEED) -> dict[str, Any]:
    """Build every fixture before any file is written."""

    rng = random.Random(seed)
    # The seed deterministically selects harmless one-millisecond jitter for
    # chain requests. It never changes any quota truth label.
    chain_jitter = [rng.randint(0, 1) for _ in range(2)]

    platforms = [
        {"platform": "android", "decimals": 4, "clock_quantum_ms": 1},
        {"platform": "ios", "decimals": 3, "clock_quantum_ms": 10},
        {"platform": "web", "decimals": 2, "clock_quantum_ms": 100},
        {"platform": "linux", "decimals": 3, "clock_quantum_ms": 1},
        {"platform": "ios", "decimals": 3, "clock_quantum_ms": 10},
    ]

    fingerprints = [
        {
            "device_id": "dev-alpha",
            "platform": "ios",
            "components": [123, 456, 789],
        },
        {
            "device_id": "dev-beta",
            "platform": "android",
            "components": [1235, 4561, 7890],
        },
        {
            "device_id": "dev-boundary",
            "platform": "web",
            "components": [6100, 7100, 8100],
        },
        {
            "device_id": "dev-chain",
            "platform": "android",
            "components": [700000, 800000, 900000],
        },
        {
            "device_id": "dev-cycle",
            "platform": "web",
            "components": [9000, 8000, 7000],
        },
        {
            "device_id": "dev-expire",
            "platform": "ios",
            "components": [111, 222, 333],
        },
        {
            "device_id": "dev-gen",
            "platform": "android",
            "components": [444, 555, 666],
        },
        {
            "device_id": "dev-interval",
            "platform": "web",
            "components": [10000, 20000, 30000],
        },
        {
            "device_id": "dev-alpha",
            "platform": "ios",
            "components": [123, 456, 789],
        },
        {
            "device_id": "dev-linux",
            "platform": "linux",
            "components": [1, 2, 3],
        },
        {
            "device_id": "dev-negative",
            "platform": "android",
            "components": [1, -2, 3],
        },
    ]

    tickets = [
        _ticket(
            ticket_id="t-bind-ios-0",
            device_id="dev-alpha",
            generation=0,
            renewal_limit=3,
            issued_at_ms=BASE_MS,
            expires_at_ms=BASE_MS + HOUR_MS,
        ),
        _ticket(
            ticket_id="t-ambig-0",
            device_id="dev-alpha",
            generation=0,
            renewal_limit=1,
            issued_at_ms=BASE_MS,
            expires_at_ms=BASE_MS + HOUR_MS,
        ),
        _ticket(
            ticket_id="t-count-0",
            device_id="dev-beta",
            generation=0,
            renewal_limit=5,
            issued_at_ms=BASE_MS + 1_000,
            expires_at_ms=BASE_MS + HOUR_MS,
        ),
        _ticket(
            ticket_id="t-interval-0",
            device_id="dev-interval",
            generation=0,
            renewal_limit=5,
            issued_at_ms=BASE_MS + 5_000,
            expires_at_ms=BASE_MS + HOUR_MS,
        ),
        _ticket(
            ticket_id="t-edge-0",
            device_id="dev-boundary",
            generation=0,
            renewal_limit=5,
            issued_at_ms=BASE_MS + 10_000,
            expires_at_ms=BASE_MS + HOUR_MS,
        ),
        _ticket(
            ticket_id="t-daybound-0",
            device_id="dev-beta",
            generation=0,
            renewal_limit=5,
            issued_at_ms=BASE_MS + DAY_MS - 120_000,
            expires_at_ms=BASE_MS + DAY_MS + HOUR_MS,
        ),
        _ticket(
            ticket_id="t-expired-0",
            device_id="dev-expire",
            generation=0,
            renewal_limit=2,
            issued_at_ms=BASE_MS,
            expires_at_ms=BASE_MS + 10_000,
        ),
        _ticket(
            ticket_id="t-gen-0",
            device_id="dev-gen",
            generation=2,
            renewal_limit=2,
            issued_at_ms=BASE_MS,
            expires_at_ms=BASE_MS + HOUR_MS,
        ),
        _ticket(
            ticket_id="chain-a-0",
            device_id="dev-chain",
            generation=0,
            renewal_limit=2,
            issued_at_ms=BASE_MS,
            expires_at_ms=BASE_MS + HOUR_MS,
        ),
        _ticket(
            ticket_id="chain-a-1",
            device_id="dev-chain",
            generation=1,
            renewal_limit=2,
            issued_at_ms=BASE_MS + 60_000,
            expires_at_ms=BASE_MS + HOUR_MS,
            parent_ticket_id="chain-a-0",
            usage_count=1,
        ),
        _ticket(
            ticket_id="chain-a-2",
            device_id="dev-chain",
            generation=2,
            renewal_limit=2,
            issued_at_ms=BASE_MS + 122_000,
            expires_at_ms=BASE_MS + HOUR_MS,
            parent_ticket_id="chain-a-1",
            usage_count=2,
        ),
        _ticket(
            ticket_id="chain-b-0",
            device_id="dev-chain",
            generation=0,
            renewal_limit=2,
            issued_at_ms=BASE_MS,
            expires_at_ms=BASE_MS + HOUR_MS,
        ),
        _ticket(
            ticket_id="chain-b-1",
            device_id="dev-chain",
            generation=1,
            renewal_limit=2,
            issued_at_ms=BASE_MS + 60_000,
            expires_at_ms=BASE_MS + HOUR_MS,
            parent_ticket_id="chain-b-0",
            usage_count=1,
        ),
        _ticket(
            ticket_id="chain-b-2",
            device_id="dev-chain",
            generation=2,
            renewal_limit=2,
            issued_at_ms=BASE_MS + 122_000,
            expires_at_ms=BASE_MS + HOUR_MS,
            parent_ticket_id="chain-b-1",
            usage_count=2,
        ),
        _ticket(
            ticket_id="cycle-0",
            device_id="dev-cycle",
            generation=0,
            renewal_limit=2,
            issued_at_ms=BASE_MS,
            expires_at_ms=BASE_MS + HOUR_MS,
            parent_ticket_id="cycle-1",
        ),
        _ticket(
            ticket_id="cycle-1",
            device_id="dev-cycle",
            generation=1,
            renewal_limit=2,
            issued_at_ms=BASE_MS,
            expires_at_ms=BASE_MS + HOUR_MS,
            parent_ticket_id="cycle-0",
        ),
        _ticket(
            ticket_id="cycle-2",
            device_id="dev-cycle",
            generation=2,
            renewal_limit=2,
            issued_at_ms=BASE_MS,
            expires_at_ms=BASE_MS + HOUR_MS,
            parent_ticket_id="cycle-1",
        ),
        {
            "ticket_id": "t-bind-ios-0",
            "device_id": "dev-alpha",
            "generation": 0,
            "renewal_limit": 3,
            "issued_at_ms": BASE_MS,
            "expires_at_ms": BASE_MS + HOUR_MS,
            "usage_count": 0,
            "parent_ticket_id": None,
        },
        _ticket(
            ticket_id="t-bad-period",
            device_id="dev-alpha",
            generation=0,
            renewal_limit=1,
            issued_at_ms=BASE_MS,
            expires_at_ms=BASE_MS,
        ),
        _ticket(
            ticket_id="t-bad-generation",
            device_id="dev-alpha",
            generation=3,
            renewal_limit=2,
            issued_at_ms=BASE_MS,
            expires_at_ms=BASE_MS + HOUR_MS,
        ),
        _ticket(
            ticket_id="t-unknown-device",
            device_id="dev-ghost",
            generation=0,
            renewal_limit=1,
            issued_at_ms=BASE_MS,
            expires_at_ms=BASE_MS + HOUR_MS,
        ),
    ]

    valid_renewals = [
        _request(
            request_id="r-bind-mismatch",
            ticket_id="t-bind-ios-0",
            device_id="dev-alpha",
            observed_at_ms=BASE_MS + 61_000,
            platform="android",
            components=[999_999, 999_999, 999_999],
        ),
        _request(
            request_id="r-fp-ambiguous",
            ticket_id="t-ambig-0",
            device_id="dev-alpha",
            observed_at_ms=BASE_MS + 62_000,
            platform="android",
            components=[1234, 4560, 7890],
        ),
        _request(
            request_id="r-count-accept-1",
            ticket_id="t-count-0",
            device_id="dev-beta",
            observed_at_ms=BASE_MS + 62_000,
            platform="android",
            components=[1235, 4561, 7890],
        ),
        _request(
            request_id="r-count-accept-2",
            ticket_id="t-count-0",
            device_id="dev-beta",
            observed_at_ms=BASE_MS + 123_000,
            platform="android",
            components=[1235, 4561, 7890],
        ),
        _request(
            request_id="r-count-both-fail",
            ticket_id="t-count-0",
            device_id="dev-beta",
            observed_at_ms=BASE_MS + 153_000,
            platform="android",
            components=[1235, 4561, 7890],
        ),
        _request(
            request_id="r-interval-accept",
            ticket_id="t-interval-0",
            device_id="dev-interval",
            observed_at_ms=BASE_MS + 66_000,
            platform="web",
            components=[10000, 20000, 30000],
        ),
        _request(
            request_id="r-interval-reject",
            ticket_id="t-interval-0",
            device_id="dev-interval",
            observed_at_ms=BASE_MS + 125_000,
            platform="web",
            components=[10000, 20000, 30000],
        ),
        _request(
            request_id="r-edge-ambiguous",
            ticket_id="t-edge-0",
            device_id="dev-boundary",
            observed_at_ms=BASE_MS + 70_030,
            platform="web",
            components=[6100, 7100, 8100],
        ),
        _request(
            request_id="r-daybound-ambiguous",
            ticket_id="t-daybound-0",
            device_id="dev-beta",
            observed_at_ms=BASE_MS + DAY_MS - 300,
            platform="android",
            components=[1235, 4561, 7890],
        ),
        _request(
            request_id="r-expired",
            ticket_id="t-expired-0",
            device_id="dev-expire",
            observed_at_ms=BASE_MS + 10_000,
            platform="ios",
            components=[111, 222, 333],
        ),
        _request(
            request_id="r-generation",
            ticket_id="t-gen-0",
            device_id="dev-gen",
            observed_at_ms=BASE_MS + 61_000,
            platform="android",
            components=[444, 555, 666],
        ),
        _request(
            request_id="r-chain-a-1",
            ticket_id="chain-a-0",
            device_id="dev-chain",
            observed_at_ms=BASE_MS + 61_000 + chain_jitter[0],
            platform="android",
            components=[700000, 800000, 900000],
            successor_ticket_id="chain-a-1",
        ),
        _request(
            request_id="r-chain-a-2",
            ticket_id="chain-a-1",
            device_id="dev-chain",
            observed_at_ms=BASE_MS + 122_000 + chain_jitter[1],
            platform="android",
            components=[700000, 800000, 900000],
            successor_ticket_id="chain-a-2",
        ),
    ]

    invalid_renewals = [
        {
            "request_id": "r-invalid-time",
            "ticket_id": "t-bind-ios-0",
            "device_id": "dev-alpha",
            "observed_at_ms": -1,
            "batch_id": 1,
            "fingerprint": _fingerprint("ios", [123, 456, 789]),
            "successor_ticket_id": None,
        },
        {
            "request_id": "r-invalid-platform",
            "ticket_id": "t-bind-ios-0",
            "device_id": "dev-alpha",
            "observed_at_ms": BASE_MS + 70_000,
            "batch_id": 1,
            "fingerprint": _fingerprint("linux", [1, 2, 3]),
            "successor_ticket_id": None,
        },
        {
            "request_id": "r-bind-mismatch",
            "ticket_id": "t-bind-ios-0",
            "device_id": "dev-alpha",
            "observed_at_ms": BASE_MS + 70_000,
            "batch_id": 1,
            "fingerprint": _fingerprint("ios", [123, 456, 789]),
            "successor_ticket_id": None,
        },
    ]

    valid_renewals = sorted(
        valid_renewals,
        key=lambda item: (item["observed_at_ms"], item["request_id"]),
    )
    for index, record in enumerate(valid_renewals):
        record["batch_id"] = index // BATCH_SIZE + 1
    renewals = valid_renewals + invalid_renewals

    quotas = [
        {
            "kind": "device_window",
            "version": COUNT_QUOTA_VERSION,
            "window_seconds": DAY_MS // 1_000,
            "window_anchor_seconds": BASE_MS // 1_000,
            "max_renewals_per_window": 2,
        },
        {
            "kind": "ticket_interval",
            "version": INTERVAL_QUOTA_VERSION,
            "min_interval_ms": MIN_INTERVAL_MS,
        },
        {
            "kind": "device_window",
            "version": COUNT_QUOTA_VERSION,
            "window_seconds": DAY_MS // 1_000,
            "window_anchor_seconds": BASE_MS // 1_000,
            "max_renewals_per_window": 2,
        },
    ]

    revocations = [
        {
            "request_id": "rev-dev-chain",
            "target_type": "device",
            "target_id": "dev-chain",
            "observed_at_ms": BASE_MS + 200_000,
        },
        {
            "request_id": "rev-ticket-a1",
            "target_type": "ticket",
            "target_id": "chain-a-1",
            "observed_at_ms": BASE_MS + 200_001,
        },
        {
            "request_id": "rev-cycle",
            "target_type": "ticket",
            "target_id": "cycle-0",
            "observed_at_ms": BASE_MS + 200_002,
        },
        {
            "request_id": "rev-unknown",
            "target_type": "ticket",
            "target_id": "missing-ticket",
            "observed_at_ms": BASE_MS + 200_003,
        },
    ]

    expected_requests = {
        "r-bind-mismatch": {"decision": "reject", "code": "E_DEVICE_MISMATCH"},
        "r-fp-ambiguous": {"decision": "ambiguous", "code": "E_FP_AMBIGUOUS"},
        "r-count-accept-1": {"decision": "accept", "code": None},
        "r-count-accept-2": {"decision": "accept", "code": None},
        "r-count-both-fail": {
            "decision": "reject",
            "code": "E_QUOTA_COUNT",
            "first_rejection": "count_window",
        },
        "r-interval-accept": {"decision": "accept", "code": None},
        "r-interval-reject": {"decision": "reject", "code": "E_QUOTA_INTERVAL"},
        "r-edge-ambiguous": {"decision": "ambiguous", "code": "E_QUOTA_AMBIGUOUS"},
        "r-daybound-ambiguous": {
            "decision": "ambiguous",
            "code": "E_QUOTA_AMBIGUOUS",
        },
        "r-expired": {"decision": "reject", "code": "E_TICKET_EXPIRED"},
        "r-generation": {"decision": "reject", "code": "E_GENERATION_EXCEEDED"},
        "r-chain-a-1": {"decision": "accept", "code": None},
        "r-chain-a-2": {"decision": "accept", "code": None},
    }
    truth = {
        "seed": seed,
        "generated_from": "sso_ticket_guard.samplegen",
        "platform_count": 3,
        "requests": expected_requests,
        "normalization_rejections": {
            "platform": [{"record_index": 4, "code": "E_PLATFORM"}, {"record_index": 5, "code": "E_ID_DUPLICATE"}],
            "fingerprint": [
                {"record_index": 9, "code": "E_ID_DUPLICATE"},
                {"record_index": 10, "code": "E_PLATFORM"},
                {"record_index": 11, "code": "E_INVALID_RECORD"},
            ],
            "ticket": [
                {"record_index": 18, "code": "E_ID_DUPLICATE"},
                {"record_index": 19, "code": "E_TICKET_PERIOD"},
                {"record_index": 20, "code": "E_GENERATION_INCONSISTENT"},
                {"record_index": 21, "code": "E_UNKNOWN_DEVICE"},
            ],
            "renewal": [
                {"record_index": 14, "code": "E_INVALID_RECORD"},
                {"record_index": 15, "code": "E_PLATFORM"},
                {"record_index": 16, "code": "E_ID_DUPLICATE"},
            ],
            "quota": [{"record_index": 3, "code": "E_ID_DUPLICATE"}],
        },
        "revocations": {
            "rev-dev-chain": {
                "decision": "accept",
                "code": None,
                "ticket_ids": [
                    "chain-a-0",
                    "chain-a-1",
                    "chain-a-2",
                    "chain-b-0",
                    "chain-b-1",
                    "chain-b-2",
                ],
            },
            "rev-ticket-a1": {
                "decision": "accept",
                "code": None,
                "ticket_ids": ["chain-a-1", "chain-a-2"],
            },
            "rev-cycle": {
                "decision": "reject",
                "code": "E_TICKET_CYCLE",
                "cycle_ticket_ids": ["cycle-0", "cycle-1"],
            },
            "rev-unknown": {"decision": "reject", "code": "E_UNKNOWN_TICKET"},
        },
    }
    return {
        "platforms": platforms,
        "fingerprints": fingerprints,
        "tickets": tickets,
        "renewals": renewals,
        "quotas": quotas,
        "revocations": revocations,
        "truth": truth,
    }


def generate_samples(output_dir: Path, seed: int = DEFAULT_SEED) -> dict[str, Any]:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    payload = build_sample_payload(seed)
    files = {
        "platforms.json": payload["platforms"],
        "fingerprints.json": payload["fingerprints"],
        "tickets.json": payload["tickets"],
        "renewals.json": payload["renewals"],
        "quotas.json": payload["quotas"],
        "revocations.json": payload["revocations"],
    }
    hashes: dict[str, str] = {}
    for name, value in files.items():
        path = output_dir / name
        write_json_atomic(path, value)
        hashes[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    write_json_atomic(output_dir / "truth.json", payload["truth"])
    manifest = {
        "seed": seed,
        "canonical_decimals": CANONICAL_DECIMALS,
        "schema_version": SCHEMA_VERSION,
        "quota_profile_version": QUOTA_PROFILE_VERSION,
        "batch_size": BATCH_SIZE,
        "files": hashes,
        "truth_file": "truth.json",
    }
    write_json_atomic(output_dir / "manifest.json", manifest)
    return manifest

