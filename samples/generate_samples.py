#!/usr/bin/env python3
"""Generate deterministic local samples and marker-based truth.

The truth file deliberately stores emission-side raw payload bytes and sync
markers, not masking results computed here. Report validation derives expected
results independently from those raw bytes and the synchronized rules view.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
from pathlib import Path
from typing import Any, Dict, List


DEFAULT_SEED = 20261125
DEFAULT_CLOCK_MS = 1_760_000_000_000


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def line_bytes(value: Any) -> bytes:
    return canonical_bytes(value) + b"\n"


def write_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)


def build_rules_snapshot(clock_ms: int, *, layout_version: str) -> Dict[str, Any]:
    return {
        "schema": "audit-mask-rules/v1",
        "snapshot_id": "baseline-20261125",
        "observation_clock_ms": clock_ms,
        "v1_generation": 1,
        "v1_relative_seconds": [0, 5, 0, 2, 4],
        "v1": [
            "contact-email|payload.email|email|20",
            "contact-phone|payload.phone|last4|15",
            "auth-secret|payload.token|redact|50",
            "legacy-note|payload.note|redact|5",
            "phone-strict|payload.phone|redact|60",
        ],
        "v2": [
            {
                "rule_name": "contact-email",
                "field_path": "payload.email",
                "mask": "hash",
                "threshold": 30,
                "effective_ms": clock_ms + 10_000,
                "generation": 2,
                "tombstone": False,
            },
            {
                "rule_name": "contact-phone",
                "field_path": "payload.phone",
                "mask": "last4",
                "threshold": 25,
                "effective_ms": clock_ms + 15_000,
                "generation": 2,
                "tombstone": False,
            },
            {
                "rule_name": "payment-card",
                "field_path": "payload.card",
                "mask": "last4",
                "threshold": 40,
                "effective_ms": clock_ms + 20_000,
                "generation": 3,
                "tombstone": False,
            },
            {
                "rule_name": "legacy-note",
                "field_path": "payload.note",
                "mask": "redact",
                "threshold": 5,
                "effective_ms": clock_ms + 5_000,
                "generation": 2,
                "tombstone": True,
            },
        ],
        "category_defaults": {
            "contact": {
                "mask": "redact",
                "rule_name": "default:contact",
                "generation": 0,
                "fields": [
                    "payload.email",
                    "payload.phone",
                    "payload.address",
                    "payload.note",
                ],
            },
            "auth": {
                "mask": "redact",
                "rule_name": "default:auth",
                "generation": 0,
                "fields": ["payload.*"],
            },
            "payment": {
                "mask": "last4",
                "rule_name": "default:payment",
                "generation": 0,
                "fields": ["payload.*"],
            },
            "public": {
                "mask": "keep",
                "rule_name": "default:public",
                "generation": 0,
                "fields": ["payload.*"],
            },
        },
        "layout": {
            "mask_methods": ["keep", "redact", "email", "last4", "hash"],
            "threshold_mode": "higher_wins",
            "version": layout_version,
        },
    }


def build_patches(clock_ms: int) -> List[Dict[str, Any]]:
    return [
        {
            "patch_id": "patch-001-phone-upgrade",
            "changed_rules": ["contact-phone"],
            "tombstones": [],
            "v2": [
                {
                    "rule_name": "contact-phone",
                    "field_path": "payload.phone",
                    "mask": "redact",
                    "threshold": 70,
                    "effective_ms": clock_ms + 30_000,
                    "generation": 3,
                    "tombstone": False,
                }
            ],
        },
        {
            "patch_id": "patch-002-auth-tombstone",
            "changed_rules": ["auth-secret"],
            "tombstones": ["auth-secret"],
            "v2": [
                {
                    "rule_name": "auth-secret",
                    "field_path": "payload.token",
                    "mask": "redact",
                    "threshold": 50,
                    "effective_ms": clock_ms + 40_000,
                    "generation": 4,
                    "tombstone": True,
                }
            ],
        },
        {
            "patch_id": "patch-003-address",
            "changed_rules": ["contact-address"],
            "tombstones": [],
            "v2": [
                {
                    "rule_name": "contact-address",
                    "field_path": "payload.address",
                    "mask": "hash",
                    "threshold": 18,
                    "effective_ms": clock_ms + 25_000,
                    "generation": 2,
                    "tombstone": False,
                }
            ],
        },
    ]


def build_event_specs(clock_ms: int) -> List[Dict[str, Any]]:
    return [
        {
            "category": "contact",
            "payload": {
                "email": "alice@example.com",
                "phone": "13800138000",
                "address": "1 Secret Road",
                "note": "call after lunch",
                "request_id": "req-0001",
            },
        },
        {
            "category": "auth",
            "payload": {
                "token": "tok-live-alpha",
                "user": "alice",
                "ip": "10.1.2.3",
            },
        },
        {
            "category": "payment",
            "payload": {
                "card": "6222020202021234",
                "holder": "Alice",
                "amount": "128.00",
            },
        },
        {
            "category": "public",
            "payload": {
                "request_id": "req-0004",
                "note": "public status page",
            },
        },
        {
            "category": "contact",
            "payload": {
                "email": "bob@example.net",
                "phone": "13900139000",
                "address": "2 Hidden Lane",
                "note": "vip",
                "request_id": "req-0005",
            },
        },
        {
            "category": "auth",
            "payload": {
                "token": "tok-live-beta",
                "user": "bob",
                "ip": "10.1.2.4",
            },
        },
        {
            "category": "payment",
            "payload": {
                "card": "4111111111111111",
                "holder": "Bob",
                "amount": "5.00",
            },
        },
        {
            "category": "contact",
            "payload": {
                "email": "carol@example.org",
                "phone": "13700137000",
                "address": "3 Private Way",
                "note": "",
                "request_id": "req-0008",
            },
        },
        {
            "category": "auth",
            "payload": {
                "token": "tok-live-gamma",
                "user": "carol",
                "ip": "10.1.2.5",
            },
        },
        {
            "category": "payment",
            "payload": {
                "card": "5555555555554444",
                "holder": "Carol",
                "amount": "42.50",
            },
        },
        {
            "category": "contact",
            "payload": {
                "email": "dave@example.com",
                "phone": "13600136000",
                "address": "4 Quiet Street",
                "note": "do not call",
                "request_id": "req-0011",
            },
        },
        {
            "category": "public",
            "payload": {
                "request_id": "req-0012",
                "note": "release notes",
            },
        },
    ]


def generate(out_dir: Path, seed: int) -> Dict[str, Any]:
    out_dir.mkdir(parents=True, exist_ok=True)
    rng = random.Random(seed)
    clock_ms = DEFAULT_CLOCK_MS
    specs = build_event_specs(clock_ms)
    events: List[Dict[str, Any]] = []
    truth_events: List[Dict[str, Any]] = []
    for index, spec in enumerate(specs):
        event_id = f"evt-{index:03d}"
        seq = index % 3
        sync_marker = f"SYNC-{seed}-{index:04d}"
        event = {
            "event_id": event_id,
            "tenant": f"tenant-{index % 3}",
            "actor": f"actor-{index % 4}",
            "category": spec["category"],
            "ts_ms": clock_ms + index * 1_000,
            "seq": seq,
            "payload": spec["payload"],
            "idempotency_key": f"idem-{seed}-{index:04d}",
            "sync_marker": sync_marker,
        }
        events.append(event)
        raw_payload = canonical_bytes(spec["payload"])
        truth_events.append(
            {
                "event_id": event_id,
                "seq": seq,
                "sync_marker": sync_marker,
                "raw_payload_utf8_hex": raw_payload.hex(),
                "raw_payload_sha256": hashlib.sha256(raw_payload).hexdigest(),
            }
        )

    shuffled = list(events)
    rng.shuffle(shuffled)
    events_path = out_dir / "events.jsonl"
    write_bytes(events_path, b"".join(line_bytes(event) for event in shuffled))

    split = max(1, len(shuffled) // 2)
    first_batch = shuffled[:split]
    second_batch = shuffled[split:]
    write_bytes(
        out_dir / "events_batch_1.jsonl",
        b"".join(line_bytes(event) for event in first_batch),
    )
    write_bytes(
        out_dir / "events_batch_2.jsonl",
        b"".join(line_bytes(event) for event in second_batch),
    )

    snapshot = build_rules_snapshot(clock_ms, layout_version="layout-v1")
    snapshot_path = out_dir / "rules_snapshot.json"
    write_bytes(snapshot_path, canonical_bytes(snapshot) + b"\n")

    layout_v2 = build_rules_snapshot(clock_ms, layout_version="layout-v2")
    for record in layout_v2["v2"]:
        if record["rule_name"] == "contact-phone":
            record["mask"] = "redact"
            record["threshold"] = 80
    write_bytes(
        out_dir / "rules_snapshot_layout_v2.json",
        canonical_bytes(layout_v2) + b"\n",
    )

    conflict = {
        "schema": "audit-mask-rules/v1",
        "snapshot_id": "conflict-sample",
        "observation_clock_ms": clock_ms,
        "v1": ["conflict-email|payload.email|email|20"],
        "v2": [
            {
                "rule_name": "conflict-email",
                "field_path": "payload.email",
                "mask": "redact",
                "threshold": 20,
                "effective_ms": clock_ms + 1_000,
                "generation": 1,
                "tombstone": False,
            }
        ],
        "category_defaults": {},
        "layout": {
            "mask_methods": ["keep", "redact", "email", "last4", "hash"],
            "threshold_mode": "higher_wins",
            "version": "layout-conflict",
        },
    }
    write_bytes(
        out_dir / "rules_conflict.json",
        canonical_bytes(conflict) + b"\n",
    )

    patches = build_patches(clock_ms)
    write_bytes(
        out_dir / "patches.json",
        canonical_bytes({"schema": "audit-mask-patches/v1", "patches": patches})
        + b"\n",
    )
    for patch in patches:
        write_bytes(
            out_dir / f"{patch['patch_id']}.json",
            canonical_bytes({"patches": [patch]}) + b"\n",
        )

    truth = {
        "schema": "audit-mask-truth/v1",
        "seed": seed,
        "observation_clock_ms": clock_ms,
        "event_count": len(truth_events),
        "events": sorted(
            truth_events,
            key=lambda item: (item["event_id"], item["seq"]),
        ),
    }
    truth_path = out_dir / "truth.json"
    write_bytes(truth_path, canonical_bytes(truth) + b"\n")

    files = {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(out_dir.iterdir())
        if path.is_file() and path.name != "manifest.json"
    }
    manifest = {
        "schema": "audit-mask-sample-manifest/v1",
        "seed": seed,
        "observation_clock_ms": clock_ms,
        "event_count": len(events),
        "files": files,
    }
    write_bytes(
        out_dir / "manifest.json",
        canonical_bytes(manifest) + b"\n",
    )
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--out",
        type=Path,
        default=Path(__file__).resolve().parent / "generated",
    )
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    args = parser.parse_args()
    manifest = generate(args.out, args.seed)
    print(
        json.dumps(
            {
                "out": str(args.out),
                "seed": manifest["seed"],
                "event_count": manifest["event_count"],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
