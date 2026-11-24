"""Deterministic offline sample generator with sender-side truth markers."""

from __future__ import annotations

import argparse
import json
import os
import random
import sqlite3
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from .constants import CORE_PATTERNS
from .io_utils import atomic_write_json, atomic_write_bytes, stable_json_text

DEFAULT_SEED = 1337
RULES_V2_EFFECTIVE_MS = 2000

RULE_SPECS = [
    ("SMUGGLE_CL_TE", "CL_TE", "content-length+transfer-encoding"),
    (
        "SMUGGLE_DUP_HEADER",
        "DUP_HEADER_CONFLICT",
        "duplicate-header-conflict",
    ),
    ("SMUGGLE_CHUNK_SIZE", "CHUNK_SIZE", "chunk-size-malformed"),
    ("SMUGGLE_CHUNK_TRAILER", "CHUNK_TRAILER", "chunk-trailer-invalid"),
    ("SMUGGLE_BOUNDARY", "BOUNDARY_RESIDUE", "boundary-unassigned"),
]


@dataclass
class MessageTruth:
    direction: str
    start_offset: int
    end_offset: int
    sync_id: str
    complete: bool
    header_offsets: dict[str, list[tuple[int, int]]]
    body_start: int

    def boundary_doc(self) -> dict:
        return {
            "direction": self.direction,
            "sync_id": self.sync_id,
            "start_offset": self.start_offset,
            "end_offset": self.end_offset,
            "complete": self.complete,
        }


@dataclass
class StreamBuilder:
    connection_id: str
    direction: str
    data: bytearray = field(default_factory=bytearray)
    messages: list[MessageTruth] = field(default_factory=list)

    def add_message(
        self,
        *,
        start_line: str,
        headers: list[tuple[str, str]],
        body: bytes,
        sync_id: str,
        sync_connection: str | None = None,
        complete: bool = True,
    ) -> MessageTruth:
        start = len(self.data)
        header_offsets: dict[str, list[tuple[int, int]]] = {}
        self.data.extend(start_line.encode("ascii") + b"\r\n")
        all_headers = list(headers)
        sync_headers = [
            ("X-Det-Sync-Id", sync_id),
            ("X-Det-Sync-Conn", sync_connection or self.connection_id),
        ]
        for name, value in sync_headers + all_headers:
            header_start = len(self.data)
            line = f"{name}: {value}\r\n".encode("latin-1")
            self.data.extend(line)
            header_offsets.setdefault(name.lower(), []).append(
                (header_start, header_start + len(line) - 2)
            )
        self.data.extend(b"\r\n")
        body_start = len(self.data)
        self.data.extend(body)
        truth = MessageTruth(
            direction=self.direction,
            start_offset=start,
            end_offset=len(self.data),
            sync_id=sync_id,
            complete=complete,
            header_offsets=header_offsets,
            body_start=body_start,
        )
        self.messages.append(truth)
        return truth

    def prepend(self, raw: bytes) -> None:
        self.data = bytearray(raw) + self.data
        shift = len(raw)
        for message in self.messages:
            message.start_offset += shift
            message.end_offset += shift
            message.body_start += shift
            message.header_offsets = {
                name: [(start + shift, end + shift) for start, end in values]
                for name, values in message.header_offsets.items()
            }


def _chunk(data: bytes, size: int = 7) -> bytes:
    return f"{len(data):x}\r\n".encode("ascii") + data + b"\r\n"


def _chunked_body(chunks: list[bytes], trailer: bytes = b"") -> bytes:
    return b"".join(_chunk(chunk) for chunk in chunks) + b"0\r\n" + trailer + b"\r\n"


def _record(
    connection_id: str,
    direction: str,
    seq: int,
    ts_ms: int,
    payload: bytes,
) -> dict:
    return {
        "connection_id": connection_id,
        "direction": direction,
        "seq": seq,
        "ts_ms": ts_ms,
        "payload_hex": payload.hex(),
    }


