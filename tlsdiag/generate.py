"""Deterministic offline sample generator.

No network, system clock, or external certificate service is used.  Every
fixture is derived from ``seed`` and fixed logical timestamps.  ``_SYNC.json``
is written last and hashes every generated file; tests refuse to consume an
incomplete sample.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import codes
from .fragments import KIND_CERTIFICATE, KIND_HANDSHAKE, make_envelope
from .locks import atomic_write_many
from .models import canonical_json, sha256_hex


def _fp(seed: int, label: str) -> str:
    return "sha256:" + hashlib.sha256(f"tlsdiag|{seed}|{label}".encode("utf-8")).hexdigest()


def _serial(seed: int, label: str) -> str:
    value = int(hashlib.sha256(f"serial|{seed}|{label}".encode("utf-8")).hexdigest()[:24], 16)
    return f"{value or 1:024x}"


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(
        "utf-8"
    )


def _jsonl_bytes(records: Sequence[Dict[str, Any]]) -> bytes:
    return (
        "\n".join(
            json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            for record in records
        )
        + ("\n" if records else "")
    ).encode("utf-8")


class SampleBuilder:
    def __init__(self, seed: int) -> None:
        self.seed = seed
        self.base_ms = 1_700_000_000_000 + (seed % 10_000) * 1_000
        self.observed_ms = self.base_ms + 1_000_000
        self.not_before_ms = self.base_ms - 30 * 86_400_000
        self.not_after_ms = self.base_ms + 365 * 86_400_000
        self.certs: Dict[str, Dict[str, Any]] = {}
        self.fps: Dict[str, str] = {}
        self._build_certificates()

    def _cert(
        self,
        label: str,
        subject: str,
        issuer: str,
        *,
        not_before_ms: Optional[int] = None,
        not_after_ms: Optional[int] = None,
        revoked: bool = False,
        revoked_at_ms: Optional[int] = None,
    ) -> Dict[str, Any]:
        fingerprint = _fp(self.seed, f"cert:{label}")
        self.fps[label] = fingerprint
        certificate = {
            "subject": subject,
            "issuer": issuer,
            "serial_number": _serial(self.seed, label),
            "not_before_ms": self.not_before_ms if not_before_ms is None else not_before_ms,
            "not_after_ms": self.not_after_ms if not_after_ms is None else not_after_ms,
            "public_key_fingerprint": fingerprint,
            "signature": _fp(self.seed, f"signature:{label}"),
            "revoked": bool(revoked),
            "revoked_at_ms": revoked_at_ms,
        }
        self.certs[label] = certificate
        return certificate

    def _build_certificates(self) -> None:
        self._cert("root_alpha", "CN=Root Alpha", "CN=Root Alpha")
        self._cert("root_beta", "CN=Root Beta", "CN=Root Beta")
        self._cert("root_conflict", "CN=Root Conflict", "CN=Root Conflict")
        self._cert("root_tombstone", "CN=Root Tombstone", "CN=Root Tombstone")
        self._cert("root_delta", "CN=Root Delta", "CN=Root Delta")
        self._cert("root_unknown", "CN=Root Unknown", "CN=Root Unknown")

        self._cert("int_ok", "CN=Intermediate OK", "CN=Root Alpha")
        self._cert("int_broken", "CN=Intermediate Broken", "CN=Root Alpha")
        self._cert("int_revoked", "CN=Intermediate Revoked", "CN=Root Alpha")
        self._cert("int_multi", "CN=Intermediate Multi", "CN=Root Alpha")

        self._cert("leaf_ok", "CN=Leaf OK", "CN=Intermediate OK")
        self._cert("leaf_broken", "CN=Leaf Broken", "CN=Wrong Intermediate")
        self._cert("leaf_revoked", "CN=Leaf Revoked", "CN=Intermediate OK")
        self._cert("leaf_child", "CN=Leaf Child", "CN=Intermediate Revoked")
        self._cert(
            "leaf_expired",
            "CN=Leaf Expired",
            "CN=Root Alpha",
            not_after_ms=self.observed_ms - 1,
        )
        self._cert("leaf_delta", "CN=Leaf Delta", "CN=Root Delta")
        self._cert("leaf_multi", "CN=Leaf Multi", "CN=Intermediate Multi")
        self._cert("leaf_tombstone", "CN=Leaf Tombstone", "CN=Root Tombstone")
        self._cert("leaf_conflict", "CN=Leaf Conflict", "CN=Root Conflict")
        self._cert("leaf_untrusted", "CN=Leaf Untrusted", "CN=Root Unknown")

    def chain(self, labels: Sequence[str]) -> List[Dict[str, Any]]:
        return [dict(self.certs[label]) for label in labels]

    def certificate_message(
        self,
        labels: Sequence[str],
        *,
        extra: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "kind": "certificate",
            "observed_time_ms": self.observed_ms,
            "certificates": self.chain(labels),
        }
        if extra:
            payload.update(extra)
        return payload

    def handshake_message(self, connection_id: str) -> Dict[str, Any]:
        return {
            "kind": "handshake",
            "observed_time_ms": self.observed_ms,
            "connection_id": connection_id,
        }

    def fragments(
        self,
        connection_id: str,
        messages: Sequence[Tuple[int, Dict[str, Any]]],
        *,
        start_seq: int = 0,
        chunk_size: int = 17,
    ) -> List[Dict[str, Any]]:
        records: List[Dict[str, Any]] = []
        seq = start_seq
        for message_id, payload in messages:
            data = canonical_json(payload).encode("utf-8")
            kind = KIND_CERTIFICATE if payload.get("kind") == "certificate" else KIND_HANDSHAKE
            offset = 0
            while offset < len(data):
                chunk = data[offset : offset + chunk_size]
                envelope = make_envelope(
                    connection_id=connection_id,
                    message_id=message_id,
                    kind=kind,
                    declared_length=len(data),
                    offset=offset,
                    data=chunk,
                )
                records.append(
                    {
                        "connection_id": connection_id,
                        "direction": "server_to_client",
                        "record_seq": seq,
                        "timestamp_ms": self.observed_ms + seq,
                        "record_type": (
                            "certificate_fragment"
                            if kind == KIND_CERTIFICATE
                            else "handshake_fragment"
                        ),
                        "payload_hex": envelope.hex(),
                    }
                )
                seq += 1
                offset += len(chunk)
        return records

    def connection_records(
        self,
        connection_id: str,
        messages: Sequence[Tuple[int, Dict[str, Any]]],
        *,
        reorder: bool = False,
        drop_seq: Optional[int] = None,
        duplicate_seq: Optional[int] = None,
        mixup: bool = False,
        chunk_size: int = 17,
    ) -> List[Dict[str, Any]]:
        records = self.fragments(connection_id, messages, chunk_size=chunk_size)
        if drop_seq is not None:
            records = [r for r in records if r["record_seq"] != drop_seq]
        if reorder:
            # A deterministic arrival permutation; reassembly must sort by seq.
            records = records[1::2][::-1] + records[0::2]
        if duplicate_seq is not None:
            duplicate = next(r for r in records if r["record_seq"] == duplicate_seq)
            records.append(dict(duplicate))
        if mixup:
            next_seq = max(r["record_seq"] for r in records) + 1
            foreign = make_envelope(
                connection_id="conn-ok",
                message_id=999,
                kind=KIND_HANDSHAKE,
                declared_length=len(b'{"kind":"handshake"}'),
                offset=0,
                data=b'{"kind":"handshake"}',
            )
            records.append(
                {
                    "connection_id": connection_id,
                    "direction": "server_to_client",
                    "record_seq": next_seq,
                    "timestamp_ms": self.observed_ms + next_seq,
                    "record_type": "handshake_fragment",
                    "payload_hex": foreign.hex(),
                }
            )
        return records

    def v1_snapshot(self) -> Dict[str, Any]:
        return {
            "format": "trust-v1",
            "snapshot_version": "v1-2024-demo",
            "epoch_ms": self.base_ms,
            "anchors": [
                {
                    "alias": "Root Alpha",
                    "fingerprint": self.fps["root_alpha"],
                    "trust_seconds": 0,
                    "enabled": True,
                },
                {
                    "alias": "Root Beta",
                    "fingerprint": self.fps["root_beta"],
                    "trust_seconds": 0,
                    "enabled": True,
                },
                {
                    "alias": "root-conflict.example",
                    "fingerprint": self.fps["root_conflict"],
                    "trust_seconds": 0,
                    "enabled": True,
                },
                {
                    "alias": "root-tombstone.example",
                    "fingerprint": self.fps["root_tombstone"],
                    "trust_seconds": 0,
                    "enabled": True,
                },
                {
                    "alias": "root-delta.example",
                    "fingerprint": self.fps["root_delta"],
                    "trust_seconds": 0,
                    "enabled": True,
                },
                {
                    "alias": "",
                    "fingerprint": self.fps["root_alpha"],
                    "trust_seconds": 0,
                    "enabled": True,
                },
                {
                    "alias": "Bad Fingerprint",
                    "fingerprint": "not-a-fingerprint",
                    "trust_seconds": 0,
                    "enabled": True,
                },
                {
                    "alias": "Bad Trust",
                    "fingerprint": self.fps["root_beta"],
                    "trust_seconds": -1,
                    "enabled": True,
                },
                {
                    "alias": "Bad Enabled",
                    "fingerprint": self.fps["root_beta"],
                    "trust_seconds": 0,
                    "enabled": "maybe",
                },
            ],
        }

    def v2_records(self) -> List[Dict[str, Any]]:
        return [
            {
                "canonical_alias": "root-alpha.example",
                "fingerprint": self.fps["root_alpha"],
                "effective_ms": self.base_ms - 10_000,
                "generation": 1,
                "tombstone": False,
                "revoked": False,
                "enabled": True,
            },
            {
                "canonical_alias": "root-beta.example",
                "fingerprint": self.fps["root_beta"],
                "effective_ms": self.base_ms,
                "generation": 1,
                "tombstone": False,
                "revoked": False,
                "enabled": True,
            },
            {
                "canonical_alias": "root-conflict.example",
                "fingerprint": self.fps["root_conflict"],
                "effective_ms": self.base_ms - 1_000,
                "generation": 2,
                "tombstone": False,
                "revoked": False,
                "enabled": True,
            },
            {
                "canonical_alias": "root-tombstone.example",
                "fingerprint": self.fps["root_tombstone"],
                "effective_ms": self.base_ms + 10_000,
                "generation": 3,
                "tombstone": True,
                "revoked": False,
                "enabled": False,
            },
            {
                "canonical_alias": "root-delta.example",
                "fingerprint": self.fps["root_delta"],
                "effective_ms": self.base_ms - 1_000,
                "generation": 2,
                "tombstone": False,
                "revoked": True,
                "revoked_at_ms": self.observed_ms - 5_000,
                "enabled": True,
            },
            {
                "canonical_alias": "leaf-revoked.example",
                "fingerprint": self.fps["leaf_revoked"],
                "effective_ms": self.base_ms - 1_000,
                "generation": 1,
                "tombstone": False,
                "revoked": True,
                "revoked_at_ms": self.observed_ms - 5_000,
                "enabled": True,
            },
            {
                "canonical_alias": "intermediate-revoked.example",
                "fingerprint": self.fps["int_revoked"],
                "effective_ms": self.base_ms - 1_000,
                "generation": 1,
                "tombstone": False,
                "revoked": True,
                "revoked_at_ms": self.observed_ms - 5_000,
                "enabled": True,
            },
            # Invalid v2 entries, retained to prove record-level diagnostics
            # do not abort the rest of the shard.
            {
                "canonical_alias": "UPPER.Example",
                "fingerprint": self.fps["root_alpha"],
                "effective_ms": self.base_ms,
                "generation": 2,
                "tombstone": False,
                "revoked": False,
            },
            {
                "canonical_alias": "bad-time.example",
                "fingerprint": self.fps["root_alpha"],
                "effective_ms": -5,
                "generation": 2,
                "tombstone": False,
                "revoked": False,
            },
            {
                "canonical_alias": "bad-generation.example",
                "fingerprint": self.fps["root_alpha"],
                "effective_ms": self.base_ms,
                "tombstone": False,
                "revoked": False,
            },
            {
                "canonical_alias": "bad-tombstone.example",
                "fingerprint": self.fps["root_alpha"],
                "effective_ms": self.base_ms,
                "generation": 2,
                "tombstone": "maybe",
                "revoked": False,
            },
            {
                "canonical_alias": "bad-fingerprint.example",
                "fingerprint": "sha256:1234",
                "effective_ms": self.base_ms,
                "generation": 2,
                "tombstone": False,
                "revoked": False,
            },
        ]

    def build_sqlite(
        self, shards: Sequence[Tuple[str, bytes]], *, temp_dir: Optional[Path] = None
    ) -> bytes:
        fd, temp_name = tempfile.mkstemp(
            dir=str(temp_dir or Path.cwd()), prefix=".tlsdiag-v2-", suffix=".sqlite3"
        )
        os.close(fd)
        temp_path = Path(temp_name)
        try:
            conn = sqlite3.connect(str(temp_path))
            try:
                conn.execute("PRAGMA journal_mode=DELETE")
                conn.execute(
                    "CREATE TABLE snapshot ("
                    "snapshot_version TEXT NOT NULL, "
                    "merge_policy_version TEXT NOT NULL, "
                    "generated_at_ms INTEGER NOT NULL)"
                )
                conn.execute(
                    "CREATE TABLE shards ("
                    "path TEXT PRIMARY KEY, ordinal INTEGER NOT NULL, "
                    "sha256 TEXT NOT NULL, record_count INTEGER NOT NULL)"
                )
                conn.execute(
                    "INSERT INTO snapshot VALUES (?, ?, ?)",
                    (
                        "v2-2024-demo",
                        codes.TRUST_MERGE_POLICY_VERSION,
                        self.base_ms + 5_000,
                    ),
                )
                for ordinal, (rel_path, data) in enumerate(shards):
                    document = json.loads(data.decode("utf-8"))
                    conn.execute(
                        "INSERT INTO shards VALUES (?, ?, ?, ?)",
                        (
                            rel_path,
                            ordinal,
                            sha256_hex(data),
                            len(document.get("records", [])),
                        ),
                    )
                conn.commit()
            finally:
                conn.close()
            return temp_path.read_bytes()
        finally:
            try:
                temp_path.unlink()
            except OSError:
                pass

    def expected_summary(self) -> Dict[str, Any]:
        root_alpha = self.fps["root_alpha"]
        root_conflict = self.fps["root_conflict"]
        root_unknown = self.fps["root_unknown"]
        root_delta = self.fps["root_delta"]
        root_tombstone = self.fps["root_tombstone"]
        connections: Dict[str, Dict[str, Any]] = {
            "conn-ok": {
                "fragment_status": "ok",
                "trust_conclusion": "trusted",
                "primary_code": None,
                "selected_root": root_alpha,
                "codes_include": [codes.E_FRAG_DUP],
                "duplicate_count": 1,
            },
            "conn-gap": {
                "fragment_status": "gap",
                "trust_conclusion": "indeterminate",
                "primary_code": codes.E_FRAG_GAP,
                "codes_include": [codes.E_FRAG_GAP],
                "missing_sequence_ranges": [[2, 2]],
            },
            "conn-mixup": {
                "fragment_status": "mixup",
                "trust_conclusion": "indeterminate",
                "primary_code": codes.E_MIXUP,
                "codes_include": [codes.E_MIXUP],
            },
            "conn-conflict": {
                "fragment_status": "ok",
                "trust_conclusion": "trusted",
                "primary_code": None,
                "selected_root": root_conflict,
                "codes_include": [],
            },
            "conn-tombstone": {
                "fragment_status": "ok",
                "trust_conclusion": "untrusted_root",
                "primary_code": codes.E_ROOT_UNTRUSTED,
                "selected_root": root_tombstone,
                "codes_include": [codes.E_ROOT_UNTRUSTED],
            },
            "conn-revoked-leaf": {
                "fragment_status": "ok",
                "trust_conclusion": "revoked_leaf",
                "primary_code": codes.E_REVOKED_LEAF,
                "codes_include": [codes.E_REVOKED_LEAF],
            },
            "conn-revoked-intermediate": {
                "fragment_status": "ok",
                "trust_conclusion": "revoked_intermediate",
                "primary_code": codes.E_REVOKED_INTERMEDIATE,
                "codes_include": [codes.E_REVOKED_INTERMEDIATE],
            },
            "conn-revoked-descendant": {
                "fragment_status": "ok",
                "trust_conclusion": "revoked_intermediate",
                "primary_code": codes.E_REVOKED_INTERMEDIATE,
                "codes_include": [codes.E_REVOKED_INTERMEDIATE],
            },
            "conn-expired": {
                "fragment_status": "ok",
                "trust_conclusion": "expired",
                "primary_code": codes.E_CERT_EXPIRED,
                "codes_include": [codes.E_CERT_EXPIRED],
            },
            "conn-chain-empty": {
                "fragment_status": "ok",
                "trust_conclusion": "empty_chain",
                "primary_code": codes.E_CHAIN_EMPTY,
                "codes_include": [codes.E_CHAIN_EMPTY],
            },
            "conn-broken": {
                "fragment_status": "ok",
                "trust_conclusion": "broken_chain",
                "primary_code": codes.E_CHAIN_BROKEN,
                "codes_include": [codes.E_CHAIN_BROKEN],
            },
            "conn-root-untrusted": {
                "fragment_status": "ok",
                "trust_conclusion": "untrusted_root",
                "primary_code": codes.E_ROOT_UNTRUSTED,
                "selected_root": root_unknown,
                "codes_include": [codes.E_ROOT_UNTRUSTED],
            },
            "conn-root-revoked": {
                "fragment_status": "ok",
                "trust_conclusion": "root_revoked",
                "primary_code": codes.E_ROOT_REVOKED,
                "selected_root": root_delta,
                "codes_include": [codes.E_ROOT_REVOKED],
            },
            "conn-multi-chain": {
                "fragment_status": "ok",
                "trust_conclusion": "trusted",
                "primary_code": None,
                "selected_root": root_alpha,
                "candidate_count": 2,
                "codes_include": [],
            },
            "conn-invalid-records": {
                "fragment_status": "ok",
                "trust_conclusion": "trusted",
                "primary_code": None,
                "selected_root": root_alpha,
                "codes_include": [codes.E_DIRECTION],
            },
        }
        return {
            "schema_version": 1,
            "seed": self.seed,
            "base_ms": self.base_ms,
            "observed_ms": self.observed_ms,
            "clock_policy": "sample_declared_observed_time_only",
            "profiles": {
                "default": {
                    "handshake": "input/handshake.jsonl",
                    "v1": "input/trust_v1.json",
                    "v2_shards": "input/trust_v2",
                    "v2_db": "input/trust_v2.sqlite3",
                    "config": {
                        "timeout_ms": 5000,
                        "max_frags_per_connection": 10000,
                        "max_expanded_bytes_per_connection": 16777216,
                        "max_total_expanded_bytes": 268435456,
                    },
                    "connections": connections,
                },
                "budget": {
                    "handshake": "scenarios/budget.jsonl",
                    "v1": "input/trust_v1.json",
                    "v2_shards": "input/trust_v2",
                    "v2_db": "input/trust_v2.sqlite3",
                    "config": {
                        "timeout_ms": 5000,
                        "max_frags_per_connection": 5,
                        "max_expanded_bytes_per_connection": 60000,
                        "max_total_expanded_bytes": 1000000,
                    },
                    "connections": {
                        "conn-frag-budget": {
                            "trust_conclusion": "indeterminate",
                            "primary_code": codes.E_FRAGS,
                            "codes_include": [codes.E_FRAGS],
                        },
                        "conn-byte-budget": {
                            "trust_conclusion": "indeterminate",
                            "primary_code": codes.E_BUDGET,
                            "codes_include": [codes.E_BUDGET],
                        },
                        "conn-budget-fast-ok": {
                            "trust_conclusion": "trusted",
                            "primary_code": None,
                            "selected_root": root_alpha,
                            "codes_include": [],
                        },
                    },
                },
                "timeout": {
                    "handshake": "scenarios/timeout.jsonl",
                    "v1": "input/trust_v1.json",
                    "v2_shards": "input/trust_v2",
                    "v2_db": "input/trust_v2.sqlite3",
                    "config": {
                        "timeout_ms": 1,
                        "max_frags_per_connection": 10000,
                        "max_expanded_bytes_per_connection": 16777216,
                        "max_total_expanded_bytes": 268435456,
                    },
                    "connections": {
                        "conn-heavy-timeout": {
                            "trust_conclusion": "indeterminate",
                            "primary_code": codes.E_TIMEOUT,
                            "codes_include": [codes.E_TIMEOUT],
                        }
                    },
                },
            },
            "trust_expectations": {
                "conflicts_include": [
                    root_alpha,
                    root_conflict,
                    root_tombstone,
                    root_delta,
                    self.fps["root_beta"],
                ],
                "trusted_include": [root_alpha, root_conflict, self.fps["root_beta"]],
                "tombstones_include": [root_tombstone],
                "revoked_include": [
                    root_delta,
                    self.fps["leaf_revoked"],
                    self.fps["int_revoked"],
                ],
                "tombstone_coverage_nonempty": [root_tombstone],
                "v1_invalid_codes_include": [
                    codes.E_V1_ALIAS,
                    codes.E_V1_FP,
                    codes.E_V1_TRUST,
                    codes.E_V1_ENABLED,
                ],
                "v2_invalid_codes_include": [
                    codes.E_V2_ALIAS,
                    codes.E_V2_FP,
                    codes.E_V2_TIME,
                    codes.E_V2_GEN,
                    codes.E_V2_TOMBSTONE,
                ],
            },
            "propagation_expectations": [
                {
                    "fingerprint": self.fps["leaf_revoked"],
                    "role": "leaf",
                    "reason": "revoked",
                    "affected_connections": ["conn-revoked-leaf"],
                },
                {
                    "fingerprint": self.fps["int_revoked"],
                    "role": "intermediate",
                    "reason": "revoked",
                    "affected_connections": [
                        "conn-revoked-descendant",
                        "conn-revoked-intermediate",
                    ],
                },
                {
                    "fingerprint": root_delta,
                    "role": "root",
                    "reason": "revoked",
                    "affected_connections": ["conn-root-revoked"],
                },
                {
                    "fingerprint": self.fps["leaf_expired"],
                    "role": "leaf",
                    "reason": "expired",
                    "affected_connections": ["conn-expired"],
                },
            ],
            "lock_expectation": {"code": codes.E_LOCK},
        }

    def build(self, out_dir: str | Path) -> Dict[str, Any]:
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        files: Dict[str, bytes] = {}

        handshake_records: List[Dict[str, Any]] = []
        handshake_records += self.connection_records(
            "conn-ok",
            [
                (10, self.certificate_message(["leaf_ok", "int_ok", "root_alpha"])),
                (11, self.handshake_message("conn-ok")),
            ],
            reorder=True,
            duplicate_seq=3,
        )
        handshake_records += self.connection_records(
            "conn-gap",
            [
                (20, self.certificate_message(["leaf_ok", "int_ok", "root_alpha"])),
                (21, self.handshake_message("conn-gap")),
            ],
            drop_seq=2,
        )
        handshake_records += self.connection_records(
            "conn-mixup",
            [
                (30, self.certificate_message(["leaf_ok", "int_ok", "root_alpha"])),
                (31, self.handshake_message("conn-mixup")),
            ],
            mixup=True,
        )
        handshake_records += self.connection_records(
            "conn-conflict",
            [
                (40, self.certificate_message(["leaf_conflict", "root_conflict"])),
                (41, self.handshake_message("conn-conflict")),
            ],
        )
        handshake_records += self.connection_records(
            "conn-tombstone",
            [
                (50, self.certificate_message(["leaf_tombstone", "root_tombstone"])),
                (51, self.handshake_message("conn-tombstone")),
            ],
        )
        handshake_records += self.connection_records(
            "conn-revoked-leaf",
            [
                (60, self.certificate_message(["leaf_revoked", "int_ok", "root_alpha"])),
                (61, self.handshake_message("conn-revoked-leaf")),
            ],
        )
        handshake_records += self.connection_records(
            "conn-revoked-intermediate",
            [
                (70, self.certificate_message(["leaf_child", "int_revoked", "root_alpha"])),
                (71, self.handshake_message("conn-revoked-intermediate")),
            ],
        )
        handshake_records += self.connection_records(
            "conn-revoked-descendant",
            [
                (80, self.certificate_message(["leaf_child", "int_revoked", "root_alpha"])),
                (81, self.handshake_message("conn-revoked-descendant")),
            ],
        )
        handshake_records += self.connection_records(
            "conn-expired",
            [
                (90, self.certificate_message(["leaf_expired", "root_alpha"])),
                (91, self.handshake_message("conn-expired")),
            ],
        )
        handshake_records += self.connection_records(
            "conn-chain-empty",
            [
                (100, self.certificate_message([])),
                (101, self.handshake_message("conn-chain-empty")),
            ],
        )
        handshake_records += self.connection_records(
            "conn-broken",
            [
                (110, self.certificate_message(["leaf_broken", "int_broken", "root_alpha"])),
                (111, self.handshake_message("conn-broken")),
            ],
        )
        handshake_records += self.connection_records(
            "conn-root-untrusted",
            [
                (120, self.certificate_message(["leaf_untrusted", "root_unknown"])),
                (121, self.handshake_message("conn-root-untrusted")),
            ],
        )
        handshake_records += self.connection_records(
            "conn-root-revoked",
            [
                (130, self.certificate_message(["leaf_delta", "root_delta"])),
                (131, self.handshake_message("conn-root-revoked")),
            ],
        )
        handshake_records += self.connection_records(
            "conn-multi-chain",
            [
                (140, self.certificate_message(["leaf_multi", "int_multi", "root_unknown"])),
                (141, self.certificate_message(["leaf_multi", "int_multi", "root_alpha"])),
                (142, self.handshake_message("conn-multi-chain")),
            ],
        )
        valid_records = self.connection_records(
            "conn-invalid-records",
            [
                (150, self.certificate_message(["leaf_ok", "int_ok", "root_alpha"])),
                (151, self.handshake_message("conn-invalid-records")),
            ],
        )
        handshake_records.append(
            {
                "connection_id": "conn-invalid-records",
                "direction": "sideways",
                "record_seq": 0,
                "timestamp_ms": self.observed_ms,
                "record_type": "handshake_fragment",
                "payload_hex": "",
            }
        )
        handshake_records += valid_records
        handshake_records.append({"connection_id": "bad id!", "broken": True})
        # A malformed JSON line is global, not owned by a connection.
        handshake_bytes = _jsonl_bytes(handshake_records) + b'{"not valid json"\n'

        files["input/handshake.jsonl"] = handshake_bytes
        files["input/trust_v1.json"] = _json_bytes(self.v1_snapshot())

        v2_records = self.v2_records()
        shard_documents: List[Tuple[str, bytes, int]] = []
        for shard_index in range(3):
            shard_records = [
                record for index, record in enumerate(v2_records) if index % 3 == shard_index
            ]
            document = {
                "snapshot_version": "v2-2024-demo",
                "shard": shard_index,
                "records": shard_records,
            }
            rel = f"part-{shard_index:03d}.json"
            data = _json_bytes(document)
            files[f"input/trust_v2/{rel}"] = data
            # Relative to the shard directory, as stored in SQLite.
            shard_documents.append((rel, data, len(shard_records)))
        db_bytes = self.build_sqlite(
            [(rel, data) for rel, data, _count in shard_documents], temp_dir=out
        )
        files["input/trust_v2.sqlite3"] = db_bytes

        # Budget profile: one over-fragment connection, one over-byte
        # connection, and one fast connection that must still succeed.
        budget_records: List[Dict[str, Any]] = []
        padded = {
            "kind": "handshake",
            "observed_time_ms": self.observed_ms,
            "connection_id": "conn-frag-budget",
            "padding": "x" * 180,
        }
        budget_records += self.fragments(
            "conn-frag-budget", [(200, padded)], chunk_size=1
        )
        huge = self.certificate_message(
            ["leaf_ok", "int_ok", "root_alpha"],
            extra={"padding": "y" * 70000},
        )
        budget_records += self.fragments(
            "conn-byte-budget",
            [(210, huge), (211, self.handshake_message("conn-byte-budget"))],
            chunk_size=50000,
        )
        fast_payload = self.certificate_message(["leaf_ok", "int_ok", "root_alpha"])
        fast_payload_bytes = canonical_json(fast_payload).encode("utf-8")
        budget_records += self.fragments(
            "conn-budget-fast-ok",
            [(220, fast_payload), (221, self.handshake_message("conn-budget-fast-ok"))],
            chunk_size=max(1, len(fast_payload_bytes)),
        )
        files["scenarios/budget.jsonl"] = _jsonl_bytes(budget_records)

        # Timeout profile: a genuinely expensive candidate set.  No sleep is
        # used; the elapsed time comes from the real work performed.
        heavy_certs = [
            self._cert(
                f"heavy_int_{index}",
                f"CN=Heavy Intermediate {index}",
                f"CN=Heavy Intermediate {index + 1}" if index + 1 < 40 else "CN=Root Alpha",
            )
            for index in range(40)
        ]
        heavy_certs.append(dict(self.certs["root_alpha"]))
        candidate_chains = []
        for index in range(2000):
            start = index % 40
            step = 1 + (index // 40) % 7
            length = 20 + (index % 11)
            candidate_chains.append(
                [(start + step * position) % 40 for position in range(length)]
            )
        heavy_payload = {
            "kind": "certificate",
            "observed_time_ms": self.observed_ms,
            "certificates": heavy_certs,
            "candidate_chains": candidate_chains,
        }
        timeout_records = self.fragments(
            "conn-heavy-timeout",
            [(300, heavy_payload), (301, self.handshake_message("conn-heavy-timeout"))],
            chunk_size=4096,
        )
        files["scenarios/timeout.jsonl"] = _jsonl_bytes(timeout_records)

        expected = self.expected_summary()
        files["expected/expected.json"] = _json_bytes(expected)
        files["manifest.json"] = _json_bytes(
            {
                "format": "tlsdiag-deterministic-sample",
                "seed": self.seed,
                "base_ms": self.base_ms,
                "observed_ms": self.observed_ms,
                "files": sorted(files),
                "scenarios": [
                    "out_of_order",
                    "duplicate_fragment",
                    "fragment_gap",
                    "cross_connection_mixup",
                    "v1_v2_conflict",
                    "tombstone_coverage",
                    "leaf_revocation_propagation",
                    "intermediate_revocation_propagation",
                    "certificate_expiry_propagation",
                    "multi_candidate_chain",
                    "fragment_budget",
                    "expanded_byte_budget",
                    "real_timeout",
                    "lock_conflict",
                    "invalid_records_continue",
                ],
            }
        )

        expected["expected_trust_policy"] = codes.TRUST_MERGE_POLICY_VERSION
        expected["expected_report_schema_version"] = codes.REPORT_SCHEMA_VERSION
        expected["truth_digest"] = "sha256:" + sha256_hex(
            canonical_json(
                {key: value for key, value in expected.items() if key != "truth_digest"}
            )
        )
        files["expected/expected.json"] = _json_bytes(expected)

        file_hashes = {
            rel: sha256_hex(data) for rel, data in sorted(files.items())
        }
        marker = "sha256:" + sha256_hex(canonical_json(file_hashes))
        sync = {
            "status": "complete",
            "sync_marker": marker,
            "seed": self.seed,
            "file_count": len(file_hashes) + 1,
            "files": file_hashes,
            "expected_sha256": file_hashes["expected/expected.json"],
            "profiles": {
                name: profile["handshake"]
                for name, profile in expected["profiles"].items()
            },
        }
        files["_SYNC.json"] = _json_bytes(sync)

        # _SYNC.json is the final item in the atomic write set.  If generation
        # is interrupted, an older marker may remain, but its hashes will not
        # match the partially replaced files and verify_sample fails closed.
        atomic_write_many(
            {str(out / rel): data for rel, data in files.items()},
            lock_path=out / ".tlsdiag.lock",
            purpose="sample-generation",
        )
        return {
            "out_dir": str(out),
            "seed": self.seed,
            "sync_marker": marker,
            "file_count": len(files),
            "expected": str(out / "expected/expected.json"),
        }


def generate_sample(out_dir: str | Path, seed: int = 20261125) -> Dict[str, Any]:
    return SampleBuilder(seed).build(out_dir)


def verify_sample(out_dir: str | Path) -> Dict[str, Any]:
    base = Path(out_dir)
    sync_path = base / "_SYNC.json"
    if not sync_path.exists():
        return {
            "ok": False,
            "code": "E_SAMPLE_INCOMPLETE",
            "message": "sync marker is absent",
        }
    try:
        sync = json.loads(sync_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return {
            "ok": False,
            "code": "E_SAMPLE_INCOMPLETE",
            "message": f"sync marker is unreadable: {exc}",
        }
    if sync.get("status") != "complete":
        return {
            "ok": False,
            "code": "E_SAMPLE_INCOMPLETE",
            "message": "sync marker is not complete",
        }
    mismatches: List[str] = []
    for rel, expected_hash in (sync.get("files") or {}).items():
        path = base / rel
        if not path.exists():
            mismatches.append(rel)
            continue
        if sha256_hex(path.read_bytes()) != expected_hash:
            mismatches.append(rel)
    calculated_marker = "sha256:" + sha256_hex(canonical_json(sync.get("files") or {}))
    return {
        "ok": not mismatches and calculated_marker == sync.get("sync_marker"),
        "sync_marker": sync.get("sync_marker"),
        "calculated_marker": calculated_marker,
        "mismatches": sorted(mismatches),
        "file_count": len(sync.get("files") or {}),
    }
