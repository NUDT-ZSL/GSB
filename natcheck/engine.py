"""Primary connection-tracking and NAT mapping adjudication engine."""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any

from .model import final_state_counts
from .normalize import rule_matches


def _mapping_signature(mapping: dict[str, Any] | None) -> tuple[Any, ...]:
    if mapping is None:
        return ("none",)
    return (
        mapping.get("status"),
        mapping.get("reason"),
        mapping.get("rule_id"),
        mapping.get("pool_id"),
        mapping.get("external_ip"),
        mapping.get("external_port"),
    )


@dataclass
class Session:
    session_id: str
    connection_id: str
    epoch: int
    connection: dict[str, Any]
    state: str
    created_at: int
    created_event_id: str
    last_activity_at: int
    expires_at: int
    terminal_at: int | None = None
    mapping: dict[str, Any] | None = None
    mapping_history: list[dict[str, Any]] = field(default_factory=list)
    trace: list[dict[str, Any]] = field(default_factory=list)
    require_rebuild: bool = False
    rebuild_reasons: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "connection_id": self.connection_id,
            "epoch": self.epoch,
            "greenhouse_id": self.connection["greenhouse_id"],
            "direction": self.connection["direction"],
            "tracking_timeout": self.connection["tracking_timeout"],
            "five_tuple": copy.deepcopy(self.connection["five_tuple"]),
            "state": self.state,
            "created_at": self.created_at,
            "created_event_id": self.created_event_id,
            "last_activity_at": self.last_activity_at,
            "expires_at": self.expires_at,
            "terminal_at": self.terminal_at,
            "trace": copy.deepcopy(self.trace),
            "mapping": copy.deepcopy(self.mapping),
            "mapping_history": copy.deepcopy(self.mapping_history),
            "require_rebuild": self.require_rebuild,
            "rebuild_reasons": copy.deepcopy(self.rebuild_reasons),
        }


