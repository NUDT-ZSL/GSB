"""Deterministic, offline sample generator.

The generator is the only place that creates fixture bytes.  All downstream
tests compare engine results with the explicit synchronization markers in
``sync_truth.json`` rather than consulting an external proxy or network.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import sqlite3
from pathlib import Path
from typing import Any

OBSERVED_CLOCK_BASE = 1_700_000_000_000


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _json_bytes(value: Any) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")


def _request(
    *,
    method: str = "POST",
    target: str = "/checkout",
    headers: list[tuple[str, str]],
    body: bytes = b"",
) -> bytes:
    lines = [f"{method} {target} HTTP/1.1"]
    lines.extend(f"{name}: {value}" for name, value in headers)
    return ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1") + body


def _message(
    connection_id: str,
    direction: str,
    sequence: int,
    timestamp_ms: int,
    payload: bytes,
) -> dict[str, Any]:
    marker = (
        "SYNC-"
        + _sha(
            f"{connection_id}|{direction}|{sequence}|{payload.hex()}".encode("utf-8")
        )[:20].upper()
    )
    return {
        "connection_id": connection_id,
        "direction": direction,
        "sequence": sequence,
        "timestamp_ms": timestamp_ms,
        "payload_hex": payload.hex(),
        "sync_marker": marker,
    }


def _standard_payloads() -> dict[str, bytes]:
    clte = _request(
        headers=[
            ("Host", "shop.example"),
            ("Content-Length", "4"),
            ("Transfer-Encoding", "chunked"),
        ],
        body=b"0\r\n\r\n",
    )
    duplicate_conflict = _request(
        headers=[
            ("Host", "shop.example"),
            ("Content-Length", "5"),
            ("Content-Length", "6"),
        ],
        body=b"hello",
    )
    chunk_hex = _request(
        headers=[
            ("Host", "shop.example"),
            ("Transfer-Encoding", "chunked"),
        ],
        body=b"Z5\r\nhello\r\n0\r\n\r\n",
    )
    trailing = _request(
        headers=[
            ("Host", "shop.example"),
            ("Content-Length", "5"),
        ],
        body=b"helloEXTRA-BOUNDARY",
    )
    clean = _request(
        target="/health",
        method="GET",
        headers=[("Host", "shop.example"), ("Content-Length", "0")],
    )
    mixed = _request(
        target="/mixed",
        headers=[("Host", "shop.example"), ("Content-Length", "0")],
    )
    gap_one = _request(
        target="/gap/1",
        headers=[("Host", "shop.example"), ("Content-Length", "0")],
    )
    gap_three = _request(
        target="/gap/3",
        headers=[("Host", "shop.example"), ("Content-Length", "0")],
    )
    response = (
        b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nContent-Type: text/plain\r\n\r\nOK"
    )
    nested_request = b"GET /inner HTTP/1.1\r\nHost: inner.example\r\n\r\n"
    nested = _request(
        target="/outer",
        headers=[
            ("Host", "shop.example"),
            ("Content-Length", "3"),
            ("Transfer-Encoding", "chunked"),
        ],
        body=(
            f"{len(nested_request):x}".encode("ascii")
            + b"\r\n"
            + nested_request
            + b"\r\n0\r\n\r\n"
        ),
    )
    return {
        "clte": clte,
        "duplicate_conflict": duplicate_conflict,
        "chunk_hex": chunk_hex,
        "trailing": trailing,
        "clean": clean,
        "mixed": mixed,
        "gap_one": gap_one,
        "gap_three": gap_three,
        "response": response,
        "nested": nested,
    }


def _build_messages(seed: int, scenario: str) -> tuple[list[dict[str, Any]], int]:
    rng = random.Random(seed)
    observed_clock_ms = OBSERVED_CLOCK_BASE + (seed % 1000)
    payloads = _standard_payloads()
    groups: list[list[dict[str, Any]]] = []

    def make_group(
        connection_id: str,
        items: list[tuple[str, int, bytes]],
        base_offset: int,
    ) -> list[dict[str, Any]]:
        return [
            _message(
                connection_id,
                direction,
                sequence,
                observed_clock_ms + base_offset + index * 17,
                payload,
            )
            for index, (direction, sequence, payload) in enumerate(items)
        ]

    clte_group = make_group(
        "conn-01-clte",
        [
            ("request", 2, payloads["clte"][-4:]),
            ("request", 1, payloads["clte"][:-4]),
        ],
        rng.randrange(0, 100),
    )
    # Duplicate delivery: the first sequence-1 payload remains authoritative.
    clte_group.insert(2, dict(clte_group[1]))
    rng.shuffle(clte_group)
    groups.append(clte_group)
    groups.append(
        make_group(
            "conn-02-duplicate-conflict",
            [("request", 1, payloads["duplicate_conflict"])],
            rng.randrange(100, 200),
        )
    )
    groups.append(
        make_group(
            "conn-03-chunk-hex",
            [("request", 1, payloads["chunk_hex"])],
            rng.randrange(200, 300),
        )
    )
    groups.append(
        make_group(
            "conn-04-trailing",
            [("request", 1, payloads["trailing"])],
            rng.randrange(300, 400),
        )
    )
    groups.append(
        make_group(
            "conn-05-clean",
            [("request", 1, payloads["clean"])],
            rng.randrange(400, 500),
        )
    )
    # Same direction/sequence/bytes under two connections is a deliberate
    # cross-connection mixup marker.
    groups.append(
        make_group(
            "conn-06-mixup-a",
            [("request", 1, payloads["mixed"])],
            rng.randrange(500, 600),
        )
    )
    groups.append(
        make_group(
            "conn-07-mixup-b",
            [("request", 1, payloads["mixed"])],
            rng.randrange(600, 700),
        )
    )
    groups.append(
        make_group(
            "conn-08-gap",
            [("request", 1, payloads["gap_one"]), ("request", 3, payloads["gap_three"])],
            rng.randrange(700, 800),
        )
    )
    groups.append(
        make_group(
            "conn-09-response",
            [("response", 1, payloads["response"])],
            rng.randrange(800, 900),
        )
    )
    groups.append(
        make_group(
            "conn-10-nested-clte",
            [("request", 1, payloads["nested"])],
            rng.randrange(900, 1000),
        )
    )
    # Preserve scenario-level randomness while keeping each connection's
    # logical sequence authoritative.
    rng.shuffle(groups)
    messages = [message for group in groups for message in group]
    return messages, observed_clock_ms


def _v1_document(observed_clock_ms: int) -> dict[str, Any]:
    return {
        "snapshot_id": "v1-fixed-seed",
        "observed_clock_ms": observed_clock_ms,
        "rules": [
            {
                "rule_name": "CL-TE",
                "effective_after_seconds": 10,
                "match": "cl-te-conflict",
                "enabled": True,
            },
            {
                "rule_name": "DUP-HEADER",
                "effective_after_seconds": 5,
                "match": "duplicate-header-conflict",
                "enabled": True,
            },
            {
                "rule_name": "CHUNK.HEX",
                "effective_after_seconds": 8,
                "match": "chunk-size-hex",
                "enabled": True,
            },
            {
                "rule_name": "TRAILING",
                "effective_after_seconds": 12,
                "match": "trailing-bytes",
                "enabled": True,
            },
            {
                "rule_name": "DISABLED-RULE",
                "effective_after_seconds": 1,
                "match": "never-matches",
                "enabled": False,
            },
        ],
    }


def _v2_rules(
    observed_clock_ms: int, scenario: str
) -> list[dict[str, Any]]:
    rules = [
        {
            "canonical_name": "cl-te",
            "effective_at_ms": observed_clock_ms + 30_000,
            "generation": 2,
            "direction": "request",
            "tombstone": True,
            "depends_on": [],
            "match": None,
        },
        {
            "canonical_name": "cl-te.strict",
            "effective_at_ms": observed_clock_ms + 40_000,
            "generation": 3,
            "direction": "request",
            "tombstone": False,
            "depends_on": [],
            "match": "cl-te-conflict",
        },
        {
            "canonical_name": "dup-header",
            "effective_at_ms": observed_clock_ms + 35_000,
            "generation": 2,
            "direction": "request",
            "tombstone": True,
            "depends_on": [],
            "match": None,
        },
        {
            "canonical_name": "dup-header.audit",
            "effective_at_ms": observed_clock_ms + 45_000,
            "generation": 2,
            "direction": "request",
            "tombstone": False,
            "depends_on": ["dup-header"],
            "match": "duplicate-header-conflict",
        },
        {
            "canonical_name": "chunk.hex",
            "effective_at_ms": observed_clock_ms + 20_000,
            "generation": 2,
            "direction": "request",
            "tombstone": False,
            "depends_on": [],
            "match": "chunk-size-hex",
        },
    ]
    if scenario == "cycle":
        rules.extend(
            [
                {
                    "canonical_name": "cycle.a",
                    "effective_at_ms": observed_clock_ms + 60_000,
                    "generation": 1,
                    "direction": "request",
                    "tombstone": False,
                    "depends_on": ["cycle.b"],
                    "match": "cl-te-conflict",
                },
                {
                    "canonical_name": "cycle.b",
                    "effective_at_ms": observed_clock_ms + 61_000,
                    "generation": 1,
                    "direction": "request",
                    "tombstone": False,
                    "depends_on": ["cycle.a"],
                    "match": "trailing-bytes",
                },
            ]
        )
    return rules


def _write_sqlite(path: Path, rules: list[dict[str, Any]]) -> None:
    if path.exists():
        path.unlink()
    connection = sqlite3.connect(str(path))
    try:
        connection.execute(
            """
            CREATE TABLE v2_rules (
                canonical_name TEXT PRIMARY KEY,
                effective_at_ms INTEGER NOT NULL,
                generation INTEGER NOT NULL,
                direction TEXT NOT NULL,
                tombstone INTEGER NOT NULL,
                depends_on TEXT NOT NULL,
                match TEXT,
                enabled INTEGER NOT NULL
            )
            """
        )
        connection.executemany(
            """
            INSERT INTO v2_rules (
                canonical_name, effective_at_ms, generation, direction,
                tombstone, depends_on, match, enabled
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 1)
            """,
            [
                (
                    rule["canonical_name"],
                    rule["effective_at_ms"],
                    rule["generation"],
                    rule["direction"],
                    1 if rule["tombstone"] else 0,
                    json.dumps(rule["depends_on"], separators=(",", ":")),
                    rule["match"],
                )
                for rule in rules
            ],
        )
        connection.commit()
    finally:
        connection.close()


def _truth(
    messages: list[dict[str, Any]],
    observed_clock_ms: int,
    rules: list[dict[str, Any]],
    scenario: str,
) -> dict[str, Any]:
    message_truth = [
        {
            **message,
            "sender": "client" if message["direction"] == "request" else "server",
            "sender_raw_hex": message["payload_hex"],
            "payload_sha256": _sha(bytes.fromhex(message["payload_hex"])),
        }
        for message in messages
    ]
    authority_by_name: dict[str, dict[str, Any]] = {}
    for rule in sorted(rules, key=lambda item: item["canonical_name"]):
        if rule["canonical_name"] == "cl-te":
            expected_source = "rules_v2"
        elif rule["canonical_name"] in {
            "cl-te.strict",
            "dup-header",
            "dup-header.audit",
            "chunk.hex",
            "cycle.a",
            "cycle.b",
        }:
            expected_source = "rules_v2"
        else:
            expected_source = "rules_v1"
        authority_by_name[rule["canonical_name"]] = {
            "canonical_name": rule["canonical_name"],
            "expected_source": expected_source,
            "generation": rule["generation"],
            "effective_at_ms": rule["effective_at_ms"],
            "direction": rule["direction"],
            "tombstone": rule["tombstone"],
            "depends_on": rule["depends_on"],
        }
    authority_by_name.update(
        {
            "trailing": {
                "canonical_name": "trailing",
                "expected_source": "rules_v1",
                "generation": 0,
                "effective_at_ms": observed_clock_ms + 12_000,
                "direction": "both",
                "tombstone": False,
                "depends_on": [],
            },
            "disabled-rule": {
                "canonical_name": "disabled-rule",
                "expected_source": "rules_v1",
                "generation": 0,
                "effective_at_ms": observed_clock_ms + 1_000,
                "direction": "both",
                "tombstone": False,
                "depends_on": [],
                "enabled": False,
            },
        }
    )
    authority = [authority_by_name[name] for name in sorted(authority_by_name)]
    expected_connections = {
        "conn-01-clte": {
            "verdict": "SMUGGLING_DETECTED",
            "business_code": "SMUG_CL_TE",
            "selected_rule": "cl-te.strict",
            "selected_generation": 3,
        },
        "conn-02-duplicate-conflict": {
            "verdict": "DETECTED_UNRULED",
            "business_code": "SMUG_DUP_HEADER",
            "selected_rule": None,
        },
        "conn-03-chunk-hex": {
            "verdict": "SMUGGLING_DETECTED",
            "business_code": "SMUG_CHUNK_HEX",
            "selected_rule": "chunk.hex",
            "selected_generation": 2,
        },
        "conn-04-trailing": {
            "verdict": "SMUGGLING_DETECTED",
            "business_code": "SMUG_TRAILING_BYTES",
            "selected_rule": "trailing",
            "selected_generation": 0,
        },
        "conn-05-clean": {
            "verdict": "CLEAN",
            "business_code": None,
            "selected_rule": None,
        },
        "conn-06-mixup-a": {"verdict": "E_MIXUP", "status": "ERROR"},
        "conn-07-mixup-b": {"verdict": "E_MIXUP", "status": "ERROR"},
        "conn-08-gap": {
            "verdict": "E_MSG_GAP",
            "status": "ERROR",
            "missing_ranges": [[2, 2]],
        },
        "conn-09-response": {
            "verdict": "CLEAN",
            "business_code": None,
            "selected_rule": None,
        },
        "conn-10-nested-clte": {
            "verdict": "SMUGGLING_DETECTED",
            "business_code": "SMUG_CL_TE",
            "selected_rule": "cl-te.strict",
            "selected_generation": 3,
        },
    }
    if scenario == "cycle":
        for value in expected_connections.values():
            value.clear()
            value.update({"verdict": "E_RULE_CYCLE", "status": "ERROR"})
    return {
        "sample_id": f"fixed-seed-{scenario}",
        "scenario": scenario,
        "observed_clock_ms": observed_clock_ms,
        "sync_marker_semantics": (
            "Per-message markers bind connection/direction/sequence to sender bytes; "
            "rule authority markers bind canonical names to snapshot source."
        ),
        "messages": message_truth,
        "rule_authority": authority,
        "expected_connections": expected_connections,
        "expected_invalid_rules": (
            []
            if scenario != "cycle"
            else [
                "cl-te",
                "cycle.a",
                "cycle.b",
                "disabled-rule",
                "dup-header",
                "dup-header.audit",
            ]
        )
        if scenario == "cycle"
        else ["cl-te", "disabled-rule", "dup-header", "dup-header.audit"],
        "expected_affected_connections": (
            []
            if scenario == "cycle"
            else [
                "conn-01-clte",
                "conn-02-duplicate-conflict",
                "conn-10-nested-clte",
            ]
        ),
        "expected_cycle": ["cycle.a", "cycle.b"] if scenario == "cycle" else [],
    }


def generate_sample(
    output_directory: str | Path,
    *,
    seed: int = 20261125,
    scenario: str = "standard",
) -> dict[str, Any]:
    if scenario not in ("standard", "cycle"):
        raise ValueError("scenario must be 'standard' or 'cycle'")
    output = Path(output_directory)
    output.mkdir(parents=True, exist_ok=True)
    shard_directory = output / "rules_v2"
    shard_directory.mkdir(parents=True, exist_ok=True)

    messages, observed_clock_ms = _build_messages(seed, scenario)
    v1 = _v1_document(observed_clock_ms)
    v2_rules = _v2_rules(observed_clock_ms, scenario)
    shard_groups = [v2_rules[:2], v2_rules[2:]]
    shard_paths: list[Path] = []
    for index, entries in enumerate(shard_groups):
        path = shard_directory / f"part-{index:03d}.json"
        path.write_bytes(
            _json_bytes(
                {
                    "snapshot_id": f"v2-fixed-seed-{index}",
                    "rules": entries,
                }
            )
        )
        shard_paths.append(path)
    database = output / "rules_v2.sqlite3"
    _write_sqlite(database, v2_rules)
    (output / "messages.jsonl").write_bytes(
        b"".join(_json_bytes(message) for message in messages)
    )
    (output / "rules_v1.json").write_bytes(_json_bytes(v1))
    truth = _truth(messages, observed_clock_ms, v2_rules, scenario)
    (output / "sync_truth.json").write_bytes(_json_bytes(truth))
    manifest = {
        "sample_id": truth["sample_id"],
        "scenario": scenario,
        "seed": seed,
        "observed_clock_ms": observed_clock_ms,
        "message_count": len(messages),
        "paths": {
            "messages": "messages.jsonl",
            "rules_v1": "rules_v1.json",
            "v2_shards": [str(path.relative_to(output)) for path in shard_paths],
            "v2_database": database.name,
            "sync_truth": "sync_truth.json",
        },
        "message_bytes_sha256": _sha(
            b"".join(bytes.fromhex(message["payload_hex"]) for message in messages)
        ),
        "truth_sha256": _sha((output / "sync_truth.json").read_bytes()),
    }
    (output / "manifest.json").write_bytes(_json_bytes(manifest))
    return manifest


def verify_report_against_truth(
    report: dict[str, Any],
    truth: dict[str, Any],
) -> dict[str, Any]:
    """Compare a report only with generator-issued synchronization markers."""

    mismatches: list[dict[str, Any]] = []
    actual_connections = {
        item.get("connection_id"): item
        for item in report.get("connections", [])
        if isinstance(item, dict)
    }
    for connection_id, expected in truth.get("expected_connections", {}).items():
        actual = actual_connections.get(connection_id)
        if actual is None:
            mismatches.append(
                {"scope": "connection", "connection_id": connection_id, "reason": "missing"}
            )
            continue
        for key, expected_value in expected.items():
            if key == "missing_ranges":
                errors = [
                    item
                    for item in actual.get("errors", [])
                    if item.get("code") == "E_MSG_GAP"
                ]
                observed = []
                if errors:
                    for gap in errors[0].get("details", {}).get(
                        "missing_ranges", []
                    ):
                        observed.extend(gap.get("missing_ranges", []))
                if observed != expected_value:
                    mismatches.append(
                        {
                            "scope": "connection",
                            "connection_id": connection_id,
                            "field": key,
                            "expected": expected_value,
                            "actual": observed,
                        }
                    )
                continue
            actual_value = actual.get(key)
            if actual_value != expected_value:
                mismatches.append(
                    {
                        "scope": "connection",
                        "connection_id": connection_id,
                        "field": key,
                        "expected": expected_value,
                        "actual": actual_value,
                    }
                )

    authority = {
        item.get("canonical_name"): item
        for item in report.get("ruleset", {}).get("entities", [])
        if isinstance(item, dict)
    }
    for expected in truth.get("rule_authority", []):
        entity = authority.get(expected["canonical_name"])
        if entity is None:
            mismatches.append(
                {
                    "scope": "rule_authority",
                    "canonical_name": expected["canonical_name"],
                    "reason": "missing",
                }
            )
            continue
        winner = entity.get("winner", {})
        for truth_key, winner_key in (
            ("expected_source", "source"),
            ("generation", "generation"),
            ("effective_at_ms", "effective_at_ms"),
            ("direction", "direction"),
            ("tombstone", "tombstone"),
        ):
            if winner.get(winner_key) != expected.get(truth_key):
                mismatches.append(
                    {
                        "scope": "rule_authority",
                        "canonical_name": expected["canonical_name"],
                        "field": winner_key,
                        "expected": expected.get(truth_key),
                        "actual": winner.get(winner_key),
                    }
                )

    closure = report.get("closure", {})
    if closure.get("invalid_rules", []) != truth.get("expected_invalid_rules", []):
        mismatches.append(
            {
                "scope": "closure",
                "field": "invalid_rules",
                "expected": truth.get("expected_invalid_rules", []),
                "actual": closure.get("invalid_rules", []),
            }
        )
    if closure.get("affected_connections", []) != truth.get(
        "expected_affected_connections", []
    ):
        mismatches.append(
            {
                "scope": "closure",
                "field": "affected_connections",
                "expected": truth.get("expected_affected_connections", []),
                "actual": closure.get("affected_connections", []),
            }
        )
    expected_cycle = truth.get("expected_cycle", [])
    actual_cycle = (closure.get("cycle") or {}).get("rules", [])
    if actual_cycle != expected_cycle:
        mismatches.append(
            {
                "scope": "closure",
                "field": "cycle",
                "expected": expected_cycle,
                "actual": actual_cycle,
            }
        )

    message_markers = {
        (
            item.get("connection_id"),
            item.get("direction"),
            item.get("sequence"),
        ): item
        for item in truth.get("messages", [])
    }
    for connection_id, connection in actual_connections.items():
        for direction, stream in connection.get("streams", {}).get("directions", {}).items():
            for fragment in stream.get("fragments", []):
                marker = message_markers.get(
                    (connection_id, direction, fragment.get("sequence"))
                )
                if marker is None:
                    mismatches.append(
                        {
                            "scope": "message_sync",
                            "connection_id": connection_id,
                            "direction": direction,
                            "sequence": fragment.get("sequence"),
                            "reason": "no_truth_marker",
                        }
                    )
                elif fragment.get("payload_sha256") != marker.get("payload_sha256"):
                    mismatches.append(
                        {
                            "scope": "message_sync",
                            "connection_id": connection_id,
                            "direction": direction,
                            "sequence": fragment.get("sequence"),
                            "expected": marker.get("payload_sha256"),
                            "actual": fragment.get("payload_sha256"),
                        }
                    )
    return {"ok": not mismatches, "mismatches": mismatches}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate deterministic offline samples")
    parser.add_argument("--out-dir", required=True, help="sample output directory")
    parser.add_argument("--seed", type=int, default=20261125)
    parser.add_argument(
        "--scenario", choices=("standard", "cycle"), default="standard"
    )
    arguments = parser.parse_args(argv)
    manifest = generate_sample(
        arguments.out_dir, seed=arguments.seed, scenario=arguments.scenario
    )
    print(json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