def _packetize(
    *,
    connection_id: str,
    direction: str,
    raw: bytes,
    base_ts: int,
    packet_size: int | None = None,
    ts_step: int = 3,
) -> list[dict]:
    chunks: list[bytes] = []
    if packet_size is None or packet_size <= 0:
        chunks = [raw]
    else:
        chunks = [
            raw[index : index + packet_size]
            for index in range(0, len(raw), packet_size)
        ]
    return [
        _record(
            connection_id,
            direction,
            seq,
            base_ts + seq * ts_step,
            payload,
        )
        for seq, payload in enumerate(chunks)
    ]


def _rule_generation(base_ts: int) -> int:
    return 2 if base_ts >= RULES_V2_EFFECTIVE_MS else 1


def _hit(
    code: str,
    *,
    direction: str,
    byte_offset: int,
    sync_id: str | None,
    end_offset: int | None = None,
) -> dict:
    return {
        "code": code,
        "direction": direction,
        "byte_offset": byte_offset,
        "end_offset": end_offset if end_offset is not None else byte_offset,
        "sync_id": sync_id,
    }


def _write_rules(root: Path) -> None:
    v1_rules = [
        {
            "name": name,
            "relative_effective_seconds": 0,
            "patterns": [flat_pattern],
            "enabled": True,
        }
        for name, _, flat_pattern in RULE_SPECS
    ]
    v1_rules.append(
        {
            "name": "SMUGGLE_OBSOLETE",
            "relative_effective_seconds": 0,
            "patterns": ["chunk-trailer-invalid"],
            "enabled": True,
        }
    )
    atomic_write_json(
        root / "rules_v1.json",
        {"relative_epoch_ms": 0, "rules": v1_rules},
    )

    shard_dir = root / "rules_v2_shards"
    shard_dir.mkdir(parents=True, exist_ok=True)
    for stale in shard_dir.glob("*.json"):
        stale.unlink()
    shard_rules = [
        {"canonical_name": name, "patterns": [normalized]}
        for name, normalized, _ in RULE_SPECS
    ]
    atomic_write_json(
        shard_dir / "rules_primary.json",
        {"rules": shard_rules},
    )

    db_path = root / "rules_v2.sqlite3"
    fd, tmp_name = tempfile.mkstemp(
        prefix=".rules_v2.sqlite3.tmp-",
        dir=str(root),
    )
    os.close(fd)
    tmp_path = Path(tmp_name)
    try:
        connection = sqlite3.connect(str(tmp_path))
        try:
            connection.execute(
                "CREATE TABLE rule_meta ("
                "canonical_name TEXT NOT NULL, "
                "effective_ms INTEGER NOT NULL, "
                "generation INTEGER NOT NULL, "
                "direction TEXT NOT NULL, "
                "tombstone INTEGER NOT NULL, "
                "shard_file TEXT NOT NULL, "
                "record_index INTEGER NOT NULL)"
            )
            for index, (name, _, _) in enumerate(RULE_SPECS):
                connection.execute(
                    "INSERT INTO rule_meta VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        name,
                        RULES_V2_EFFECTIVE_MS,
                        2,
                        "c2s",
                        0,
                        "rules_primary.json",
                        index,
                    ),
                )
            connection.execute(
                "INSERT INTO rule_meta VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    "SMUGGLE_OBSOLETE",
                    RULES_V2_EFFECTIVE_MS,
                    2,
                    "c2s",
                    1,
                    "",
                    len(RULE_SPECS),
                ),
            )
            connection.commit()
        finally:
            connection.close()
        os.replace(tmp_path, db_path)
    except BaseException:
        try:
            tmp_path.unlink()
        except OSError:
            pass
        raise


def _write_plan(root: Path) -> dict:
    plan = {
        "concurrency": 4,
        "connection_timeout_ms": 40,
        "batch_budget_ms": 10000,
        "queue_limit": 64,
        "output_path": "report.json",
    }
    atomic_write_json(root / "plan.json", plan)
    return plan


