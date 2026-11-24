"""Deterministic fixed-seed sample generator."""

from __future__ import annotations

import json
import random
import sqlite3
from pathlib import Path
from typing import Any, Dict, List


SEED = 400
BOOT_TIME_MS = 1_760_000_000_000
ADDRESS_LIMIT = 1 << 32
ENDPOINT = "EP-SEED-400"
PROCESS_ALPHA = "aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa"


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2, sort_keys=False)
        handle.write("\n")


def _event(
    seq: int,
    marker: str,
    region_id: str,
    map_kind: str,
    base: int,
    before: Any,
    after: Any,
    source: str,
    observed_offset_ms: int,
    *,
    precision_error_bound: int | None = None,
) -> Dict[str, Any]:
    event = {
        "event_seq": seq,
        "endpoint_id": ENDPOINT,
        "process_guid": PROCESS_ALPHA,
        "region_id": region_id,
        "map_kind": map_kind,
        "base_offset": base,
        "region_size": 4096,
        "prot_before": before,
        "prot_after": after,
        "write_source": source,
        "observed_ms": BOOT_TIME_MS + observed_offset_ms,
        "sync_marker": marker,
    }
    if precision_error_bound is not None:
        event["precision_error_bound"] = precision_error_bound
    return event


def generate_sample(out_dir: Path | str, seed: int = SEED) -> Dict[str, Any]:
    rng = random.Random(seed)
    out = Path(out_dir)
    (out / "policy_v2").mkdir(parents=True, exist_ok=True)

    manifest = {
        "schema": "memguard-sample-manifest/v1",
        "seed": seed,
        "address_space_limit": ADDRESS_LIMIT,
        "observation_clock": {
            "clock_id": "fixed-deterministic-boot-clock",
            "boot_time_ms": BOOT_TIME_MS,
            "epoch": "unix-epoch-ms",
            "conversion_rule": "v1_absolute_ms = boot_time_ms + relative_seconds * 1000",
            "second_quantization": {
                "lower_endpoint_ms": "boot_time_ms + seconds * 1000",
                "upper_endpoint_exclusive_ms": "boot_time_ms + (seconds + 1) * 1000",
                "endpoint_width_ms": 1000,
            },
        },
    }
    _write_json(out / "manifest.json", manifest)

    region_specs = [
        ("r-text", "app.exe", "image", 0x10000, 5, "pol-app-text", []),
        ("r-rdata", "app.exe", "image", 0x20000, ["read"], "pol-app-ro", []),
        ("r-data", "app.exe", "image", 0x30000, "0o3", "pol-app-data", []),
        ("r-stack", "stack-main", "stack", 0x40000, ["read", "write"], "pol-stack", []),
        ("r-heap", "heap-default", "anon", 0x50000, 3, "pol-heap", []),
        ("r-cycle-a", "cycle.dll", "image", 0x60000, "r", "pol-cycle", ["r-cycle-b"]),
        ("r-cycle-b", "cycle.dll", "image", 0x61000, "r", "pol-cycle", ["r-cycle-a"]),
        ("r-boundary", "boundary.dll", "image", 0x80000, ["read"], "pol-boundary", []),
        ("r-legacy", "legacy.dll", "image", 0x90000, 1, "pol-legacy", []),
        ("r-conflict", "conflict.dll", "image", 0xA0000, "0o1", "pol-conflict", []),
    ]
    regions = []
    for region_id, module, kind, base, initial, policy_key, depends in region_specs:
        regions.append(
            {
                "endpoint_id": ENDPOINT,
                "process_guid": PROCESS_ALPHA,
                "region_id": region_id,
                "region_generation": 1,
                "module": module,
                "map_kind": kind,
                "base_offset": base,
                "region_size": 4096,
                "legal_range": [base, base + 4096],
                "initial_prot": initial,
                "policy_key": policy_key,
                "rollback_depends_on_regions": depends,
            }
        )

    regions_with_invalid = regions + [
        {
            "endpoint_id": ENDPOINT,
            "process_guid": PROCESS_ALPHA,
            "region_id": "r-bad-size",
            "region_generation": 1,
            "module": "bad.dll",
            "map_kind": "anon",
            "base_offset": 0xB0000,
            "region_size": -1,
            "initial_prot": 1,
        },
        {
            "endpoint_id": ENDPOINT,
            "process_guid": PROCESS_ALPHA,
            "region_id": "r-text",
            "region_generation": 1,
            "module": "duplicate.dll",
            "map_kind": "image",
            "base_offset": 0xC0000,
            "region_size": 4096,
            "initial_prot": 5,
        },
    ]
    _write_json(out / "regions.json", {"endpoint_id": ENDPOINT, "regions": regions_with_invalid})

    events = [
        _event(
            1, "sync-001-ro-append-w", "r-rdata", "image", 0x20000,
            ["read"], "0o3", "owner_private:VirtualProtect", 5_000,
        ),
        "{'event_seq': 2, broken json line",
        {
            "event_seq": "not-an-int",
            "endpoint_id": ENDPOINT,
            "process_guid": PROCESS_ALPHA,
            "region_id": "r-bad-event-seq",
            "map_kind": "anon",
            "base_offset": 1,
            "region_size": 1,
            "prot_before": 1,
            "prot_after": 3,
            "write_source": "owner_private",
            "observed_ms": BOOT_TIME_MS + 5_500,
        },
        _event(
            2, "sync-002-text-remove-x", "r-text", "image", 0x10000,
            5, ["read"], "owner_private:NtProtectVirtualMemory", 6_000,
        ),
        {
            "event_seq": 999,
            "endpoint_id": "bad endpoint with spaces",
            "process_guid": PROCESS_ALPHA,
            "region_id": "r-bad-endpoint",
            "map_kind": "not-a-kind",
            "base_offset": -8,
            "region_size": "4096",
            "prot_before": "0o99",
            "prot_after": {"not": "a valid representation"},
            "write_source": "",
            "observed_ms": -1,
        },
        _event(
            3, "sync-003-data-legal-downgrade", "r-data", "image", 0x30000,
            "0o3", 1, "owner_private:VirtualProtect", 7_000,
        ),
        _event(
            4, "sync-004-stack-external-x", "r-stack", "stack", 0x40000,
            ["read", "write"], 7, "external_injection:WriteProcessMemory", 8_000,
        ),
        _event(
            5, "sync-005-heap-append-x", "r-heap", "anon", 0x50000,
            "0o3", 7, "owner_private:VirtualProtect", 9_000,
        ),
        _event(
            6, "sync-006-cycle-a-write", "r-cycle-a", "image", 0x60000,
            "r", "rw", "owner_private:VirtualProtect", 10_000,
        ),
        _event(
            7, "sync-007-cycle-b-write", "r-cycle-b", "image", 0x61000,
            1, 3, "owner_private:VirtualProtect", 11_000,
        ),
        _event(
            8, "sync-008-boundary-uncertain", "r-boundary", "image", 0x80000,
            1, 3, "owner_private:VirtualProtect", 12_000,
            precision_error_bound=2,
        ),
        _event(
            9, "sync-009-legacy-tombstone", "r-legacy", "image", 0x90000,
            1, 3, "owner_private:VirtualProtect", 13_000,
        ),
        _event(
            10, "sync-010-conflict-v2-authority", "r-conflict", "image", 0xA0000,
            "0o1", 3, "owner_private:VirtualProtect", 14_000,
        ),
    ]
    events_path = out / "events.jsonl"
    with events_path.open("w", encoding="utf-8") as handle:
        for item in events:
            if isinstance(item, str):
                handle.write(item + "\n")
            else:
                handle.write(json.dumps(item, ensure_ascii=False) + "\n")

    v1_policies = {
        "schema": "memguard.policy_v1/v1",
        "snapshot_generated_from_clock": manifest["observation_clock"]["clock_id"],
        "policies": [
            {
                "policy_key": "pol-app-text", "generation": 1,
                "effective_after_boot_seconds": 0, "protection": "rx",
                "allowed_masks": [5], "required_mask": 4,
                "map_kinds": ["image"], "disabled": False,
            },
            {
                "policy_key": "pol-app-ro", "generation": 1,
                "effective_after_boot_seconds": 0, "flat_protection": "r",
                "allowed_masks": [1], "map_kinds": ["image"], "disabled": False,
            },
            {
                "policy_key": "pol-app-data", "generation": 1,
                "effective_after_boot_seconds": 0, "protection": "rw",
                "allowed_masks": [1, 3], "map_kinds": ["image"], "disabled": False,
            },
            {
                "policy_key": "pol-stack", "generation": 1,
                "effective_after_boot_seconds": 0, "protection": "rw",
                "allowed_masks": [3], "map_kinds": ["stack"], "disabled": True,
            },
            {
                "policy_key": "pol-heap", "generation": 1,
                "effective_after_boot_seconds": 0, "protection": "rw",
                "allowed_masks": [3], "map_kinds": ["anon"], "disabled": False,
            },
            {
                "policy_key": "pol-cycle", "generation": 1,
                "effective_after_boot_seconds": 0, "protection": "r",
                "allowed_masks": [1], "map_kinds": ["image"], "disabled": False,
            },
            {
                "policy_key": "pol-boundary", "generation": 1,
                "effective_after_boot_seconds": 0, "protection": "r",
                "allowed_masks": [1], "map_kinds": ["image"], "disabled": False,
            },
            {
                "policy_key": "pol-legacy", "generation": 1,
                "effective_after_boot_seconds": 0, "protection": "rwx",
                "allowed_masks": [1, 3, 7], "map_kinds": ["image"], "disabled": False,
            },
            {
                "policy_key": "pol-conflict", "generation": 1,
                "effective_after_boot_seconds": 0, "protection": "rwx",
                "allowed_masks": [7], "map_kinds": ["anon"],
                "effective_window_seconds": [0, 100], "disabled": False,
            },
            {
                "policy_key": "pol-bad-v1", "generation": 1,
                "effective_after_boot_seconds": -4, "protection": "r",
            },
        ],
    }
    _write_json(out / "policy_v1.json", v1_policies)

    v2_records = [
        {
            "policy_key": "pol-app-text", "generation": 2,
            "effective_at_ms": BOOT_TIME_MS, "max_prot_mask": 5,
            "allowed_masks": [5], "required_mask": 4,
            "map_kinds": ["image"], "tombstone": False, "disabled": False,
        },
        {
            "policy_key": "pol-app-ro", "generation": 2,
            "effective_at_ms": BOOT_TIME_MS, "max_prot_mask": 1,
            "allowed_masks": [1], "map_kinds": ["image"],
            "tombstone": False, "disabled": False,
        },
        {
            "policy_key": "pol-app-data", "generation": 2,
            "effective_at_ms": BOOT_TIME_MS, "max_prot_mask": 3,
            "allowed_masks": [1, 3], "map_kinds": ["image"],
            "tombstone": False, "disabled": False,
        },
        {
            "policy_key": "pol-stack", "generation": 2,
            "effective_at_ms": BOOT_TIME_MS, "max_prot_mask": 3,
            "allowed_masks": [3], "map_kinds": ["stack"],
            "tombstone": False, "disabled": False,
        },
        {
            "policy_key": "pol-heap", "generation": 2,
            "effective_at_ms": BOOT_TIME_MS, "max_prot_mask": 3,
            "allowed_masks": [3], "map_kinds": ["anon"],
            "tombstone": False, "disabled": False,
        },
        {
            "policy_key": "pol-cycle", "generation": 2,
            "effective_at_ms": BOOT_TIME_MS, "max_prot_mask": 1,
            "allowed_masks": [1], "map_kinds": ["image"],
            "tombstone": False, "disabled": False,
        },
        {
            "policy_key": "pol-boundary", "generation": 2,
            "effective_at_ms": BOOT_TIME_MS, "max_prot_mask": 1,
            "allowed_masks": [1], "map_kinds": ["image"],
            "tombstone": False, "disabled": False,
        },
        {
            "policy_key": "pol-legacy", "generation": 2,
            "effective_at_ms": BOOT_TIME_MS + 2_000,
            "max_prot_mask": 0, "tombstone": True,
            "map_kinds": ["image"], "disabled": False,
        },
        {
            "policy_key": "pol-conflict", "generation": 2,
            "effective_at_ms": BOOT_TIME_MS, "max_prot_mask": 1,
            "allowed_masks": [1], "map_kinds": ["image"],
            "effective_window_ms": [BOOT_TIME_MS, BOOT_TIME_MS + 3_600_000],
            "tombstone": False, "disabled": False,
        },
        {
            "policy_key": "pol-bad-v2-shard", "generation": -1,
            "effective_at_ms": BOOT_TIME_MS, "max_prot_mask": 1,
        },
    ]
    _write_json(out / "policy_v2" / "shard-000.json", {"shard": 0, "policies": v2_records[:5]})
    _write_json(out / "policy_v2" / "shard-001.json", {"shard": 1, "policies": v2_records[5:]})
    _write_json(
        out / "policy_v2" / "manifest.json",
        {"schema": "memguard.policy_v2_manifest/v1", "shards": ["shard-000.json", "shard-001.json"], "sqlite": "meta.sqlite"},
    )
    _write_json(out / "policy_v2" / "tombstones.json", {"tombstones": [
        {"policy_key": "pol-legacy", "generation": 2, "effective_at_ms": BOOT_TIME_MS + 2_000}
    ]})

    db_path = out / "policy_v2" / "meta.sqlite"
    if db_path.exists():
        db_path.unlink()
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE policy_meta(
            policy_key TEXT, generation INTEGER, effective_at_ms INTEGER,
            max_prot_mask INTEGER, map_kinds_json TEXT,
            effective_window_json TEXT, tombstone INTEGER, disabled INTEGER,
            target_json TEXT, required_mask INTEGER,
            allowed_masks_json TEXT, source_shard TEXT
        )
        """
    )
    for index, record in enumerate(v2_records):
        conn.execute(
            """
            INSERT INTO policy_meta VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record["policy_key"], record["generation"], record["effective_at_ms"],
                record.get("max_prot_mask"),
                json.dumps(record.get("map_kinds")),
                json.dumps(record.get("effective_window_ms")),
                1 if record.get("tombstone") else 0,
                1 if record.get("disabled") else 0,
                json.dumps(record.get("target", {})),
                record.get("required_mask"),
                json.dumps(record.get("allowed_masks")),
                f"shard-{index // 5:03d}.json",
            ),
        )
    conn.execute(
        "INSERT INTO policy_meta VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("pol-bad-db", 1, -10, 1, "[]", None, 0, 0, "{}", None, "[]", "meta.sqlite"),
    )
    conn.commit()
    conn.close()

    expected_by_seq = {
        1: ("unauthorized", "E_OWNER_POLICY_DENIED", True, 1),
        2: ("unauthorized", "E_OWNER_POLICY_DENIED", True, 5),
        3: ("compliant", "E_OK", False, None),
        4: ("unauthorized", "E_EXTERNAL_INJECTION", True, 3),
        5: ("unauthorized", "E_OWNER_POLICY_DENIED", True, 3),
        6: ("unauthorized", "E_OWNER_POLICY_DENIED", True, 1),
        7: ("unauthorized", "E_OWNER_POLICY_DENIED", True, 1),
        8: ("uncertain", "E_ADJUDICATION_UNCERTAIN", False, None),
        9: ("indeterminate", "E_POLICY_TOMBSTONED", False, None),
        10: ("unauthorized", "E_OWNER_POLICY_DENIED", True, 1),
    }
    truth_records = []
    for item in events:
        if not isinstance(item, dict):
            continue
        seq = item["event_seq"]
        if seq not in expected_by_seq:
            continue
        verdict, code, credential, restore = expected_by_seq[seq]
        truth_records.append(
            {
                "sync_marker": item["sync_marker"],
                "sync_status": "synced",
                "event_seq": seq,
                "sender_raw_prot_before": item["prot_before"],
                "sender_raw_prot_after": item["prot_after"],
                "expected_verdict": verdict,
                "expected_business_code": code,
                "expected_credential": credential,
                "expected_restore_prot_mask": restore,
            }
        )

    truth = {
        "schema": "memguard-truth/v1",
        "seed": seed,
        "rng_draws_used": rng.getstate()[1][0],
        "comparison_basis": "sync_marker only; do not join expected outcomes by event_seq",
        "endpoint_id": ENDPOINT,
        "process_guid": PROCESS_ALPHA,
        "expected_credential_count": 7,
        "expected_unauthorized_sync_markers": [
            record["sync_marker"]
            for record in truth_records
            if record["expected_verdict"] == "unauthorized"
        ],
        "expected_cycles": [["CRED-E00006", "CRED-E00007"]],
        "records": truth_records,
    }
    _write_json(out / "truth.json", truth)
    return truth
