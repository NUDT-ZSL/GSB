"""Deterministic local sample generator.

The generator deliberately writes both the ordinary case JSONL and a truth file
with IPv4 sender five-tuples encoded as raw network-order bytes. Tests compare
the engine output to truth by sync_marker; no system clock or random seed source
outside ``--seed`` is consulted.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import random
import sqlite3
import struct
import sys
from pathlib import Path
from typing import Any, Sequence

from .atomic import atomic_write_json
from .models import PROTOCOL_NUMBERS, canonical_hash

DEFAULT_SEED = 20261125
OBSERVED_CLOCK_MS = 1_735_689_600_000
WIRE_FORMAT = "!4sB4sBBHHHH"


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = "".join(
        json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n"
        for row in rows
    )
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(text, encoding="utf-8", newline="\n")
    temporary.replace(path)


def _v1_rule(
    *,
    rule_key: str,
    src_prefix: str,
    dst_prefix: str,
    protocol: str,
    src_port_range: str,
    dst_port_range: str,
    rule_seq: int,
    action: int,
    relative_sec: int,
) -> dict[str, Any]:
    return {
        "rule_key": rule_key,
        "src_prefix": src_prefix,
        "dst_prefix": dst_prefix,
        "protocol": protocol,
        "src_port_range": src_port_range,
        "dst_port_range": dst_port_range,
        "rule_seq": rule_seq,
        "action": action,
        "effective_relative_sec": relative_sec,
    }


def _v2_rule(
    *,
    record_id: str,
    rule_key: str,
    src_network: str,
    src_suffix: int,
    dst_network: str,
    dst_suffix: int,
    protocol: str,
    src_ports: tuple[int, int],
    dst_ports: tuple[int, int],
    src_end_closed: bool,
    dst_end_closed: bool,
    rule_seq: int,
    action: str,
    generation: int,
    effective_ms: int,
    tombstone: bool,
) -> tuple[dict[str, Any], dict[str, Any]]:
    shard = {
        "record_id": record_id,
        "rule_key": rule_key,
        "src_network": src_network,
        "dst_network": dst_network,
        "protocol": protocol,
        "src_port_start": src_ports[0],
        "src_port_end": src_ports[1],
        "dst_port_start": dst_ports[0],
        "dst_port_end": dst_ports[1],
        "rule_seq": rule_seq,
        "action": action,
    }
    meta = {
        "record_id": record_id,
        "rule_key": rule_key,
        "generation": generation,
        "effective_ms": effective_ms,
        "tombstone": 1 if tombstone else 0,
        "src_cidr_suffix": src_suffix,
        "dst_cidr_suffix": dst_suffix,
        "src_port_end_closed": 1 if src_end_closed else 0,
        "dst_port_end_closed": 1 if dst_end_closed else 0,
    }
    return shard, meta


def _case(
    *,
    case_id: str,
    src_prefix: str,
    dst_prefix: str,
    protocol: str,
    src_range: str,
    dst_range: str,
    sync_marker: str,
    rule_seq: int | None = None,
    action: str | None = None,
) -> dict[str, Any]:
    value: dict[str, Any] = {
        "case_id": case_id,
        "sync_marker": sync_marker,
        "src_prefix": src_prefix,
        "dst_prefix": dst_prefix,
        "protocol": protocol,
        "src_port_range": src_range,
        "dst_port_range": dst_range,
    }
    if rule_seq is not None:
        value["rule_seq"] = rule_seq
    if action is not None:
        value["action"] = action
    return value


def _wire_hex(case: dict[str, Any]) -> str:
    src = ipaddress.ip_network(case["src_prefix"], strict=False)
    dst = ipaddress.ip_network(case["dst_prefix"], strict=False)
    if src.version != 4 or dst.version != 4:
        raise ValueError("truth wire format in this sample is IPv4-only")
    src_lo, src_hi = (int(part) for part in case["src_port_range"].split("-", 1))
    dst_lo, dst_hi = (int(part) for part in case["dst_port_range"].split("-", 1))
    packed = struct.pack(
        WIRE_FORMAT,
        src.network_address.packed,
        src.prefixlen,
        dst.network_address.packed,
        dst.prefixlen,
        PROTOCOL_NUMBERS[case["protocol"]],
        src_lo,
        src_hi,
        dst_lo,
        dst_hi,
    )
    return packed.hex()


def _create_meta_db(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    connection = sqlite3.connect(str(path))
    try:
        connection.execute("PRAGMA journal_mode = DELETE")
        connection.execute("PRAGMA synchronous = FULL")
        connection.execute(
            """
            CREATE TABLE rule_meta (
                record_id TEXT PRIMARY KEY,
                rule_key TEXT NOT NULL,
                generation INTEGER NOT NULL CHECK (generation >= 0),
                effective_ms INTEGER NOT NULL CHECK (effective_ms >= 0),
                tombstone INTEGER NOT NULL CHECK (tombstone IN (0, 1)),
                src_cidr_suffix INTEGER NOT NULL,
                dst_cidr_suffix INTEGER NOT NULL,
                src_port_end_closed INTEGER NOT NULL
                    CHECK (src_port_end_closed IN (0, 1)),
                dst_port_end_closed INTEGER NOT NULL
                    CHECK (dst_port_end_closed IN (0, 1))
            )
            """
        )
        connection.executemany(
            "INSERT INTO rule_meta("
            "record_id, rule_key, generation, effective_ms, tombstone, "
            "src_cidr_suffix, dst_cidr_suffix, src_port_end_closed, "
            "dst_port_end_closed"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    row["record_id"],
                    row["rule_key"],
                    row["generation"],
                    row["effective_ms"],
                    row["tombstone"],
                    row["src_cidr_suffix"],
                    row["dst_cidr_suffix"],
                    row["src_port_end_closed"],
                    row["dst_port_end_closed"],
                )
                for row in sorted(rows, key=lambda item: item["record_id"])
            ],
        )
        connection.commit()
        connection.execute("VACUUM")
    finally:
        connection.close()


def generate(out_dir: str | Path, seed: int = DEFAULT_SEED) -> dict[str, Any]:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    rng = random.Random(seed)
    sample_id = f"firewall-equivalence-{seed}"

    host_a = rng.randint(1, 240)
    host_b = rng.randint(1, 240)

    v1_rules: list[dict[str, Any]] = [
        _v1_rule(
            rule_key="shared-web",
            src_prefix=f"10.20.30.{host_a}/24",
            dst_prefix="192.0.2.99/24",
            protocol="tcp",
            src_port_range="10000-20000",
            dst_port_range="80-80",
            rule_seq=10,
            action=1,
            relative_sec=0,
        ),
        _v1_rule(
            rule_key="v1-alias-dns",
            src_prefix=f"10.20.31.{host_b}/24",
            dst_prefix="198.51.100.42/32",
            protocol="udp",
            src_port_range="30000-40000",
            dst_port_range="53-53",
            rule_seq=20,
            action=1,
            relative_sec=5,
        ),
        _v1_rule(
            rule_key="v1-semantic-only",
            src_prefix="10.20.32.9/24",
            dst_prefix="203.0.113.9/24",
            protocol="tcp",
            src_port_range="20000-21000",
            dst_port_range="443-443",
            rule_seq=25,
            action=1,
            relative_sec=7,
        ),
        _v1_rule(
            rule_key="shared-retired",
            src_prefix="10.20.33.9/24",
            dst_prefix="203.0.113.77/24",
            protocol="tcp",
            src_port_range="12000-13000",
            dst_port_range="8443-8443",
            rule_seq=30,
            action=0,
            relative_sec=10,
        ),
        _v1_rule(
            rule_key="v1-legacy-only",
            src_prefix="10.20.34.9/24",
            dst_prefix="192.0.2.200/32",
            protocol="udp",
            src_port_range="15000-15010",
            dst_port_range="1812-1812",
            rule_seq=35,
            action=0,
            relative_sec=15,
        ),
        _v1_rule(
            rule_key="v1-ambig",
            src_prefix="10.20.36.9/24",
            dst_prefix="203.0.113.100/32",
            protocol="tcp",
            src_port_range="33000-34000",
            dst_port_range="9090-9090",
            rule_seq=45,
            action=1,
            relative_sec=20,
        ),
        _v1_rule(
            rule_key="v1-conflict-allow",
            src_prefix="10.20.37.9/24",
            dst_prefix="198.51.100.150/24",
            protocol="tcp",
            src_port_range="40000-41000",
            dst_port_range="3306-3306",
            rule_seq=50,
            action=1,
            relative_sec=25,
        ),
        _v1_rule(
            rule_key="v1-conflict-deny",
            src_prefix="10.20.37.9/24",
            dst_prefix="198.51.100.150/24",
            protocol="tcp",
            src_port_range="40000-41000",
            dst_port_range="3306-3306",
            rule_seq=50,
            action=0,
            relative_sec=25,
        ),
        _v1_rule(
            rule_key="bad-prefix",
            src_prefix="10.0.0.1/33",
            dst_prefix="203.0.113.1/24",
            protocol="tcp",
            src_port_range="1-2",
            dst_port_range="80-80",
            rule_seq=90,
            action=1,
            relative_sec=0,
        ),
        _v1_rule(
            rule_key="bad-protocol",
            src_prefix="10.0.0.0/24",
            dst_prefix="203.0.113.1/24",
            protocol="ftp",
            src_port_range="1-2",
            dst_port_range="21-21",
            rule_seq=91,
            action=1,
            relative_sec=0,
        ),
        _v1_rule(
            rule_key="bad-port",
            src_prefix="10.0.0.0/24",
            dst_prefix="203.0.113.1/24",
            protocol="tcp",
            src_port_range="20-10",
            dst_port_range="80-80",
            rule_seq=92,
            action=1,
            relative_sec=0,
        ),
        _v1_rule(
            rule_key="bad-relative",
            src_prefix="10.0.0.0/24",
            dst_prefix="203.0.113.1/24",
            protocol="tcp",
            src_port_range="1000-2000",
            dst_port_range="80-80",
            rule_seq=93,
            action=1,
            relative_sec=-1,
        ),
        _v1_rule(
            rule_key="shared-web",
            src_prefix="10.20.30.0/24",
            dst_prefix="192.0.2.0/24",
            protocol="tcp",
            src_port_range="1-2",
            dst_port_range="3-4",
            rule_seq=94,
            action=1,
            relative_sec=0,
        ),
    ]
    v1 = {
        "format": "v1",
        "snapshot_id": f"v1-{seed}",
        "generation": 1,
        "rules": v1_rules,
    }

    v2_shards: list[dict[str, Any]] = []
    meta_rows: list[dict[str, Any]] = []

    def add_v2(**kwargs: Any) -> None:
        shard, meta = _v2_rule(**kwargs)
        v2_shards.append(shard)
        meta_rows.append(meta)

    add_v2(
        record_id="v2r-shared-web",
        rule_key="shared-web",
        src_network="10.20.30.0",
        src_suffix=24,
        dst_network="192.0.2.0",
        dst_suffix=24,
        protocol="tcp",
        src_ports=(10000, 20001),
        dst_ports=(80, 81),
        src_end_closed=False,
        dst_end_closed=False,
        rule_seq=10,
        action="allow",
        generation=2,
        effective_ms=OBSERVED_CLOCK_MS,
        tombstone=False,
    )
    add_v2(
        record_id="v2r-alias-dns",
        rule_key="v2-alias-dns",
        src_network="10.20.31.0",
        src_suffix=24,
        dst_network="198.51.100.42",
        dst_suffix=32,
        protocol="udp",
        src_ports=(30000, 40001),
        dst_ports=(53, 53),
        src_end_closed=False,
        dst_end_closed=True,
        rule_seq=20,
        action="allow",
        generation=2,
        effective_ms=OBSERVED_CLOCK_MS + 5000,
        tombstone=False,
    )
    add_v2(
        record_id="v2r-semantic-only",
        rule_key="v2-semantic-only",
        src_network="10.20.32.0",
        src_suffix=24,
        dst_network="203.0.113.0",
        dst_suffix=24,
        protocol="tcp",
        src_ports=(20000, 21000),
        dst_ports=(443, 443),
        src_end_closed=True,
        dst_end_closed=True,
        rule_seq=25,
        action="allow",
        generation=2,
        effective_ms=OBSERVED_CLOCK_MS + 7000,
        tombstone=False,
    )
    add_v2(
        record_id="v2r-retired-active",
        rule_key="shared-retired",
        src_network="10.20.33.0",
        src_suffix=24,
        dst_network="203.0.113.0",
        dst_suffix=24,
        protocol="tcp",
        src_ports=(12000, 13000),
        dst_ports=(8443, 8443),
        src_end_closed=True,
        dst_end_closed=True,
        rule_seq=30,
        action="deny",
        generation=2,
        effective_ms=OBSERVED_CLOCK_MS + 10000,
        tombstone=False,
    )
    add_v2(
        record_id="v2r-retired-tombstone",
        rule_key="shared-retired",
        src_network="10.20.33.0",
        src_suffix=24,
        dst_network="203.0.113.0",
        dst_suffix=24,
        protocol="tcp",
        src_ports=(12000, 13000),
        dst_ports=(8443, 8443),
        src_end_closed=True,
        dst_end_closed=True,
        rule_seq=30,
        action="deny",
        generation=3,
        effective_ms=OBSERVED_CLOCK_MS + 20000,
        tombstone=True,
    )
    add_v2(
        record_id="v2r-extra-only",
        rule_key="v2-extra-only",
        src_network="10.20.35.0",
        src_suffix=24,
        dst_network="198.51.100.0",
        dst_suffix=24,
        protocol="tcp",
        src_ports=(22000, 23000),
        dst_ports=(8080, 8080),
        src_end_closed=True,
        dst_end_closed=True,
        rule_seq=40,
        action="allow",
        generation=2,
        effective_ms=OBSERVED_CLOCK_MS + 2000,
        tombstone=False,
    )
    add_v2(
        record_id="v2r-ambig-a",
        rule_key="v2-ambig-a",
        src_network="10.20.36.0",
        src_suffix=24,
        dst_network="203.0.113.100",
        dst_suffix=32,
        protocol="tcp",
        src_ports=(33000, 34000),
        dst_ports=(9090, 9090),
        src_end_closed=True,
        dst_end_closed=True,
        rule_seq=45,
        action="allow",
        generation=2,
        effective_ms=OBSERVED_CLOCK_MS + 20000,
        tombstone=False,
    )
    add_v2(
        record_id="v2r-ambig-b",
        rule_key="v2-ambig-b",
        src_network="10.20.36.0",
        src_suffix=24,
        dst_network="203.0.113.100",
        dst_suffix=32,
        protocol="tcp",
        src_ports=(33000, 34000),
        dst_ports=(9090, 9090),
        src_end_closed=True,
        dst_end_closed=True,
        rule_seq=45,
        action="allow",
        generation=2,
        effective_ms=OBSERVED_CLOCK_MS + 20000,
        tombstone=False,
    )
    for suffix, seq, action in (
        ("allow", 50, "allow"),
        ("deny", 50, "deny"),
    ):
        add_v2(
            record_id=f"v2r-conflict-{suffix}",
            rule_key=f"v2-conflict-{suffix}",
            src_network="10.20.37.0",
            src_suffix=24,
            dst_network="198.51.100.0",
            dst_suffix=24,
            protocol="tcp",
            src_ports=(40000, 41000),
            dst_ports=(3306, 3306),
            src_end_closed=True,
            dst_end_closed=True,
            rule_seq=seq,
            action=action,
            generation=2,
            effective_ms=OBSERVED_CLOCK_MS + 25000,
            tombstone=False,
        )
    add_v2(
        record_id="v2r-bad-port",
        rule_key="bad-v2-port",
        src_network="10.0.0.0",
        src_suffix=24,
        dst_network="203.0.113.0",
        dst_suffix=24,
        protocol="tcp",
        src_ports=(100, 50),
        dst_ports=(80, 80),
        src_end_closed=False,
        dst_end_closed=True,
        rule_seq=95,
        action="allow",
        generation=2,
        effective_ms=OBSERVED_CLOCK_MS,
        tombstone=False,
    )
    # This metadata row deliberately has no shard record and a bad IPv4 suffix.
    meta_rows.append(
        {
            "record_id": "v2r-invalid-meta",
            "rule_key": "bad-v2-suffix",
            "generation": 2,
            "effective_ms": OBSERVED_CLOCK_MS,
            "tombstone": 0,
            "src_cidr_suffix": 129,
            "dst_cidr_suffix": 24,
            "src_port_end_closed": 1,
            "dst_port_end_closed": 1,
        }
    )

    split = 3 + rng.randrange(0, 5)
    shard_sets = [v2_shards[:split], v2_shards[split:]]
    v2_dir = out / "v2"
    v2_dir.mkdir(parents=True, exist_ok=True)
    shard_names: list[str] = []
    for index, rules in enumerate(shard_sets):
        name = f"shard-{index:03d}.json"
        shard_names.append(name)
        atomic_write_json(
            v2_dir / name,
            {
                "format": "v2-shard",
                "shard_index": index,
                "rules": rules,
            },
        )
    _create_meta_db(v2_dir / "meta.sqlite3", meta_rows)
    v2_manifest = {
        "format": "v2",
        "snapshot_id": f"v2-{seed}",
        "generation": 2,
        "shards": shard_names,
        "metadata_db": "meta.sqlite3",
    }
    atomic_write_json(out / "v2" / "manifest.json", v2_manifest)

    mapping_entries: list[dict[str, Any]] = [
        {"v1_rule_key": "shared-web", "v2_rule_key": "shared-web"},
        {"v1_rule_key": "v1-alias-dns", "v2_rule_key": "v2-alias-dns"},
        {"v1_rule_key": "shared-retired", "v2_rule_key": "shared-retired"},
        {"v1_rule_key": "v1-ambig", "v2_rule_key": "v2-ambig-a"},
        {"v1_rule_key": "v1-ambig", "v2_rule_key": "v2-ambig-b"},
        {
            "v1_rule_key": "v1-conflict-allow",
            "v2_rule_key": "v2-conflict-allow",
        },
        {
            "v1_rule_key": "v1-conflict-deny",
            "v2_rule_key": "v2-conflict-deny",
        },
        {
            "v1_rule_key": "shared-web",
            "mapping_generation": 3,
            "active": True,
        },
    ]
    mapping = {
        "format": "mapping-declaration",
        "declaration_generation": 3,
        "direction": "v1_to_v2",
        "conflict_policy": "strict",
        "entries": mapping_entries,
    }
    atomic_write_json(out / "mapping.json", mapping)

    cases: list[dict[str, Any]] = [
        _case(
            case_id="case-shared-web",
            src_prefix="10.20.30.50/32",
            dst_prefix="192.0.2.10/32",
            protocol="tcp",
            src_range="15000-25000",
            dst_range="79-81",
            sync_marker=f"SYNC-{seed}-0001",
            rule_seq=10,
            action="allow",
        ),
        _case(
            case_id="case-alias-dns",
            src_prefix="10.20.31.10/32",
            dst_prefix="198.51.100.42/32",
            protocol="udp",
            src_range="35000-35010",
            dst_range="53-53",
            sync_marker=f"SYNC-{seed}-0002",
            rule_seq=20,
            action="allow",
        ),
        _case(
            case_id="case-semantic",
            src_prefix="10.20.32.10/32",
            dst_prefix="203.0.113.10/32",
            protocol="tcp",
            src_range="20500-20510",
            dst_range="443-443",
            sync_marker=f"SYNC-{seed}-0003",
            rule_seq=25,
            action="allow",
        ),
        _case(
            case_id="case-tombstone",
            src_prefix="10.20.33.10/32",
            dst_prefix="203.0.113.77/32",
            protocol="tcp",
            src_range="12500-12500",
            dst_range="8443-8443",
            sync_marker=f"SYNC-{seed}-0004",
            rule_seq=30,
            action="deny",
        ),
        _case(
            case_id="case-v1-legacy",
            src_prefix="10.20.34.10/32",
            dst_prefix="192.0.2.200/32",
            protocol="udp",
            src_range="15005-15005",
            dst_range="1812-1812",
            sync_marker=f"SYNC-{seed}-0005",
            rule_seq=35,
            action="deny",
        ),
        _case(
            case_id="case-v2-extra",
            src_prefix="10.20.35.10/32",
            dst_prefix="198.51.100.200/32",
            protocol="tcp",
            src_range="22500-22500",
            dst_range="8080-8080",
            sync_marker=f"SYNC-{seed}-0006",
            rule_seq=40,
            action="allow",
        ),
        _case(
            case_id="case-ambiguous",
            src_prefix="10.20.36.10/32",
            dst_prefix="203.0.113.100/32",
            protocol="tcp",
            src_range="33500-33500",
            dst_range="9090-9090",
            sync_marker=f"SYNC-{seed}-0007",
            rule_seq=45,
            action="allow",
        ),
        _case(
            case_id="case-conflict",
            src_prefix="10.20.37.10/32",
            dst_prefix="198.51.100.150/32",
            protocol="tcp",
            src_range="40500-40500",
            dst_range="3306-3306",
            sync_marker=f"SYNC-{seed}-0008",
            rule_seq=50,
        ),
        _case(
            case_id="case-none",
            src_prefix="10.99.0.10/32",
            dst_prefix="203.0.113.250/32",
            protocol="tcp",
            src_range="50000-50010",
            dst_range="9999-9999",
            sync_marker=f"SYNC-{seed}-0009",
        ),
    ]
    valid_cases = [dict(case) for case in cases]
    cases.extend(
        [
            {
                "case_id": "case-shared-web",
                "sync_marker": f"SYNC-{seed}-DUP",
                "src_prefix": "10.20.30.1/32",
                "dst_prefix": "192.0.2.1/32",
                "protocol": "tcp",
                "src_port_range": "1-1",
                "dst_port_range": "1-1",
            },
            {
                "case_id": "case-bad-protocol",
                "sync_marker": f"SYNC-{seed}-BAD",
                "src_prefix": "10.20.30.1/32",
                "dst_prefix": "192.0.2.1/32",
                "protocol": "ftp",
                "src_port_range": "1-1",
                "dst_port_range": "1-1",
            },
        ]
    )
    _write_jsonl(out / "cases.jsonl", cases)
    truth = [
        {
            "wire_format": WIRE_FORMAT,
            "case_id": case["case_id"],
            "sync_marker": case["sync_marker"],
            "observed_clock_ms": OBSERVED_CLOCK_MS,
            "seed": seed,
            "wire_hex": _wire_hex(case),
        }
        for case in valid_cases
    ]
    _write_jsonl(out / "truth.jsonl", truth)
    atomic_write_json(out / "v1.json", v1)

    sample_manifest = {
        "format": "firewall-audit-sample",
        "schema_version": 1,
        "sample_id": sample_id,
        "seed": seed,
        "observed_clock_ms": OBSERVED_CLOCK_MS,
        "cases": "cases.jsonl",
        "truth": "truth.jsonl",
        "v1": "v1.json",
        "v2_manifest": "v2/manifest.json",
        "mapping": "mapping.json",
    }
    atomic_write_json(out / "sample_manifest.json", sample_manifest)

    incremental_dir = out / "incremental"
    incremental_dir.mkdir(parents=True, exist_ok=True)
    patch_entries = [
        *mapping_entries[:-1],
        {
            "v1_rule_key": "v1-semantic-only",
            "v2_rule_key": "v2-semantic-only",
        },
    ]
    atomic_write_json(
        incremental_dir / "mapping-v4.json",
        {
            "format": "mapping-declaration",
            "declaration_generation": 4,
            "direction": "v1_to_v2",
            "conflict_policy": "strict",
            "entries": patch_entries,
        },
    )
    atomic_write_json(
        incremental_dir / "mapping-patch-v4.json",
        {
            "format": "mapping-declaration",
            "declaration_generation": 4,
            "direction": "v1_to_v2",
            "conflict_policy": "strict",
            "entries": [
                {
                    "v1_rule_key": "v1-semantic-only",
                    "v2_rule_key": "v2-semantic-only",
                }
            ],
        },
    )
    atomic_write_json(
        incremental_dir / "sample_manifest.json",
        {
            "format": "firewall-audit-sample",
            "schema_version": 1,
            "sample_id": f"{sample_id}-mapping-patch",
            "seed": seed,
            "observed_clock_ms": OBSERVED_CLOCK_MS,
            "cases": "../cases.jsonl",
            "truth": "../truth.jsonl",
            "v1": "../v1.json",
            "v2_manifest": "../v2/manifest.json",
            "mapping": "mapping-v4.json",
        },
    )
    (out.parent / "state").mkdir(parents=True, exist_ok=True)
    (out.parent / "reports").mkdir(parents=True, exist_ok=True)
    return {
        "ok": True,
        "seed": seed,
        "sample_manifest": str((out / "sample_manifest.json").resolve()),
        "incremental_manifest": str(
            (incremental_dir / "sample_manifest.json").resolve()
        ),
        "sample_id": sample_id,
        "observed_clock_ms": OBSERVED_CLOCK_MS,
        "fingerprint": canonical_hash(sample_manifest),
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m firewall_audit.samplegen",
        description="Generate deterministic firewall audit samples",
    )
    parser.add_argument("--out", default="samples")
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    args = parser.parse_args(argv)
    if args.seed < 0:
        parser.error("--seed must be non-negative")
    print(json.dumps(generate(args.out, args.seed), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
