from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

import numpy as np

from .config import (
    ForwardPolicy,
    LinkProfile,
    TunnelSpec,
    WorkbenchConfig,
    parse_policy,
)
from .errors import BusinessError, fail
from .framing import HEADER_SIZE, build_fragment, stream_token
from .util import b64e, canonical_json, sha256_hex

TERMINAL_RUN_STATUSES = {"COMPLETED", "FAILED", "ABORTED"}
RESUMABLE_RUN_STATUSES = {"READY", "PARTIAL", "ACTIVE"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tunnels (
    name TEXT PRIMARY KEY,
    listen_host TEXT NOT NULL,
    listen_port INTEGER NOT NULL,
    transport TEXT NOT NULL,
    outer_mtu INTEGER NOT NULL,
    encapsulation_overhead INTEGER NOT NULL,
    link_weight REAL NOT NULL,
    payload_lengths_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS profiles (
    tunnel TEXT NOT NULL,
    version INTEGER NOT NULL,
    digest TEXT NOT NULL,
    content_json TEXT NOT NULL,
    PRIMARY KEY (tunnel, version),
    FOREIGN KEY (tunnel) REFERENCES tunnels(name)
);

CREATE TABLE IF NOT EXISTS profile_pointers (
    tunnel TEXT PRIMARY KEY,
    version INTEGER NOT NULL,
    digest TEXT NOT NULL,
    FOREIGN KEY (tunnel, version) REFERENCES profiles(tunnel, version)
);

CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    tunnel TEXT NOT NULL,
    payload BLOB NOT NULL,
    payload_index INTEGER NOT NULL,
    payload_length INTEGER NOT NULL,
    payload_digest TEXT NOT NULL,
    stream_token BLOB NOT NULL,
    threshold INTEGER NOT NULL,
    overhead INTEGER NOT NULL,
    initial_mtu INTEGER NOT NULL,
    initial_profile_version INTEGER NOT NULL,
    current_mtu INTEGER NOT NULL,
    next_offset INTEGER NOT NULL,
    next_sequence INTEGER NOT NULL,
    committed_watermark INTEGER NOT NULL,
    logical_clock_ms INTEGER NOT NULL,
    retransmission_count INTEGER NOT NULL,
    status TEXT NOT NULL,
    outcome TEXT,
    business_code TEXT,
    trigger_source TEXT,
    retransmission_generation INTEGER NOT NULL DEFAULT 0,
    reassembly_digest TEXT,
    round_no INTEGER NOT NULL DEFAULT 0,
    bound_profile_version INTEGER NOT NULL,
    abort_requested INTEGER NOT NULL DEFAULT 0,
    transport TEXT NOT NULL,
    created_order INTEGER NOT NULL UNIQUE,
    FOREIGN KEY (tunnel) REFERENCES tunnels(name)
);

CREATE TABLE IF NOT EXISTS rounds (
    round_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    round_no INTEGER NOT NULL,
    profile_version INTEGER NOT NULL,
    profile_digest TEXT NOT NULL,
    started_clock_ms INTEGER NOT NULL,
    deadline_clock_ms INTEGER NOT NULL,
    ended_clock_ms INTEGER,
    aggregation_window_ms REAL NOT NULL,
    previous_mtu INTEGER NOT NULL,
    current_mtu INTEGER NOT NULL,
    adaptation_json TEXT NOT NULL,
    outcome TEXT NOT NULL,
    business_code TEXT,
    UNIQUE (run_id, round_no),
    FOREIGN KEY (run_id) REFERENCES runs(run_id)
);

CREATE TABLE IF NOT EXISTS fragments (
    run_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    profile_version INTEGER NOT NULL,
    offset INTEGER NOT NULL,
    length INTEGER NOT NULL,
    wire BLOB NOT NULL,
    header_digest TEXT NOT NULL,
    fragment_digest TEXT NOT NULL,
    status TEXT NOT NULL,
    attempts INTEGER NOT NULL,
    retransmission_generation INTEGER NOT NULL,
    trigger_source TEXT NOT NULL,
    first_sent_clock_ms INTEGER NOT NULL,
    last_sent_clock_ms INTEGER NOT NULL,
    acked_clock_ms INTEGER,
    PRIMARY KEY (run_id, seq),
    FOREIGN KEY (run_id) REFERENCES runs(run_id)
);

CREATE INDEX IF NOT EXISTS idx_fragments_status ON fragments(run_id, status, seq);
CREATE INDEX IF NOT EXISTS idx_runs_tunnel ON runs(tunnel, created_order);

CREATE TABLE IF NOT EXISTS confirmations (
    run_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    profile_version INTEGER NOT NULL,
    confirmed_clock_ms INTEGER NOT NULL,
    PRIMARY KEY (run_id, seq),
    FOREIGN KEY (run_id, seq) REFERENCES fragments(run_id, seq)
);
"""


def _rowdict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


class Storage:
    def __init__(self, db_path: str | Path, lock_dir: str | Path | None = None) -> None:
        self.db_path = Path(db_path)
        self.lock_dir = Path(lock_dir) if lock_dir is not None else self.db_path.parent / "locks"

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            self.db_path,
            timeout=10.0,
            isolation_level=None,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 10000")
        return conn

    def initialize(self) -> None:
        try:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            self.lock_dir.mkdir(parents=True, exist_ok=True)
            conn = self._connect()
        except (OSError, sqlite3.Error) as exc:
            fail(
                "E_STORAGE_UNAVAILABLE",
                "SQLite database or lock directory is not writable",
                details={"db_path": str(self.db_path), "reason": str(exc)},
                http_status=500,
            )
        try:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = FULL")
            conn.executescript(SCHEMA)
        except sqlite3.Error as exc:
            fail(
                "E_STORAGE_UNAVAILABLE",
                "SQLite schema initialization failed",
                details={"db_path": str(self.db_path), "reason": str(exc)},
                http_status=500,
            )
        finally:
            conn.close()

    def has_schema(self) -> bool:
        if not self.db_path.exists():
            return False
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='runs'"
            ).fetchone()
            return row is not None
        finally:
            conn.close()

    @contextmanager
    def transaction(self, *, immediate: bool = True) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            try:
                yield conn
            except Exception:
                conn.rollback()
                raise
            else:
                conn.commit()
        finally:
            conn.close()

    @contextmanager
    def read_snapshot(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            conn.execute("BEGIN")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def install_config(self, config: WorkbenchConfig) -> dict[str, Any]:
        self.initialize()
        installed: list[dict[str, Any]] = []
        with self.transaction() as conn:
            for tunnel in config.tunnels:
                conn.execute(
                    """
                    INSERT INTO tunnels(
                        name, listen_host, listen_port, transport, outer_mtu,
                        encapsulation_overhead, link_weight, payload_lengths_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(name) DO UPDATE SET
                        listen_host=excluded.listen_host,
                        listen_port=excluded.listen_port,
                        transport=excluded.transport,
                        outer_mtu=excluded.outer_mtu,
                        encapsulation_overhead=excluded.encapsulation_overhead,
                        link_weight=excluded.link_weight,
                        payload_lengths_json=excluded.payload_lengths_json
                    """,
                    (
                        tunnel.name,
                        tunnel.listen_host,
                        tunnel.listen_port,
                        tunnel.transport,
                        tunnel.outer_mtu,
                        tunnel.encapsulation_overhead,
                        tunnel.link_weight,
                        canonical_json(list(tunnel.payload_lengths)),
                    ),
                )
            by_tunnel: dict[str, list[LinkProfile]] = {}
            for profile in config.profiles:
                by_tunnel.setdefault(profile.tunnel, []).append(profile)
                existing = conn.execute(
                    "SELECT digest FROM profiles WHERE tunnel=? AND version=?",
                    (profile.tunnel, profile.version),
                ).fetchone()
                if existing is not None and existing["digest"] != profile.digest:
                    fail(
                        "E_PROFILE_VERSION_CONFLICT",
                        "profile version already exists with different content",
                        details={"tunnel": profile.tunnel, "version": profile.version},
                        http_status=409,
                    )
                conn.execute(
                    """
                    INSERT OR IGNORE INTO profiles(tunnel, version, digest, content_json)
                    VALUES (?, ?, ?, ?)
                    """,
                    (
                        profile.tunnel,
                        profile.version,
                        profile.digest,
                        canonical_json(profile.as_dict()),
                    ),
                )
            for tunnel_name, profiles in sorted(by_tunnel.items()):
                incoming = max(profiles, key=lambda profile: profile.version)
                pointer = conn.execute(
                    "SELECT version, digest FROM profile_pointers WHERE tunnel=?",
                    (tunnel_name,),
                ).fetchone()
                if pointer is not None:
                    if incoming.version < pointer["version"]:
                        fail(
                            "E_PROFILE_ROLLBACK",
                            "configuration attempts to move the profile pointer backwards",
                            details={
                                "tunnel": tunnel_name,
                                "current_version": pointer["version"],
                                "incoming_version": incoming.version,
                            },
                            http_status=409,
                        )
                    if (
                        incoming.version == pointer["version"]
                        and incoming.digest != pointer["digest"]
                    ):
                        fail(
                            "E_PROFILE_VERSION_CONFLICT",
                            "current profile pointer has conflicting content",
                            details={"tunnel": tunnel_name, "version": incoming.version},
                            http_status=409,
                        )
                conn.execute(
                    """
                    INSERT INTO profile_pointers(tunnel, version, digest)
                    VALUES (?, ?, ?)
                    ON CONFLICT(tunnel) DO UPDATE SET
                        version=excluded.version,
                        digest=excluded.digest
                    WHERE excluded.version > profile_pointers.version
                    """,
                    (tunnel_name, incoming.version, incoming.digest),
                )
                installed.append(
                    {
                        "tunnel": tunnel_name,
                        "profile_version": incoming.version,
                        "profile_digest": incoming.digest,
                    }
                )
            conn.execute(
                """
                INSERT INTO meta(key, value) VALUES('policy_json', ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value
                """,
                (canonical_json(config.policy.as_dict()),),
            )
            conn.execute(
                """
                INSERT INTO meta(key, value) VALUES('config_digest', ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value
                """,
                (config.content_digest,),
            )
        return {"installed": installed, "config_digest": config.content_digest}

    def tunnel_spec(self, name: str) -> TunnelSpec:
        with self.read_snapshot() as conn:
            row = conn.execute("SELECT * FROM tunnels WHERE name=?", (name,)).fetchone()
        if row is None:
            fail("E_TUNNEL_NOT_FOUND", "tunnel is not loaded", details={"tunnel": name})
        return TunnelSpec(
            name=row["name"],
            listen_host=row["listen_host"],
            listen_port=row["listen_port"],
            transport=row["transport"],
            outer_mtu=row["outer_mtu"],
            encapsulation_overhead=row["encapsulation_overhead"],
            link_weight=row["link_weight"],
            payload_lengths=tuple(json.loads(row["payload_lengths_json"])),
        )

    def policy(self) -> ForwardPolicy:
        with self.read_snapshot() as conn:
            row = conn.execute("SELECT value FROM meta WHERE key='policy_json'").fetchone()
        if row is None:
            fail("E_CONFIG_NOT_LOADED", "no forwarding policy is loaded")
        return parse_policy(json.loads(row["value"]))

    def current_profile(self, tunnel: str) -> LinkProfile:
        with self.read_snapshot() as conn:
            row = conn.execute(
                """
                SELECT p.* FROM profile_pointers pp
                JOIN profiles p ON p.tunnel=pp.tunnel AND p.version=pp.version
                WHERE pp.tunnel=?
                """,
                (tunnel,),
            ).fetchone()
        if row is None:
            fail("E_PROFILE_NOT_FOUND", "tunnel has no current profile", details={"tunnel": tunnel})
        return self._profile_from_row(row)

    def profile_by_version(self, tunnel: str, version: int) -> LinkProfile | None:
        with self.read_snapshot() as conn:
            row = conn.execute(
                "SELECT * FROM profiles WHERE tunnel=? AND version=?",
                (tunnel, version),
            ).fetchone()
        return self._profile_from_row(row) if row is not None else None

    @staticmethod
    def _profile_from_row(row: sqlite3.Row) -> LinkProfile:
        content = json.loads(row["content_json"])
        return LinkProfile(
            tunnel=content["tunnel"],
            version=content["version"],
            rtt_ms=content["rtt_ms"],
            loss_rate=content["loss_rate"],
            bandwidth_kbps=content["bandwidth_kbps"],
            jitter_ms=content["jitter_ms"],
        )

    def replace_profile(self, profile: LinkProfile) -> dict[str, Any]:
        self.initialize()
        with self.transaction() as conn:
            pointer = conn.execute(
                "SELECT version, digest FROM profile_pointers WHERE tunnel=?",
                (profile.tunnel,),
            ).fetchone()
            if pointer is not None and profile.version <= pointer["version"]:
                fail(
                    "E_PROFILE_ROLLBACK",
                    "profile replacement must advance the version",
                    details={
                        "tunnel": profile.tunnel,
                        "current_version": pointer["version"],
                        "incoming_version": profile.version,
                    },
                    http_status=409,
                )
            existing = conn.execute(
                "SELECT digest FROM profiles WHERE tunnel=? AND version=?",
                (profile.tunnel, profile.version),
            ).fetchone()
            if existing is not None and existing["digest"] != profile.digest:
                fail(
                    "E_PROFILE_VERSION_CONFLICT",
                    "the requested profile version already has different content",
                    details={"tunnel": profile.tunnel, "version": profile.version},
                    http_status=409,
                )
            conn.execute(
                """
                INSERT OR IGNORE INTO profiles(tunnel, version, digest, content_json)
                VALUES (?, ?, ?, ?)
                """,
                (
                    profile.tunnel,
                    profile.version,
                    profile.digest,
                    canonical_json(profile.as_dict()),
                ),
            )
            conn.execute(
                """
                INSERT INTO profile_pointers(tunnel, version, digest)
                VALUES (?, ?, ?)
                ON CONFLICT(tunnel) DO UPDATE SET
                    version=excluded.version,
                    digest=excluded.digest
                """,
                (profile.tunnel, profile.version, profile.digest),
            )
        return {
            "tunnel": profile.tunnel,
            "previous_version": pointer["version"] if pointer else None,
            "profile_version": profile.version,
            "profile_digest": profile.digest,
        }

    def create_or_get_run(
        self,
        *,
        tunnel: str,
        payload: bytes,
        payload_index: int,
        transport_override: str | None = None,
    ) -> dict[str, Any]:
        spec = self.tunnel_spec(tunnel)
        if payload_index < 0 or payload_index >= len(spec.payload_lengths):
            fail(
                "E_PAYLOAD_INDEX",
                "payload index is outside the configured distribution",
                details={
                    "tunnel": tunnel,
                    "payload_index": payload_index,
                    "available": len(spec.payload_lengths),
                },
            )
        if len(payload) != spec.payload_lengths[payload_index]:
            fail(
                "E_PAYLOAD_LENGTH_INVALID",
                "payload length does not match the selected distribution entry",
                details={
                    "expected": spec.payload_lengths[payload_index],
                    "actual": len(payload),
                },
            )
        profile = self.current_profile(tunnel)
        policy = self.policy()
        transport = transport_override or spec.transport
        if transport not in ("udp", "tcp"):
            fail("E_TRANSPORT_INVALID", "transport must be udp or tcp")
        payload_digest = sha256_hex(payload)
        run_id = sha256_hex(
            canonical_json(
                {
                    "tunnel": tunnel,
                    "payload_digest": payload_digest,
                    "payload_index": payload_index,
                    "initial_mtu": spec.outer_mtu,
                    "threshold": policy.fragment_threshold,
                    "overhead": spec.encapsulation_overhead,
                    "initial_profile_version": profile.version,
                    "transport": transport,
                }
            )
        )[:32]
        stream = stream_token(
            tunnel,
            payload_digest,
            spec.outer_mtu,
            policy.fragment_threshold,
            spec.encapsulation_overhead,
            profile.version,
            run_id,
        )
        with self.transaction() as conn:
            existing = conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
            if existing is not None:
                return self._decorate_run(conn, existing)
            order = conn.execute(
                "SELECT COALESCE(MAX(created_order), 0) + 1 AS value FROM runs"
            ).fetchone()["value"]
            conn.execute(
                """
                INSERT INTO runs(
                    run_id, tunnel, payload, payload_index, payload_length, payload_digest,
                    stream_token, threshold, overhead, initial_mtu, initial_profile_version,
                    current_mtu, next_offset, next_sequence, committed_watermark,
                    logical_clock_ms, retransmission_count, status, outcome, business_code,
                    trigger_source, retransmission_generation, reassembly_digest, round_no,
                    bound_profile_version, abort_requested, transport, created_order
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0, 0, 0, 0,
                          'READY', NULL, NULL, NULL, 0, NULL, 0, ?, 0, ?, ?)
                """,
                (
                    run_id,
                    tunnel,
                    payload,
                    payload_index,
                    len(payload),
                    payload_digest,
                    stream,
                    policy.fragment_threshold,
                    spec.encapsulation_overhead,
                    spec.outer_mtu,
                    profile.version,
                    spec.outer_mtu,
                    profile.version,
                    transport,
                    order,
                ),
            )
            row = conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
            return self._decorate_run(conn, row)

    def _decorate_run(self, conn: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
        data = dict(row)
        data.pop("payload", None)
        stream = data.pop("stream_token", None)
        data["stream_token_hex"] = bytes(stream).hex() if stream is not None else None
        fragments = conn.execute(
            "SELECT seq, status, attempts, retransmission_generation FROM fragments "
            "WHERE run_id=? ORDER BY seq",
            (row["run_id"],),
        ).fetchall()
        data["fragment_count"] = len(fragments)
        data["committed_fragments"] = sum(
            1 for fragment in fragments if fragment["status"] == "COMMITTED"
        )
        data["inflight_sequences"] = [
            fragment["seq"] for fragment in fragments if fragment["status"] == "INFLIGHT"
        ]
        data["retransmission_count"] = row["retransmission_count"]
        data["profile_version"] = self._current_pointer_version(conn, row["tunnel"])
        return data

    @staticmethod
    def _current_pointer_version(conn: sqlite3.Connection, tunnel: str) -> int | None:
        row = conn.execute(
            "SELECT version FROM profile_pointers WHERE tunnel=?", (tunnel,)
        ).fetchone()
        return row["version"] if row else None

    def get_run(self, run_id: str) -> dict[str, Any]:
        with self.read_snapshot() as conn:
            row = conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
            if row is None:
                fail("E_RUN_NOT_FOUND", "forwarding run was not found", details={"run_id": run_id})
            return self._decorate_run(conn, row)

    def get_run_payload(self, run_id: str) -> bytes:
        with self.read_snapshot() as conn:
            row = conn.execute("SELECT payload FROM runs WHERE run_id=?", (run_id,)).fetchone()
        if row is None:
            fail("E_RUN_NOT_FOUND", "forwarding run was not found", details={"run_id": run_id})
        return bytes(row["payload"])

    def profiles_for_tunnel(self, tunnel: str) -> list[dict[str, Any]]:
        with self.read_snapshot() as conn:
            rows = conn.execute(
                "SELECT * FROM profiles WHERE tunnel=? ORDER BY version", (tunnel,)
            ).fetchall()
            pointer = conn.execute(
                "SELECT version FROM profile_pointers WHERE tunnel=?", (tunnel,)
            ).fetchone()
        current = pointer["version"] if pointer else None
        return [
            {
                "tunnel": row["tunnel"],
                "version": row["version"],
                "digest": row["digest"],
                "current": row["version"] == current,
                "content": json.loads(row["content_json"]),
            }
            for row in rows
        ]

    def latest_resumable_run(self, tunnel: str) -> dict[str, Any]:
        with self.read_snapshot() as conn:
            row = conn.execute(
                """
                SELECT * FROM runs
                WHERE tunnel=? AND status IN ('READY', 'PARTIAL', 'ACTIVE')
                ORDER BY created_order DESC LIMIT 1
                """,
                (tunnel,),
            ).fetchone()
            if row is None:
                fail(
                    "E_NO_RESUMABLE_RUN",
                    "tunnel has no resumable forwarding run",
                    details={"tunnel": tunnel},
                    http_status=404,
                )
            return self._decorate_run(conn, row)

    def beginning_round(self, run_id: str) -> dict[str, Any]:
        with self.transaction() as conn:
            run = conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
            if run is None:
                fail("E_RUN_NOT_FOUND", "forwarding run was not found", details={"run_id": run_id})
            if run["status"] in TERMINAL_RUN_STATUSES:
                fail(
                    "E_RUN_TERMINAL",
                    "forwarding run is already in a terminal state",
                    details={"run_id": run_id, "status": run["status"]},
                    http_status=409,
                )
            if run["abort_requested"]:
                self._abort_in_transaction(conn, run, run["logical_clock_ms"])
                fail(
                    "E_ABORT_REQUESTED",
                    "active abort was requested before the round started",
                    details={"run_id": run_id},
                    http_status=409,
                )
            tunnel = conn.execute(
                "SELECT * FROM tunnels WHERE name=?", (run["tunnel"],)
            ).fetchone()
            pointer = conn.execute(
                "SELECT p.* FROM profile_pointers pp "
                "JOIN profiles p ON p.tunnel=pp.tunnel AND p.version=pp.version "
                "WHERE pp.tunnel=?",
                (run["tunnel"],),
            ).fetchone()
            previous = conn.execute(
                "SELECT * FROM profiles WHERE tunnel=? AND version=?",
                (run["tunnel"], run["bound_profile_version"]),
            ).fetchone()
            if pointer is None or previous is None:
                fail(
                    "E_PROFILE_NOT_FOUND",
                    "round cannot bind a complete profile snapshot",
                    details={"run_id": run_id},
                )
            new_profile = self._profile_from_row(pointer)
            old_profile = self._profile_from_row(previous)
            current_mtu, adaptation = self._adapt_mtu(
                current_mtu=run["current_mtu"],
                old_profile=old_profile,
                new_profile=new_profile,
                overhead=run["overhead"],
            )
            if current_mtu <= HEADER_SIZE + run["overhead"]:
                fail(
                    "E_MTU_EXHAUSTED",
                    "adaptive MTU leaves no room for fragment payload",
                    details={
                        "current_mtu": current_mtu,
                        "header_size": HEADER_SIZE,
                        "overhead": run["overhead"],
                    },
                )
            round_no = run["round_no"] + 1
            deadline = run["logical_clock_ms"] + self.policy_from_conn(conn).logical_budget_ms
            conn.execute(
                """
                INSERT INTO rounds(
                    run_id, round_no, profile_version, profile_digest, started_clock_ms,
                    deadline_clock_ms, aggregation_window_ms, previous_mtu, current_mtu,
                    adaptation_json, outcome
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'ACTIVE')
                """,
                (
                    run_id,
                    round_no,
                    new_profile.version,
                    new_profile.digest,
                    run["logical_clock_ms"],
                    deadline,
                    new_profile.rtt_ms + new_profile.jitter_ms,
                    run["current_mtu"],
                    current_mtu,
                    canonical_json(adaptation),
                ),
            )
            conn.execute(
                """
                UPDATE runs SET status='ACTIVE', round_no=?, bound_profile_version=?,
                    current_mtu=? WHERE run_id=?
                """,
                (round_no, new_profile.version, current_mtu, run_id),
            )
            round_row = conn.execute(
                "SELECT * FROM rounds WHERE run_id=? AND round_no=?", (run_id, round_no)
            ).fetchone()
            updated = conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
            return {
                "run": self._decorate_run(conn, updated),
                "profile": new_profile,
                "round": dict(round_row),
                "adaptation": adaptation,
                "tunnel": self._tunnel_from_row(tunnel),
            }

    @staticmethod
    def policy_from_conn(conn: sqlite3.Connection) -> ForwardPolicy:
        row = conn.execute("SELECT value FROM meta WHERE key='policy_json'").fetchone()
        if row is None:
            fail("E_CONFIG_NOT_LOADED", "no forwarding policy is loaded")
        return parse_policy(json.loads(row["value"]))

    @staticmethod
    def _adapt_mtu(
        *,
        current_mtu: int,
        old_profile: LinkProfile,
        new_profile: LinkProfile,
        overhead: int,
    ) -> tuple[int, dict[str, Any]]:
        if new_profile.version == old_profile.version:
            return current_mtu, {
                "triggered": False,
                "reasons": [],
                "previous_profile_version": old_profile.version,
                "profile_version": new_profile.version,
                "previous_mtu": current_mtu,
                "current_mtu": current_mtu,
            }
        factor = 1.0
        reasons: list[str] = []
        if new_profile.rtt_ms > old_profile.rtt_ms:
            factor *= old_profile.rtt_ms / new_profile.rtt_ms
            reasons.append("RTT_INCREASED")
        if new_profile.loss_rate > old_profile.loss_rate:
            factor *= 1.0 - min(0.5, new_profile.loss_rate - old_profile.loss_rate)
            reasons.append("LOSS_RATE_INCREASED")
        if factor < 1.0:
            base_capacity = max(1, current_mtu - HEADER_SIZE - overhead)
            adapted_capacity = max(1, int(base_capacity * factor))
            candidate = HEADER_SIZE + overhead + adapted_capacity
            candidate = min(candidate, current_mtu)
        else:
            candidate = current_mtu
        return candidate, {
            "triggered": candidate < current_mtu,
            "reasons": reasons,
            "previous_profile_version": old_profile.version,
            "profile_version": new_profile.version,
            "previous_mtu": current_mtu,
            "current_mtu": candidate,
            "factor": factor,
        }

    @staticmethod
    def _tunnel_from_row(row: sqlite3.Row) -> TunnelSpec:
        return TunnelSpec(
            name=row["name"],
            listen_host=row["listen_host"],
            listen_port=row["listen_port"],
            transport=row["transport"],
            outer_mtu=row["outer_mtu"],
            encapsulation_overhead=row["encapsulation_overhead"],
            link_weight=row["link_weight"],
            payload_lengths=tuple(json.loads(row["payload_lengths_json"])),
        )

    def allocate_new_fragment(
        self,
        *,
        run_id: str,
        send_clock_ms: int,
        trigger_source: str,
        max_inflight: int,
    ) -> dict[str, Any]:
        with self.transaction() as conn:
            run = conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
            if run is None:
                fail("E_RUN_NOT_FOUND", "forwarding run was not found", details={"run_id": run_id})
            self._check_active_for_send(conn, run)
            inflight = conn.execute(
                "SELECT COUNT(*) AS count FROM fragments WHERE run_id=? AND status='INFLIGHT'",
                (run_id,),
            ).fetchone()["count"]
            if inflight >= max_inflight:
                fail(
                    "E_CONCURRENCY_LIMIT",
                    "in-flight fragment limit would be exceeded",
                    details={"inflight": inflight, "limit": max_inflight},
                    http_status=409,
                )
            if run["next_offset"] >= run["payload_length"]:
                fail(
                    "E_NO_UNSENT_PAYLOAD",
                    "all payload bytes have already been allocated to fragments",
                )
            chunk_size = min(
                run["threshold"],
                run["current_mtu"] - HEADER_SIZE - run["overhead"],
            )
            if chunk_size <= 0:
                fail(
                    "E_MTU_EXHAUSTED",
                    "current MTU leaves no payload capacity",
                    details={"current_mtu": run["current_mtu"]},
                )
            length = min(chunk_size, run["payload_length"] - run["next_offset"])
            payload = run["payload"]
            parsed = build_fragment(
                payload,
                offset=run["next_offset"],
                length=length,
                sequence=run["next_sequence"],
                tunnel=run["tunnel"],
                stream_token_value=run["stream_token"],
                total_digest=bytes.fromhex(run["payload_digest"]),
                overhead=run["overhead"],
                profile_version=run["bound_profile_version"],
                outer_mtu=run["current_mtu"],
            )
            conn.execute(
                """
                INSERT INTO fragments(
                    run_id, seq, profile_version, offset, length, wire,
                    header_digest, fragment_digest, status, attempts,
                    retransmission_generation, trigger_source, first_sent_clock_ms,
                    last_sent_clock_ms, acked_clock_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'INFLIGHT', 1, 0, ?, ?, ?, NULL)
                """,
                (
                    run_id,
                    run["next_sequence"],
                    run["bound_profile_version"],
                    run["next_offset"],
                    length,
                    parsed.frame,
                    parsed.header_digest,
                    parsed.fragment_digest,
                    trigger_source,
                    send_clock_ms,
                    send_clock_ms,
                ),
            )
            conn.execute(
                """
                UPDATE runs SET next_offset=?, next_sequence=?, logical_clock_ms=?,
                    status='ACTIVE' WHERE run_id=?
                """,
                (
                    run["next_offset"] + length,
                    run["next_sequence"] + 1,
                    max(run["logical_clock_ms"], send_clock_ms),
                    run_id,
                ),
            )
            fragment = conn.execute(
                "SELECT * FROM fragments WHERE run_id=? AND seq=?",
                (run_id, run["next_sequence"]),
            ).fetchone()
            return self._decorate_fragment(fragment)

    def retry_fragment(
        self,
        *,
        run_id: str,
        seq: int,
        send_clock_ms: int,
        trigger_source: str,
        recovery_replay: bool = False,
    ) -> dict[str, Any]:
        with self.transaction() as conn:
            run = conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
            if run is None:
                fail("E_RUN_NOT_FOUND", "forwarding run was not found", details={"run_id": run_id})
            self._check_active_for_send(conn, run)
            fragment = conn.execute(
                "SELECT * FROM fragments WHERE run_id=? AND seq=?", (run_id, seq)
            ).fetchone()
            if fragment is None:
                fail("E_FRAGMENT_NOT_FOUND", "fragment does not exist", details={"seq": seq})
            if fragment["status"] != "INFLIGHT":
                fail(
                    "E_FRAGMENT_STATE",
                    "only in-flight fragments can be retried",
                    details={"seq": seq, "status": fragment["status"]},
                    http_status=409,
                )
            policy = self.policy_from_conn(conn)
            if (
                not recovery_replay
                and fragment["attempts"] - 1 >= policy.retransmission_budget
            ):
                fail(
                    "E_RETRANSMIT_EXHAUSTED",
                    "fragment has no retransmission budget remaining",
                    details={
                        "seq": seq,
                        "attempts": fragment["attempts"],
                        "retransmission_budget": policy.retransmission_budget,
                    },
                    http_status=409,
                )
            attempts = fragment["attempts"] if recovery_replay else fragment["attempts"] + 1
            generation = fragment["retransmission_generation"] if recovery_replay else attempts - 1
            conn.execute(
                """
                UPDATE fragments SET attempts=?, retransmission_generation=?,
                    trigger_source=?, last_sent_clock_ms=? WHERE run_id=? AND seq=?
                """,
                (
                    attempts,
                    generation,
                    trigger_source,
                    send_clock_ms,
                    run_id,
                    seq,
                ),
            )
            if recovery_replay:
                conn.execute(
                    """
                    UPDATE runs SET retransmission_generation=?, trigger_source=?,
                        logical_clock_ms=?, status='ACTIVE' WHERE run_id=?
                    """,
                    (
                        generation,
                        trigger_source,
                        max(run["logical_clock_ms"], send_clock_ms),
                        run_id,
                    ),
                )
            else:
                conn.execute(
                    """
                    UPDATE runs SET retransmission_count=retransmission_count + 1,
                        retransmission_generation=?, trigger_source=?,
                        logical_clock_ms=?, status='ACTIVE' WHERE run_id=?
                    """,
                    (
                        generation,
                        trigger_source,
                        max(run["logical_clock_ms"], send_clock_ms),
                        run_id,
                    ),
                )
            updated = conn.execute(
                "SELECT * FROM fragments WHERE run_id=? AND seq=?", (run_id, seq)
            ).fetchone()
            return self._decorate_fragment(updated)

    def list_inflight(self, run_id: str) -> list[dict[str, Any]]:
        with self.read_snapshot() as conn:
            rows = conn.execute(
                "SELECT * FROM fragments WHERE run_id=? AND status='INFLIGHT' ORDER BY seq",
                (run_id,),
            ).fetchall()
        return [self._decorate_fragment(row) for row in rows]

    def record_timeout(
        self,
        *,
        run_id: str,
        seq: int,
        timeout_clock_ms: int,
        trigger_source: str,
    ) -> dict[str, Any]:
        with self.transaction() as conn:
            run = conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
            if run is None:
                fail("E_RUN_NOT_FOUND", "forwarding run was not found", details={"run_id": run_id})
            fragment = conn.execute(
                "SELECT * FROM fragments WHERE run_id=? AND seq=?", (run_id, seq)
            ).fetchone()
            if fragment is None:
                fail("E_FRAGMENT_NOT_FOUND", "fragment does not exist", details={"seq": seq})
            policy = self.policy_from_conn(conn)
            exhausted = fragment["attempts"] - 1 >= policy.retransmission_budget
            conn.execute(
                "UPDATE fragments SET trigger_source=? WHERE run_id=? AND seq=?",
                (trigger_source, run_id, seq),
            )
            conn.execute(
                """
                UPDATE runs SET logical_clock_ms=?, trigger_source=?,
                    retransmission_generation=? WHERE run_id=?
                """,
                (
                    max(run["logical_clock_ms"], timeout_clock_ms),
                    trigger_source,
                    fragment["attempts"] - 1,
                    run_id,
                ),
            )
            if exhausted:
                conn.execute(
                    """
                    UPDATE runs SET status='FAILED', outcome='FAILED_AFTER_SEND',
                        business_code='E_RETRANSMIT_EXHAUSTED', trigger_source=?,
                        retransmission_generation=? WHERE run_id=?
                    """,
                    (trigger_source, fragment["attempts"] - 1, run_id),
                )
            updated = conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
            return {
                "exhausted": exhausted,
                "attempts": fragment["attempts"],
                "generation": fragment["attempts"] - 1,
                "run": self._decorate_run(conn, updated),
            }

    def confirm_fragment(
        self,
        *,
        run_id: str,
        seq: int,
        ack_clock_ms: int,
        fragment_digest: str,
        receiver_summary: dict[str, Any] | None,
    ) -> dict[str, Any]:
        with self.transaction() as conn:
            run = conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
            if run is None:
                fail("E_RUN_NOT_FOUND", "forwarding run was not found", details={"run_id": run_id})
            fragment = conn.execute(
                "SELECT * FROM fragments WHERE run_id=? AND seq=?", (run_id, seq)
            ).fetchone()
            if fragment is None:
                fail("E_FRAGMENT_NOT_FOUND", "fragment does not exist", details={"seq": seq})
            if fragment["fragment_digest"] != fragment_digest:
                fail(
                    "E_ACK_DIGEST_MISMATCH",
                    "echo acknowledgement does not identify the sent fragment",
                    details={
                        "seq": seq,
                        "expected": fragment["fragment_digest"],
                        "actual": fragment_digest,
                    },
                )
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO confirmations(
                    run_id, seq, profile_version, confirmed_clock_ms
                ) VALUES (?, ?, ?, ?)
                """,
                (run_id, seq, fragment["profile_version"], ack_clock_ms),
            )
            inserted = cursor.rowcount == 1
            if not inserted:
                updated = conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
                return {
                    "idempotent": True,
                    "committed_now": False,
                    "watermark": updated["committed_watermark"],
                    "run": self._decorate_run(conn, updated),
                }
            conn.execute(
                """
                UPDATE fragments SET status='COMMITTED', acked_clock_ms=?
                WHERE run_id=? AND seq=?
                """,
                (ack_clock_ms, run_id, seq),
            )
            watermark = self._compute_watermark(conn, run_id)
            conn.execute(
                """
                UPDATE runs SET committed_watermark=?, logical_clock_ms=?,
                    status='ACTIVE' WHERE run_id=?
                """,
                (
                    watermark,
                    max(run["logical_clock_ms"], ack_clock_ms),
                    run_id,
                ),
            )
            fragment_count = conn.execute(
                "SELECT COUNT(*) AS count FROM fragments WHERE run_id=?", (run_id,)
            ).fetchone()["count"]
            committed_count = conn.execute(
                "SELECT COUNT(*) AS count FROM fragments WHERE run_id=? AND status='COMMITTED'",
                (run_id,),
            ).fetchone()["count"]
            complete = (
                run["next_offset"] == run["payload_length"]
                and fragment_count == run["next_sequence"]
                and committed_count == fragment_count
                and watermark == fragment_count
            )
            business_code = None
            if complete:
                receiver_digest = None
                if receiver_summary is not None:
                    receiver_digest = receiver_summary.get("digest")
                if receiver_digest is not None and receiver_digest != run["payload_digest"]:
                    business_code = "E_DIGEST_MISMATCH"
                    conn.execute(
                        """
                        UPDATE runs SET status='FAILED', outcome='FAILED_AFTER_SEND',
                            business_code='E_DIGEST_MISMATCH', trigger_source='RECEIVER_DIGEST',
                            reassembly_digest=? WHERE run_id=?
                        """,
                        (receiver_digest, run_id),
                    )
                else:
                    conn.execute(
                        """
                        UPDATE runs SET status='COMPLETED', outcome='COMPLETED',
                            business_code=NULL, trigger_source=NULL, reassembly_digest=?
                        WHERE run_id=?
                        """,
                        (receiver_digest or run["payload_digest"], run_id),
                    )
            updated = conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
            return {
                "idempotent": False,
                "committed_now": True,
                "watermark": watermark,
                "complete": complete,
                "business_code": business_code,
                "run": self._decorate_run(conn, updated),
            }

    @staticmethod
    def _compute_watermark(conn: sqlite3.Connection, run_id: str) -> int:
        rows = conn.execute(
            "SELECT seq FROM fragments WHERE run_id=? AND status='COMMITTED' ORDER BY seq",
            (run_id,),
        ).fetchall()
        watermark = 0
        for row in rows:
            if row["seq"] == watermark:
                watermark += 1
            elif row["seq"] > watermark:
                break
        return watermark

    def finish_round(
        self,
        *,
        run_id: str,
        round_no: int,
        outcome: str,
        ended_clock_ms: int,
        business_code: str | None = None,
        trigger_source: str | None = None,
    ) -> dict[str, Any]:
        with self.transaction() as conn:
            run = conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
            if run is None:
                fail("E_RUN_NOT_FOUND", "forwarding run was not found", details={"run_id": run_id})
            round_row = conn.execute(
                "SELECT * FROM rounds WHERE run_id=? AND round_no=?", (run_id, round_no)
            ).fetchone()
            if round_row is None:
                fail("E_ROUND_NOT_FOUND", "forwarding round was not found")
            terminal = run["status"] in TERMINAL_RUN_STATUSES
            new_status = run["status"]
            new_outcome = run["outcome"]
            new_code = run["business_code"]
            if not terminal:
                if outcome == "ABORTED_ACTIVE":
                    new_status = "ABORTED"
                    new_outcome = "ABORTED_ACTIVE"
                    new_code = "E_ACTIVE_ABORT"
                elif outcome == "FAILED_AFTER_SEND":
                    new_status = "FAILED"
                    new_outcome = "FAILED_AFTER_SEND"
                    new_code = business_code or run["business_code"] or "E_SEND_FAILED"
                elif outcome == "COMPLETED":
                    new_status = "COMPLETED"
                    new_outcome = "COMPLETED"
                    new_code = None
                else:
                    new_status = "PARTIAL"
                    new_outcome = "NOT_STARTED_BUDGET"
                    new_code = business_code or "E_BUDGET_DEFERRED"
            conn.execute(
                """
                UPDATE rounds SET ended_clock_ms=?, outcome=?, business_code=?
                WHERE run_id=? AND round_no=?
                """,
                (
                    max(round_row["started_clock_ms"], ended_clock_ms),
                    outcome,
                    business_code,
                    run_id,
                    round_no,
                ),
            )
            conn.execute(
                """
                UPDATE runs SET status=?, outcome=?, business_code=?,
                    logical_clock_ms=?, trigger_source=COALESCE(?, trigger_source)
                WHERE run_id=?
                """,
                (
                    new_status,
                    new_outcome,
                    new_code,
                    max(run["logical_clock_ms"], ended_clock_ms),
                    trigger_source,
                    run_id,
                ),
            )
            updated = conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
            return self._decorate_run(conn, updated)

    def request_abort(self, run_id: str, clock_ms: int) -> dict[str, Any]:
        with self.transaction() as conn:
            row = conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
            if row is None:
                fail("E_RUN_NOT_FOUND", "forwarding run was not found", details={"run_id": run_id})
            if row["status"] in TERMINAL_RUN_STATUSES:
                return {"requested": False, "run": self._decorate_run(conn, row)}
            conn.execute(
                "UPDATE runs SET abort_requested=1, logical_clock_ms=? WHERE run_id=?",
                (max(row["logical_clock_ms"], clock_ms), run_id),
            )
            updated = conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
            return {"requested": True, "run": self._decorate_run(conn, updated)}

    def fail_run(
        self,
        *,
        run_id: str,
        business_code: str,
        trigger_source: str,
        clock_ms: int,
        retransmission_generation: int | None = None,
    ) -> dict[str, Any]:
        with self.transaction() as conn:
            row = conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
            if row is None:
                fail("E_RUN_NOT_FOUND", "forwarding run was not found", details={"run_id": run_id})
            if row["status"] in TERMINAL_RUN_STATUSES:
                return self._decorate_run(conn, row)
            generation = (
                row["retransmission_generation"]
                if retransmission_generation is None
                else retransmission_generation
            )
            conn.execute(
                """
                UPDATE runs SET status='FAILED', outcome='FAILED_AFTER_SEND',
                    business_code=?, trigger_source=?, retransmission_generation=?,
                    logical_clock_ms=?
                WHERE run_id=?
                """,
                (
                    business_code,
                    trigger_source,
                    generation,
                    max(row["logical_clock_ms"], clock_ms),
                    run_id,
                ),
            )
            updated = conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
            return self._decorate_run(conn, updated)

    def is_abort_requested(self, run_id: str) -> bool:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT abort_requested, status FROM runs WHERE run_id=?", (run_id,)
            ).fetchone()
            return bool(row and (row["abort_requested"] or row["status"] == "ABORTED"))
        finally:
            conn.close()

    def abort_run(self, run_id: str, clock_ms: int) -> dict[str, Any]:
        with self.transaction() as conn:
            row = conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
            if row is None:
                fail("E_RUN_NOT_FOUND", "forwarding run was not found", details={"run_id": run_id})
            if row["status"] in TERMINAL_RUN_STATUSES:
                return self._decorate_run(conn, row)
            self._abort_in_transaction(conn, row, clock_ms)
            updated = conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
            return self._decorate_run(conn, updated)

    def _abort_in_transaction(
        self, conn: sqlite3.Connection, run: sqlite3.Row, clock_ms: int
    ) -> None:
        conn.execute(
            """
            UPDATE runs SET abort_requested=1, status='ABORTED',
                outcome='ABORTED_ACTIVE', business_code='E_ACTIVE_ABORT',
                logical_clock_ms=?
            WHERE run_id=?
            """,
            (max(run["logical_clock_ms"], clock_ms), run["run_id"]),
        )
        conn.execute(
            """
            UPDATE rounds SET outcome='ABORTED_ACTIVE', business_code='E_ACTIVE_ABORT',
                ended_clock_ms=COALESCE(ended_clock_ms, ?)
            WHERE run_id=? AND outcome='ACTIVE'
            """,
            (clock_ms, run["run_id"]),
        )

    def _check_active_for_send(self, conn: sqlite3.Connection, run: sqlite3.Row) -> None:
        if run["status"] in TERMINAL_RUN_STATUSES:
            fail(
                "E_RUN_TERMINAL",
                "forwarding run is terminal",
                details={"run_id": run["run_id"], "status": run["status"]},
                http_status=409,
            )
        if run["abort_requested"]:
            fail(
                "E_ABORT_REQUESTED",
                "active abort is pending for this run",
                details={"run_id": run["run_id"]},
                http_status=409,
            )

    @staticmethod
    def _decorate_fragment(row: sqlite3.Row) -> dict[str, Any]:
        data = dict(row)
        data["wire_b64"] = b64e(row["wire"])
        data["wire"] = bytes(row["wire"])
        return data

    def all_runs(self) -> list[dict[str, Any]]:
        with self.read_snapshot() as conn:
            rows = conn.execute("SELECT * FROM runs ORDER BY tunnel, created_order").fetchall()
            return [self._decorate_run(conn, row) for row in rows]

    def fragments_for_run(self, run_id: str) -> list[dict[str, Any]]:
        with self.read_snapshot() as conn:
            rows = conn.execute(
                "SELECT * FROM fragments WHERE run_id=? ORDER BY seq", (run_id,)
            ).fetchall()
            return [self._decorate_fragment(row) for row in rows]

    def rounds_for_run(self, run_id: str) -> list[dict[str, Any]]:
        with self.read_snapshot() as conn:
            rows = conn.execute(
                "SELECT * FROM rounds WHERE run_id=? ORDER BY round_no", (run_id,)
            ).fetchall()
            return [dict(row) for row in rows]

    def status(self) -> dict[str, Any]:
        with self.read_snapshot() as conn:
            tunnel_rows = conn.execute("SELECT * FROM tunnels ORDER BY name").fetchall()
            pointer_rows = conn.execute(
                "SELECT * FROM profile_pointers ORDER BY tunnel"
            ).fetchall()
            pointers = {row["tunnel"]: dict(row) for row in pointer_rows}
            run_rows = conn.execute("SELECT * FROM runs ORDER BY tunnel, created_order").fetchall()
            runs_by_tunnel: dict[str, list[dict[str, Any]]] = {}
            for row in run_rows:
                fragments = conn.execute(
                    "SELECT seq, offset, length, status, attempts, retransmission_generation "
                    "FROM fragments WHERE run_id=? ORDER BY seq",
                    (row["run_id"],),
                ).fetchall()
                fragment_dicts = [dict(fragment) for fragment in fragments]
                inflight = [
                    fragment for fragment in fragment_dicts if fragment["status"] == "INFLIGHT"
                ]
                versions = conn.execute(
                    "SELECT DISTINCT profile_version FROM rounds WHERE run_id=? "
                    "ORDER BY profile_version",
                    (row["run_id"],),
                ).fetchall()
                run_status = dict(row)
                run_status.pop("payload", None)
                stream = run_status.pop("stream_token", None)
                run_status["stream_token_hex"] = (
                    bytes(stream).hex() if stream is not None else None
                )
                run_status["fragment_count"] = len(fragment_dicts)
                run_status["committed_fragments"] = sum(
                    1 for fragment in fragment_dicts if fragment["status"] == "COMMITTED"
                )
                run_status["inflight_intervals"] = self._sequence_intervals(
                    [fragment["seq"] for fragment in inflight]
                )
                run_status["inflight_byte_intervals"] = self._byte_intervals(inflight)
                run_status["profile_versions_used"] = [
                    version["profile_version"] for version in versions
                ]
                runs_by_tunnel.setdefault(row["tunnel"], []).append(run_status)

            tunnels: list[dict[str, Any]] = []
            for row in tunnel_rows:
                lengths = json.loads(row["payload_lengths_json"])
                values = np.asarray(lengths, dtype=np.int64)
                pointer = pointers.get(row["name"], {})
                runs = runs_by_tunnel.get(row["name"], [])
                latest = runs[-1] if runs else None
                entry: dict[str, Any] = {
                    "tunnel": row["name"],
                    "listen": (
                        f"[{row['listen_host']}]:{row['listen_port']}"
                        if ":" in row["listen_host"]
                        else f"{row['listen_host']}:{row['listen_port']}"
                    ),
                    "transport": row["transport"],
                    "outer_mtu": row["outer_mtu"],
                    "encapsulation_overhead": row["encapsulation_overhead"],
                    "link_weight": row["link_weight"],
                    "payload_lengths": lengths,
                    "payload_stats": {
                        "count": len(lengths),
                        "min": int(np.min(values)),
                        "max": int(np.max(values)),
                        "mean": round(float(np.mean(values)), 6),
                    },
                    "profile_version": pointer.get("version"),
                    "profile_digest": pointer.get("digest"),
                    "status": latest["status"] if latest else "LOADED",
                    "run_id": latest["run_id"] if latest else None,
                    "fragment_watermark": (
                        latest["committed_watermark"] if latest else 0
                    ),
                    "inflight_intervals": (
                        latest["inflight_intervals"] if latest else []
                    ),
                    "current_mtu": latest["current_mtu"] if latest else row["outer_mtu"],
                    "retransmission_count": (
                        latest["retransmission_count"] if latest else 0
                    ),
                    "logical_clock_ms": latest["logical_clock_ms"] if latest else 0,
                    "bound_profile_version": (
                        latest["bound_profile_version"] if latest else None
                    ),
                    "outcome": latest["outcome"] if latest else None,
                    "business_code": latest["business_code"] if latest else None,
                    "trigger_source": latest["trigger_source"] if latest else None,
                    "retransmission_generation": (
                        latest["retransmission_generation"] if latest else 0
                    ),
                    "runs": runs,
                }
                tunnels.append(entry)
        return {"ok": True, "tunnels": tunnels}

    @staticmethod
    def _sequence_intervals(sequences: list[int]) -> list[list[int]]:
        if not sequences:
            return []
        result: list[list[int]] = []
        start = previous = sequences[0]
        for value in sequences[1:]:
            if value == previous + 1:
                previous = value
                continue
            result.append([start, previous])
            start = previous = value
        result.append([start, previous])
        return result

    @staticmethod
    def _byte_intervals(fragments: list[dict[str, Any]]) -> list[list[int]]:
        if not fragments:
            return []
        sorted_fragments = sorted(fragments, key=lambda fragment: fragment["offset"])
        result: list[list[int]] = []
        start = sorted_fragments[0]["offset"]
        previous_end = (
            sorted_fragments[0]["offset"] + sorted_fragments[0]["length"]
        )
        for fragment in sorted_fragments[1:]:
            fragment_start = fragment["offset"]
            fragment_end = fragment_start + fragment["length"]
            if fragment_start == previous_end:
                previous_end = fragment_end
                continue
            result.append([start, previous_end])
            start, previous_end = fragment_start, fragment_end
        result.append([start, previous_end])
        return result

    def database_report(self) -> dict[str, Any]:
        return {
            "db_path": str(self.db_path),
            "exists": self.db_path.exists(),
            "journal_mode": self._journal_mode(),
        }

    def _journal_mode(self) -> str | None:
        if not self.db_path.exists():
            return None
        conn = self._connect()
        try:
            return conn.execute("PRAGMA journal_mode").fetchone()[0]
        finally:
            conn.close()


def is_business_error(exc: BaseException) -> bool:
    return isinstance(exc, BusinessError)

