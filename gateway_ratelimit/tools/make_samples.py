"""Generate fully deterministic, offline samples (no network, fixed data)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import hashlib
import hmac
import json

ROOT = Path(__file__).resolve().parents[1]
SAMPLES = ROOT / "samples"


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def write_ndjson(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n",
        encoding="utf-8",
    )


def sign(secret: str, key_id: str, client_id: str, route: str, signed_at: int, ts: int) -> str:
    payload = f"{key_id}|{client_id}|{route}|{signed_at}|{ts}"
    return hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()


def basic_sample() -> None:
    directory = SAMPLES / "basic"
    routes = {
        "version": 1,
        "routes": [
            {
                "name": "orders",
                "prefix": "/api/orders",
                "auth_policy": "token",
                "rate_per_sec": 10,
                "burst": 3,
                "window_ms": 1000,
                "max_in_window": 3,
            },
            {
                "name": "inventory",
                "prefix": "/api/inventory",
                "auth_policy": "token",
                "rate_per_sec": 5,
                "burst": 2,
                "window_ms": 1000,
                "max_in_window": 2,
            },
        ],
    }
    tokens = {
        "version": 1,
        "tokens": [
            {"token": "tok-alice-1", "client_id": "alice", "expires_at_ms": 10_000},
            {"token": "tok-bob-1", "client_id": "bob", "expires_at_ms": 10_000},
            {"token": "tok-carol-expired", "client_id": "carol", "expires_at_ms": 50},
        ],
        "signatures": [],
    }
    write_json(directory / "routes.json", routes)
    write_json(directory / "tokens.json", tokens)

    rows: list[dict] = []
    seq = 1

    def add(route: str, client: str, token: str, ts: int) -> None:
        nonlocal seq
        rows.append(
            {"seq": seq, "route": route, "client_id": client, "token": token, "ts_ms": ts}
        )
        seq += 1

    # Global timestamps are strictly monotonic across all routes.
    # orders/alice: burst of 4 at t=100 -> 3 allow, 1 limit.
    for _ in range(4):
        add("orders", "alice", "tok-alice-1", 100)
    # inventory/bob at t=200: 2 allow then 1 limit.
    add("inventory", "bob", "tok-bob-1", 200)
    add("inventory", "bob", "tok-bob-1", 200)
    add("inventory", "bob", "tok-bob-1", 200)
    # Auth failures never consume quota: binding, unknown, expired.
    add("inventory", "bob", "tok-alice-1", 300)  # token bound to alice -> mismatch
    add("inventory", "bob", "tok-missing", 400)
    add("orders", "carol", "tok-carol-expired", 500)
    # t=1100: integer refill gives one extra orders token; two calls -> 1 allow, 1 limit.
    add("orders", "alice", "tok-alice-1", 1100)
    add("orders", "alice", "tok-alice-1", 1100)
    write_ndjson(directory / "requests.ndjson", rows)


def boundary_sample() -> None:
    """Designed to expose refill-truncation and window-alignment boundaries."""
    directory = SAMPLES / "boundary"
    routes = {
        "version": 1,
        "routes": [
            {
                "name": "edge",
                "prefix": "/api/edge",
                "auth_policy": "token",
                "rate_per_sec": 10,
                "burst": 2,
                "window_ms": 500,
                "max_in_window": 2,
            }
        ],
    }
    tokens = {
        "version": 1,
        "tokens": [
            {"token": "tok-edge-1", "client_id": "edge-client", "expires_at_ms": 1_000_000}
        ],
        "signatures": [],
    }
    write_json(directory / "routes.json", routes)
    write_json(directory / "tokens.json", tokens)
    rows = []
    timestamps = [0, 0, 150, 300, 500, 501, 650, 900, 1000, 1001, 1250]
    for index, ts in enumerate(timestamps, start=1):
        rows.append(
            {
                "seq": index,
                "route": "edge",
                "client_id": "edge-client",
                "token": "tok-edge-1",
                "ts_ms": ts,
            }
        )
    write_ndjson(directory / "requests.ndjson", rows)


def large_sample(client_count: int = 120, per_client: int = 12) -> None:
    directory = SAMPLES / "large"
    routes = {
        "version": 1,
        "routes": [
            {
                "name": "telemetry",
                "prefix": "/api/telemetry",
                "auth_policy": "token",
                "rate_per_sec": 20,
                "burst": 5,
                "window_ms": 1000,
                "max_in_window": 5,
            }
        ],
    }
    token_rows = []
    for index in range(client_count):
        token_rows.append(
            {
                "token": f"tok-scale-{index:04d}",
                "client_id": f"client-{index:04d}",
                "expires_at_ms": 10_000_000,
            }
        )
    write_json(directory / "routes.json", routes)
    write_json(directory / "tokens.json", {"version": 1, "tokens": token_rows, "signatures": []})
    rows = []
    seq = 1
    # Interleave clients deterministically; global timestamps stay monotonic.
    for step in range(per_client):
        for index in range(client_count):
            rows.append(
                {
                    "seq": seq,
                    "route": "telemetry",
                    "client_id": f"client-{index:04d}",
                    "token": f"tok-scale-{index:04d}",
                    "ts_ms": step * 250,
                }
            )
            seq += 1
    write_ndjson(directory / "requests.ndjson", rows)


def bursty_large_sample(client_count: int = 120) -> None:
    """Bursty traffic: every client fills its exact log within one window."""
    directory = SAMPLES / "bursty"
    routes = {
        "version": 1,
        "routes": [
            {
                "name": "burst",
                "prefix": "/api/burst",
                "auth_policy": "token",
                "rate_per_sec": 50,
                "burst": 5,
                "window_ms": 1000,
                "max_in_window": 5,
            }
        ],
    }
    token_rows = [
        {"token": f"tok-burst-{index:04d}",
         "client_id": f"bclient-{index:04d}", "expires_at_ms": 10_000_000}
        for index in range(client_count)
    ]
    write_json(directory / "routes.json", routes)
    write_json(directory / "tokens.json",
               {"version": 1, "tokens": token_rows, "signatures": []})
    rows = []
    seq = 1
    # Six requests per client inside one 1s window -> log saturates at 5.
    for step in range(6):
        for index in range(client_count):
            rows.append({
                "seq": seq,
                "route": "burst",
                "client_id": f"bclient-{index:04d}",
                "token": f"tok-burst-{index:04d}",
                "ts_ms": 100 + step * 100,
            })
            seq += 1
    write_ndjson(directory / "requests.ndjson", rows)


def signature_sample() -> None:
    directory = SAMPLES / "signature"
    routes = {
        "version": 1,
        "routes": [
            {
                "name": "payments",
                "prefix": "/api/payments",
                "auth_policy": "signature",
                "rate_per_sec": 4,
                "burst": 2,
                "window_ms": 1000,
                "max_in_window": 2,
            }
        ],
    }
    secret = "local-demo-secret"
    tokens = {
        "version": 1,
        "tokens": [],
        "signatures": [
            {
                "key_id": "key-dave-1",
                "client_id": "dave",
                "secret": secret,
                "expires_at_ms": 1_000_000,
            }
        ],
    }
    write_json(directory / "routes.json", routes)
    write_json(directory / "tokens.json", tokens)
    rows = []
    timestamps = [100, 200, 300, 400, 1500, 1600]
    for index, ts in enumerate(timestamps, start=1):
        signed_at = ts - 10
        rows.append(
            {
                "seq": index,
                "route": "payments",
                "client_id": "dave",
                "token": "key-dave-1",
                "ts_ms": ts,
                "signed_at_ms": signed_at,
                "signature": sign(secret, "key-dave-1", "dave", "payments", signed_at, ts),
            }
        )
    # Tampered signature must fail auth.
    rows.append(
        {
            "seq": len(timestamps) + 1,
            "route": "payments",
            "client_id": "dave",
            "token": "key-dave-1",
            "ts_ms": 1700,
            "signed_at_ms": 1690,
            "signature": "deadbeef",
        }
    )
    write_ndjson(directory / "requests.ndjson", rows)


def invalid_spec_samples() -> None:
    directory = SAMPLES / "invalid"
    write_json(
        directory / "routes_bad_window.json",
        {
            "version": 1,
            "routes": [
                {
                    "name": "broken",
                    "prefix": "/api/broken",
                    "auth_policy": "token",
                    "rate_per_sec": 1,
                    "burst": 1,
                    "window_ms": 0,
                    "max_in_window": 1,
                }
            ],
        },
    )
    write_json(
        directory / "routes_duplicate.json",
        {
            "version": 1,
            "routes": [
                {
                    "name": "dup",
                    "prefix": "/a",
                    "auth_policy": "token",
                    "rate_per_sec": 1,
                    "burst": 1,
                    "window_ms": 100,
                    "max_in_window": 1,
                },
                {
                    "name": "dup",
                    "prefix": "/b",
                    "auth_policy": "token",
                    "rate_per_sec": 1,
                    "burst": 1,
                    "window_ms": 100,
                    "max_in_window": 1,
                },
            ],
        },
    )
    write_ndjson(
        directory / "requests_non_monotonic.ndjson",
        [
            {"seq": 1, "route": "x", "client_id": "c", "token": "t", "ts_ms": 100},
            {"seq": 2, "route": "x", "client_id": "c", "token": "t", "ts_ms": 99},
        ],
    )


def main() -> None:
    basic_sample()
    boundary_sample()
    large_sample()
    bursty_large_sample()
    signature_sample()
    invalid_spec_samples()
    print(f"samples written under {SAMPLES}")


if __name__ == "__main__":
    main()