def generate_sample(root: Path, *, seed: int = DEFAULT_SEED) -> dict:
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    _write_rules(root)
    plan = _write_plan(root)

    all_records: list[dict] = []
    truth_connections: dict[str, dict] = {}

    def finish(
        connection_id: str,
        *,
        base_ts: int,
        streams: list[StreamBuilder],
        records: list[dict],
        expected_hits: list[dict] | None = None,
        expected_errors: list[dict] | None = None,
        expected_duplicate_count: int = 0,
    ) -> None:
        all_records.extend(records)
        boundaries = [
            message.boundary_doc()
            for stream in streams
            for message in stream.messages
        ]
        boundaries.sort(
            key=lambda item: (
                item["direction"],
                item["start_offset"],
                item["sync_id"],
            )
        )
        hits = sorted(
            expected_hits or [],
            key=lambda item: (
                item["direction"],
                item["byte_offset"],
                item["code"],
                item["sync_id"] or "",
            ),
        )
        errors = sorted(
            expected_errors or [],
            key=lambda item: (item["direction"], item["code"]),
        )
        truth_connections[connection_id] = {
            "expected_status": "ok",
            "expected_rule_generation": _rule_generation(base_ts),
            "expected_boundaries": boundaries,
            "expected_hits": hits,
            "expected_errors": errors,
            "expected_codes": sorted(
                {item["code"] for item in hits + errors}
            ),
            "expected_duplicate_count": expected_duplicate_count,
        }

    # Clean request/response, packetized at 19 bytes.
    cid = "conn-clean"
    base = 100
    c2s = StreamBuilder(cid, "c2s")
    c2s.add_message(
        start_line="GET /clean HTTP/1.1",
        headers=[("Host", "example.test")],
        body=b"",
        sync_id="clean-c2s-1",
    )
    s2c = StreamBuilder(cid, "s2c")
    s2c.add_message(
        start_line="HTTP/1.1 200 OK",
        headers=[("Content-Length", "5"), ("X-Det-Response", "yes")],
        body=b"hello",
        sync_id="clean-s2c-1",
    )
    records = _packetize(
        connection_id=cid,
        direction="c2s",
        raw=bytes(c2s.data),
        base_ts=base,
        packet_size=19,
    )
    records += _packetize(
        connection_id=cid,
        direction="s2c",
        raw=bytes(s2c.data),
        base_ts=base + 1,
        packet_size=19,
    )
    finish(cid, base_ts=base, streams=[c2s, s2c], records=records)

    # Content-Length + Transfer-Encoding coexistence.
    cid = "conn-cl-te"
    base = 200
    stream = StreamBuilder(cid, "c2s")
    body = _chunked_body([b"smuggled"])
    message = stream.add_message(
        start_line="POST /cl-te HTTP/1.1",
        headers=[
            ("Host", "example.test"),
            ("Content-Length", "8"),
            ("Transfer-Encoding", "chunked"),
        ],
        body=body,
        sync_id="cl-te-1",
    )
    cl_offset = message.header_offsets["content-length"][0][0]
    records = _packetize(
        connection_id=cid,
        direction="c2s",
        raw=bytes(stream.data),
        base_ts=base,
    )
    finish(
        cid,
        base_ts=base,
        streams=[stream],
        records=records,
        expected_hits=[
            _hit("E_CL_TE", direction="c2s", byte_offset=cl_offset, sync_id="cl-te-1")
        ],
    )

    # Conflicting repeated header values.
    cid = "conn-dup-header"
    base = 300
    stream = StreamBuilder(cid, "c2s")
    message = stream.add_message(
        start_line="GET /dup HTTP/1.1",
        headers=[
            ("Host", "example.test"),
            ("X-Duplicate", "alpha"),
            ("X-Duplicate", "beta"),
        ],
        body=b"",
        sync_id="dup-header-1",
    )
    duplicate_offset = message.header_offsets["x-duplicate"][1][0]
    records = _packetize(
        connection_id=cid,
        direction="c2s",
        raw=bytes(stream.data),
        base_ts=base,
    )
    finish(
        cid,
        base_ts=base,
        streams=[stream],
        records=records,
        expected_hits=[
            _hit(
                "E_DUP_HEADER_CONFLICT",
                direction="c2s",
                byte_offset=duplicate_offset,
                sync_id="dup-header-1",
            )
        ],
    )

    # Malformed hexadecimal chunk length.
    cid = "conn-chunk-size"
    base = 400
    stream = StreamBuilder(cid, "c2s")
    message = stream.add_message(
        start_line="POST /chunk-size HTTP/1.1",
        headers=[
            ("Host", "example.test"),
            ("Transfer-Encoding", "chunked"),
        ],
        body=b"Z\r\nbroken\r\n0\r\n\r\n",
        sync_id="chunk-size-1",
        complete=False,
    )
    records = _packetize(
        connection_id=cid,
        direction="c2s",
        raw=bytes(stream.data),
        base_ts=base,
    )
    finish(
        cid,
        base_ts=base,
        streams=[stream],
        records=records,
        expected_hits=[
            _hit(
                "E_CHUNK_SIZE",
                direction="c2s",
                byte_offset=message.body_start,
                sync_id="chunk-size-1",
            )
        ],
    )

    # Invalid chunk trailer line.
    cid = "conn-chunk-trailer"
    base = 2550
    stream = StreamBuilder(cid, "c2s")
    body = b"3\r\nabc\r\n0\r\nX-Ok: yes\r\nBROKEN\r\n\r\n"
    message = stream.add_message(
        start_line="POST /chunk-trailer HTTP/1.1",
        headers=[
            ("Host", "example.test"),
            ("Transfer-Encoding", "chunked"),
        ],
        body=body,
        sync_id="chunk-trailer-1",
    )
    trailer_offset = message.body_start + body.find(b"BROKEN")
    records = _packetize(
        connection_id=cid,
        direction="c2s",
        raw=bytes(stream.data),
        base_ts=base,
    )
    finish(
        cid,
        base_ts=base,
        streams=[stream],
        records=records,
        expected_hits=[
            _hit(
                "E_CHUNK_TRAILER",
                direction="c2s",
                byte_offset=trailer_offset,
                sync_id="chunk-trailer-1",
            )
        ],
    )

    # Unassignable prefix bytes before a valid message.
    cid = "conn-boundary"
    base = 600
    stream = StreamBuilder(cid, "c2s")
    stream.prepend(b"JUNK\r\n")
    stream.add_message(
        start_line="GET /after-prefix HTTP/1.1",
        headers=[("Host", "example.test")],
        body=b"",
        sync_id="boundary-1",
    )
    records = _packetize(
        connection_id=cid,
        direction="c2s",
        raw=bytes(stream.data),
        base_ts=base,
    )
    finish(
        cid,
        base_ts=base,
        streams=[stream],
        records=records,
        expected_hits=[
            _hit(
                "E_BOUNDARY_RESIDUE",
                direction="c2s",
                byte_offset=0,
                end_offset=6,
                sync_id=None,
            )
        ],
    )

    # Missing sequence number in the middle of a directional stream.
    cid = "conn-gap"
    base = 700
    stream = StreamBuilder(cid, "c2s")
    first = stream.add_message(
        start_line="GET /gap-a HTTP/1.1",
        headers=[("Host", "example.test")],
        body=b"",
        sync_id="gap-a",
    )
    second = stream.add_message(
        start_line="GET /gap-b HTTP/1.1",
        headers=[("Host", "example.test")],
        body=b"",
        sync_id="gap-b",
    )
    records = [
        _record(cid, "c2s", 0, base, bytes(stream.data[first.start_offset : first.end_offset])),
        _record(cid, "c2s", 2, base + 6, bytes(stream.data[second.start_offset : second.end_offset])),
    ]
    finish(
        cid,
        base_ts=base,
        streams=[stream],
        records=records,
        expected_errors=[
            {
                "code": "E_MSG_GAP",
                "direction": "c2s",
                "missing_ranges": [[1, 1]],
            }
        ],
    )

    # Exact duplicate delivery; the first logical delivery is authoritative.
    cid = "conn-duplicate"
    base = 800
    stream = StreamBuilder(cid, "c2s")
    stream.add_message(
        start_line="GET /duplicate HTTP/1.1",
        headers=[("Host", "example.test")],
        body=b"",
        sync_id="duplicate-1",
    )
    records = _packetize(
        connection_id=cid,
        direction="c2s",
        raw=bytes(stream.data),
        base_ts=base,
        packet_size=13,
    )
    records.append(dict(records[0]))
    finish(
        cid,
        base_ts=base,
        streams=[stream],
        records=records,
        expected_duplicate_count=1,
    )

    # Records intentionally emitted out of sequence order.
    cid = "conn-out-of-order"
    base = 900
    stream = StreamBuilder(cid, "c2s")
    first = stream.add_message(
        start_line="GET /ooo-a HTTP/1.1",
        headers=[("Host", "example.test")],
        body=b"",
        sync_id="ooo-a",
    )
    second = stream.add_message(
        start_line="GET /ooo-b HTTP/1.1",
        headers=[("Host", "example.test")],
        body=b"",
        sync_id="ooo-b",
    )
    records = [
        _record(cid, "c2s", 0, base, bytes(stream.data[first.start_offset : first.end_offset])),
        _record(cid, "c2s", 1, base + 3, bytes(stream.data[second.start_offset : second.end_offset])),
    ]
    records.reverse()
    finish(cid, base_ts=base, streams=[stream], records=records)

    # Sender synchronization marker names a different connection.
    cid = "conn-mixed"
    base = 1000
    stream = StreamBuilder(cid, "c2s")
    message = stream.add_message(
        start_line="GET /mixed HTTP/1.1",
        headers=[("Host", "example.test")],
        body=b"",
        sync_id="mixed-1",
        sync_connection="conn-other",
    )
    mixed_offset = message.header_offsets["x-det-sync-conn"][0][0]
    records = _packetize(
        connection_id=cid,
        direction="c2s",
        raw=bytes(stream.data),
        base_ts=base,
    )
    finish(
        cid,
        base_ts=base,
        streams=[stream],
        records=records,
        expected_errors=[
            {
                "code": "E_MIXED_CONN",
                "direction": "c2s",
                "byte_offset": mixed_offset,
            }
        ],
    )

    # Next-generation clean rule view.
    cid = "conn-v2-clean"
    base = 2500
    stream = StreamBuilder(cid, "c2s")
    stream.add_message(
        start_line="GET /v2-clean HTTP/1.1",
        headers=[("Host", "example.test")],
        body=b"",
        sync_id="v2-clean-1",
    )
    records = _packetize(
        connection_id=cid,
        direction="c2s",
        raw=bytes(stream.data),
        base_ts=base,
    )
    finish(cid, base_ts=base, streams=[stream], records=records)

    # Next-generation CL+TE hit.
    cid = "conn-v2-cl-te"
    base = 2600
    stream = StreamBuilder(cid, "c2s")
    body = _chunked_body([b"next"])
    message = stream.add_message(
        start_line="POST /v2-cl-te HTTP/1.1",
        headers=[
            ("Host", "example.test"),
            ("Content-Length", "4"),
            ("Transfer-Encoding", "chunked"),
        ],
        body=body,
        sync_id="v2-cl-te-1",
    )
    cl_offset = message.header_offsets["content-length"][0][0]
    records = _packetize(
        connection_id=cid,
        direction="c2s",
        raw=bytes(stream.data),
        base_ts=base,
    )
    finish(
        cid,
        base_ts=base,
        streams=[stream],
        records=records,
        expected_hits=[
            _hit(
                "E_CL_TE",
                direction="c2s",
                byte_offset=cl_offset,
                sync_id="v2-cl-te-1",
            )
        ],
    )

    # Next-generation duplicate-header hit.
    cid = "conn-v2-dup"
    base = 2700
    stream = StreamBuilder(cid, "c2s")
    message = stream.add_message(
        start_line="GET /v2-dup HTTP/1.1",
        headers=[
            ("Host", "example.test"),
            ("X-Duplicate", "one"),
            ("X-Duplicate", "two"),
        ],
        body=b"",
        sync_id="v2-dup-1",
    )
    duplicate_offset = message.header_offsets["x-duplicate"][1][0]
    records = _packetize(
        connection_id=cid,
        direction="c2s",
        raw=bytes(stream.data),
        base_ts=base,
    )
    finish(
        cid,
        base_ts=base,
        streams=[stream],
        records=records,
        expected_hits=[
            _hit(
                "E_DUP_HEADER_CONFLICT",
                direction="c2s",
                byte_offset=duplicate_offset,
                sync_id="v2-dup-1",
            )
        ],
    )

    # Genuine CPU-bound parser workload used to exercise wall-clock timeout.
    cid = "conn-timeout"
    base = 2800
    stream = StreamBuilder(cid, "c2s")
    timeout_body = b"1\r\na\r\n" * 120_000 + b"0\r\n\r\n"
    stream.add_message(
        start_line="POST /slow HTTP/1.1",
        headers=[
            ("Host", "example.test"),
            ("Transfer-Encoding", "chunked"),
        ],
        body=timeout_body,
        sync_id="timeout-1",
    )
    records = _packetize(
        connection_id=cid,
        direction="c2s",
        raw=bytes(stream.data),
        base_ts=base,
    )
    finish(
        cid,
        base_ts=base,
        streams=[stream],
        records=records,
        expected_errors=[{"code": "E_TIMEOUT", "direction": "c2s"}],
    )
    truth_connections[cid]["expected_status"] = "timeout"
    truth_connections[cid]["expected_rule_generation"] = 2
    truth_connections[cid]["expected_codes"] = ["E_TIMEOUT"]

    # The validator only needs these markers plus report output.
    for connection_id in truth_connections:
        truth_connections[connection_id]["expected_codes"] = sorted(
            set(truth_connections[connection_id]["expected_codes"])
        )

    # Deterministic interleaving shuffle.  Sequence numbers remain authoritative.
    rng = random.Random(seed)
    shuffled = list(all_records)
    rng.shuffle(shuffled)
    jsonl = "".join(
        json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n"
        for record in shuffled
    )
    atomic_write_bytes(root / "messages.jsonl", jsonl.encode("utf-8"))

    truth = {
        "schema_version": 1,
        "seed": seed,
        "rules": {
            "v1_generation": 1,
            "v2_generation": 2,
            "v2_effective_ms": RULES_V2_EFFECTIVE_MS,
            "core_patterns": list(CORE_PATTERNS),
        },
        "connections": dict(sorted(truth_connections.items())),
    }
    atomic_write_json(root / "truth.json", truth)
    atomic_write_json(
        root / "manifest.json",
        {
            "seed": seed,
            "message_records": len(all_records),
            "connections": len(truth_connections),
            "plan": plan,
            "deterministic": True,
        },
    )
    return truth


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Generate deterministic offline HTTP smuggling samples"
    )
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    args = parser.parse_args(argv)
    truth = generate_sample(args.out, seed=args.seed)
    print(
        stable_json_text(
            {
                "ok": True,
                "out": str(args.out),
                "seed": args.seed,
                "connections": len(truth["connections"]),
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

