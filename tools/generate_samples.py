#!/usr/bin/env python3
"""Generate the fixed-seed local verification workspace.

The generator is intentionally dependency-free and never uses system time.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import shutil
import sqlite3
import stat
from pathlib import Path
from typing import Any

DEFAULT_SEED = 20261125
DEFAULT_CLOCK_MS = 1_700_000_000_000


def dump_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


def flow(
    connection_id: str,
    direction: str,
    seq: int,
    logical_ms: int,
    payload: bytes,
    *,
    connection_hint: str | None = None,
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "connection_id": connection_id,
        "direction": direction,
        "seq": seq,
        "logical_ms": logical_ms,
        "payload_hex": payload.hex(),
    }
    if connection_hint is not None:
        record["payload_connection_id"] = connection_hint
    return record


def request(
    method: str,
    target: str,
    headers: list[tuple[str, str]],
    body: bytes = b"",
) -> bytes:
    lines = [f"{method} {target} HTTP/1.1".encode("ascii")]
    lines.extend(f"{name}: {value}".encode("latin-1") for name, value in headers)
    return b"\r\n".join(lines) + b"\r\n\r\n" + body


def build_flows(seed: int, clock_ms: int) -> list[dict[str, Any]]:
    rng = random.Random(seed)
    token = f"{rng.getrandbits(32):08x}"
    normal = request(
        "POST",
        "/echo",
        [
            ("Host", "local"),
            ("Content-Length", "5"),
            ("X-Trace", token),
        ],
        b"hello",
    )
    split = normal.index(b"\r\n\r\n") + 2
    duplicate_cl = request(
        "POST",
        "/duplicate",
        [("Host", "local"), ("Content-Length", "4")],
        b"data",
    )
    chunked = request(
        "POST",
        "/chunk",
        [("Host", "local"), ("Transfer-Encoding", "chunked")],
        b"4\r\nWiki\r\n5\r\npedia\r\n0\r\n\r\n",
    )
    trailer = request(
        "POST",
        "/trailer",
        [("Host", "local"), ("Transfer-Encoding", "chunked")],
        b"4\r\ndata\r\n0\r\nX-Source: sample\r\n\r\n",
    )
    tecl = request(
        "POST",
        "/tecl",
        [("Host", "local"), ("Content-Length", "5")],
        b"hello",
    )
    clean = request(
        "GET",
        "/clean",
        [("Host", "local"), ("X-Control", "clean")],
    )
    gap_one = b"POST /gap HTTP/1.1\r\nHost: local\r\nContent-Length: 4\r\n\r\n"
    gap_three = b"data"
    mixed = request("GET", "/mixed", [("Host", "local")])
    duplicate_delivery = request(
        "GET",
        "/delivery",
        [("Host", "local"), ("X-Duplicate-Delivery", "1")],
    )
    response = (
        b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nOK"
    )

    records = [
        flow("conn-normal", "C2S", 2, clock_ms + 20, normal[split:]),
        flow("conn-normal", "C2S", 1, clock_ms + 10, normal[:split]),
        flow("conn-normal", "S2C", 3, clock_ms + 30, response),
        flow("conn-dup", "C2S", 1, clock_ms + 40, duplicate_cl),
        flow("conn-chunk", "C2S", 1, clock_ms + 50, chunked),
        flow("conn-trailer", "C2S", 1, clock_ms + 60, trailer),
        flow("conn-tecl", "C2S", 1, clock_ms + 70, tecl),
        flow("conn-clean", "C2S", 1, clock_ms + 80, clean),
        flow("conn-gap", "C2S", 1, clock_ms + 90, gap_one),
        flow("conn-gap", "C2S", 3, clock_ms + 92, gap_three),
        flow(
            "conn-mix",
            "C2S",
            1,
            clock_ms + 100,
            mixed,
            connection_hint="another-connection",
        ),
        flow("conn-dupdelivery", "C2S", 1, clock_ms + 110, duplicate_delivery),
        flow("conn-dupdelivery", "C2S", 1, clock_ms + 111, duplicate_delivery),
    ]
    return records


def build_v1(clock_ms: int) -> dict[str, Any]:
    return {
        "rules": [
            {
                "rule_name": "te-cl-observation",
                "relative_effective_seconds": 0,
                "generation": 1,
                "enabled": True,
                "match": (
                    "state=HEADERS;event=HEADERS_DONE;header=content-length;"
                    "also_header=transfer-encoding;action=FLAG;code=D_TE_CL"
                ),
            },
            {
                "rule_name": "duplicate-cl-observation",
                "relative_effective_seconds": 0,
                "generation": 1,
                "enabled": True,
                "match": (
                    "state=HEADERS;event=HEADER_FIELD;header=content-length;"
                    "duplicate_header=true;action=FLAG;code=D_DUP_HEADER"
                ),
            },
            {
                "rule_name": "x-trace-observer",
                "relative_effective_seconds": 0,
                "generation": 1,
                "enabled": True,
                "match": (
                    "state=HEADERS;event=HEADER_FIELD;header=x-trace;"
                    "action=FLAG;code=RULE_FLAG"
                ),
            },
            {
                "rule_name": "obsolete-rule",
                "relative_effective_seconds": 0,
                "generation": 1,
                "enabled": True,
                "match": (
                    "state=HEADERS;event=HEADER_FIELD;header=x-obsolete;"
                    "action=FLAG;code=RULE_FLAG"
                ),
            },
            {
                "rule_name": "chunk-observation-v1",
                "relative_effective_seconds": 0,
                "generation": 1,
                "enabled": True,
                "match": (
                    "state=CHUNK_SIZE;event=CHUNK_SIZE_INVALID;"
                    "action=FLAG;code=D_CHUNK_LENGTH"
                ),
            },
        ]
    }


def build_v2(
    clock_ms: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    shard_a = [
        {
            "canonical_name": "te-cl-observation",
            "display_name": "TE/CL state conflict",
            "generation": 2,
            "effective_from_ms": clock_ms - 1000,
            "effective_to_ms": None,
            "enabled": True,
            "tombstone": False,
            "depends_on": [],
            "transitions": [
                {
                    "states": ["HEADERS"],
                    "on": "HEADERS_DONE",
                    "when": {
                        "header_names_all": [
                            "content-length",
                            "transfer-encoding",
                        ],
                        "framing_pattern": "AMBIGUOUS",
                    },
                    "action": "REJECT",
                    "code": "D_TE_CL",
                }
            ],
        },
        {
            "canonical_name": "duplicate-cl-observation",
            "display_name": "Duplicate CL tombstone",
            "generation": 2,
            "effective_from_ms": clock_ms - 500,
            "effective_to_ms": None,
            "enabled": False,
            "tombstone": True,
            "depends_on": [],
            "transitions": [
                {
                    "states": ["HEADERS"],
                    "on": "HEADER_FIELD",
                    "when": {
                        "header_names_any": ["content-length"],
                        "duplicate_header": True,
                    },
                    "action": "FLAG",
                    "code": "D_DUP_HEADER",
                }
            ],
        },
        {
            "canonical_name": "obsolete-rule",
            "display_name": "Obsolete rule tombstone",
            "generation": 2,
            "effective_from_ms": clock_ms,
            "effective_to_ms": None,
            "enabled": False,
            "tombstone": True,
            "depends_on": [],
            "transitions": [
                {
                    "states": ["HEADERS"],
                    "on": "HEADER_FIELD",
                    "when": {"header_names_any": ["x-obsolete"]},
                    "action": "FLAG",
                    "code": "RULE_FLAG",
                }
            ],
        },
    ]
    shard_b = [
        {
            "canonical_name": "chunk-anomaly",
            "display_name": "Malformed chunk-size",
            "generation": 2,
            "effective_from_ms": clock_ms - 100,
            "effective_to_ms": None,
            "enabled": True,
            "tombstone": False,
            "depends_on": [],
            "transitions": [
                {
                    "states": ["CHUNK_SIZE"],
                    "on": "CHUNK_SIZE_INVALID",
                    "when": {},
                    "action": "REJECT",
                    "code": "D_CHUNK_LENGTH",
                }
            ],
        },
        {
            "canonical_name": "trailer-injection",
            "display_name": "Trailer field observed",
            "generation": 2,
            "effective_from_ms": clock_ms - 100,
            "effective_to_ms": None,
            "enabled": True,
            "tombstone": False,
            "depends_on": ["te-cl-observation"],
            "transitions": [
                {
                    "states": ["TRAILERS"],
                    "on": "TRAILER_FIELD",
                    "when": {"trailer_present": True},
                    "action": "FLAG",
                    "code": "D_TRAILER",
                }
            ],
        },
    ]
    db_rows = []
    for record in shard_a + shard_b:
        db_rows.append(
            {
                "canonical_name": record["canonical_name"],
                "display_name": record["display_name"],
                "generation": record["generation"],
                "effective_from_ms": record["effective_from_ms"],
                "effective_to_ms": record["effective_to_ms"],
                "enabled": int(record["enabled"]),
                "tombstone": int(record["tombstone"]),
                "depends_on": ",".join(record["depends_on"]),
            }
        )
    return shard_a, shard_b, db_rows


def build_mutations(seed: int) -> dict[str, Any]:
    return {
        "seed": seed,
        "mutations": [
            {
                "mutation_id": "m01-te-cl-conflict",
                "connection_id": "conn-tecl",
                "request_index": 0,
                "target": "transfer_encoding",
                "operation": "transfer_encoding",
                "parameters": {"name": "Transfer-Encoding", "value": "chunked"},
                "seed": seed,
            },
            {
                "mutation_id": "m02-duplicate-content-length",
                "connection_id": "conn-dup",
                "request_index": 0,
                "target": "same_name_header",
                "operation": "duplicate_header",
                "parameters": {"name": "Content-Length", "value": "0"},
                "seed": seed,
            },
            {
                "mutation_id": "m03-chunk-length-distortion",
                "connection_id": "conn-chunk",
                "request_index": 0,
                "target": "chunk_length",
                "operation": "chunk_length",
                "parameters": {"index": 0, "value": "4x"},
                "seed": seed,
            },
            {
                "mutation_id": "m04-trailer-injection",
                "connection_id": "conn-trailer",
                "request_index": 0,
                "target": "trailer",
                "operation": "append_trailer",
                "parameters": {"name": "X-Leak", "value": "smuggled"},
                "seed": seed,
            },
            {
                "mutation_id": "m05-boundary-injection",
                "connection_id": "conn-clean",
                "request_index": 0,
                "target": "request_boundary",
                "operation": "request_boundary",
                "parameters": {"action": "append_request"},
                "seed": seed,
            },
            {
                "mutation_id": "m06-flag-control",
                "connection_id": "conn-normal",
                "request_index": 0,
                "target": "header",
                "operation": "noop",
                "parameters": {},
                "seed": seed,
            },
            {
                "mutation_id": "m07-gap",
                "connection_id": "conn-gap",
                "request_index": 0,
                "target": "header",
                "operation": "noop",
                "parameters": {},
                "seed": seed,
            },
            {
                "mutation_id": "m08-delayed-clean",
                "connection_id": "conn-clean",
                "request_index": 0,
                "target": "header",
                "operation": "noop",
                "parameters": {},
                "delay_ms": 120,
                "seed": seed,
            },
            {
                "mutation_id": "m09-duplicate-delivery",
                "connection_id": "conn-dupdelivery",
                "request_index": 0,
                "target": "header",
                "operation": "noop",
                "parameters": {},
                "seed": seed,
            },
            {
                "mutation_id": "m10-mixed-connection",
                "connection_id": "conn-mix",
                "request_index": 0,
                "target": "header",
                "operation": "noop",
                "parameters": {},
                "seed": seed,
            },
        ],
    }


def generate(output: Path, seed: int, clock_ms: int) -> None:
    if output.exists():
        for path in output.rglob("*"):
            try:
                os.chmod(path, stat.S_IWRITE | stat.S_IREAD | stat.S_IEXEC)
            except OSError:
                pass
        shutil.rmtree(output)
    output.mkdir(parents=True)

    meta = {
        "seed": seed,
        "observed_clock_ms": clock_ms,
        "generator_version": 1,
        "echo_profile": {
            "te_cl_policy": "content-length",
            "duplicate_cl_policy": "last",
            "chunk_policy": "lenient",
            "trailer_policy": "ignore",
        },
    }
    dump_json(output / "sample_meta.json", meta)
    with (output / "flows.jsonl").open("w", encoding="utf-8", newline="\n") as handle:
        for record in build_flows(seed, clock_ms):
            handle.write(json.dumps(record, sort_keys=True) + "\n")
    dump_json(output / "rules_v1.json", build_v1(clock_ms))
    shard_a, shard_b, db_rows = build_v2(clock_ms)
    dump_json(output / "rules_v2" / "shard-000.json", {"rules": shard_a})
    dump_json(output / "rules_v2" / "shard-001.json", {"rules": shard_b})
    dump_json(output / "mutations.json", build_mutations(seed))

    database = output / "rules_v2.sqlite"
    connection = sqlite3.connect(database)
    try:
        connection.execute(
            """
            CREATE TABLE rules (
                canonical_name TEXT NOT NULL,
                display_name TEXT NOT NULL,
                generation INTEGER NOT NULL,
                effective_from_ms INTEGER NOT NULL,
                effective_to_ms INTEGER,
                enabled INTEGER NOT NULL,
                tombstone INTEGER NOT NULL,
                depends_on TEXT NOT NULL,
                PRIMARY KEY (canonical_name, generation, effective_from_ms)
            )
            """
        )
        connection.executemany(
            """
            INSERT INTO rules (
                canonical_name, display_name, generation,
                effective_from_ms, effective_to_ms, enabled, tombstone, depends_on
            ) VALUES (
                :canonical_name, :display_name, :generation,
                :effective_from_ms, :effective_to_ms, :enabled, :tombstone, :depends_on
            )
            """,
            db_rows,
        )
        connection.commit()
    finally:
        connection.close()

    for path in sorted(output.rglob("*"), key=lambda item: len(item.parts), reverse=True):
        try:
            if path.is_dir():
                os.chmod(path, stat.S_IREAD | stat.S_IEXEC)
            else:
                os.chmod(path, stat.S_IREAD)
        except OSError:
            pass


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default="samples", type=Path)
    parser.add_argument("--seed", default=DEFAULT_SEED, type=int)
    parser.add_argument("--clock-ms", default=DEFAULT_CLOCK_MS, type=int)
    args = parser.parse_args()
    if args.clock_ms < 0:
        parser.error("--clock-ms must be non-negative")
    generate(args.output, args.seed, args.clock_ms)
    print(f"generated {args.output} (seed={args.seed}, clock_ms={args.clock_ms})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