class AdjudicationEngine:
    """Deterministic offline adjudicator for a normalized project."""

    def __init__(self, model: dict[str, Any]):
        self.model = model
        self.connection_by_id = {
            conn["connection_id"]: copy.deepcopy(conn)
            for conn in model["connections"]
        }
        self.rules = {
            rule["rule_id"]: copy.deepcopy(rule) for rule in model["rules"]
        }
        self.pools = {
            pool["pool_id"]: copy.deepcopy(pool) for pool in model["pools"]
        }
        self.events = sorted(
            (copy.deepcopy(event) for event in model["events"]),
            key=lambda event: (event["time"], event["seq"]),
        )
        self.horizon = model.get("horizon")
        if self.horizon is None and self.events:
            self.horizon = max(event["time"] for event in self.events)

        self.sessions: list[Session] = []
        self.active_by_connection: dict[str, Session] = {}
        self.epoch_by_connection: dict[str, int] = {}
        self.reservations: dict[tuple[str, int], str] = {}
        self.impacts: list[dict[str, Any]] = []
        self.cleanups: list[dict[str, Any]] = []
        self.ignored_events: list[dict[str, Any]] = []
        self.last_time = 0

    def run(self) -> dict[str, Any]:
        current_time: int | None = None
        for event in self.events:
            if current_time is None or event["time"] != current_time:
                self._expire_before(event["time"], detected_at=event["time"])
                current_time = event["time"]
            self.last_time = max(self.last_time, event["time"])
            self._process_event(event)

        if self.horizon is not None:
            self._expire_due(
                self.horizon,
                inclusive=True,
                trigger="horizon_check",
                detected_at=self.horizon,
            )
            self.last_time = max(self.last_time, self.horizon)

        return self._snapshot()

    def _process_event(self, event: dict[str, Any]) -> None:
        event_type = event["type"]
        if event_type == "tick":
            self._expire_due(
                event["time"],
                inclusive=True,
                trigger=event["event_id"],
                detected_at=event["time"],
            )
            return
        if event_type == "policy_change":
            self._apply_policy_change(event)
            return

        connection_id = event.get("connection_id")
        connection = self.connection_by_id.get(connection_id)
        if connection is None:
            self.ignored_events.append(
                {
                    "event_id": event["event_id"],
                    "time": event["time"],
                    "seq": event["seq"],
                    "reason": f"unknown connection {connection_id}",
                }
            )
            return
        active = self.active_by_connection.get(connection_id)

        if event_type == "new":
            if active is None:
                self._create_session(connection, event)
            else:
                self._update_activity(active, event["time"])
                self._trace(
                    active,
                    event,
                    trigger="duplicate_new_ignored",
                    from_state=active.state,
                    to_state=active.state,
                    transition=False,
                    reason="connection is already tracked; duplicate new is ignored",
                )
        elif event_type == "established":
            if active is None:
                self.ignored_events.append(
                    {
                        "event_id": event["event_id"],
                        "time": event["time"],
                        "seq": event["seq"],
                        "reason": "established event without an active new session",
                    }
                )
                return
            self._update_activity(active, event["time"])
            if active.state == "new":
                self._trace(
                    active,
                    event,
                    trigger="established",
                    from_state="new",
                    to_state="established",
                    transition=True,
                    reason="session handshake completed",
                )
                active.state = "established"
                if not active.mapping or active.mapping.get("status") != "mapped":
                    self._assign_session(
                        active,
                        event["time"],
                        event["event_id"],
                        cause="established_retry",
                    )
            else:
                self._trace(
                    active,
                    event,
                    trigger="duplicate_established_ignored",
                    from_state=active.state,
                    to_state=active.state,
                    transition=False,
                    reason="session is already established",
                )
        elif event_type == "teardown":
            if active is None:
                self.ignored_events.append(
                    {
                        "event_id": event["event_id"],
                        "time": event["time"],
                        "seq": event["seq"],
                        "reason": "teardown without an active session",
                    }
                )
                return
            self._trace(
                active,
                event,
                trigger="teardown",
                from_state=active.state,
                to_state="teardown",
                transition=True,
                reason="connection teardown observed",
            )
            self._release(active, event["time"], "teardown")
            active.state = "teardown"
            active.terminal_at = event["time"]
            self.active_by_connection.pop(connection_id, None)
        elif event_type == "timeout":
            if active is None:
                self.ignored_events.append(
                    {
                        "event_id": event["event_id"],
                        "time": event["time"],
                        "seq": event["seq"],
                        "reason": "timeout without an active session",
                    }
                )
                return
            self._timeout_session(
                active,
                event["time"],
                trigger_event_id=event["event_id"],
                detected_at=event["time"],
                cause="explicit_timeout",
            )

    def _trace(
        self,
        session: Session,
        event: dict[str, Any],
        *,
        trigger: str,
        from_state: str | None,
        to_state: str,
        transition: bool,
        reason: str,
    ) -> None:
        session.trace.append(
            {
                "event_id": event.get("event_id"),
                "time": event.get("time"),
                "seq": event.get("seq"),
                "trigger": trigger,
                "from_state": from_state,
                "to_state": to_state,
                "transition": transition,
                "reason": reason,
            }
        )

    def _update_activity(self, session: Session, time: int) -> None:
        session.last_activity_at = time
        session.expires_at = time + session.connection["tracking_timeout"]

    def _create_session(
        self, connection: dict[str, Any], event: dict[str, Any]
    ) -> None:
        connection_id = connection["connection_id"]
        epoch = self.epoch_by_connection.get(connection_id, 0) + 1
        self.epoch_by_connection[connection_id] = epoch
        session = Session(
            session_id=f"{connection_id}#{epoch}",
            connection_id=connection_id,
            epoch=epoch,
            connection=copy.deepcopy(connection),
            state="new",
            created_at=event["time"],
            created_event_id=event["event_id"],
            last_activity_at=event["time"],
            expires_at=event["time"] + connection["tracking_timeout"],
        )
        self.sessions.append(session)
        self.active_by_connection[connection_id] = session
        self._assign_session(
            session, event["time"], event["event_id"], cause="session_new"
        )
        self._trace(
            session,
            event,
            trigger="new",
            from_state=None,
            to_state="new",
            transition=True,
            reason="first packet created a new tracked session",
        )

    def _select_rule(self, connection: dict[str, Any]) -> dict[str, Any] | None:
        candidates = [
            rule
            for rule in self.rules.values()
            if rule_matches(rule, connection)
        ]
        if not candidates:
            return None
        candidates.sort(
            key=lambda rule: (tuple(rule["priority"]), rule["rule_id"])
        )
        return copy.deepcopy(candidates[0])

    def _build_assignment(
        self,
        connection: dict[str, Any],
        time: int,
        event_id: str,
    ) -> dict[str, Any]:
        rule = self._select_rule(connection)
        if rule is None:
            return {
                "status": "unmapped",
                "reason": "no_matching_rule",
                "rule_id": None,
                "pool_id": None,
                "priority": None,
                "external_ip": None,
                "external_port": None,
                "assigned_at": time,
                "assigned_event_id": event_id,
            }
        pool = self.pools.get(rule["pool_id"])
        if pool is None:
            return {
                "status": "unmapped",
                "reason": "pool_missing",
                "rule_id": rule["rule_id"],
                "pool_id": rule["pool_id"],
                "priority": copy.deepcopy(rule["priority"]),
                "external_ip": None,
                "external_port": None,
                "assigned_at": time,
                "assigned_event_id": event_id,
            }

        for port in range(pool["port_start"], pool["port_end"] + 1):
            key = (pool["external_ip"], port)
            if key not in self.reservations:
                return {
                    "status": "mapped",
                    "reason": "selected",
                    "rule_id": rule["rule_id"],
                    "pool_id": pool["pool_id"],
                    "priority": copy.deepcopy(rule["priority"]),
                    "external_ip": pool["external_ip"],
                    "external_port": port,
                    "assigned_at": time,
                    "assigned_event_id": event_id,
                }
        return {
            "status": "unmapped",
            "reason": "pool_exhausted",
            "rule_id": rule["rule_id"],
            "pool_id": rule["pool_id"],
            "priority": copy.deepcopy(rule["priority"]),
            "external_ip": None,
            "external_port": None,
            "assigned_at": time,
            "assigned_event_id": event_id,
        }

    def _reserve(self, session: Session, assignment: dict[str, Any]) -> None:
        if assignment.get("status") == "mapped":
            key = (assignment["external_ip"], assignment["external_port"])
            self.reservations[key] = session.session_id

    def _assign_session(
        self,
        session: Session,
        time: int,
        event_id: str,
        *,
        cause: str,
    ) -> dict[str, Any]:
        assignment = self._build_assignment(session.connection, time, event_id)
        session.mapping = assignment
        self._reserve(session, assignment)
        session.mapping_history.append(
            {
                "time": time,
                "event_id": event_id,
                "cause": cause,
                "assignment": copy.deepcopy(assignment),
            }
        )
        return assignment

    def _release(self, session: Session, time: int, reason: str) -> None:
        mapping = session.mapping
        if not mapping or mapping.get("status") != "mapped":
            return
        key = (mapping["external_ip"], mapping["external_port"])
        if self.reservations.get(key) == session.session_id:
            self.reservations.pop(key, None)
        mapping["released_at"] = time
        mapping["release_reason"] = reason

    def _timeout_session(
        self,
        session: Session,
        time: int,
        *,
        trigger_event_id: str,
        detected_at: int,
        cause: str,
    ) -> None:
        if session.state not in {"new", "established"}:
            return
        released = copy.deepcopy(session.mapping)
        self._release(session, time, "timeout")
        self._trace(
            session,
            {
                "event_id": trigger_event_id,
                "time": time,
                "seq": None,
            },
            trigger="tracking_timeout",
            from_state=session.state,
            to_state="timeout",
            transition=True,
            reason=(
                f"no activity for tracking timeout "
                f"{session.connection['tracking_timeout']}"
            ),
        )
        session.state = "timeout"
        session.terminal_at = time
        self.active_by_connection.pop(session.connection_id, None)
        self.cleanups.append(
            {
                "session_id": session.session_id,
                "connection_id": session.connection_id,
                "greenhouse_id": session.connection["greenhouse_id"],
                "expired_at": time,
                "detected_at": detected_at,
                "last_activity_at": session.last_activity_at,
                "tracking_timeout": session.connection["tracking_timeout"],
                "trigger_event_id": trigger_event_id,
                "cause": cause,
                "released_mapping": released,
            }
        )

    def _expire_before(self, time: int, *, detected_at: int) -> None:
        self._expire_due(
            time,
            inclusive=False,
            trigger="automatic_timeout_sweep",
            detected_at=detected_at,
        )

    def _expire_due(
        self,
        limit: int,
        *,
        inclusive: bool,
        trigger: str,
        detected_at: int,
    ) -> None:
        candidates = sorted(
            (
                session
                for session in self.sessions
                if session.state in {"new", "established"}
            ),
            key=lambda session: (session.expires_at, session.session_id),
        )
        for session in candidates:
            due = (
                session.expires_at <= limit
                if inclusive
                else session.expires_at < limit
            )
            if not due or session.state not in {"new", "established"}:
                continue
            synthetic_id = (
                f"{trigger}:{session.session_id}:{session.expires_at}"
            )
            self._timeout_session(
                session,
                session.expires_at,
                trigger_event_id=synthetic_id,
                detected_at=detected_at,
                cause="tracking_timeout",
            )

    def _apply_policy_change(self, event: dict[str, Any]) -> None:
        operation = event.get("operation")
        target: dict[str, Any] = {"operation": operation}
        applied = False
        if operation == "set_rule_priority":
            rule_id = event.get("rule_id")
            target["rule_id"] = rule_id
            if rule_id in self.rules:
                self.rules[rule_id] = copy.deepcopy(self.rules[rule_id])
                self.rules[rule_id]["priority"] = copy.deepcopy(event["priority"])
                applied = True
        elif operation == "delete_rule":
            rule_id = event.get("rule_id")
            target["rule_id"] = rule_id
            if rule_id in self.rules:
                del self.rules[rule_id]
                applied = True
        elif operation == "upsert_rule":
            rule_id = event.get("rule_id")
            target["rule_id"] = rule_id
            if isinstance(event.get("rule"), dict):
                self.rules[rule_id] = copy.deepcopy(event["rule"])
                applied = True
        elif operation == "delete_pool":
            pool_id = event.get("pool_id")
            target["pool_id"] = pool_id
            if pool_id in self.pools:
                del self.pools[pool_id]
                applied = True
        elif operation == "upsert_pool":
            pool_id = event.get("pool_id")
            target["pool_id"] = pool_id
            if isinstance(event.get("pool_object"), dict):
                self.pools[pool_id] = copy.deepcopy(event["pool_object"])
                applied = True

        affected, unaffected = self._recompute_established(
            event["time"], event["event_id"], operation
        )
        self.impacts.append(
            {
                "change_event_id": event["event_id"],
                "time": event["time"],
                "operation": operation,
                "target": target,
                "applied": applied,
                "affected": affected,
                "unaffected_sessions": unaffected,
            }
        )

    def _recompute_established(
        self, time: int, event_id: str, operation: str | None
    ) -> tuple[list[dict[str, Any]], list[str]]:
        established = sorted(
            (
                session
                for session in self.sessions
                if session.state == "established"
            ),
            key=lambda session: session.session_id,
        )
        anchors: list[Session] = []
        candidates: list[Session] = []

        for session in established:
            selected = self._select_rule(session.connection)
            old = session.mapping
            if old and old.get("status") == "mapped" and selected is not None:
                pool = self.pools.get(selected["pool_id"])
                key = (old["external_ip"], old["external_port"])
                if (
                    old["rule_id"] == selected["rule_id"]
                    and old["pool_id"] == selected["pool_id"]
                    and pool is not None
                    and old["external_ip"] == pool["external_ip"]
                    and pool["port_start"] <= old["external_port"] <= pool["port_end"]
                    and self.reservations.get(key) == session.session_id
                ):
                    anchors.append(session)
                    continue
            if (
                old
                and old.get("status") == "unmapped"
                and old.get("reason") == "no_matching_rule"
                and selected is None
            ):
                anchors.append(session)
                continue
            candidates.append(session)

        for session in candidates:
            old = session.mapping
            if old and old.get("status") == "mapped":
                key = (old["external_ip"], old["external_port"])
                if self.reservations.get(key) == session.session_id:
                    self.reservations.pop(key, None)

        affected: list[dict[str, Any]] = []
        for session in candidates:
            before = copy.deepcopy(session.mapping)
            after = self._build_assignment(session.connection, time, event_id)
            self._reserve(session, after)
            if _mapping_signature(before) == _mapping_signature(after):
                session.mapping = before
                continue
            session.mapping = after
            session.mapping_history.append(
                {
                    "time": time,
                    "event_id": event_id,
                    "cause": "policy_recompute",
                    "operation": operation,
                    "assignment": copy.deepcopy(after),
                }
            )
            session.require_rebuild = True
            session.rebuild_reasons.append(
                {
                    "time": time,
                    "event_id": event_id,
                    "operation": operation,
                    "reason": "mapping changed after rule or pool mutation",
                }
            )
            affected.append(
                {
                    "session_id": session.session_id,
                    "connection_id": session.connection_id,
                    "greenhouse_id": session.connection["greenhouse_id"],
                    "state": session.state,
                    "before": before,
                    "after": copy.deepcopy(after),
                    "require_rebuild": True,
                    "reason": "mapping assignment changed",
                }
            )

        affected_ids = {item["session_id"] for item in affected}
        unaffected = [
            session.session_id
            for session in established
            if session.session_id not in affected_ids
        ]
        return affected, unaffected

    def _snapshot(self) -> dict[str, Any]:
        sessions = [session.to_dict() for session in sorted(
            self.sessions, key=lambda item: item.session_id
        )]
        total_counts = final_state_counts(sessions)
        pool_views = self._pool_views()
        groups = self._group_reports(sessions, pool_views)
        affected_ids = [
            session["session_id"]
            for session in sessions
            if session["require_rebuild"]
        ]
        return {
            "result_schema": "natcheck-result/1",
            "project_id": self.model["project_id"],
            "domain_id": self.model["domain_id"],
            "horizon": self.horizon,
            "sessions": sessions,
            "impacts": copy.deepcopy(self.impacts),
            "residual_cleanups": copy.deepcopy(self.cleanups),
            "ignored_events": copy.deepcopy(self.ignored_events),
            "pools": pool_views,
            "reports": {
                "groups": groups,
                "final_state_counts": total_counts,
            },
            "summary": {
                "session_count": len(sessions),
                "final_state_counts": total_counts,
                "impact_events": len(self.impacts),
                "affected_session_ids": affected_ids,
                "residual_cleanup_count": len(self.cleanups),
            },
        }

    def _pool_views(self) -> list[dict[str, Any]]:
        occupied_by_pool: dict[str, list[dict[str, Any]]] = {
            pool_id: [] for pool_id in self.pools
        }
        for session in self.sessions:
            if session.state not in {"new", "established"}:
                continue
            mapping = session.mapping
            if not mapping or mapping.get("status") != "mapped":
                continue
            pool_id = mapping["pool_id"]
            if pool_id not in occupied_by_pool:
                continue
            key = (mapping["external_ip"], mapping["external_port"])
            if self.reservations.get(key) != session.session_id:
                continue
            occupied_by_pool[pool_id].append(
                {
                    "external_ip": mapping["external_ip"],
                    "external_port": mapping["external_port"],
                    "session_id": session.session_id,
                    "connection_id": session.connection_id,
                    "state": session.state,
                }
            )

        views: list[dict[str, Any]] = []
        for pool_id, pool in sorted(self.pools.items()):
            capacity = pool["port_end"] - pool["port_start"] + 1
            occupied = sorted(
                occupied_by_pool.get(pool_id, []),
                key=lambda item: (item["external_ip"], item["external_port"]),
            )
            views.append(
                {
                    "pool_id": pool_id,
                    "greenhouse_id": pool["greenhouse_id"],
                    "external_ip": pool["external_ip"],
                    "port_start": pool["port_start"],
                    "port_end": pool["port_end"],
                    "capacity": capacity,
                    "occupied_count": len(occupied),
                    "occupancy_rate": round(len(occupied) / capacity, 6),
                    "occupied": copy.deepcopy(occupied),
                }
            )
        return views

    def _group_reports(
        self,
        sessions: list[dict[str, Any]],
        pool_views: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        groups: dict[tuple[str, str], dict[str, Any]] = {}
        pool_by_id = {pool["pool_id"]: pool for pool in pool_views}
        for session in sessions:
            mapping = session.get("mapping") or {}
            rule_id = mapping.get("rule_id") or "<unmapped>"
            key = (session["greenhouse_id"], rule_id)
            if key not in groups:
                groups[key] = {
                    "greenhouse_id": key[0],
                    "rule_id": key[1],
                    "counts": {state: 0 for state in (
                        "new", "established", "teardown", "timeout"
                    )},
                    "sessions": [],
                    "pool_ids": [],
                }
            group = groups[key]
            group["counts"][session["state"]] += 1
            group["sessions"].append(session["session_id"])
            pool_id = mapping.get("pool_id")
            if pool_id and pool_id not in group["pool_ids"]:
                group["pool_ids"].append(pool_id)

        result = []
        for group in sorted(
            groups.values(), key=lambda item: (item["greenhouse_id"], item["rule_id"])
        ):
            group["sessions"].sort()
            group["pool_ids"].sort()
            group["pool_occupancy"] = [
                copy.deepcopy(pool_by_id[pool_id])
                for pool_id in group["pool_ids"]
                if pool_id in pool_by_id
            ]
            group["session_count"] = len(group["sessions"])
            result.append(group)
        return result


def adjudicate(model: dict[str, Any]) -> dict[str, Any]:
    return AdjudicationEngine(model).run()

