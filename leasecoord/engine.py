from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Any, Iterable

from .clock import ClockAdvance, advance_clock, clock_fixed, parse_clock
from .config import ClusterConfig, ElectionConfig, NodeConfig, parse_cluster
from .db import Database, canonical_json, stable_hash
from .errors import BusinessError, fail
from .numeric import (
    FixedValue,
    exact_product_fixed,
    fixed_from_fraction,
    fraction,
    fraction_abs,
    fraction_text,
)


CRASH_EXIT_CODE = 86


@dataclass(frozen=True)
class LeaseView:
    node_name: str
    lease_generation: int
    session_epoch: int
    data_version: int
    granted: Fraction
    expires: Fraction
    status: str
    last_renew_key: str | None

    def remaining(self, clock: Fraction) -> Fraction:
        return self.expires - clock

    def key_dict(self) -> dict[str, Any]:
        return {
            "node": self.node_name,
            "lease_generation": self.lease_generation,
            "session_epoch": self.session_epoch,
            "data_version": self.data_version,
            "granted_ms": fraction_text(self.granted),
            "expires_ms": fraction_text(self.expires),
        }


@dataclass
class Evaluation:
    clock: Fraction
    leader: str | None
    leader_source: str
    eligible: list[dict[str, Any]]
    ranking: list[dict[str, Any]]
    rejections: list[dict[str, Any]]
    first_rejection: dict[str, Any] | None
    boundary_candidates: list[str]
    precision_sensitive: bool
    precision_pairs: list[dict[str, Any]]
    quorum_satisfied: bool
    election_key: str
    election_id: str
    state: dict[str, Any]


