"""SQLite persistence, atomic fragment watermarks and tunnel-level locking."""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import sqlite3
import threading
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

from .codec import FragmentPacket, parse_fragment, sha256_hex
from .config import LinkProfile, Peer, Policy
from .errors import (
    E_BAD_FRAGMENT,
    E_DIGEST_MISMATCH,
    E_NOT_FOUND,
    E_RETRY_EXHAUSTED,
    E_SEQUENCE_INFLIGHT,
    E_STATE_CORRUPT,
    E_TUNNEL_BUSY,
    E_TUNNEL_LOCKED,
    BusinessError,
    fail,
)


SCHEMA = """
CREATE TABLE IF NOT EXISTS tunnels (
    tunnel_id TEXT PRIMARY KEY,
    peer_name TEXT NOT NULL,
    protocol TEXT NOT NULL,
    host TEXT NOT NULL,
    port INTEGER NOT NULL,
    payload_digest TEXT NOT NULL,
    payload BLOB NOT NULL,
    message_id TEXT NOT NULL,
    algorithm TEXT NOT NULL,
    current_mtu INTEGER NOT NULL,
    logical_clock_ms INTEGER NOT NULL,
    retransmit_budget INTEGER NOT NULL,
    status TEXT NOT NULL,
    confirmed_through INTEGER NOT NULL DEFAULT -1,
    expected_fragments INTEGER NOT NULL,
    retransmit_count INTEGER NOT NULL DEFAULT 0,
    reassembly_digest TEXT,
    echo_fragment_count INTEGER,
    failure_code TEXT,
    recovery_count INTEGER NOT NULL DEFAULT 0,
    trace_json TEXT,
    UNIQUE (message_id)
);

CREATE TABLE IF NOT EXISTS fragments (
    tunnel_id TEXT NOT NULL,
    message_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    payload_offset INTEGER NOT NULL,
    payload_len INTEGER NOT NULL,
    status TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    packet BLOB NOT NULL,
    packet_digest TEXT NOT NULL,
    final INTEGER NOT NULL,
    last_sent_ms INTEGER,
    confirmed_clock_ms INTEGER,
    PRIMARY KEY (tunnel_id, message_id, seq),
    FOREIGN KEY (tunnel_id) REFERENCES tunnels(tunnel_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS fragment_acks (
    tunnel_id TEXT NOT NULL,
    message_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    confirmed_clock_ms INTEGER NOT NULL,
    PRIMARY KEY (tunnel_id, message_id, seq),
    FOREIGN KEY (tunnel_id) REFERENCES tunnels(tunnel_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS fragments_status_idx
    ON fragments(tunnel_id, status, seq);
"""


def tunnel_id_for(peer_name: str, payload_digest: str) -> str:
    return hashlib.sha256(
        f"{peer_name}\x00{payload_digest}".encode("utf-8")
    ).hexdigest()[:20]


def ranges_from_sequences(sequences: Sequence[int]) -> list[list[int]]:
    ordered = sorted(set(sequences))
    if not ordered:
        return []
    ranges: list[list[int]] = []
    start = previous = ordered[0]
    for sequence in ordered[1:]:
        if sequence == previous + 1:
            previous = sequence
            continue
        ranges.append([start, previous])
        start = previous = sequence
    ranges.append([start, previous])
    return ranges


