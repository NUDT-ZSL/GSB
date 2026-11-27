"""Independent reference state machine and NAT mapping adjudicator.

This module intentionally does not import or call :mod:`natcheck.engine`.
It consumes only the normalized project model and reimplements lifecycle,
port allocation and policy-impact semantics for differential checking.
"""

from __future__ import annotations

import copy
import ipaddress
from typing import Any


def _same_mapping(left: dict[str, Any] | None, right: dict[str, Any] | None) -> bool:
    if left is None or right is None:
        return left is right
    return (
        left.get("status") == right.get("status")
        and left.get("reason") == right.get("reason")
        and left.get("rule_id") == right.get("rule_id")
        and left.get("pool_id") == right.get("pool_id")
        and left.get("external_ip") == right.get("external_ip")
        and left.get("external_port") == right.get("external_port")
    )


class ReferenceAdjudicator:
    def __init__(self, model: dict[str, Any]):
        self.connections = {
            item["connection_id"]: copy.deepcopy(item)
            for item in model["connections"]
        }
        self.rules = {
            item["rule_id"]: copy.deepcopy(item) for item in model["rules"]
        }
        self.pools = {
            item["pool_id"]: copy.deepcopy(item) for item in model["pools"]
        }
        self.timeline = sorted(
            (copy.deepcopy(item) for item in model["events"]),
            key=lambda item: (item["time"], item["seq"]),
        )
        self.horizon = model.get("horizon")
        if self.horizon is None and self.timeline:
            self.horizon = max(item["time"] for item in self.timeline)
        self.active: dict[str, str] = {}
        self.sessions: dict[str, dict[str, Any]] = {}
        self.generations: dict[str, int] = {}
        self.leased: dict[tuple[str, int], str] = {}
        self.impacts: list[dict[str, Any]] = []
        self.cleanups: list[dict[str, Any]] = []
        self.ignored: list[dict[str, Any]] = []

    def run(self) -> dict[str, Any]:
        previous_time: int | None = None
        for item in self.timeline:
            if previous_time is None or previous_time != item["time"]:
                self.sweep(item["time"], item["time"], False, "automatic_timeout_sweep")
                previous_time = item["time"]
            action = item["type"]
            if action == "tick":
                self.sweep(item["time"], item["time"], True, item["event_id"])
            elif action == "policy_change":
                self.mutate_policy(item)
            else:
                self.process_connection_event(item)
        if self.horizon is not None:
            self.sweep(
                self.horizon,
                self.horizon,
                True,
                "horizon_check",
            )
        return self.view()

    def process_connection_event(self, event: dict[str, Any]) -> None:
        connection_id = event.get("connection_id")
        connection = self.connections.get(connection_id)
        if connection is None:
            self.ignored.append(
                {
                    "event_id": event["event_id"],
                    "time": event["time"],
                    "seq": event["seq"],
                    "reason": f"unknown connection {connection_id}",
                }
            )
            return
        active_id = self.active.get(connection_id)
        if active_id is None:
            if event["type"] == "new":
                self.open_session(connection, event)
            else:
                self.ignored.append(
                    {
                        "event_id": event["event_id"],
                        "time": event["time"],
                        "seq": event["seq"],
                        "reason": (
                            f"{event['type']} without an active session"
                        ),
                    }
                )
            return

        session = self.sessions[active_id]
        if event["type"] == "new":
            self.touch(session, event["time"])
            self.add_trace(
                session,
                event,
                "duplicate_new_ignored",
                session["state"],
                session["state"],
                False,
                "connection is already tracked; duplicate new is ignored",
            )
        elif event["type"] == "established":
            self.touch(session, event["time"])
            if session["state"] == "new":
                self.add_trace(
                    session,
                    event,
                    "established",
                    "new",
                    "established",
                    True,
                    "session handshake completed",
                )
                session["state"] = "established"
                if (
                    session["mapping"] is None
                    or session["mapping"].get("status") != "mapped"
                ):
                    self.assign(
                        session,
                        event["time"],
                        event["event_id"],
                        "established_retry",
                    )
            else:
                self.add_trace(
                    session,
                    event,
                    "duplicate_established_ignored",
                    session["state"],
                    session["state"],
                    False,
                    "session is already established",
                )
        elif event["type"] == "teardown":
            self.add_trace(
                session,
                event,
                "teardown",
                session["state"],
                "teardown",
                True,
                "connection teardown observed",
            )
            self.drop_lease(session, event["time"], "teardown")
            session["state"] = "teardown"
            session["terminal_at"] = event["time"]
            self.active.pop(connection_id, None)
        elif event["type"] == "timeout":
            self.expire(
                session,
                event["time"],
                event["event_id"],
                event["time"],
                "explicit_timeout",
            )

    def open_session(
        self, connection: dict[str, Any], event: dict[str, Any]
    ) -> None:
        connection_id = connection["connection_id"]
        generation = self.generations.get(connection_id, 0) + 1
        self.generations[connection_id] = generation
        session_id = f"{connection_id}#{generation}"
        session = {
            "session_id": session_id,
            "connection_id": connection_id,
            "epoch": generation,
            "connection": copy.deepcopy(connection),
            "state": "new",
            "created_at": event["time"],
            "created_event_id": event["event_id"],
            "last_activity_at": event["time"],
            "expires_at": event["time"] + connection["tracking_timeout"],
            "terminal_at": None,
            "mapping": None,
            "mapping_history": [],
            "trace": [],
            "require_rebuild": False,
            "rebuild_reasons": [],
        }
        self.sessions[session_id] = session
        self.active[connection_id] = session_id
        self.assign(session, event["time"], event["event_id"], "session_new")
        self.add_trace(
            session,
            event,
            "new",
            None,
            "new",
            True,
            "first packet created a new tracked session",
        )

    def touch(self, session: dict[str, Any], time: int) -> None:
        session["last_activity_at"] = time
        session["expires_at"] = time + session["connection"]["tracking_timeout"]

    def add_trace(
        self,
        session: dict[str, Any],
        event: dict[str, Any],
        trigger: str,
        from_state: str | None,
        to_state: str,
        transition: bool,
        reason: str,
    ) -> None:
        session["trace"].append(
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

    def eligible(self, rule: dict[str, Any], connection: dict[str, Any]) -> bool:
        tuple_value = connection["five_tuple"]
        if (
            rule["greenhouse_id"] != "*"
            and rule["greenhouse_id"] != "any"
            and rule["greenhouse_id"] != connection["greenhouse_id"]
        ):
            return False
        if rule["protocol"] != "any" and rule["protocol"] != tuple_value["protocol"]:
            return False
        if (
            rule["direction"] != "any"
            and rule["direction"] != connection["direction"]
        ):
            return False
        try:
            network = ipaddress.ip_network(rule["src_cidr"], strict=False)
            address = ipaddress.ip_address(tuple_value["src_ip"])
        except ValueError:
            return False
        if address not in network:
            return False
        if not (
            rule["dst_port_start"]
            <= tuple_value["dst_port"]
            <= rule["dst_port_end"]
        ):
            return False
        if (
            rule["src_port_start"] is not None
            and rule["src_port_end"] is not None
            and not (
                rule["src_port_start"]
                <= tuple_value["src_port"]
                <= rule["src_port_end"]
            )
        ):
            return False
        return True

    def winner(self, connection: dict[str, Any]) -> dict[str, Any] | None:
        ordered = sorted(
            self.rules.values(),
            key=lambda rule: (tuple(rule["priority"]), rule["rule_id"]),
        )
        for rule in ordered:
            if self.eligible(rule, connection):
                return copy.deepcopy(rule)
        return None

    def allocate(
        self, connection: dict[str, Any], time: int, event_id: str
    ) -> dict[str, Any]:
        rule = self.winner(connection)
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
        for candidate in range(pool["port_start"], pool["port_end"] + 1):
            if (pool["external_ip"], candidate) not in self.leased:
                return {
                    "status": "mapped",
                    "reason": "selected",
                    "rule_id": rule["rule_id"],
                    "pool_id": pool["pool_id"],
                    "priority": copy.deepcopy(rule["priority"]),
                    "external_ip": pool["external_ip"],
                    "external_port": candidate,
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

    def assign(
        self,
        session: dict[str, Any],
        time: int,
        event_id: str,
        cause: str,
    ) -> dict[str, Any]:
        mapping = self.allocate(session["connection"], time, event_id)
        session["mapping"] = mapping
        if mapping["status"] == "mapped":
            self.leased[
                (mapping["external_ip"], mapping["external_port"])
            ] = session["session_id"]
        session["mapping_history"].append(
            {
                "time": time,
                "event_id": event_id,
                "cause": cause,
                "assignment": copy.deepcopy(mapping),
            }
        )
        return mapping

    def drop_lease(
        self, session: dict[str, Any], time: int, reason: str
    ) -> None:
        mapping = session["mapping"]
        if not mapping or mapping.get("status") != "mapped":
            return
        key = (mapping["external_ip"], mapping["external_port"])
        if self.leased.get(key) == session["session_id"]:
            self.leased.pop(key, None)
        mapping["released_at"] = time
        mapping["release_reason"] = reason

    def expire(
        self,
        session: dict[str, Any],
        time: int,
        trigger_event_id: str,
        detected_at: int,
        cause: str,
    ) -> None:
        if session["state"] not in {"new", "established"}:
            return
        previous_mapping = copy.deepcopy(session["mapping"])
        self.drop_lease(session, time, "timeout")
        self.add_trace(
            session,
            {"event_id": trigger_event_id, "time": time, "seq": None},
            "tracking_timeout",
            session["state"],
            "timeout",
            True,
            (
                f"no activity for tracking timeout "
                f"{session['connection']['tracking_timeout']}"
            ),
        )
        session["state"] = "timeout"
        session["terminal_at"] = time
        self.active.pop(session["connection_id"], None)
        self.cleanups.append(
            {
                "session_id": session["session_id"],
                "connection_id": session["connection_id"],
                "greenhouse_id": session["connection"]["greenhouse_id"],
                "expired_at": time,
                "detected_at": detected_at,
                "last_activity_at": session["last_activity_at"],
                "tracking_timeout": session["connection"]["tracking_timeout"],
                "trigger_event_id": trigger_event_id,
                "cause": cause,
                "released_mapping": previous_mapping,
            }
        )

    def sweep(
        self,
        limit: int,
        detected_at: int,
        inclusive: bool,
        trigger: str,
    ) -> None:
        due_sessions = []
        for session in self.sessions.values():
            if session["state"] not in {"new", "established"}:
                continue
            if inclusive:
                due = session["expires_at"] <= limit
            else:
                due = session["expires_at"] < limit
            if due:
                due_sessions.append(session)
        due_sessions.sort(
            key=lambda session: (
                session["expires_at"],
                session["session_id"],
            )
        )
        for session in due_sessions:
            if session["state"] not in {"new", "established"}:
                continue
            trigger_id = (
                f"{trigger}:{session['session_id']}:{session['expires_at']}"
            )
            self.expire(
                session,
                session["expires_at"],
                trigger_id,
                detected_at,
                "tracking_timeout",
            )

    def mutate_policy(self, event: dict[str, Any]) -> None:
        operation = event.get("operation")
        target: dict[str, Any] = {"operation": operation}
        applied = False
        if operation == "set_rule_priority":
            target["rule_id"] = event.get("rule_id")
            if event.get("rule_id") in self.rules:
                self.rules[event["rule_id"]]["priority"] = copy.deepcopy(
                    event["priority"]
                )
                applied = True
        elif operation == "delete_rule":
            target["rule_id"] = event.get("rule_id")
            if event.get("rule_id") in self.rules:
                del self.rules[event["rule_id"]]
                applied = True
        elif operation == "upsert_rule":
            target["rule_id"] = event.get("rule_id")
            if isinstance(event.get("rule"), dict):
                self.rules[event["rule_id"]] = copy.deepcopy(event["rule"])
                applied = True
        elif operation == "delete_pool":
            target["pool_id"] = event.get("pool_id")
            if event.get("pool_id") in self.pools:
                del self.pools[event["pool_id"]]
                applied = True
        elif operation == "upsert_pool":
            target["pool_id"] = event.get("pool_id")
            if isinstance(event.get("pool_object"), dict):
                self.pools[event["pool_id"]] = copy.deepcopy(
                    event["pool_object"]
                )
                applied = True

        changed, unchanged = self.recompute(event["time"], event["event_id"], operation)
        self.impacts.append(
            {
                "change_event_id": event["event_id"],
                "time": event["time"],
                "operation": operation,
                "target": target,
                "applied": applied,
                "affected": changed,
                "unaffected_sessions": unchanged,
            }
        )

    def recompute(
        self, time: int, event_id: str, operation: str | None
    ) -> tuple[list[dict[str, Any]], list[str]]:
        established = sorted(
            (
                session
                for session in self.sessions.values()
                if session["state"] == "established"
            ),
            key=lambda session: session["session_id"],
        )
        stable = []
        movable = []
        for session in established:
            rule = self.winner(session["connection"])
            old = session["mapping"]
            if old and old.get("status") == "mapped" and rule is not None:
                pool = self.pools.get(rule["pool_id"])
                endpoint = (old["external_ip"], old["external_port"])
                if (
                    old["rule_id"] == rule["rule_id"]
                    and old["pool_id"] == rule["pool_id"]
                    and pool is not None
                    and old["external_ip"] == pool["external_ip"]
                    and pool["port_start"]
                    <= old["external_port"]
                    <= pool["port_end"]
                    and self.leased.get(endpoint) == session["session_id"]
                ):
                    stable.append(session)
                    continue
            if (
                old
                and old.get("status") == "unmapped"
                and old.get("reason") == "no_matching_rule"
                and rule is None
            ):
                stable.append(session)
                continue
            movable.append(session)

        for session in movable:
            old = session["mapping"]
            if old and old.get("status") == "mapped":
                endpoint = (old["external_ip"], old["external_port"])
                if self.leased.get(endpoint) == session["session_id"]:
                    self.leased.pop(endpoint, None)

        affected = []
        for session in movable:
            before = copy.deepcopy(session["mapping"])
            after = self.allocate(session["connection"], time, event_id)
            if after["status"] == "mapped":
                self.leased[
                    (after["external_ip"], after["external_port"])
                ] = session["session_id"]
            if _same_mapping(before, after):
                session["mapping"] = before
                continue
            session["mapping"] = after
            session["mapping_history"].append(
                {
                    "time": time,
                    "event_id": event_id,
                    "cause": "policy_recompute",
                    "operation": operation,
                    "assignment": copy.deepcopy(after),
                }
            )
            session["require_rebuild"] = True
            session["rebuild_reasons"].append(
                {
                    "time": time,
                    "event_id": event_id,
                    "operation": operation,
                    "reason": "mapping changed after rule or pool mutation",
                }
            )
            affected.append(
                {
                    "session_id": session["session_id"],
                    "connection_id": session["connection_id"],
                    "greenhouse_id": session["connection"]["greenhouse_id"],
                    "state": session["state"],
                    "before": before,
                    "after": copy.deepcopy(after),
                    "require_rebuild": True,
                    "reason": "mapping assignment changed",
                }
            )
        affected_ids = {item["session_id"] for item in affected}
        unchanged = [
            session["session_id"]
            for session in established
            if session["session_id"] not in affected_ids
        ]
        return affected, unchanged

    def view(self) -> dict[str, Any]:
        sessions = []
        for session_id in sorted(self.sessions):
            session = self.sessions[session_id]
            sessions.append(
                {
                    "session_id": session["session_id"],
                    "connection_id": session["connection_id"],
                    "epoch": session["epoch"],
                    "greenhouse_id": session["connection"]["greenhouse_id"],
                    "direction": session["connection"]["direction"],
                    "tracking_timeout": session["connection"]["tracking_timeout"],
                    "five_tuple": copy.deepcopy(session["connection"]["five_tuple"]),
                    "state": session["state"],
                    "created_at": session["created_at"],
                    "created_event_id": session["created_event_id"],
                    "last_activity_at": session["last_activity_at"],
                    "expires_at": session["expires_at"],
                    "terminal_at": session["terminal_at"],
                    "trace": copy.deepcopy(session["trace"]),
                    "mapping": copy.deepcopy(session["mapping"]),
                    "mapping_history": copy.deepcopy(session["mapping_history"]),
                    "require_rebuild": session["require_rebuild"],
                    "rebuild_reasons": copy.deepcopy(session["rebuild_reasons"]),
                }
            )
        return {
            "reference_schema": "natcheck-reference/1",
            "sessions": sessions,
            "impacts": copy.deepcopy(self.impacts),
            "residual_cleanups": copy.deepcopy(self.cleanups),
            "ignored_events": copy.deepcopy(self.ignored),
        }


def reference_adjudicate(model: dict[str, Any]) -> dict[str, Any]:
    return ReferenceAdjudicator(model).run()