class Engine:
    def __init__(self, workspace: str | Path) -> None:
        self.workspace = Path(workspace).resolve()
        self.db = Database(self.workspace)

    # ------------------------------------------------------------------
    # Loading and bootstrap
    # ------------------------------------------------------------------
    def load(
        self,
        nodes_document: Any,
        election_document: Any,
        *,
        clock: Any = "0",
        reset: bool = False,
    ) -> dict[str, Any]:
        # All candidate/config validation happens before opening or writing SQLite.
        config = parse_cluster(nodes_document, election_document)
        requested, requested_fixed = parse_clock(clock, config.election)

        with self.db.exclusive():
            if self.db.exists():
                self.db.require_loaded()
                self._validate_session_watermarks(config)
                with self.db.transaction(immediate=False) as connection:
                    existing = connection.execute(
                        "SELECT config_hash FROM cluster_config WHERE singleton = 1"
                    ).fetchone()
                    state = self.db.clock_row(connection)
                if existing is not None and existing["config_hash"] == config.config_hash and not reset:
                    with self.db.transaction(immediate=False) as connection:
                        state = self.db.clock_row(connection)
                    return {
                        "ok": True,
                        "command": "load",
                        "idempotent": True,
                        "config_hash": config.config_hash,
                        "clock_ms": state["clock_text"] if state else fraction_text(requested),
                        "nodes": [node.as_dict() for node in config.nodes],
                        "conversions": self._load_conversions(config, requested_fixed),
                    }
                if existing is not None and existing["config_hash"] != config.config_hash and not reset:
                    fail(
                        "E_CONFIG_CHANGED",
                        "Configuration changed; use --reset to create a new deterministic runtime",
                        details={
                            "persisted_config_hash": existing["config_hash"],
                            "incoming_config_hash": config.config_hash,
                        },
                    )

            self.db.initialize()
            with self.db.transaction() as connection:
                if reset:
                    for table in (
                        "precision_records",
                        "elections",
                        "renewals",
                        "leases",
                        "lease_history",
                        "prepared_ops",
                        "audit_events",
                        "cluster_state",
                        "nodes",
                        "cluster_config",
                    ):
                        connection.execute(f"DELETE FROM {table}")
                for node in config.nodes:
                    existing_session = connection.execute(
                        "SELECT max_session_epoch FROM session_watermarks WHERE name = ?",
                        (node.name,),
                    ).fetchone()
                    max_session = max(
                        int(existing_session["max_session_epoch"]) if existing_session else 0,
                        node.session_epoch,
                    )
                    connection.execute(
                        """
                        INSERT INTO session_watermarks(name, max_session_epoch)
                        VALUES (?, ?)
                        ON CONFLICT(name) DO UPDATE SET max_session_epoch = excluded.max_session_epoch
                        """,
                        (node.name, max_session),
                    )
                    connection.execute(
                        """
                        INSERT INTO nodes(
                            name, priority, lease_ms, heartbeat_ms, session_epoch, data_version
                        ) VALUES (?, ?, ?, ?, ?, ?)
                        ON CONFLICT(name) DO UPDATE SET
                            priority = excluded.priority,
                            lease_ms = excluded.lease_ms,
                            heartbeat_ms = excluded.heartbeat_ms,
                            session_epoch = excluded.session_epoch,
                            data_version = excluded.data_version
                        """,
                        (
                            node.name,
                            node.priority,
                            node.lease_ms,
                            node.heartbeat_ms,
                            node.session_epoch,
                            node.data_version,
                        ),
                    )
                config_document = {
                    "nodes": [node.as_dict() for node in config.nodes],
                    "election": config.election.as_dict(),
                }
                connection.execute(
                    """
                    INSERT INTO cluster_config(singleton, config_json, config_hash)
                    VALUES (1, ?, ?)
                    ON CONFLICT(singleton) DO UPDATE SET
                        config_json = excluded.config_json,
                        config_hash = excluded.config_hash
                    """,
                    (canonical_json(config_document), config.config_hash),
                )
                fixed = clock_fixed(requested, config.election)
                connection.execute(
                    """
                    INSERT INTO cluster_state(
                        singleton, clock_text, clock_num, clock_den, clock_units,
                        clock_residual_num, clock_residual_den, leader, election_id,
                        config_hash, epoch_counter
                    ) VALUES (1, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, 0)
                    ON CONFLICT(singleton) DO UPDATE SET
                        clock_text = excluded.clock_text,
                        clock_num = excluded.clock_num,
                        clock_den = excluded.clock_den,
                        clock_units = excluded.clock_units,
                        clock_residual_num = excluded.clock_residual_num,
                        clock_residual_den = excluded.clock_residual_den,
                        leader = NULL,
                        election_id = NULL,
                        config_hash = excluded.config_hash,
                        epoch_counter = 0
                    """,
                    (
                        fraction_text(requested),
                        str(requested.numerator),
                        str(requested.denominator),
                        fixed.units,
                        str(fixed.residual.numerator),
                        str(fixed.residual.denominator),
                        config.config_hash,
                    ),
                )
        return {
            "ok": True,
            "command": "load",
            "idempotent": False,
            "reset": reset,
            "config_hash": config.config_hash,
            "clock_ms": fraction_text(requested),
            "wal": True,
            "nodes": [node.as_dict() for node in config.nodes],
            "conversions": self._load_conversions(config, requested_fixed),
        }

    def _validate_session_watermarks(self, config: ClusterConfig) -> None:
        with self.db.transaction(immediate=False) as connection:
            for node in config.nodes:
                row = connection.execute(
                    "SELECT max_session_epoch FROM session_watermarks WHERE name = ?",
                    (node.name,),
                ).fetchone()
                if row is not None and node.session_epoch < int(row["max_session_epoch"]):
                    fail(
                        "E_SESSION_REGRESSION",
                        "Session epoch must be monotonic for each node",
                        details={
                            "node": node.name,
                            "incoming_session_epoch": node.session_epoch,
                            "persisted_max_session_epoch": int(row["max_session_epoch"]),
                        },
                    )

    def _load_conversions(self, config: ClusterConfig, clock_fixed_value: FixedValue) -> dict[str, Any]:
        ratio = fixed_from_fraction(
            config.election.safety_margin_ratio,
            config.election.scale,
            label="safety_margin_ratio",
            raw_value=config.election.safety_margin_raw,
            raw_float=float(config.election.safety_margin_ratio),
        )
        nodes: list[dict[str, Any]] = []
        for node in config.nodes:
            lease = fixed_from_fraction(
                Fraction(node.lease_ms, 1),
                config.election.scale,
                label="lease_ms",
                raw_value=str(node.lease_ms),
                raw_float=float(node.lease_ms),
            )
            heartbeat = fixed_from_fraction(
                Fraction(node.heartbeat_ms, 1),
                config.election.scale,
                label="heartbeat_ms",
                raw_value=str(node.heartbeat_ms),
                raw_float=float(node.heartbeat_ms),
            )
            safety = exact_product_fixed(
                node.lease_ms,
                config.election.safety_margin_ratio,
                config.election.scale,
            )
            nodes.append(
                {
                    "node": node.name,
                    "lease": lease.as_dict(),
                    "heartbeat": heartbeat.as_dict(),
                    "safety_margin": safety.as_dict(),
                }
            )
        return {
            "clock": clock_fixed_value.as_dict(),
            "safety_margin_ratio": ratio.as_dict(),
            "nodes": nodes,
        }

    # ------------------------------------------------------------------
    # Election
    # ------------------------------------------------------------------
    def elect(self, *, clock: Any, crash_at: str | None = None) -> dict[str, Any]:
        self.db.require_loaded()
        with self.db.exclusive():
            with self.db.transaction() as connection:
                config = self.db.read_cluster(connection)
                state = self._require_state(connection)
                previous = self._state_clock(state)
                advance = advance_clock(clock, previous, config.election)
                current = self._lease_views(connection)
                projected, bootstrapped = self._project_bootstrap(
                    config, current, advance.effective
                )
                evaluation = self._evaluate(
                    config,
                    advance.effective,
                    projected,
                    state["leader"],
                )
                op_id = "elect_" + evaluation.election_key[:40]
                payload = {
                    "base_state_hash": self._state_hash(connection, advance.effective),
                    "clock": fraction_text(advance.effective),
                    "requested_clock": fraction_text(advance.requested),
                    "rewind_tolerated": advance.rewind_tolerated,
                    "rewind_ms": fraction_text(advance.rewind_ms),
                    "bootstrapped": bootstrapped,
                    "election_key": evaluation.election_key,
                }
                cached = self._prepare(
                    connection,
                    op_id=op_id,
                    kind="elect",
                    request_key=evaluation.election_key,
                    clock_text=fraction_text(advance.effective),
                    payload=payload,
                )
                if cached is not None:
                    return cached

            self._crash(crash_at, "after_prepare")
            with self.db.transaction() as connection:
                self._assert_state_hash(
                    connection, advance.effective, payload["base_state_hash"]
                )
                config = self.db.read_cluster(connection)
                state = self._require_state(connection)
                current = self._lease_views(connection)
                projected, bootstrapped = self._project_bootstrap(
                    config, current, advance.effective
                )
                if bootstrapped:
                    self._insert_bootstrap_leases(
                        connection, config, projected, bootstrapped, advance.effective
                    )
                self._persist_all_lease_snapshots(connection, config, projected, advance.effective)
                evaluation = self._evaluate(
                    config,
                    advance.effective,
                    projected,
                    state["leader"],
                )
                response = self._election_response(
                    "elect",
                    config,
                    evaluation,
                    advance,
                    bootstrapped,
                    idempotent=False,
                )
                self._persist_election(connection, evaluation, config, op_id)
                new_epoch = int(state["epoch_counter"]) + 1
                self._set_clock_state(
                    connection,
                    advance.effective,
                    config,
                    evaluation.leader,
                    evaluation.election_id,
                    new_epoch,
                )
                connection.execute(
                    """
                    UPDATE prepared_ops
                    SET status = 'COMMITTED', response_json = ?
                    WHERE op_id = ?
                    """,
                    (canonical_json(response), op_id),
                )
                self._crash(crash_at, "after_final_insert")
            self._crash(crash_at, "after_commit")
            return response

    # ------------------------------------------------------------------
    # Renewal
    # ------------------------------------------------------------------
    def renew(
        self,
        *,
        node: str,
        session_epoch: int,
        clock: Any,
        lease_generation: int | None = None,
        data_version: int | None = None,
        crash_at: str | None = None,
    ) -> dict[str, Any]:
        self.db.require_loaded()
        validation_error: BusinessError | None = None
        prepared_op_id: str | None = None
        request_key: str | None = None
        advance: ClockAdvance | None = None
        old_expiry: Fraction | None = None
        expected_expiry: Fraction | None = None
        resolved_generation = lease_generation
        resolved_data_version = data_version
        with self.db.exclusive():
            with self.db.transaction() as connection:
                config = self.db.read_cluster(connection)
                state = self._require_state(connection)
                try:
                    advance = advance_clock(clock, self._state_clock(state), config.election)
                except BusinessError as exc:
                    validation_error = exc
                node_config = config.node_map.get(node)
                if validation_error is None and node_config is None:
                    validation_error = BusinessError(
                        "E_UNKNOWN_NODE",
                        "Renewal node is not part of the candidate set",
                        details={"node": node},
                    )
                if validation_error is None and (
                    isinstance(session_epoch, bool) or not isinstance(session_epoch, int)
                ):
                    validation_error = BusinessError(
                        "E_BAD_SESSION",
                        "Requested session epoch must be an integer",
                        details={"node": node, "session_epoch": session_epoch},
                    )
                lease_row = connection.execute(
                    "SELECT * FROM leases WHERE node_name = ?", (node,)
                ).fetchone()
                if validation_error is None and lease_row is None:
                    validation_error = BusinessError(
                        "E_NOT_LEASE_HOLDER",
                        "Node does not currently hold a lease",
                        details={"node": node},
                    )
                if validation_error is None and lease_row is not None:
                    lease = self._lease_from_row(lease_row)
                    assert advance is not None and node_config is not None
                    if isinstance(resolved_generation, bool) or (
                        resolved_generation is not None
                        and not isinstance(resolved_generation, int)
                    ):
                        validation_error = BusinessError(
                            "E_BAD_LEASE",
                            "Lease generation must be an integer",
                            details={"node": node, "lease_generation": resolved_generation},
                        )
                    if validation_error is None and (
                        session_epoch < node_config.session_epoch
                        or session_epoch < lease.session_epoch
                    ):
                        validation_error = BusinessError(
                            "E_STALE_SESSION",
                            "Renewal session epoch is behind the current session",
                            details={
                                "node": node,
                                "requested_session_epoch": session_epoch,
                                "node_session_epoch": node_config.session_epoch,
                                "lease_session_epoch": lease.session_epoch,
                            },
                        )
                    elif validation_error is None and (
                        session_epoch != node_config.session_epoch
                        or session_epoch != lease.session_epoch
                    ):
                        validation_error = BusinessError(
                            "E_SESSION_MISMATCH",
                            "Renewal session epoch does not own the current lease",
                            details={
                                "node": node,
                                "requested_session_epoch": session_epoch,
                                "node_session_epoch": node_config.session_epoch,
                                "lease_session_epoch": lease.session_epoch,
                            },
                        )
                    elif validation_error is None and lease.status != "active":
                        validation_error = BusinessError(
                            "E_LEASE_EXPIRED",
                            "Lease is no longer active",
                            details={"node": node, "status": lease.status},
                        )
                    elif validation_error is None and lease.remaining(advance.effective) <= 0:
                        validation_error = BusinessError(
                            "E_LEASE_EXPIRED",
                            "Lease has expired at the supplied logical clock",
                            details={
                                "node": node,
                                "clock_ms": fraction_text(advance.effective),
                                "expires_ms": fraction_text(lease.expires),
                            },
                        )
                    elif validation_error is None:
                        if resolved_generation is None:
                            resolved_generation = lease.lease_generation
                        elif resolved_generation != lease.lease_generation:
                            validation_error = BusinessError(
                                "E_STALE_LEASE",
                                "Renewal lease generation does not match the held lease",
                                details={
                                    "node": node,
                                    "requested_lease_generation": resolved_generation,
                                    "current_lease_generation": lease.lease_generation,
                                },
                            )
                        if validation_error is None and resolved_data_version is None:
                            resolved_data_version = lease.data_version
                        if validation_error is None and (
                            isinstance(resolved_data_version, bool)
                            or not isinstance(resolved_data_version, int)
                        ):
                            validation_error = BusinessError(
                                "E_BAD_DATA_VERSION",
                                "Data version must be an integer",
                                details={"node": node, "data_version": resolved_data_version},
                            )
                        if validation_error is None and resolved_data_version < 0:
                            validation_error = BusinessError(
                                "E_BAD_DATA_VERSION",
                                "Data version must be non-negative",
                                details={"node": node, "data_version": resolved_data_version},
                            )
                        if validation_error is None and resolved_data_version < lease.data_version:
                            validation_error = BusinessError(
                                "E_STALE_DATA_VERSION",
                                "Data version cannot move backwards during renewal",
                                details={
                                    "node": node,
                                    "requested_data_version": resolved_data_version,
                                    "current_data_version": lease.data_version,
                                },
                            )
                        if validation_error is None:
                            old_expiry = lease.expires
                            expected_expiry = advance.effective + node_config.lease_ms
                            request_key = stable_hash(
                                {
                                    "kind": "renew",
                                    "config_hash": config.config_hash,
                                    "node": node,
                                    "session_epoch": session_epoch,
                                    "lease_generation": resolved_generation,
                                    "data_version": resolved_data_version,
                                    "clock": fraction_text(advance.effective),
                                }
                            )
                            op_id = "renew_" + request_key[:40]
                            prepared_op_id = op_id
                            payload = {
                                "base_state_hash": self._state_hash(connection, advance.effective),
                                "clock": fraction_text(advance.effective),
                                "requested_clock": fraction_text(advance.requested),
                                "rewind_tolerated": advance.rewind_tolerated,
                                "rewind_ms": fraction_text(advance.rewind_ms),
                                "node": node,
                                "session_epoch": session_epoch,
                                "lease_generation": resolved_generation,
                                "data_version": resolved_data_version,
                                "old_expiry": fraction_text(lease.expires),
                                "new_expiry": fraction_text(expected_expiry),
                            }
                            cached = self._prepare(
                                connection,
                                op_id=op_id,
                                kind="renew",
                                request_key=request_key,
                                clock_text=fraction_text(advance.effective),
                                payload=payload,
                            )
                            if cached is not None:
                                return cached
                if validation_error is not None:
                    self.db.event(
                        connection,
                        kind="renew_rejected",
                        clock_text=fraction_text(advance.effective) if advance else str(clock),
                        node_name=node,
                        code=validation_error.code,
                        message=validation_error.message,
                        details=validation_error.details,
                    )
            if validation_error is not None:
                raise validation_error

            assert prepared_op_id and request_key and advance and resolved_generation is not None
            assert (
                resolved_data_version is not None
                and expected_expiry is not None
                and old_expiry is not None
            )
            self._crash(crash_at, "after_prepare")
            with self.db.transaction() as connection:
                self._assert_state_hash(
                    connection, advance.effective, json.loads(
                        connection.execute(
                            "SELECT payload_json FROM prepared_ops WHERE op_id = ?",
                            (prepared_op_id,),
                        ).fetchone()["payload_json"]
                    )["base_state_hash"],
                )
                config = self.db.read_cluster(connection)
                state = self._require_state(connection)
                node_config = config.node_map[node]
                row = connection.execute(
                    "SELECT * FROM leases WHERE node_name = ?", (node,)
                ).fetchone()
                if row is None:
                    fail(
                        "E_NOT_LEASE_HOLDER",
                        "Lease disappeared before atomic commit",
                        details={"node": node},
                    )
                lease = self._lease_from_row(row)
                if (
                    lease.lease_generation != resolved_generation
                    or lease.session_epoch != session_epoch
                    or lease.expires != old_expiry
                ):
                    fail(
                        "E_CONCURRENT_STATE_CHANGED",
                        "Lease changed between prepare and commit",
                        details={
                            "node": node,
                            "lease_generation": lease.lease_generation,
                            "expected_generation": resolved_generation,
                            "lease_session_epoch": lease.session_epoch,
                            "expected_session_epoch": session_epoch,
                            "lease_expiry": fraction_text(lease.expires),
                            "expected_old_expiry": fraction_text(old_expiry),
                        },
                    )
                existing_renewal = connection.execute(
                    "SELECT response_json FROM renewals WHERE request_key = ?",
                    (request_key,),
                ).fetchone()
                if existing_renewal is not None:
                    response = json.loads(existing_renewal["response_json"])
                    response["idempotent"] = True
                    connection.execute(
                        "UPDATE prepared_ops SET status='COMMITTED', response_json=? WHERE op_id=?",
                        (canonical_json(response), prepared_op_id),
                    )
                    return response

                views = self._lease_views(connection)
                renewed = LeaseView(
                    node_name=node,
                    lease_generation=lease.lease_generation,
                    session_epoch=session_epoch,
                    data_version=resolved_data_version,
                    granted=lease.granted,
                    expires=expected_expiry,
                    status="active",
                    last_renew_key=request_key,
                )
                views[node] = renewed
                self._persist_all_lease_snapshots(
                    connection, config, views, advance.effective
                )

                evaluation, election_created = self._reconcile_after_mutation(
                    connection,
                    config,
                    views,
                    advance.effective,
                    state["leader"],
                )
                response = {
                    "ok": True,
                    "command": "renew",
                    "idempotent": False,
                    "node": node,
                    "lease_generation": resolved_generation,
                    "session_epoch": session_epoch,
                    "data_version": resolved_data_version,
                    "clock_ms": fraction_text(advance.effective),
                    "requested_clock_ms": fraction_text(advance.requested),
                    "rewind_tolerated": advance.rewind_tolerated,
                    "old_expiry_ms": fraction_text(lease.expires),
                    "expires_ms": fraction_text(expected_expiry),
                    "remaining_fixed": self._remaining_fixed(
                        config, node_config, expected_expiry, advance.effective
                    ).as_dict(),
                    "leader": evaluation.leader,
                    "election_id": evaluation.election_id,
                    "election_recomputed": election_created,
                    "precision_sensitive": evaluation.precision_sensitive,
                }
                connection.execute(
                    """
                    INSERT INTO renewals(
                        request_key, node_name, lease_generation, session_epoch,
                        data_version, clock_text, expires_text, response_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        request_key,
                        node,
                        resolved_generation,
                        session_epoch,
                        resolved_data_version,
                        fraction_text(advance.effective),
                        fraction_text(expected_expiry),
                        canonical_json(response),
                    ),
                )
                connection.execute(
                    "UPDATE leases SET last_renew_key = ? WHERE node_name = ?",
                    (request_key, node),
                )
                new_epoch = int(state["epoch_counter"]) + 1
                self._set_clock_state(
                    connection,
                    advance.effective,
                    config,
                    evaluation.leader,
                    evaluation.election_id,
                    new_epoch,
                )
                connection.execute(
                    """
                    UPDATE prepared_ops
                    SET status = 'COMMITTED', response_json = ?
                    WHERE op_id = ?
                    """,
                    (canonical_json(response), prepared_op_id),
                )
                self._crash(crash_at, "after_final_insert")
            self._crash(crash_at, "after_commit")
            return response

    # ------------------------------------------------------------------
    # Recovery
    # ------------------------------------------------------------------
    def resume(self, *, clock: Any | None = None) -> dict[str, Any]:
        self.db.require_loaded()
        with self.db.exclusive():
            with self.db.transaction() as connection:
                config = self.db.read_cluster(connection)
                state = self._require_state(connection)
                previous_clock = self._state_clock(state)
                if clock is None:
                    advance = ClockAdvance(
                        requested=previous_clock,
                        effective=previous_clock,
                        fixed=clock_fixed(previous_clock, config.election),
                        rewind_tolerated=False,
                        rewind_ms=Fraction(0, 1),
                    )
                else:
                    advance = advance_clock(clock, previous_clock, config.election)
                prepared = connection.execute(
                    "SELECT * FROM prepared_ops WHERE status = 'PREPARED' ORDER BY op_id"
                ).fetchall()
                cleaned: list[dict[str, Any]] = []
                confirmed: list[str] = []
                for row in prepared:
                    if row["kind"] == "renew":
                        committed = connection.execute(
                            "SELECT 1 FROM renewals WHERE request_key = ?",
                            (row["request_key"],),
                        ).fetchone()
                    elif row["kind"] == "elect":
                        committed = connection.execute(
                            "SELECT 1 FROM elections WHERE op_id = ?",
                            (row["op_id"],),
                        ).fetchone()
                    else:
                        committed = None
                    if committed is not None:
                        connection.execute(
                            "UPDATE prepared_ops SET status='COMMITTED' WHERE op_id=?",
                            (row["op_id"],),
                        )
                        confirmed.append(row["op_id"])
                    else:
                        connection.execute(
                            """
                            UPDATE prepared_ops
                            SET status='CLEANED',
                                clean_reason='no_atomic_final_commit_found'
                            WHERE op_id=?
                            """,
                            (row["op_id"],),
                        )
                        cleaned.append(
                            {
                                "op_id": row["op_id"],
                                "kind": row["kind"],
                                "request_key": row["request_key"],
                            }
                        )

                views = self._lease_views(connection)
                before_leader = state["leader"]
                evaluation: Evaluation | None = None
                election_created = False
                if views:
                    self._persist_all_lease_snapshots(
                        connection, config, views, advance.effective
                    )
                    evaluation, election_created = self._reconcile_after_mutation(
                        connection,
                        config,
                        views,
                        advance.effective,
                        before_leader,
                    )
                else:
                    leader_changed = before_leader is not None
                    if leader_changed:
                        connection.execute(
                            "UPDATE cluster_state SET leader=NULL, election_id=NULL WHERE singleton=1"
                        )

                clock_changed = advance.effective != self._state_clock(state)
                changed = bool(cleaned or confirmed or clock_changed or election_created)
                if changed:
                    new_epoch = int(state["epoch_counter"]) + 1
                    if evaluation is not None:
                        leader = evaluation.leader
                        election_id = evaluation.election_id
                    else:
                        leader = None
                        election_id = None
                    self._set_clock_state(
                        connection,
                        advance.effective,
                        config,
                        leader,
                        election_id,
                        new_epoch,
                    )
                response = {
                    "ok": True,
                    "command": "resume",
                    "clock_ms": fraction_text(advance.effective),
                    "requested_clock_ms": fraction_text(advance.requested),
                    "rewind_tolerated": advance.rewind_tolerated,
                    "cleaned_prepared_ops": cleaned,
                    "confirmed_committed_ops": confirmed,
                    "leader": evaluation.leader if evaluation else None,
                    "election_id": evaluation.election_id if evaluation else None,
                    "election_reconstructed": election_created,
                    "source": "persisted_state_only",
                }
                return response

    # ------------------------------------------------------------------
    # Read-only status and verification
    # ------------------------------------------------------------------
    def status(self, *, clock: Any | None = None) -> dict[str, Any]:
        self.db.require_loaded()
        with self.db.transaction(immediate=False) as connection:
            config = self.db.read_cluster(connection)
            state = self._require_state(connection)
            watermark = self._state_clock(state)
            if clock is None:
                view = watermark
                view_mode = "persisted_watermark"
                rewind_tolerated = False
                requested = watermark
                view_fixed = clock_fixed(view, config.election)
            else:
                advance = advance_clock(clock, watermark, config.election)
                view = advance.effective
                requested = advance.requested
                rewind_tolerated = advance.rewind_tolerated
                view_fixed = advance.fixed
                view_mode = (
                    "persisted_watermark"
                    if view == watermark
                    else "view_at_injected_clock"
                )
            views = self._lease_views(connection)
            stored_election = self.db.current_election(connection)
            stored_pairs = self.db.precision_pairs(connection)
            evaluation = self._evaluate(
                config, view, views, state["leader"]
            )
            leader = evaluation.leader
            leader_changed_at_view = leader != state["leader"]
            precision_pairs = stored_pairs
            precision_ids = {
                candidate
                for pair in stored_pairs
                for candidate in (pair["node_a"], pair["node_b"])
            }
            if stored_election is None or leader_changed_at_view:
                precision_pairs = evaluation.precision_pairs
                precision_ids = {
                    candidate
                    for pair in evaluation.precision_pairs
                    for candidate in (pair["node_a"], pair["node_b"])
                }
            reject_map = {item["node"]: item for item in evaluation.rejections}
            ranking_map = {item["node"]: item for item in evaluation.ranking}
            nodes: list[dict[str, Any]] = []
            for node in config.nodes:
                view_lease = views.get(node.name)
                lease_dict: dict[str, Any]
                if view_lease is None:
                    lease_dict = {
                        "held": False,
                        "lease_generation": None,
                        "session_epoch": node.session_epoch,
                        "data_version": node.data_version,
                        "granted_ms": None,
                        "expires_ms": None,
                        "signed_remaining_ms": None,
                        "remaining_ms": "0",
                        "remaining_units": None,
                        "remaining_residual_num": None,
                        "remaining_residual_den": None,
                        "safety_margin_ms": fraction_text(
                            Fraction(node.lease_ms) * config.election.safety_margin_ratio
                        ),
                        "margin_to_safety_ms": None,
                        "state": "absent",
                    }
                else:
                    signed_remaining = view_lease.remaining(view)
                    remaining = max(Fraction(0, 1), signed_remaining)
                    safety = Fraction(node.lease_ms) * config.election.safety_margin_ratio
                    margin = signed_remaining - safety
                    remaining_fixed = fixed_from_fraction(
                        signed_remaining,
                        config.election.scale,
                        label="remaining_duration_ms",
                        raw_value=fraction_text(signed_remaining),
                        raw_float=float(signed_remaining),
                    )
                    safety_fixed = exact_product_fixed(
                        node.lease_ms,
                        config.election.safety_margin_ratio,
                        config.election.scale,
                    )
                    margin_fixed = fixed_from_fraction(
                        margin,
                        config.election.scale,
                        label="margin_to_safety_ms",
                        raw_value=fraction_text(margin),
                        raw_float=float(margin),
                    )
                    effective_state = (
                        "active" if signed_remaining > 0 and view_lease.status == "active" else "expired"
                    )
                    lease_dict = {
                        "held": True,
                        "lease_generation": view_lease.lease_generation,
                        "session_epoch": view_lease.session_epoch,
                        "data_version": view_lease.data_version,
                        "granted_ms": fraction_text(view_lease.granted),
                        "expires_ms": fraction_text(view_lease.expires),
                        "signed_remaining_ms": fraction_text(signed_remaining),
                        "remaining_ms": fraction_text(remaining),
                        "remaining_fixed": remaining_fixed.as_dict(),
                        "remaining_units": remaining_fixed.units,
                        "remaining_residual_num": remaining_fixed.residual.numerator,
                        "remaining_residual_den": remaining_fixed.residual.denominator,
                        "safety_margin_ms": fraction_text(safety),
                        "safety_fixed": safety_fixed.as_dict(),
                        "margin_to_safety_ms": fraction_text(margin),
                        "margin_fixed": margin_fixed.as_dict(),
                        "state": effective_state,
                    }
                rank = ranking_map.get(node.name)
                rejection = reject_map.get(node.name)
                boundary = node.name in evaluation.boundary_candidates
                nodes.append(
                    {
                        "name": node.name,
                        "priority": node.priority,
                        "heartbeat_ms": node.heartbeat_ms,
                        "configured_lease_ms": node.lease_ms,
                        "lease": lease_dict,
                        "eligible": node.name in {item["node"] for item in evaluation.eligible},
                        "ranking_position": rank["position"] if rank else None,
                        "rejection": rejection,
                        "boundary_candidate": boundary,
                        "precision_sensitive": node.name in precision_ids,
                    }
                )
            return {
                "ok": True,
                "command": "status",
                "config_hash": config.config_hash,
                "logical_clock_ms": fraction_text(requested),
                "view_clock_ms": fraction_text(view),
                "view_clock_fixed": view_fixed.as_dict(),
                "watermark_ms": fraction_text(watermark),
                "view_mode": view_mode,
                "clock_rewind_tolerated": rewind_tolerated,
                "leader": leader,
                "election_id": (
                    evaluation.election_id
                    if leader_changed_at_view
                    else state["election_id"]
                ),
                "leader_source": evaluation.leader_source,
                "precision_sensitive": bool(precision_pairs),
                "precision_pairs": precision_pairs,
                "quorum": config.election.quorum,
                "eligible_count": len(evaluation.eligible),
                "quorum_satisfied": evaluation.quorum_satisfied,
                "boundary_candidates": evaluation.boundary_candidates,
                "first_rejection": evaluation.first_rejection,
                "nodes": sorted(nodes, key=lambda item: item["name"]),
            }

    def verify(self) -> dict[str, Any]:
        self.db.require_loaded()
        checks: list[dict[str, Any]] = []

        def check(name: str, ok: bool, detail: Any) -> None:
            checks.append({"name": name, "ok": bool(ok), "detail": detail})

        with self.db.transaction(immediate=False) as connection:
            config = self.db.read_cluster(connection)
            state = self._require_state(connection)
            check(
                "config_hash",
                state["config_hash"] == config.config_hash,
                {
                    "state": state["config_hash"],
                    "computed": config.config_hash,
                },
            )
            watermark = self._state_clock(state)
            watermark_fixed = clock_fixed(watermark, config.election)
            check(
                "watermark_fixed_consistency",
                int(state["clock_units"]) == watermark_fixed.units
                and Fraction(
                    int(state["clock_residual_num"]), int(state["clock_residual_den"])
                )
                == watermark_fixed.residual,
                {
                    "stored_units": int(state["clock_units"]),
                    "computed_units": watermark_fixed.units,
                    "stored_residual": f"{state['clock_residual_num']}/{state['clock_residual_den']}",
                    "computed_residual": fraction_text(watermark_fixed.residual),
                },
            )
            prepared = connection.execute(
                "SELECT op_id, kind FROM prepared_ops WHERE status='PREPARED' ORDER BY op_id"
            ).fetchall()
            check(
                "no_half_committed_operations",
                len(prepared) == 0,
                {"count": len(prepared), "op_ids": [row["op_id"] for row in prepared]},
            )
            views = self._lease_views(connection)
            check(
                "lease_cap",
                len(views) <= config.election.max_leases,
                {"total": len(views), "max_leases": config.election.max_leases},
            )
            history_errors: list[dict[str, Any]] = []
            for name, lease in sorted(views.items()):
                history = connection.execute(
                    """
                    SELECT * FROM lease_history
                    WHERE node_name=? AND lease_generation=?
                    """,
                    (name, lease.lease_generation),
                ).fetchone()
                if history is None:
                    history_errors.append(
                        {
                            "node": name,
                            "code": "E_MISSING_LEASE_HISTORY",
                            "lease_generation": lease.lease_generation,
                        }
                    )
                    continue
                if lease.session_epoch != history["session_epoch"]:
                    history_errors.append(
                        {"node": name, "code": "E_HISTORY_SESSION_MISMATCH"}
                    )
                if lease.expires < lease.granted:
                    history_errors.append(
                        {"node": name, "code": "E_EXPIRY_BEFORE_GRANT"}
                    )
                node_config = config.node_map.get(name)
                if node_config is None:
                    history_errors.append({"node": name, "code": "E_UNKNOWN_NODE"})
                    continue
                safety = Fraction(node_config.lease_ms) * config.election.safety_margin_ratio
                expected_safety_fixed = exact_product_fixed(
                    node_config.lease_ms,
                    config.election.safety_margin_ratio,
                    config.election.scale,
                )
                remaining_fixed = fixed_from_fraction(
                    lease.remaining(watermark),
                    config.election.scale,
                    label="remaining_duration_ms",
                    raw_value=fraction_text(lease.remaining(watermark)),
                    raw_float=float(lease.remaining(watermark)),
                )
                current_row = connection.execute(
                    "SELECT * FROM leases WHERE node_name = ?", (name,)
                ).fetchone()
                if current_row is None:
                    history_errors.append({"node": name, "code": "E_CURRENT_LEASE_MISSING"})
                    continue
                if int(current_row["safety_units"]) != expected_safety_fixed.units:
                    history_errors.append(
                        {"node": name, "code": "E_SAFETY_FIXED_MISMATCH"}
                    )
                if int(current_row["remaining_units"]) != remaining_fixed.units:
                    history_errors.append(
                        {"node": name, "code": "E_REMAINING_FIXED_MISMATCH"}
                    )
                stored_remaining_residual = Fraction(
                    int(current_row["remaining_residual_num"]),
                    int(current_row["remaining_residual_den"]),
                )
                if stored_remaining_residual != remaining_fixed.residual:
                    history_errors.append(
                        {"node": name, "code": "E_REMAINING_RESIDUAL_MISMATCH"}
                    )
            check(
                "lease_history_integrity",
                len(history_errors) == 0,
                {"errors": history_errors},
            )
            duplicate_generations = connection.execute(
                """
                SELECT node_name, lease_generation, COUNT(*) AS amount
                FROM lease_history
                GROUP BY node_name, lease_generation
                HAVING COUNT(*) > 1
                """
            ).fetchall()
            check(
                "generation_unique",
                len(duplicate_generations) == 0,
                {
                    "duplicates": [
                        {
                            "node": row["node_name"],
                            "lease_generation": row["lease_generation"],
                            "count": row["amount"],
                        }
                        for row in duplicate_generations
                    ]
                },
            )
            stored_election = self.db.current_election(connection)
            evaluation = self._evaluate(config, watermark, views, state["leader"])
            leader_ok = True
            leader_detail: dict[str, Any] = {
                "leader": state["leader"],
                "reason": None,
            }
            if state["leader"] is not None:
                if state["leader"] not in views:
                    leader_ok = False
                    leader_detail["reason"] = "E_LEADER_HAS_NO_LEASE"
                elif views[state["leader"]].remaining(watermark) <= 0:
                    leader_ok = False
                    leader_detail["reason"] = "E_LEADER_LEASE_EXPIRED"
                elif not evaluation.quorum_satisfied:
                    leader_ok = False
                    leader_detail["reason"] = "E_NO_QUORUM"
            elif evaluation.quorum_satisfied and evaluation.eligible:
                leader_ok = False
                leader_detail["reason"] = "E_ELIGIBLE_BUT_NO_LEADER"
            check("leader_eligibility", leader_ok, leader_detail)

            election_ok = stored_election is not None
            election_detail: dict[str, Any] = {
                "leader": stored_election["leader"] if stored_election else None,
                "reason": None,
            }
            if stored_election is None:
                if state["leader"] is None and not views:
                    election_ok = True
                    election_detail["reason"] = "no_leases_no_election_required"
                else:
                    election_detail["reason"] = "E_ELECTION_MISSING"
            else:
                election_detail["leader_source"] = stored_election.get("leader_source")
                if stored_election["leader"] != state["leader"]:
                    election_ok = False
                    election_detail["reason"] = "E_STORED_LEADER_MISMATCH"
                else:
                    snapshot = stored_election.get("lease_snapshot")
                    if snapshot is not None:
                        snapshot_views = self._views_from_snapshot(snapshot)
                        election_clock = Fraction(stored_election.get("clock_ms", "0"))
                        previous_leader = stored_election.get("previous_leader")
                        recomputed = self._evaluate(
                            config, election_clock, snapshot_views, previous_leader
                        )
                    else:
                        recomputed = evaluation
                    if recomputed.leader != stored_election["leader"]:
                        election_ok = False
                        election_detail["reason"] = "E_RECOMPUTED_LEADER_MISMATCH"
                        election_detail["recomputed_leader"] = recomputed.leader
                    if (
                        snapshot is not None
                        and recomputed.election_key != stored_election.get("election_key")
                    ):
                        election_ok = False
                        election_detail["reason"] = "E_ELECTION_KEY_MISMATCH"
                    if (
                        bool(recomputed.precision_sensitive)
                        != bool(stored_election.get("precision_sensitive"))
                    ):
                        election_ok = False
                        election_detail["reason"] = "E_PRECISION_FLAG_MISMATCH"
            check("election_consistency", election_ok, election_detail)

            precision_rows = connection.execute(
                """
                SELECT * FROM precision_records
                WHERE election_id = ?
                ORDER BY pair_key
                """,
                (state["election_id"],),
            ).fetchall()
            precision_ok = True
            precision_detail: dict[str, Any] = {
                "stored_pairs": len(precision_rows),
                "expected_sensitive": bool(stored_election and stored_election.get("precision_sensitive")),
            }
            if stored_election and stored_election.get("precision_sensitive") and not precision_rows:
                precision_ok = False
                precision_detail["reason"] = "E_PRECISION_RECORD_MISSING"
            if precision_rows and stored_election and not stored_election.get("precision_sensitive"):
                precision_ok = False
                precision_detail["reason"] = "E_UNEXPECTED_PRECISION_RECORD"
            if stored_election and stored_election.get("precision_sensitive"):
                snapshot = stored_election.get("lease_snapshot")
                if snapshot is not None:
                    recomputed = self._evaluate(
                        config,
                        Fraction(stored_election["clock_ms"]),
                        self._views_from_snapshot(snapshot),
                        stored_election.get("previous_leader"),
                    )
                    stored_pairs = sorted(
                        (row["node_a"], row["node_b"]) for row in precision_rows
                    )
                    expected_pairs = sorted(
                        (pair["node_a"], pair["node_b"])
                        for pair in recomputed.precision_pairs
                    )
                    if stored_pairs != expected_pairs:
                        precision_ok = False
                        precision_detail["reason"] = "E_PRECISION_PAIR_MISMATCH"
                        precision_detail["stored_pairs"] = stored_pairs
                        precision_detail["expected_pairs"] = expected_pairs
            check("precision_record_consistency", precision_ok, precision_detail)

        failed = [item for item in checks if not item["ok"]]
        if failed:
            raise BusinessError(
                "E_VERIFY_FAILED",
                "Persisted state failed consistency verification",
                details={"checks": checks, "failed": failed},
            )
        return {
            "ok": True,
            "command": "verify",
            "watermark_ms": fraction_text(watermark),
            "config_hash": config.config_hash,
            "leader": state["leader"],
            "checks": checks,
            "verified": True,
        }

    # ------------------------------------------------------------------
    # Internal state helpers
    # ------------------------------------------------------------------
    def _require_state(self, connection: sqlite3.Connection) -> sqlite3.Row:
        state = self.db.clock_row(connection)
        if state is None:
            fail("E_STATE_CORRUPT", "Cluster state row is missing")
        return state

    def _state_clock(self, state: sqlite3.Row) -> Fraction:
        return Fraction(int(state["clock_num"]), int(state["clock_den"]))

    def _lease_views(self, connection: sqlite3.Connection) -> dict[str, LeaseView]:
        rows = connection.execute("SELECT * FROM leases ORDER BY node_name").fetchall()
        return {row["node_name"]: self._lease_from_row(row) for row in rows}

    def _lease_from_row(self, row: sqlite3.Row) -> LeaseView:
        return LeaseView(
            node_name=row["node_name"],
            lease_generation=int(row["lease_generation"]),
            session_epoch=int(row["session_epoch"]),
            data_version=int(row["data_version"]),
            granted=Fraction(int(row["granted_num"]), int(row["granted_den"])),
            expires=Fraction(int(row["expires_num"]), int(row["expires_den"])),
            status=row["status"],
            last_renew_key=row["last_renew_key"],
        )

    def _project_bootstrap(
        self,
        config: ClusterConfig,
        current: dict[str, LeaseView],
        clock: Fraction,
    ) -> tuple[dict[str, LeaseView], list[str]]:
        if current:
            return dict(current), []
        ordered = sorted(
            config.nodes,
            key=lambda node: (-node.priority, -node.session_epoch, node.name),
        )[: config.election.max_leases]
        projected: dict[str, LeaseView] = {}
        names: list[str] = []
        for node in ordered:
            lease = LeaseView(
                node_name=node.name,
                lease_generation=1,
                session_epoch=node.session_epoch,
                data_version=node.data_version,
                granted=clock,
                expires=clock + node.lease_ms,
                status="active",
                last_renew_key=None,
            )
            projected[node.name] = lease
            names.append(node.name)
        return projected, names

    def _insert_bootstrap_leases(
        self,
        connection: sqlite3.Connection,
        config: ClusterConfig,
        views: dict[str, LeaseView],
        bootstrapped: Iterable[str],
        clock: Fraction,
    ) -> None:
        for name in bootstrapped:
            lease = views[name]
            request_key = "bootstrap_" + stable_hash(
                {
                    "config_hash": config.config_hash,
                    "node": name,
                    "lease_generation": lease.lease_generation,
                    "session_epoch": lease.session_epoch,
                    "clock": fraction_text(clock),
                }
            )[:40]
            connection.execute(
                """
                INSERT INTO lease_history(
                    node_name, lease_generation, session_epoch, data_version,
                    granted_text, expires_text, request_key
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    name,
                    lease.lease_generation,
                    lease.session_epoch,
                    lease.data_version,
                    fraction_text(lease.granted),
                    fraction_text(lease.expires),
                    request_key,
                ),
            )
            self._persist_lease_snapshot(connection, config, lease, clock)

    def _persist_all_lease_snapshots(
        self,
        connection: sqlite3.Connection,
        config: ClusterConfig,
        views: dict[str, LeaseView],
        clock: Fraction,
    ) -> None:
        for name, lease in sorted(views.items()):
            self._persist_lease_snapshot(connection, config, lease, clock)

    def _persist_lease_snapshot(
        self,
        connection: sqlite3.Connection,
        config: ClusterConfig,
        lease: LeaseView,
        clock: Fraction,
    ) -> None:
        node = config.node_map[lease.node_name]
        remaining_fixed = self._remaining_fixed(config, node, lease.expires, clock)
        safety_fixed = exact_product_fixed(
            node.lease_ms,
            config.election.safety_margin_ratio,
            config.election.scale,
        )
        signed_remaining = lease.expires - clock
        status = "active" if signed_remaining > 0 and lease.status == "active" else "expired"
        values = (
            lease.lease_generation,
            lease.session_epoch,
            lease.data_version,
            fraction_text(lease.granted),
            str(lease.granted.numerator),
            str(lease.granted.denominator),
            fraction_text(lease.expires),
            str(lease.expires.numerator),
            str(lease.expires.denominator),
            status,
            remaining_fixed.units,
            str(remaining_fixed.residual.numerator),
            str(remaining_fixed.residual.denominator),
            safety_fixed.units,
            str(safety_fixed.residual.numerator),
            str(safety_fixed.residual.denominator),
            lease.last_renew_key,
            lease.node_name,
        )
        existing = connection.execute(
            "SELECT 1 FROM leases WHERE node_name = ?", (lease.node_name,)
        ).fetchone()
        if existing is None:
            connection.execute(
                """
                INSERT INTO leases(
                    lease_generation, session_epoch, data_version,
                    granted_text, granted_num, granted_den,
                    expires_text, expires_num, expires_den, status,
                    remaining_units, remaining_residual_num, remaining_residual_den,
                    safety_units, safety_residual_num, safety_residual_den,
                    last_renew_key, node_name
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                values,
            )
        else:
            connection.execute(
                """
                UPDATE leases SET
                    lease_generation=?, session_epoch=?, data_version=?,
                    granted_text=?, granted_num=?, granted_den=?,
                    expires_text=?, expires_num=?, expires_den=?, status=?,
                    remaining_units=?, remaining_residual_num=?, remaining_residual_den=?,
                    safety_units=?, safety_residual_num=?, safety_residual_den=?,
                    last_renew_key=?
                WHERE node_name=?
                """,
                values,
            )

    def _remaining_fixed(
        self,
        config: ClusterConfig,
        node: NodeConfig,
        expires: Fraction,
        clock: Fraction,
    ) -> FixedValue:
        remaining = expires - clock
        return fixed_from_fraction(
            remaining,
            config.election.scale,
            label="remaining_duration_ms",
            raw_value=fraction_text(remaining),
            raw_float=float(remaining),
        )

    def _evaluate(
        self,
        config: ClusterConfig,
        clock: Fraction,
        views: dict[str, LeaseView],
        current_leader: str | None,
    ) -> Evaluation:
        eligible: list[dict[str, Any]] = []
        rejections: list[dict[str, Any]] = []
        boundary: list[str] = []
        for node in config.nodes:
            lease = views.get(node.name)
            safety = Fraction(node.lease_ms) * config.election.safety_margin_ratio
            if lease is None:
                rejections.append(
                    {
                        "node": node.name,
                        "priority": node.priority,
                        "code": "E_NO_LEASE",
                        "reason": "node has no persisted lease",
                    }
                )
                continue
            remaining = lease.remaining(clock)
            margin = remaining - safety
            if fraction_abs(margin) <= config.election.precision_tolerance:
                boundary.append(node.name)
            reason: tuple[str, str] | None = None
            if lease.status != "active":
                reason = ("E_LEASE_EXPIRED", "persisted lease status is expired")
            elif lease.session_epoch != node.session_epoch:
                reason = ("E_SESSION_MISMATCH", "lease session epoch differs from candidate")
            elif remaining <= 0:
                reason = ("E_LEASE_EXPIRED", "remaining lease duration is zero")
            elif remaining < safety:
                reason = (
                    "E_BELOW_SAFETY_MARGIN",
                    "remaining lease duration is below the fixed safety reserve",
                )
            if reason is not None:
                rejections.append(
                    {
                        "node": node.name,
                        "priority": node.priority,
                        "lease_generation": lease.lease_generation,
                        "remaining_ms": fraction_text(max(Fraction(0, 1), remaining)),
                        "safety_margin_ms": fraction_text(safety),
                        "margin_to_safety_ms": fraction_text(margin),
                        "code": reason[0],
                        "reason": reason[1],
                    }
                )
                continue
            margin_fixed = fixed_from_fraction(
                margin,
                config.election.scale,
                label="election_score_margin_ms",
                raw_value=fraction_text(margin),
                raw_float=float(margin),
            )
            remaining_fixed = fixed_from_fraction(
                remaining,
                config.election.scale,
                label="remaining_duration_ms",
                raw_value=fraction_text(remaining),
                raw_float=float(remaining),
            )
            eligible.append(
                {
                    "node": node.name,
                    "priority": node.priority,
                    "lease_generation": lease.lease_generation,
                    "session_epoch": lease.session_epoch,
                    "data_version": lease.data_version,
                    "remaining_ms": fraction_text(remaining),
                    "remaining_fixed": remaining_fixed.as_dict(),
                    "safety_margin_ms": fraction_text(safety),
                    "margin_to_safety_ms": fraction_text(margin),
                    "margin_fixed": margin_fixed.as_dict(),
                    "margin": margin,
                    "stable_order": node.name,
                }
            )

        ordered = sorted(
            eligible,
            key=lambda item: (
                -item["priority"],
                -item["lease_generation"],
                -item["margin"],
                item["stable_order"],
            ),
        )
        for position, item in enumerate(ordered, start=1):
            item["position"] = position
            item.pop("margin", None)

        eligible_names = [item["node"] for item in ordered]
        quorum_satisfied = len(eligible_names) >= config.election.quorum
        leader: str | None
        leader_source: str
        if current_leader is not None and current_leader in eligible_names and quorum_satisfied:
            leader = current_leader
            leader_source = "retained"
        elif ordered and quorum_satisfied:
            leader = ordered[0]["node"]
            leader_source = "selected"
        else:
            leader = None
            leader_source = "none"

        rejections.sort(
            key=lambda item: (
                0 if item["node"] == current_leader else 1,
                -item["priority"],
                -int(item.get("lease_generation", 0)),
                item["node"],
            )
        )
        first_rejection = rejections[0] if rejections else None
        precision_pairs: list[dict[str, Any]] = []
        if leader is not None and leader_source == "selected" and len(ordered) >= 2:
            winner = ordered[0]
            runner = ordered[1]
            winner_margin = Fraction(winner["margin_to_safety_ms"])
            runner_margin = Fraction(runner["margin_to_safety_ms"])
            gap = winner_margin - runner_margin
            if (
                winner["priority"] == runner["priority"]
                and winner["lease_generation"] == runner["lease_generation"]
                and gap > 0
                and gap <= config.election.precision_tolerance
            ):
                pair_key = f"{winner['node']}|{runner['node']}"
                pair_fixed_a = fixed_from_fraction(
                    winner_margin,
                    config.election.scale,
                    label="election_score_margin_ms",
                    raw_value=fraction_text(winner_margin),
                    raw_float=float(winner_margin),
                )
                pair_fixed_b = fixed_from_fraction(
                    runner_margin,
                    config.election.scale,
                    label="election_score_margin_ms",
                    raw_value=fraction_text(runner_margin),
                    raw_float=float(runner_margin),
                )
                tolerance_fixed = fixed_from_fraction(
                    config.election.precision_tolerance,
                    config.election.scale,
                    label="precision_tolerance_ms",
                    raw_value=fraction_text(config.election.precision_tolerance),
                    raw_float=float(config.election.precision_tolerance),
                )
                precision_pairs.append(
                    {
                        "pair_key": pair_key,
                        "node_a": winner["node"],
                        "node_b": runner["node"],
                        "winner": winner["node"],
                        "score_a_units": pair_fixed_a.units,
                        "score_b_units": pair_fixed_b.units,
                        "gap_ms": fraction_text(gap),
                        "gap_units": fraction_text(gap * config.election.scale),
                        "tolerance_ms": fraction_text(config.election.precision_tolerance),
                        "tolerance_units": tolerance_fixed.units,
                        "affected_candidates": [winner["node"], runner["node"]],
                        "first_rejection": first_rejection,
                        "error_source_chain": [
                            (
                                "safety_margin_ratio "
                                f"{config.election.safety_margin_raw} -> fixed "
                                f"{config.election.scale} units/ms, floor, residual retained per node"
                            ),
                            (
                                "lease_ms is an exact integer and is multiplied as an exact "
                                "rational before fixed-point comparison"
                            ),
                            (
                                "remaining duration includes the exact logical-clock decimal "
                                "and keeps each node's sub-unit residual"
                            ),
                            (
                                f"winner margin {fraction_text(winner_margin)} ms, "
                                f"runner margin {fraction_text(runner_margin)} ms, "
                                f"gap {fraction_text(gap)} ms"
                            ),
                            (
                                f"gap {fraction_text(gap * config.election.scale)} fixed units "
                                f"is within tolerance {tolerance_fixed.units} units"
                            ),
                            (
                                "exact rational ordering is used; stable node order remains "
                                "the deterministic final tie-break"
                            ),
                        ],
                    }
                )
        election_key = self._election_key(config, clock, views)
        election_id = "el_" + election_key[:24]
        state = {
            "election_id": election_id,
            "election_key": election_key,
            "clock_ms": fraction_text(clock),
            "previous_leader": current_leader,
            "leader": leader,
            "leader_source": leader_source,
            "eligible": eligible_names,
            "ranking": ordered,
            "rejections": rejections,
            "first_rejection": first_rejection,
            "boundary_candidates": sorted(boundary),
            "precision_sensitive": bool(precision_pairs),
            "precision_pairs": precision_pairs,
            "quorum": config.election.quorum,
            "quorum_satisfied": quorum_satisfied,
            "lease_snapshot": [
                views[name].key_dict() for name in sorted(views)
            ],
        }
        return Evaluation(
            clock=clock,
            leader=leader,
            leader_source=leader_source,
            eligible=ordered,
            ranking=ordered,
            rejections=rejections,
            first_rejection=first_rejection,
            boundary_candidates=sorted(boundary),
            precision_sensitive=bool(precision_pairs),
            precision_pairs=precision_pairs,
            quorum_satisfied=quorum_satisfied,
            election_key=election_key,
            election_id=election_id,
            state=state,
        )

    def _election_key(
        self,
        config: ClusterConfig,
        clock: Fraction,
        views: dict[str, LeaseView],
    ) -> str:
        return stable_hash(
            {
                "config_hash": config.config_hash,
                "clock_ms": fraction_text(clock),
                "leases": [
                    views[name].key_dict()
                    for name in sorted(views)
                ],
            }
        )

    def _views_from_snapshot(self, snapshot: Iterable[dict[str, Any]]) -> dict[str, LeaseView]:
        views: dict[str, LeaseView] = {}
        for item in snapshot:
            lease = LeaseView(
                node_name=item["node"],
                lease_generation=int(item["lease_generation"]),
                session_epoch=int(item["session_epoch"]),
                data_version=int(item["data_version"]),
                granted=fraction(item["granted_ms"]),
                expires=fraction(item["expires_ms"]),
                status="active",
                last_renew_key=None,
            )
            views[lease.node_name] = lease
        return views

    def _election_response(
        self,
        command: str,
        config: ClusterConfig,
        evaluation: Evaluation,
        advance: ClockAdvance | None,
        bootstrapped: list[str],
        *,
        idempotent: bool,
    ) -> dict[str, Any]:
        response: dict[str, Any] = {
            "ok": True,
            "command": command,
            "idempotent": idempotent,
            "election_id": evaluation.election_id,
            "election_key": evaluation.election_key,
            "leader": evaluation.leader,
            "leader_source": evaluation.leader_source,
            "eligible": [item["node"] for item in evaluation.eligible],
            "ranking": evaluation.ranking,
            "rejections": evaluation.rejections,
            "first_rejection": evaluation.first_rejection,
            "precision_sensitive": evaluation.precision_sensitive,
            "precision_pairs": evaluation.precision_pairs,
            "boundary_candidates": evaluation.boundary_candidates,
            "quorum": config.election.quorum,
            "quorum_satisfied": evaluation.quorum_satisfied,
            "bootstrapped_nodes": bootstrapped,
            "election_clock_ms": fraction_text(evaluation.clock),
        }
        if advance is not None:
            response["requested_clock_ms"] = fraction_text(advance.requested)
            response["clock_rewind_tolerated"] = advance.rewind_tolerated
            response["clock_fixed"] = advance.fixed.as_dict()
        return response

    def _persist_election(
        self,
        connection: sqlite3.Connection,
        evaluation: Evaluation,
        config: ClusterConfig,
        op_id: str,
    ) -> None:
        existing = connection.execute(
            "SELECT election_id FROM elections WHERE election_key = ?",
            (evaluation.election_key,),
        ).fetchone()
        election_id = existing["election_id"] if existing else evaluation.election_id
        if existing is None:
            response = {
                "ok": True,
                "command": "election_record",
                "election_id": election_id,
                "election_key": evaluation.election_key,
                "leader": evaluation.leader,
                "precision_sensitive": evaluation.precision_sensitive,
            }
            connection.execute(
                """
                INSERT INTO elections(
                    election_id, election_key, op_id, clock_text, leader,
                    state_json, precision_sensitive, response_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    election_id,
                    evaluation.election_key,
                    op_id,
                    fraction_text(evaluation.clock),
                    evaluation.leader,
                    canonical_json(evaluation.state),
                    1 if evaluation.precision_sensitive else 0,
                    canonical_json(response),
                ),
            )
        connection.execute(
            "DELETE FROM precision_records WHERE election_id = ?", (election_id,)
        )
        for pair in evaluation.precision_pairs:
            connection.execute(
                """
                INSERT INTO precision_records(
                    election_id, pair_key, node_a, node_b, score_a_units,
                    score_b_units, gap_ms, tolerance_ms, error_source_chain_json,
                    affected_candidates_json, first_rejection_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    election_id,
                    pair["pair_key"],
                    pair["node_a"],
                    pair["node_b"],
                    int(pair["score_a_units"]),
                    int(pair["score_b_units"]),
                    pair["gap_ms"],
                    pair["tolerance_ms"],
                    canonical_json(pair["error_source_chain"]),
                    canonical_json(pair["affected_candidates"]),
                    canonical_json(pair["first_rejection"]),
                ),
            )

    def _reconcile_after_mutation(
        self,
        connection: sqlite3.Connection,
        config: ClusterConfig,
        views: dict[str, LeaseView],
        clock: Fraction,
        previous_leader: str | None,
    ) -> tuple[Evaluation, bool]:
        evaluation = self._evaluate(config, clock, views, previous_leader)
        election_created = False
        if evaluation.leader != previous_leader or previous_leader is None:
            op_id = "mutation_" + stable_hash(
                {
                    "clock": fraction_text(clock),
                    "leader": evaluation.leader,
                    "previous": previous_leader,
                    "leases": [views[name].key_dict() for name in sorted(views)],
                }
            )[:40]
            self._persist_election(connection, evaluation, config, op_id)
            election_created = True
        else:
            state = self._require_state(connection)
            if state["election_id"] is not None:
                evaluation.election_id = state["election_id"]
                evaluation.state["election_id"] = state["election_id"]
        return evaluation, election_created

    def _set_clock_state(
        self,
        connection: sqlite3.Connection,
        clock: Fraction,
        config: ClusterConfig,
        leader: str | None,
        election_id: str | None,
        epoch: int,
    ) -> None:
        fixed = clock_fixed(clock, config.election)
        connection.execute(
            """
            UPDATE cluster_state SET
                clock_text=?, clock_num=?, clock_den=?, clock_units=?,
                clock_residual_num=?, clock_residual_den=?,
                leader=?, election_id=?, config_hash=?, epoch_counter=?
            WHERE singleton=1
            """,
            (
                fraction_text(clock),
                str(clock.numerator),
                str(clock.denominator),
                fixed.units,
                str(fixed.residual.numerator),
                str(fixed.residual.denominator),
                leader,
                election_id,
                config.config_hash,
                epoch,
            ),
        )

    def _state_hash(self, connection: sqlite3.Connection, clock: Fraction) -> str:
        state = self._require_state(connection)
        views = self._lease_views(connection)
        return stable_hash(
            {
                "clock": fraction_text(clock),
                "watermark": state["clock_text"],
                "leader": state["leader"],
                "epoch": int(state["epoch_counter"]),
                "leases": [views[name].key_dict() for name in sorted(views)],
            }
        )

    def _assert_state_hash(
        self,
        connection: sqlite3.Connection,
        clock: Fraction,
        expected: str,
    ) -> None:
        actual = self._state_hash(connection, clock)
        if actual != expected:
            fail(
                "E_CONCURRENT_STATE_CHANGED",
                "Persisted state changed between prepare and commit",
                details={"expected_state_hash": expected, "actual_state_hash": actual},
            )

    def _prepare(
        self,
        connection: sqlite3.Connection,
        *,
        op_id: str,
        kind: str,
        request_key: str,
        clock_text: str,
        payload: dict[str, Any],
    ) -> dict[str, Any] | None:
        row = connection.execute(
            "SELECT * FROM prepared_ops WHERE op_id = ?", (op_id,)
        ).fetchone()
        if row is not None and row["status"] == "COMMITTED":
            response = json.loads(row["response_json"])
            response["idempotent"] = True
            response["cached"] = True
            return response
        if row is not None and row["status"] == "PREPARED":
            fail(
                "E_RECOVERY_REQUIRED",
                "A half-committed operation exists; run resume before retrying",
                details={"op_id": op_id, "kind": kind},
            )
        if row is None:
            connection.execute(
                """
                INSERT INTO prepared_ops(
                    op_id, kind, request_key, clock_text, status, payload_json,
                    response_json, clean_reason
                ) VALUES (?, ?, ?, ?, 'PREPARED', ?, NULL, NULL)
                """,
                (op_id, kind, request_key, clock_text, canonical_json(payload)),
            )
        else:
            connection.execute(
                """
                UPDATE prepared_ops SET
                    kind=?, request_key=?, clock_text=?, status='PREPARED',
                    payload_json=?, response_json=NULL, clean_reason=NULL
                WHERE op_id=?
                """,
                (kind, request_key, clock_text, canonical_json(payload), op_id),
            )
        return None

    def _crash(self, requested: str | None, point: str) -> None:
        if requested == point:
            os._exit(CRASH_EXIT_CODE)

    # ------------------------------------------------------------------
    # Utility for callers and tests
    # ------------------------------------------------------------------
    def inspect_database(self) -> dict[str, Any]:
        self.db.require_loaded()
        with self.db.transaction(immediate=False) as connection:
            state = self._require_state(connection)
            result: dict[str, Any] = {
                "watermark_ms": state["clock_text"],
                "leader": state["leader"],
                "election_id": state["election_id"],
                "leases": connection.execute("SELECT COUNT(*) AS n FROM leases").fetchone()["n"],
                "lease_history": connection.execute(
                    "SELECT COUNT(*) AS n FROM lease_history"
                ).fetchone()["n"],
                "renewals": connection.execute("SELECT COUNT(*) AS n FROM renewals").fetchone()["n"],
                "elections": connection.execute("SELECT COUNT(*) AS n FROM elections").fetchone()["n"],
                "prepared": {
                    row["status"]: row["n"]
                    for row in connection.execute(
                        """
                        SELECT status, COUNT(*) AS n FROM prepared_ops GROUP BY status
                        ORDER BY status
                        """
                    ).fetchall()
                },
            }
            return result
