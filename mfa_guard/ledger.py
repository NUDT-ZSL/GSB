"""SQLite authentication decision ledger and ticket dependency graph."""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import quote

from .errors import Codes, MFAError
from .models import DecisionResult, PolicyView, TrustedFactor
from .util import canonical_json_bytes, sha256_canonical

SCHEMA_VERSION = "1"
STATE_TABLES = (
    "meta",
    "decisions",
    "decision_ledger",
    "tickets",
    "ticket_edges",
    "revocations",
)


def user_node(user_id: str) -> str:
    return f"user:{quote(user_id, safe='')}"


def factor_node(factor_id: str) -> str:
    return f"factor:{quote(factor_id, safe='')}"


def ticket_node(user_id: str, factor_id: str, generation: int) -> str:
    return (
        f"ticket:{quote(user_id, safe='')}:"
        f"{quote(factor_id, safe='')}:{generation}"
    )


class Ledger:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.connection = sqlite3.connect(
            str(self.path),
            timeout=30,
            isolation_level=None,
            check_same_thread=False,
        )
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA journal_mode = DELETE")
        self.connection.execute("PRAGMA synchronous = FULL")
        # sqlite3.executescript() implicitly commits, so schema creation is
        # deliberately performed outside an explicit transaction.
        self._create_schema()

    def close(self) -> None:
        with self._lock:
            self.connection.close()

    @contextmanager
    def transaction(self, *, rollback: bool = False) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self.connection.execute("BEGIN IMMEDIATE")
            try:
                yield self.connection
            except BaseException:
                self.connection.execute("ROLLBACK")
                raise
            else:
                if rollback:
                    self.connection.execute("ROLLBACK")
                else:
                    self.connection.execute("COMMIT")

    def _create_schema(self) -> None:
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS decisions (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                attempt_no INTEGER NOT NULL UNIQUE,
                user_id TEXT NOT NULL,
                factor_id TEXT NOT NULL,
                ticket_generation INTEGER NOT NULL,
                conclusion TEXT NOT NULL,
                policy_generation INTEGER NOT NULL,
                policy_version TEXT NOT NULL,
                canonical_fp_key TEXT,
                watermark INTEGER NOT NULL,
                reasons_json TEXT NOT NULL,
                detail_json TEXT NOT NULL,
                latency_ms REAL NOT NULL,
                batch_seq INTEGER NOT NULL,
                created_wall_ms INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS ix_decisions_ticket
                ON decisions(user_id, factor_id, ticket_generation);
            CREATE TABLE IF NOT EXISTS decision_ledger (
                user_id TEXT NOT NULL,
                factor_id TEXT NOT NULL,
                ticket_generation INTEGER NOT NULL,
                conclusion TEXT NOT NULL,
                policy_generation INTEGER NOT NULL,
                policy_version TEXT NOT NULL,
                canonical_fp_key TEXT,
                watermark INTEGER NOT NULL,
                last_attempt_no INTEGER NOT NULL,
                updated_wall_ms INTEGER NOT NULL,
                PRIMARY KEY(user_id, factor_id, ticket_generation)
            );
            CREATE TABLE IF NOT EXISTS tickets (
                user_id TEXT NOT NULL,
                factor_id TEXT NOT NULL,
                ticket_generation INTEGER NOT NULL,
                status TEXT NOT NULL,
                conclusion TEXT NOT NULL,
                issued_attempt_no INTEGER NOT NULL,
                issued_at_ms INTEGER NOT NULL,
                window_ms INTEGER NOT NULL,
                ticket_generation_max INTEGER NOT NULL,
                policy_generation INTEGER NOT NULL,
                policy_version TEXT NOT NULL,
                canonical_fp_key TEXT,
                watermark INTEGER NOT NULL,
                replay_attempt_nos TEXT NOT NULL DEFAULT '[]',
                revoke_reason TEXT,
                batch_seq INTEGER NOT NULL,
                PRIMARY KEY(user_id, factor_id, ticket_generation)
            );
            CREATE INDEX IF NOT EXISTS ix_tickets_user_generation
                ON tickets(user_id, ticket_generation);
            CREATE TABLE IF NOT EXISTS ticket_edges (
                parent TEXT NOT NULL,
                child TEXT NOT NULL,
                batch_seq INTEGER NOT NULL,
                PRIMARY KEY(parent, child)
            );
            CREATE TABLE IF NOT EXISTS revocations (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_node TEXT NOT NULL,
                closure_json TEXT NOT NULL,
                ticket_generations_json TEXT NOT NULL,
                batch_seq INTEGER NOT NULL,
                created_wall_ms INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS checkpoints (
                seq INTEGER PRIMARY KEY,
                checkpoint_id TEXT NOT NULL UNIQUE,
                watermark INTEGER NOT NULL,
                ledger_digest TEXT NOT NULL,
                snapshot_path TEXT NOT NULL,
                snapshot_digest TEXT NOT NULL,
                checkpoint_path TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                created_wall_ms INTEGER NOT NULL
            );
            """
        )
        self._set_meta_if_absent("schema_version", SCHEMA_VERSION)
        self._set_meta_if_absent("next_batch_seq", "1")
        self._set_meta_if_absent("last_watermark", "0")
        self._set_meta_if_absent("ledger_epoch", "0")

    def _set_meta_if_absent(self, key: str, value: str) -> None:
        self.connection.execute(
            "INSERT OR IGNORE INTO meta(key, value) VALUES (?, ?)",
            (key, value),
        )

    def get_meta(self, key: str, default: str | None = None) -> str | None:
        row = self.connection.execute(
            "SELECT value FROM meta WHERE key = ?", (key,)
        ).fetchone()
        return row["value"] if row is not None else default

    def _set_meta(self, key: str, value: Any) -> None:
        self.connection.execute(
            "INSERT INTO meta(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )

    def ensure_policy(self, view: PolicyView) -> dict[str, Any]:
        with self.transaction():
            previous_version = self.get_meta("policy_version")
            previous_layout = self.get_meta("layout_version")
            rebuilt = False
            reason: str | None = None
            if previous_version is None and previous_layout is None:
                rebuilt = True
                reason = "initial"
            elif (
                previous_version != view.policy_version
                or previous_layout != view.layout_version
            ):
                rebuilt = True
                reason = (
                    "layout_change"
                    if previous_layout != view.layout_version
                    else "policy_version_change"
                )
                for table in (
                    "decisions",
                    "decision_ledger",
                    "tickets",
                    "ticket_edges",
                    "revocations",
                    "checkpoints",
                ):
                    self.connection.execute(f"DELETE FROM {table}")
                epoch = int(self.get_meta("ledger_epoch", "0") or "0") + 1
                self._set_meta("ledger_epoch", epoch)
                self._set_meta("next_batch_seq", 1)
                self._set_meta("last_watermark", 0)

            self._set_meta("policy_version", view.policy_version)
            self._set_meta("policy_generation", view.policy_generation)
            self._set_meta("layout_version", view.layout_version)
            return {
                "rebuilt": rebuilt,
                "reason": reason,
                "previous_policy_version": previous_version,
                "previous_layout_version": previous_layout,
                "ledger_epoch": int(self.get_meta("ledger_epoch", "0") or "0"),
            }

    def allocate_batch_seq(self) -> int:
        with self.transaction():
            current = int(self.get_meta("next_batch_seq", "1") or "1")
            self._set_meta("next_batch_seq", current + 1)
            return current

    def mark_watermark(self, watermark: int) -> None:
        with self.transaction():
            previous = int(self.get_meta("last_watermark", "0") or "0")
            self._set_meta("last_watermark", max(previous, watermark))

    def find_ticket_by_user_generation(
        self, user_id: str, ticket_generation: int
    ) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM tickets "
            "WHERE user_id = ? AND ticket_generation = ? "
            "ORDER BY issued_attempt_no LIMIT 1",
            (user_id, ticket_generation),
        ).fetchone()
        if row is None:
            return None
        return self._ticket_row_to_dict(row)

    def find_ticket(
        self, user_id: str, factor_id: str, ticket_generation: int
    ) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM tickets "
            "WHERE user_id = ? AND factor_id = ? AND ticket_generation = ?",
            (user_id, factor_id, ticket_generation),
        ).fetchone()
        return self._ticket_row_to_dict(row) if row is not None else None

    @staticmethod
    def _ticket_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
        value = dict(row)
        value["replay_attempt_nos"] = json.loads(value["replay_attempt_nos"])
        return value

    def insert_decision(
        self,
        *,
        result: DecisionResult,
        attempt_no: int,
        user_id: str,
        factor_id: str,
        ticket_generation: int,
        watermark: int,
        batch_seq: int,
        wall_ms: int,
    ) -> None:
        detail = result.to_dict()
        detail.pop("latency_ms", None)
        self.connection.execute(
            """
            INSERT INTO decisions(
                attempt_no, user_id, factor_id, ticket_generation, conclusion,
                policy_generation, policy_version, canonical_fp_key, watermark,
                reasons_json, detail_json, latency_ms, batch_seq, created_wall_ms
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                attempt_no,
                user_id,
                factor_id,
                ticket_generation,
                result.status,
                result.policy_generation,
                result.policy_version,
                result.canonical_fp_key,
                watermark,
                json.dumps(result.reasons, sort_keys=True),
                json.dumps(detail, sort_keys=True, ensure_ascii=False),
                result.latency_ms,
                batch_seq,
                wall_ms,
            ),
        )
        self.connection.execute(
            """
            INSERT INTO decision_ledger(
                user_id, factor_id, ticket_generation, conclusion,
                policy_generation, policy_version, canonical_fp_key,
                watermark, last_attempt_no, updated_wall_ms
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id, factor_id, ticket_generation) DO UPDATE SET
                conclusion=excluded.conclusion,
                policy_generation=excluded.policy_generation,
                policy_version=excluded.policy_version,
                canonical_fp_key=excluded.canonical_fp_key,
                watermark=excluded.watermark,
                last_attempt_no=excluded.last_attempt_no,
                updated_wall_ms=excluded.updated_wall_ms
            """,
            (
                user_id,
                factor_id,
                ticket_generation,
                result.status,
                result.policy_generation,
                result.policy_version,
                result.canonical_fp_key,
                watermark,
                attempt_no,
                wall_ms,
            ),
        )

    def issue_ticket(
        self,
        *,
        user_id: str,
        factor_id: str,
        ticket_generation: int,
        attempt_no: int,
        submitted_at_ms: int,
        trusted: TrustedFactor,
        policy_version: str,
        canonical_fp_key: str | None,
        watermark: int,
        batch_seq: int,
    ) -> dict[str, Any]:
        self.connection.execute(
            """
            INSERT INTO tickets(
                user_id, factor_id, ticket_generation, status, conclusion,
                issued_attempt_no, issued_at_ms, window_ms, ticket_generation_max, policy_generation,
                policy_version, canonical_fp_key, watermark, replay_attempt_nos,
                revoke_reason, batch_seq
            ) VALUES (?, ?, ?, 'ISSUED', 'APPROVED', ?, ?, ?, ?, ?, ?, ?, ?, '[]', NULL, ?)
            """,
            (
                user_id,
                factor_id,
                ticket_generation,
                attempt_no,
                submitted_at_ms,
                trusted.verification_window_ms,
                trusted.ticket_generation_max,
                trusted.policy_generation,
                policy_version,
                canonical_fp_key,
                watermark,
                batch_seq,
            ),
        )
        self._add_edge(user_node(user_id), factor_node(factor_id), batch_seq)
        self._add_edge(
            factor_node(factor_id),
            ticket_node(user_id, factor_id, ticket_generation),
            batch_seq,
        )
        return {
            "ticket_generation": ticket_generation,
            "status": "ISSUED",
            "issued_attempt_no": attempt_no,
            "issued_at_ms": submitted_at_ms,
            "window_ms": trusted.verification_window_ms,
            "ticket_generation_max": trusted.ticket_generation_max,
        }

    def append_replay(
        self,
        *,
        user_id: str,
        factor_id: str,
        ticket_generation: int,
        attempt_no: int,
    ) -> dict[str, Any]:
        ticket = self.find_ticket(user_id, factor_id, ticket_generation)
        if ticket is None:
            # The strict replay identity is user + ticket generation. The
            # factor is retained in the trace for diagnostics.
            ticket = self.find_ticket_by_user_generation(user_id, ticket_generation)
        if ticket is None:
            raise MFAError(
                Codes.TICKET_NOT_FOUND,
                "replay ledger entry disappeared",
                {"user_id": user_id, "ticket_generation": ticket_generation},
            )
        replay_attempts = list(ticket["replay_attempt_nos"])
        if attempt_no not in replay_attempts:
            replay_attempts.append(attempt_no)
        self.connection.execute(
            "UPDATE tickets SET replay_attempt_nos = ?, conclusion='REJECTED' "
            "WHERE user_id = ? AND factor_id = ? AND ticket_generation = ?",
            (
                json.dumps(replay_attempts),
                ticket["user_id"],
                ticket["factor_id"],
                ticket["ticket_generation"],
            ),
        )
        return {
            "first_attempt_no": ticket["issued_attempt_no"],
            "second_attempt_no": attempt_no,
            "attempt_nos": [ticket["issued_attempt_no"], *replay_attempts],
            "ticket_generation": ticket["ticket_generation"],
            "first_factor_id": ticket["factor_id"],
        }

    def _add_edge(self, parent: str, child: str, batch_seq: int) -> None:
        self.connection.execute(
            "INSERT OR IGNORE INTO ticket_edges(parent, child, batch_seq) VALUES (?, ?, ?)",
            (parent, child, batch_seq),
        )

    def add_ticket_edge(self, parent: str, child: str, batch_seq: int = 0) -> None:
        with self.transaction():
            self._add_edge(parent, child, batch_seq)

    def renew_ticket(
        self,
        *,
        user_id: str,
        factor_id: str,
        ticket_generation: int,
        now_ms: int,
        batch_seq: int = 0,
    ) -> dict[str, Any]:
        with self.transaction():
            ticket = self.find_ticket(user_id, factor_id, ticket_generation)
            if ticket is None:
                raise MFAError(
                    Codes.TICKET_NOT_FOUND,
                    "ticket does not exist",
                    {
                        "user_id": user_id,
                        "factor_id": factor_id,
                        "ticket_generation": ticket_generation,
                    },
                )
            if ticket["status"] == "REVOKED":
                raise MFAError(
                    Codes.TICKET_REVOKED,
                    "ticket has been revoked",
                    {"ticket_generation": ticket_generation},
                )
            issued_at = int(ticket["issued_at_ms"])
            if now_ms < issued_at or now_ms - issued_at > int(ticket["window_ms"]):
                raise MFAError(
                    Codes.TICKET_WINDOW_EXPIRED,
                    "ticket renewal is outside the verification window",
                    {
                        "issued_at_ms": issued_at,
                        "now_ms": now_ms,
                        "window_ms": ticket["window_ms"],
                    },
                )
            current_generation = int(ticket["ticket_generation"])
            policy_generation = int(ticket["policy_generation"])
            policy_version = str(ticket["policy_version"])
            max_generation = int(ticket["ticket_generation_max"])
            if current_generation >= max_generation:
                raise MFAError(
                    Codes.TICKET_GENERATION_EXCEEDED,
                    "ticket generation cap has been reached",
                    {
                        "ticket_generation": current_generation,
                        "ticket_generation_max": max_generation,
                    },
                )
            new_generation = current_generation + 1
            existing = self.find_ticket_by_user_generation(user_id, new_generation)
            if existing is not None:
                raise MFAError(
                    Codes.TICKET_REPLAY,
                    "new ticket generation is already in use",
                    {
                        "ticket_generation": new_generation,
                        "first_attempt_no": existing["issued_attempt_no"],
                    },
                )
            self.connection.execute(
                "UPDATE tickets SET status='RENEWED', conclusion='RENEWED' "
                "WHERE user_id=? AND factor_id=? AND ticket_generation=?",
                (user_id, factor_id, ticket_generation),
            )
            self.connection.execute(
                """
                INSERT INTO tickets(
                    user_id, factor_id, ticket_generation, status, conclusion,
                    issued_attempt_no, issued_at_ms, window_ms, ticket_generation_max, policy_generation,
                    policy_version, canonical_fp_key, watermark, replay_attempt_nos,
                    revoke_reason, batch_seq
                ) VALUES (?, ?, ?, 'ISSUED', 'APPROVED', ?, ?, ?, ?, ?, ?, ?, ?, '[]', NULL, ?)
                """,
                (
                    user_id,
                    factor_id,
                    new_generation,
                    ticket["issued_attempt_no"],
                    now_ms,
                    ticket["window_ms"],
                    ticket["ticket_generation_max"],
                    policy_generation,
                    policy_version,
                    ticket["canonical_fp_key"],
                    now_ms,
                    batch_seq,
                ),
            )
            self._add_edge(
                ticket_node(user_id, factor_id, ticket_generation),
                ticket_node(user_id, factor_id, new_generation),
                batch_seq,
            )
            return {
                "status": "RENEWED",
                "old_ticket_generation": ticket_generation,
                "new_ticket_generation": new_generation,
                "issued_at_ms": now_ms,
                "window_ms": ticket["window_ms"],
            }

    def set_ticket_generation_max(self, maximum: int) -> None:
        with self.transaction():
            self._set_meta("ticket_generation_max", maximum)

    def _graph(self) -> dict[str, list[str]]:
        graph: dict[str, list[str]] = {}
        for row in self.connection.execute(
            "SELECT parent, child FROM ticket_edges ORDER BY parent, child"
        ):
            graph.setdefault(row["parent"], []).append(row["child"])
        return graph

    @staticmethod
    def _find_cycle(
        graph: dict[str, list[str]], start: str
    ) -> list[str] | None:
        colors: dict[str, int] = {}
        stack: list[str] = []

        def visit(node: str) -> list[str] | None:
            colors[node] = 1
            stack.append(node)
            for child in graph.get(node, []):
                color = colors.get(child, 0)
                if color == 0:
                    cycle = visit(child)
                    if cycle is not None:
                        return cycle
                elif color == 1:
                    index = stack.index(child)
                    return stack[index:] + [child]
            stack.pop()
            colors[node] = 2
            return None

        if start in graph or any(start == parent for parent in graph):
            return visit(start)
        return None

    def revoke(
        self,
        *,
        source_user: str | None = None,
        source_factor: str | None = None,
        reason: str,
        batch_seq: int = 0,
        wall_ms: int = 0,
    ) -> dict[str, Any]:
        if (source_user is None) == (source_factor is None):
            raise MFAError(
                Codes.INPUT_STRUCTURE,
                "exactly one of source_user or source_factor must be supplied",
            )
        start = user_node(source_user) if source_user is not None else factor_node(source_factor or "")
        with self.transaction():
            graph = self._graph()
            cycle = self._find_cycle(graph, start)
            if cycle is not None:
                ticket_generations = []
                for node in cycle:
                    if node.startswith("ticket:"):
                        try:
                            ticket_generations.append(int(node.rsplit(":", 1)[1]))
                        except ValueError:
                            pass
                raise MFAError(
                    Codes.TICKET_CYCLE,
                    "cycle detected in ticket dependency graph",
                    {
                        "cycle_nodes": cycle,
                        "ticket_generations": sorted(set(ticket_generations)),
                    },
                )

            closure: list[str] = []
            seen: set[str] = set()
            stack = [start]
            while stack:
                node = stack.pop()
                if node in seen:
                    continue
                seen.add(node)
                closure.append(node)
                stack.extend(sorted(graph.get(node, []), reverse=True))

            ticket_rows = self.connection.execute("SELECT * FROM tickets").fetchall()
            node_to_ticket = {
                ticket_node(row["user_id"], row["factor_id"], row["ticket_generation"]): row
                for row in ticket_rows
            }
            revoked_tickets: list[dict[str, Any]] = []
            for node in sorted(seen):
                row = node_to_ticket.get(node)
                if row is None:
                    continue
                self.connection.execute(
                    "UPDATE tickets SET status='REVOKED', conclusion='REVOKED', revoke_reason=? "
                    "WHERE user_id=? AND factor_id=? AND ticket_generation=?",
                    (
                        reason,
                        row["user_id"],
                        row["factor_id"],
                        row["ticket_generation"],
                    ),
                )
                revoked_tickets.append(
                    {
                        "user_id": row["user_id"],
                        "factor_id": row["factor_id"],
                        "ticket_generation": row["ticket_generation"],
                        "issued_attempt_no": row["issued_attempt_no"],
                    }
                )
            self.connection.execute(
                """
                INSERT INTO revocations(
                    source_node, closure_json, ticket_generations_json,
                    batch_seq, created_wall_ms
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    start,
                    json.dumps(sorted(closure)),
                    json.dumps([item["ticket_generation"] for item in revoked_tickets]),
                    batch_seq,
                    wall_ms,
                ),
            )
            return {
                "status": "REVOKED",
                "source_node": start,
                "closure": sorted(closure),
                "tickets": sorted(
                    revoked_tickets,
                    key=lambda item: (
                        item["user_id"],
                        item["factor_id"],
                        item["ticket_generation"],
                    ),
                ),
            }

    def revoke_user(self, user_id: str, **kwargs: Any) -> dict[str, Any]:
        return self.revoke(source_user=user_id, **kwargs)

    def revoke_factor(self, factor_id: str, **kwargs: Any) -> dict[str, Any]:
        return self.revoke(source_factor=factor_id, **kwargs)

    def list_checkpoints(self) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT * FROM checkpoints ORDER BY seq"
        ).fetchall()
        return [dict(row) for row in rows]

    def put_checkpoint(
        self,
        *,
        seq: int,
        checkpoint_id: str,
        watermark: int,
        ledger_digest: str,
        snapshot_path: str,
        snapshot_digest: str,
        checkpoint_path: str,
        payload: dict[str, Any],
        created_wall_ms: int,
    ) -> None:
        with self.transaction():
            self.connection.execute(
                """
                INSERT INTO checkpoints(
                    seq, checkpoint_id, watermark, ledger_digest,
                    snapshot_path, snapshot_digest, checkpoint_path,
                    payload_json, created_wall_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(seq) DO UPDATE SET
                    checkpoint_id=excluded.checkpoint_id,
                    watermark=excluded.watermark,
                    ledger_digest=excluded.ledger_digest,
                    snapshot_path=excluded.snapshot_path,
                    snapshot_digest=excluded.snapshot_digest,
                    checkpoint_path=excluded.checkpoint_path,
                    payload_json=excluded.payload_json,
                    created_wall_ms=excluded.created_wall_ms
                """,
                (
                    seq,
                    checkpoint_id,
                    watermark,
                    ledger_digest,
                    snapshot_path,
                    snapshot_digest,
                    checkpoint_path,
                    json.dumps(payload, ensure_ascii=False, sort_keys=True),
                    created_wall_ms,
                ),
            )

    def _semantic_state(self) -> dict[str, Any]:
        decisions = []
        for row in self.connection.execute(
            "SELECT * FROM decisions ORDER BY attempt_no"
        ):
            decisions.append(
                {
                    "attempt_no": row["attempt_no"],
                    "user_id": row["user_id"],
                    "factor_id": row["factor_id"],
                    "ticket_generation": row["ticket_generation"],
                    "conclusion": row["conclusion"],
                    "policy_generation": row["policy_generation"],
                    "policy_version": row["policy_version"],
                    "canonical_fp_key": row["canonical_fp_key"],
                    "watermark": row["watermark"],
                    "reasons": json.loads(row["reasons_json"]),
                    "detail": json.loads(row["detail_json"]),
                }
            )
        decision_ledger = [
            {
                "user_id": row["user_id"],
                "factor_id": row["factor_id"],
                "ticket_generation": row["ticket_generation"],
                "conclusion": row["conclusion"],
                "policy_generation": row["policy_generation"],
                "policy_version": row["policy_version"],
                "canonical_fp_key": row["canonical_fp_key"],
                "watermark": row["watermark"],
                "last_attempt_no": row["last_attempt_no"],
            }
            for row in self.connection.execute(
                "SELECT * FROM decision_ledger "
                "ORDER BY user_id, factor_id, ticket_generation"
            )
        ]
        tickets = []
        for row in self.connection.execute(
            "SELECT * FROM tickets ORDER BY user_id, factor_id, ticket_generation"
        ):
            tickets.append(
                {
                    "user_id": row["user_id"],
                    "factor_id": row["factor_id"],
                    "ticket_generation": row["ticket_generation"],
                    "status": row["status"],
                    "conclusion": row["conclusion"],
                    "issued_attempt_no": row["issued_attempt_no"],
                    "issued_at_ms": row["issued_at_ms"],
                    "window_ms": row["window_ms"],
                    "ticket_generation_max": row["ticket_generation_max"],
                    "policy_generation": row["policy_generation"],
                    "policy_version": row["policy_version"],
                    "canonical_fp_key": row["canonical_fp_key"],
                    "watermark": row["watermark"],
                    "replay_attempt_nos": json.loads(row["replay_attempt_nos"]),
                    "revoke_reason": row["revoke_reason"],
                }
            )
        edges = [
            {"parent": row["parent"], "child": row["child"]}
            for row in self.connection.execute(
                "SELECT parent, child FROM ticket_edges ORDER BY parent, child"
            )
        ]
        revocations = [
            {
                "source_node": row["source_node"],
                "closure": json.loads(row["closure_json"]),
                "ticket_generations": json.loads(row["ticket_generations_json"]),
            }
            for row in self.connection.execute(
                "SELECT * FROM revocations ORDER BY event_id"
            )
        ]
        return {
            "schema_version": self.get_meta("schema_version"),
            "policy_version": self.get_meta("policy_version"),
            "policy_generation": self.get_meta("policy_generation"),
            "layout_version": self.get_meta("layout_version"),
            "decisions": decisions,
            "decision_ledger": decision_ledger,
            "tickets": tickets,
            "ticket_edges": edges,
            "revocations": revocations,
        }

    def ledger_digest(self) -> str:
        return sha256_canonical(self._semantic_state())

    def export_snapshot(self) -> dict[str, Any]:
        with self._lock:
            state = {
                "meta": [dict(row) for row in self.connection.execute(
                    "SELECT key, value FROM meta ORDER BY key"
                )],
                "decisions": [dict(row) for row in self.connection.execute(
                    "SELECT * FROM decisions ORDER BY event_id"
                )],
                "decision_ledger": [dict(row) for row in self.connection.execute(
                    "SELECT * FROM decision_ledger "
                    "ORDER BY user_id, factor_id, ticket_generation"
                )],
                "tickets": [dict(row) for row in self.connection.execute(
                    "SELECT * FROM tickets ORDER BY user_id, factor_id, ticket_generation"
                )],
                "ticket_edges": [dict(row) for row in self.connection.execute(
                    "SELECT * FROM ticket_edges ORDER BY parent, child"
                )],
                "revocations": [dict(row) for row in self.connection.execute(
                    "SELECT * FROM revocations ORDER BY event_id"
                )],
            }
            return {
                "ledger_digest": self.ledger_digest(),
                "state": state,
            }

    def restore_snapshot(self, state: dict[str, Any]) -> None:
        with self.transaction():
            for table in (
                "decisions",
                "decision_ledger",
                "tickets",
                "ticket_edges",
                "revocations",
                "meta",
            ):
                self.connection.execute(f"DELETE FROM {table}")
            for row in state.get("meta", []):
                self.connection.execute(
                    "INSERT INTO meta(key, value) VALUES (?, ?)",
                    (row["key"], row["value"]),
                )
            for row in state.get("decisions", []):
                self.connection.execute(
                    """
                    INSERT INTO decisions(
                        event_id, attempt_no, user_id, factor_id, ticket_generation,
                        conclusion, policy_generation, policy_version,
                        canonical_fp_key, watermark, reasons_json, detail_json,
                        latency_ms, batch_seq, created_wall_ms
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        row["event_id"], row["attempt_no"], row["user_id"],
                        row["factor_id"], row["ticket_generation"], row["conclusion"],
                        row["policy_generation"], row["policy_version"],
                        row["canonical_fp_key"], row["watermark"],
                        row["reasons_json"], row["detail_json"], row["latency_ms"],
                        row["batch_seq"], row["created_wall_ms"],
                    ),
                )
            for row in state.get("decision_ledger", []):
                self.connection.execute(
                    """
                    INSERT INTO decision_ledger(
                        user_id, factor_id, ticket_generation, conclusion,
                        policy_generation, policy_version, canonical_fp_key,
                        watermark, last_attempt_no, updated_wall_ms
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        row["user_id"],
                        row["factor_id"],
                        row["ticket_generation"],
                        row["conclusion"],
                        row["policy_generation"],
                        row["policy_version"],
                        row["canonical_fp_key"],
                        row["watermark"],
                        row["last_attempt_no"],
                        row["updated_wall_ms"],
                    ),
                )
            for row in state.get("tickets", []):
                self.connection.execute(
                    """
                    INSERT INTO tickets(
                        user_id, factor_id, ticket_generation, status, conclusion,
                        issued_attempt_no, issued_at_ms, window_ms,
                        ticket_generation_max, policy_generation, policy_version, canonical_fp_key,
                        watermark, replay_attempt_nos, revoke_reason, batch_seq
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        row["user_id"], row["factor_id"], row["ticket_generation"],
                        row["status"], row["conclusion"], row["issued_attempt_no"], row["issued_at_ms"],
                        row["window_ms"], row["ticket_generation_max"], row["policy_generation"],
                        row["policy_version"], row["canonical_fp_key"],
                        row["watermark"], row["replay_attempt_nos"],
                        row["revoke_reason"], row["batch_seq"],
                    ),
                )
            for row in state.get("ticket_edges", []):
                self.connection.execute(
                    "INSERT INTO ticket_edges(parent, child, batch_seq) VALUES (?, ?, ?)",
                    (row["parent"], row["child"], row["batch_seq"]),
                )
            for row in state.get("revocations", []):
                self.connection.execute(
                    """
                    INSERT INTO revocations(
                        event_id, source_node, closure_json,
                        ticket_generations_json, batch_seq, created_wall_ms
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        row["event_id"], row["source_node"], row["closure_json"],
                        row["ticket_generations_json"], row["batch_seq"],
                        row["created_wall_ms"],
                    ),
                )

    def counts(self) -> dict[str, int]:
        result: dict[str, int] = {}
        for table in (
            "decisions",
            "decision_ledger",
            "tickets",
            "ticket_edges",
            "revocations",
            "checkpoints",
        ):
            row = self.connection.execute(
                f"SELECT COUNT(*) AS count FROM {table}"
            ).fetchone()
            result[table] = int(row["count"])
        return result