class Store:
    """Small transaction-oriented store; one SQLite file can serve many processes."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.lock_path = Path(str(self.db_path) + ".lock")
        self._thread_lock = threading.RLock()

    @property
    def exists(self) -> bool:
        return self.db_path.exists()

    def initialize(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._db_lock():
            connection = self._connect()
            try:
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute("PRAGMA synchronous=FULL")
                connection.executescript(SCHEMA)
                connection.commit()
            finally:
                connection.close()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.db_path,
            timeout=5.0,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=5000")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    @contextlib.contextmanager
    def _db_lock(self) -> Iterator[None]:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._thread_lock:
            descriptor = os.open(self.lock_path, os.O_CREAT | os.O_RDWR, 0o600)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX)
                yield
            finally:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
                os.close(descriptor)

    @contextlib.contextmanager
    def write_transaction(self) -> Iterator[sqlite3.Connection]:
        with self._db_lock():
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                yield connection
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.close()

    @contextlib.contextmanager
    def tunnel_session_lock(self, tunnel_id: str) -> Iterator[None]:
        """Hold an advisory lock for a whole send/recovery session.

        Different tunnels can progress in parallel. A second process touching
        the same tunnel fails with ``E_TUNNEL_LOCKED`` instead of racing its
        in-flight interval.
        """

        lock_dir = self.db_path.parent / "locks"
        lock_dir.mkdir(parents=True, exist_ok=True)
        safe_name = hashlib.sha256(tunnel_id.encode("utf-8")).hexdigest()[:32]
        path = lock_dir / f"{safe_name}.lock"
        descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise BusinessError(
                    E_TUNNEL_LOCKED,
                    "another forwarding instance owns this tunnel",
                    details={"tunnel_id": tunnel_id},
                ) from exc
            yield
        finally:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)

    def prepare_tunnel(
        self,
        *,
        peer: Peer,
        payload: bytes,
        policy: Policy,
        link: LinkProfile,
        algorithm: str,
        mtu: int,
        message_id: str,
        fragments: Sequence[FragmentPacket],
        clock_start_ms: int,
    ) -> dict[str, Any]:
        if not fragments:
            fail(E_BAD_FRAGMENT, "tunnel plan contains no fragments")
        digest = sha256_hex(payload)
        tunnel_id = tunnel_id_for(peer.name, digest)
        ordered = sorted(fragments, key=lambda packet: packet.sequence)
        if [packet.sequence for packet in ordered] != list(range(len(ordered))):
            fail(E_BAD_FRAGMENT, "initial fragment sequences must start at zero")
        if not ordered[-1].final:
            fail(E_BAD_FRAGMENT, "last planned fragment must be marked final")

        with self.write_transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM tunnels WHERE tunnel_id = ?", (tunnel_id,)
            ).fetchone()
            if existing is not None:
                if existing["payload_digest"] != digest:
                    fail(
                        E_TUNNEL_BUSY,
                        "tunnel id collision with a different payload",
                        details={"tunnel_id": tunnel_id},
                    )
                return self._state_from_connection(connection, tunnel_id)

            active = connection.execute(
                """
                SELECT tunnel_id, status
                FROM tunnels
                WHERE peer_name = ? AND status IN ('prepared', 'in_progress')
                ORDER BY tunnel_id
                """,
                (peer.name,),
            ).fetchall()
            if active:
                fail(
                    E_TUNNEL_BUSY,
                    "peer already has an active tunnel",
                    details={
                        "active_tunnel_ids": [row["tunnel_id"] for row in active],
                        "peer": peer.name,
                    },
                )

            connection.execute(
                """
                INSERT INTO tunnels (
                    tunnel_id, peer_name, protocol, host, port,
                    payload_digest, payload, message_id, algorithm,
                    current_mtu, logical_clock_ms, retransmit_budget,
                    status, confirmed_through, expected_fragments,
                    retransmit_count, reassembly_digest, echo_fragment_count,
                    failure_code, recovery_count, trace_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'prepared',
                          -1, ?, 0, NULL, NULL, NULL, 0, NULL)
                """,
                (
                    tunnel_id,
                    peer.name,
                    peer.protocol,
                    peer.host,
                    peer.port,
                    digest,
                    payload,
                    message_id,
                    algorithm,
                    mtu,
                    clock_start_ms,
                    policy.retransmit_budget,
                    len(ordered),
                ),
            )
            offset = 0
            for packet in ordered:
                connection.execute(
                    """
                    INSERT INTO fragments (
                        tunnel_id, message_id, seq, payload_offset,
                        payload_len, status, attempts, packet,
                        packet_digest, final, last_sent_ms, confirmed_clock_ms
                    ) VALUES (?, ?, ?, ?, ?, 'pending', 0, ?, ?, ?, NULL, NULL)
                    """,
                    (
                        tunnel_id,
                        packet.message_id,
                        packet.sequence,
                        offset,
                        len(packet.payload),
                        packet.raw,
                        packet.digest,
                        1 if packet.final else 0,
                    ),
                )
                offset += len(packet.payload)
            if offset != len(payload):
                fail(
                    E_BAD_FRAGMENT,
                    "fragment plan does not cover the whole payload",
                    details={"covered": offset, "payload": len(payload)},
                )
            return self._state_from_connection(connection, tunnel_id)

    def get_state(self, tunnel_id: str) -> dict[str, Any]:
        if not self.exists:
            fail(E_NOT_FOUND, "tunnel database does not exist")
        connection = self._connect()
        try:
            return self._state_from_connection(connection, tunnel_id)
        finally:
            connection.close()

    def get_optional_state(self, tunnel_id: str) -> dict[str, Any] | None:
        if not self.exists:
            return None
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT tunnel_id FROM tunnels WHERE tunnel_id = ?",
                (tunnel_id,),
            ).fetchone()
            if row is None:
                return None
            return self._state_from_connection(connection, tunnel_id)
        finally:
            connection.close()

    def list_states(self) -> list[dict[str, Any]]:
        if not self.exists:
            return []
        connection = self._connect()
        try:
            rows = connection.execute(
                "SELECT tunnel_id FROM tunnels ORDER BY tunnel_id"
            ).fetchall()
            return [
                self._state_from_connection(connection, row["tunnel_id"])
                for row in rows
            ]
        finally:
            connection.close()

    def get_fragment_rows(self, tunnel_id: str) -> list[dict[str, Any]]:
        connection = self._connect()
        try:
            rows = connection.execute(
                """
                SELECT seq, payload_offset, payload_len, status, attempts,
                       packet, packet_digest, final, last_sent_ms,
                       confirmed_clock_ms
                FROM fragments
                WHERE tunnel_id = ?
                ORDER BY seq
                """,
                (tunnel_id,),
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            connection.close()

    def reserve_fragment(
        self,
        tunnel_id: str,
        sequence: int,
        *,
        now_ms: int,
    ) -> dict[str, Any]:
        with self.write_transaction() as connection:
            tunnel = self._tunnel_row(connection, tunnel_id)
            if tunnel["status"] == "complete":
                fragment = self._fragment_row(connection, tunnel_id, sequence)
                if fragment["status"] == "confirmed":
                    return {
                        "attempt": fragment["attempts"],
                        "idempotent": True,
                        "packet": bytes(fragment["packet"]),
                        "sequence": sequence,
                    }
            if tunnel["status"] == "failed":
                raise BusinessError(
                    tunnel["failure_code"] or E_RETRY_EXHAUSTED,
                    "tunnel is in a failed state",
                    details={"tunnel_id": tunnel_id},
                )
            fragment = self._fragment_row(connection, tunnel_id, sequence)
            if fragment["status"] == "confirmed":
                return {
                    "attempt": fragment["attempts"],
                    "idempotent": True,
                    "packet": bytes(fragment["packet"]),
                    "sequence": sequence,
                }
            if fragment["status"] == "inflight":
                fail(
                    E_SEQUENCE_INFLIGHT,
                    "fragment sequence is already in flight",
                    details={"sequence": sequence, "tunnel_id": tunnel_id},
                )
            attempts = int(fragment["attempts"])
            if attempts >= tunnel["retransmit_budget"] + 1:
                connection.execute(
                    """
                    UPDATE tunnels
                    SET status = 'failed', failure_code = ?,
                        logical_clock_ms = MAX(logical_clock_ms, ?)
                    WHERE tunnel_id = ?
                    """,
                    (E_RETRY_EXHAUSTED, now_ms, tunnel_id),
                )
                # The failed state must survive even though this call exits
                # with an exception. Commit explicitly before propagating.
                connection.commit()
                raise BusinessError(
                    E_RETRY_EXHAUSTED,
                    "fragment retransmission budget exhausted",
                    details={
                        "attempts": attempts,
                        "retransmit_budget": tunnel["retransmit_budget"],
                        "sequence": sequence,
                        "tunnel_id": tunnel_id,
                    },
                )
            new_attempt = attempts + 1
            connection.execute(
                """
                UPDATE fragments
                SET status = 'inflight', attempts = ?, last_sent_ms = ?
                WHERE tunnel_id = ? AND message_id = ? AND seq = ?
                """,
                (
                    new_attempt,
                    now_ms,
                    tunnel_id,
                    tunnel["message_id"],
                    sequence,
                ),
            )
            if new_attempt > 1:
                connection.execute(
                    """
                    UPDATE tunnels
                    SET retransmit_count = retransmit_count + 1,
                        status = 'in_progress',
                        logical_clock_ms = MAX(logical_clock_ms, ?)
                    WHERE tunnel_id = ?
                    """,
                    (now_ms, tunnel_id),
                )
            else:
                connection.execute(
                    """
                    UPDATE tunnels
                    SET status = 'in_progress',
                        logical_clock_ms = MAX(logical_clock_ms, ?)
                    WHERE tunnel_id = ?
                    """,
                    (now_ms, tunnel_id),
                )
            return {
                "attempt": new_attempt,
                "idempotent": False,
                "message_id": tunnel["message_id"],
                "packet": bytes(fragment["packet"]),
                "sequence": sequence,
            }

    def release_inflight(
        self,
        tunnel_id: str,
        sequences: Sequence[int],
        *,
        now_ms: int,
    ) -> int:
        if not sequences:
            return 0
        with self.write_transaction() as connection:
            tunnel = self._tunnel_row(connection, tunnel_id)
            if tunnel["status"] in {"complete", "failed"}:
                return 0
            placeholders = ",".join("?" for _ in sequences)
            cursor = connection.execute(
                f"""
                UPDATE fragments
                SET status = 'pending'
                WHERE tunnel_id = ? AND seq IN ({placeholders})
                  AND status = 'inflight'
                """,
                (tunnel_id, *sorted(set(sequences))),
            )
            connection.execute(
                """
                UPDATE tunnels
                SET logical_clock_ms = MAX(logical_clock_ms, ?)
                WHERE tunnel_id = ?
                """,
                (now_ms, tunnel_id),
            )
            return int(cursor.rowcount)

    def apply_ack(
        self,
        tunnel_id: str,
        *,
        acked_sequences: Sequence[int],
        summary: dict[str, Any],
        now_ms: int,
    ) -> dict[str, Any]:
        with self.write_transaction() as connection:
            tunnel = self._tunnel_row(connection, tunnel_id)
            if tunnel["status"] == "complete":
                return self._state_from_connection(connection, tunnel_id)
            if tunnel["status"] == "failed":
                raise BusinessError(
                    tunnel["failure_code"] or E_RETRY_EXHAUSTED,
                    "cannot confirm fragments on a failed tunnel",
                    details={"tunnel_id": tunnel_id},
                )
            known = {
                int(row["seq"]): row
                for row in connection.execute(
                    """
                    SELECT seq, status, packet
                    FROM fragments WHERE tunnel_id = ? ORDER BY seq
                    """,
                    (tunnel_id,),
                ).fetchall()
            }
            for sequence in sorted(set(acked_sequences)):
                if sequence not in known:
                    fail(
                        E_BAD_FRAGMENT,
                        "ack references an unknown fragment sequence",
                        details={"sequence": sequence, "tunnel_id": tunnel_id},
                    )
                connection.execute(
                    """
                    INSERT OR IGNORE INTO fragment_acks (
                        tunnel_id, message_id, seq, confirmed_clock_ms
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (tunnel_id, tunnel["message_id"], sequence, now_ms),
                )
                connection.execute(
                    """
                    UPDATE fragments
                    SET status = 'confirmed', confirmed_clock_ms = ?
                    WHERE tunnel_id = ? AND message_id = ? AND seq = ?
                      AND status != 'confirmed'
                    """,
                    (now_ms, tunnel_id, tunnel["message_id"], sequence),
                )

            confirmed_sequences = [
                int(row["seq"])
                for row in connection.execute(
                    """
                    SELECT seq FROM fragments
                    WHERE tunnel_id = ? AND message_id = ? AND status = 'confirmed'
                    ORDER BY seq
                    """,
                    (tunnel_id, tunnel["message_id"]),
                ).fetchall()
            ]
            total = int(tunnel["expected_fragments"])
            all_confirmed = len(confirmed_sequences) == total

            summary_complete = bool(summary.get("complete"))
            summary_digest = summary.get("digest")
            if summary_complete:
                if not isinstance(summary_digest, str):
                    fail(
                        E_BAD_FRAGMENT,
                        "complete echo summary is missing a digest",
                        details={"tunnel_id": tunnel_id},
                    )
                if summary_digest != tunnel["payload_digest"]:
                    fail(
                        E_DIGEST_MISMATCH,
                        "echo reassembly digest does not match persisted payload",
                        details={
                            "actual": summary_digest,
                            "expected": tunnel["payload_digest"],
                            "tunnel_id": tunnel_id,
                        },
                    )
                if not all_confirmed:
                    fail(
                        E_STATE_CORRUPT,
                        "echo reports completion before all local fragments are confirmed",
                        details={
                            "confirmed": len(confirmed_sequences),
                            "expected_fragments": total,
                            "tunnel_id": tunnel_id,
                        },
                    )

            confirmed_through = self._confirmed_through(
                confirmed_sequences, total
            )
            if all_confirmed and summary_complete:
                connection.execute(
                    """
                    UPDATE tunnels
                    SET status = 'complete', confirmed_through = ?,
                        reassembly_digest = ?, echo_fragment_count = ?,
                        logical_clock_ms = MAX(logical_clock_ms, ?)
                    WHERE tunnel_id = ?
                    """,
                    (
                        confirmed_through,
                        summary_digest,
                        int(summary.get("fragment_count", total)),
                        now_ms,
                        tunnel_id,
                    ),
                )
            else:
                connection.execute(
                    """
                    UPDATE tunnels
                    SET status = 'in_progress', confirmed_through = ?,
                        logical_clock_ms = MAX(logical_clock_ms, ?)
                    WHERE tunnel_id = ?
                    """,
                    (confirmed_through, now_ms, tunnel_id),
                )
            return self._state_from_connection(connection, tunnel_id)

    def replan_tail(
        self,
        tunnel_id: str,
        *,
        new_mtu: int,
        expected_frontier: int,
        base_sequence: int,
        fragments: Sequence[FragmentPacket],
        now_ms: int,
    ) -> dict[str, Any]:
        ordered = sorted(fragments, key=lambda packet: packet.sequence)
        if not ordered:
            fail(E_BAD_FRAGMENT, "replan requires at least one fragment")
        if ordered[0].sequence != base_sequence:
            fail(E_BAD_FRAGMENT, "replan base sequence mismatch")
        if [packet.sequence for packet in ordered] != list(
            range(base_sequence, base_sequence + len(ordered))
        ):
            fail(E_BAD_FRAGMENT, "replan fragment sequences must be contiguous")
        if not ordered[-1].final:
            fail(E_BAD_FRAGMENT, "replanned last fragment must be final")

        with self.write_transaction() as connection:
            tunnel = self._tunnel_row(connection, tunnel_id)
            if tunnel["status"] in {"complete", "failed"}:
                fail(
                    E_STATE_CORRUPT,
                    "cannot replan a completed or failed tunnel",
                    details={"status": tunnel["status"], "tunnel_id": tunnel_id},
                )
            frontier_row = connection.execute(
                """
                SELECT COALESCE(MAX(payload_offset + payload_len), 0) AS frontier
                FROM fragments
                WHERE tunnel_id = ? AND attempts > 0
                """,
                (tunnel_id,),
            ).fetchone()
            frontier = int(frontier_row["frontier"])
            if frontier != expected_frontier:
                fail(
                    E_STATE_CORRUPT,
                    "fragment frontier changed during replan",
                    details={
                        "actual": frontier,
                        "expected": expected_frontier,
                        "tunnel_id": tunnel_id,
                    },
                )
            pending_after = connection.execute(
                """
                SELECT COUNT(*) AS count
                FROM fragments
                WHERE tunnel_id = ? AND seq >= ? AND status != 'pending'
                """,
                (tunnel_id, base_sequence),
            ).fetchone()
            if int(pending_after["count"]) != 0:
                fail(
                    E_STATE_CORRUPT,
                    "cannot replace confirmed or in-flight fragments",
                    details={"tunnel_id": tunnel_id},
                )
            connection.execute(
                """
                DELETE FROM fragments
                WHERE tunnel_id = ? AND seq >= ? AND status = 'pending'
                """,
                (tunnel_id, base_sequence),
            )
            offset = expected_frontier
            for packet in ordered:
                connection.execute(
                    """
                    INSERT INTO fragments (
                        tunnel_id, message_id, seq, payload_offset,
                        payload_len, status, attempts, packet,
                        packet_digest, final, last_sent_ms, confirmed_clock_ms
                    ) VALUES (?, ?, ?, ?, ?, 'pending', 0, ?, ?, ?, NULL, NULL)
                    """,
                    (
                        tunnel_id,
                        packet.message_id,
                        packet.sequence,
                        offset,
                        len(packet.payload),
                        packet.raw,
                        packet.digest,
                        1 if packet.final else 0,
                    ),
                )
                offset += len(packet.payload)
            connection.execute(
                """
                UPDATE tunnels
                SET current_mtu = ?, expected_fragments = ?,
                    logical_clock_ms = MAX(logical_clock_ms, ?)
                WHERE tunnel_id = ?
                """,
                (
                    new_mtu,
                    base_sequence + len(ordered),
                    now_ms,
                    tunnel_id,
                ),
            )
            return self._state_from_connection(connection, tunnel_id)

    def get_replan_context(self, tunnel_id: str) -> dict[str, Any]:
        connection = self._connect()
        try:
            tunnel = self._tunnel_row(connection, tunnel_id)
            frontier_row = connection.execute(
                """
                SELECT COALESCE(MAX(payload_offset + payload_len), 0) AS frontier
                FROM fragments
                WHERE tunnel_id = ? AND attempts > 0
                """,
                (tunnel_id,),
            ).fetchone()
            base_row = connection.execute(
                """
                SELECT COALESCE(MAX(seq), -1) + 1 AS base_sequence
                FROM fragments
                WHERE tunnel_id = ? AND attempts > 0
                """,
                (tunnel_id,),
            ).fetchone()
            return {
                "base_sequence": int(base_row["base_sequence"]),
                "frontier": int(frontier_row["frontier"]),
                "message_id": tunnel["message_id"],
                "payload": bytes(tunnel["payload"]),
            }
        finally:
            connection.close()

    def recover_inflight(self, tunnel_id: str, *, now_ms: int) -> int:
        with self.write_transaction() as connection:
            tunnel = self._tunnel_row(connection, tunnel_id)
            if tunnel["status"] == "complete":
                return 0
            if tunnel["status"] == "failed":
                raise BusinessError(
                    tunnel["failure_code"] or E_RETRY_EXHAUSTED,
                    "tunnel is in a failed state",
                    details={"tunnel_id": tunnel_id},
                )
            cursor = connection.execute(
                """
                UPDATE fragments
                SET status = 'pending'
                WHERE tunnel_id = ? AND status = 'inflight'
                """,
                (tunnel_id,),
            )
            connection.execute(
                """
                UPDATE tunnels
                SET status = 'in_progress',
                    recovery_count = recovery_count + 1,
                    logical_clock_ms = MAX(logical_clock_ms, ?)
                WHERE tunnel_id = ?
                """,
                (now_ms, tunnel_id),
            )
            return int(cursor.rowcount)

    def mark_failed(self, tunnel_id: str, code: str, *, now_ms: int) -> None:
        with self.write_transaction() as connection:
            connection.execute(
                """
                UPDATE tunnels
                SET status = 'failed', failure_code = ?,
                    logical_clock_ms = MAX(logical_clock_ms, ?)
                WHERE tunnel_id = ?
                """,
                (code, now_ms, tunnel_id),
            )

    def record_clock(self, tunnel_id: str, now_ms: int) -> None:
        with self.write_transaction() as connection:
            connection.execute(
                """
                UPDATE tunnels
                SET logical_clock_ms = MAX(logical_clock_ms, ?)
                WHERE tunnel_id = ?
                """,
                (now_ms, tunnel_id),
            )

    def update_mtu(self, tunnel_id: str, mtu: int, *, now_ms: int) -> None:
        with self.write_transaction() as connection:
            connection.execute(
                """
                UPDATE tunnels
                SET current_mtu = ?, logical_clock_ms = MAX(logical_clock_ms, ?)
                WHERE tunnel_id = ?
                """,
                (mtu, now_ms, tunnel_id),
            )

    def save_trace(self, tunnel_id: str, trace: dict[str, Any]) -> None:
        rendered = json.dumps(trace, ensure_ascii=False, sort_keys=True)
        with self.write_transaction() as connection:
            connection.execute(
                "UPDATE tunnels SET trace_json = ? WHERE tunnel_id = ?",
                (rendered, tunnel_id),
            )

    def verify_local(self, tunnel_id: str) -> dict[str, Any]:
        state = self.get_state(tunnel_id)
        rows = self.get_fragment_rows(tunnel_id)
        checks = {
            "all_fragments_confirmed": all(
                row["status"] == "confirmed" for row in rows
            ),
            "confirmed_through_matches": state["confirmed_through"]
            == state["expected_fragments"] - 1,
            "digest_matches_payload": state["reassembly_digest"]
            == state["payload_digest"],
            "fragment_payload_digests_valid": all(
                sha256_hex(bytes(row["packet"])) == row["packet_digest"]
                for row in rows
            ),
            "no_inflight": not state["inflight_ranges"],
            "state_complete": state["status"] == "complete",
        }
        connection = self._connect()
        try:
            tunnel = self._tunnel_row(connection, tunnel_id)
            payload = bytes(tunnel["payload"])
        finally:
            connection.close()
        reassembled = b"".join(
            parse_fragment(bytes(row["packet"])).payload for row in rows
        )
        checks["payload_digest_valid"] = sha256_hex(payload) == state["payload_digest"]
        checks["fragments_reassemble_payload"] = reassembled == payload
        result = {
            "checks": dict(sorted(checks.items())),
            "code": "OK" if all(checks.values()) else E_STATE_CORRUPT,
            "message_id": state["message_id"],
            "payload_digest": state["payload_digest"],
            "tunnel_id": tunnel_id,
        }
        if not all(checks.values()):
            raise BusinessError(
                E_STATE_CORRUPT,
                "persisted tunnel state failed local verification",
                details=result,
            )
        return result

    def _tunnel_row(
        self, connection: sqlite3.Connection, tunnel_id: str
    ) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM tunnels WHERE tunnel_id = ?", (tunnel_id,)
        ).fetchone()
        if row is None:
            fail(E_NOT_FOUND, "tunnel not found", details={"tunnel_id": tunnel_id})
        return row

    def _fragment_row(
        self,
        connection: sqlite3.Connection,
        tunnel_id: str,
        sequence: int,
    ) -> sqlite3.Row:
        row = connection.execute(
            """
            SELECT * FROM fragments
            WHERE tunnel_id = ? AND seq = ?
            """,
            (tunnel_id, sequence),
        ).fetchone()
        if row is None:
            fail(
                E_NOT_FOUND,
                "fragment not found",
                details={"sequence": sequence, "tunnel_id": tunnel_id},
            )
        return row

    def _confirmed_through(self, confirmed: Sequence[int], total: int) -> int:
        if not confirmed:
            return -1
        confirmed_set = set(confirmed)
        watermark = -1
        for sequence in range(total):
            if sequence not in confirmed_set:
                break
            watermark = sequence
        return watermark

    def _state_from_connection(
        self, connection: sqlite3.Connection, tunnel_id: str
    ) -> dict[str, Any]:
        tunnel = self._tunnel_row(connection, tunnel_id)
        fragments = connection.execute(
            """
            SELECT seq, payload_offset, payload_len, status, attempts,
                   packet_digest, final, last_sent_ms, confirmed_clock_ms
            FROM fragments
            WHERE tunnel_id = ?
            ORDER BY seq
            """,
            (tunnel_id,),
        ).fetchall()
        inflight = [
            int(row["seq"]) for row in fragments if row["status"] == "inflight"
        ]
        pending = [
            int(row["seq"]) for row in fragments if row["status"] == "pending"
        ]
        confirmed_rows = [
            row for row in fragments if row["status"] == "confirmed"
        ]
        confirmed_bytes = sum(int(row["payload_len"]) for row in confirmed_rows)
        trace = json.loads(tunnel["trace_json"]) if tunnel["trace_json"] else None
        return {
            "algorithm": tunnel["algorithm"],
            "confirmed_bytes": confirmed_bytes,
            "confirmed_through": int(tunnel["confirmed_through"]),
            "current_mtu": int(tunnel["current_mtu"]),
            "expected_fragments": int(tunnel["expected_fragments"]),
            "failure_code": tunnel["failure_code"],
            "inflight_ranges": ranges_from_sequences(inflight),
            "logical_clock_ms": int(tunnel["logical_clock_ms"]),
            "message_id": tunnel["message_id"],
            "payload_digest": tunnel["payload_digest"],
            "peer": tunnel["peer_name"],
            "pending_ranges": ranges_from_sequences(pending),
            "recovery_count": int(tunnel["recovery_count"]),
            "reassembly_digest": tunnel["reassembly_digest"],
            "retransmit_count": int(tunnel["retransmit_count"]),
            "status": tunnel["status"],
            "trace": trace,
            "tunnel_id": tunnel["tunnel_id"],
        }
