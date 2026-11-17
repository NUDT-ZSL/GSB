"""Deterministic session/token state machine driven by validated event streams."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

from .diagnostics import Diagnostic, sort_diagnostics
from .parser import PolicyEntry, RawEvent, validate_raw_event


@dataclass
class EventOutcome:
    accepted: bool
    record: dict[str, Any]
    diagnostics: list[Diagnostic] = field(default_factory=list)


class EventState:
    def __init__(self) -> None:
        self.sessions: dict[str, dict[str, Any]] = {}
        self.tokens: dict[str, dict[str, Any]] = {}
        self.records: list[dict[str, Any]] = []
        self.diagnostics: list[Diagnostic] = []
        self.processed_keys: set[str] = set()

    def reset(self) -> None:
        self.sessions.clear()
        self.tokens.clear()
        self.records.clear()
        self.diagnostics.clear()
        self.processed_keys.clear()

    def process(
        self,
        event: RawEvent,
        entries: dict[str, PolicyEntry],
    ) -> EventOutcome:
        validation = validate_raw_event(event)
        if validation:
            return self._reject(event, validation)

        event_id = event.event_id
        if event_id in self.processed_keys:
            raise ValueError(f"event {event_id} has already been processed")

        event_type = event.event_type
        session_id = str(event.event["session_id"])
        token_id = event.event.get("token_id")
        binding_id = event.event.get("binding_id")
        timestamp = event.timestamp

        if event_type == "login":
            assert isinstance(token_id, str)
            assert isinstance(binding_id, str)
            binding = entries.get(binding_id)
            if binding is None or binding.kind != "role_binding":
                return self._reject_reference(
                    event, "binding_id does not resolve to a role binding"
                )
            session = self.sessions.get(session_id)
            if session is not None and session["active"]:
                return self._reject_state(event, "session is already active")
            if token_id in self.tokens:
                return self._reject_state(event, "token_id was already issued")
            dependencies = list(binding.runtime_dependencies())
            now = {
                "session_id": session_id,
                "subject": binding.subject,
                "binding_id": binding_id,
                "role": binding.role,
                "dependencies": dependencies,
                "active": True,
                "created_event_id": event_id,
                "last_event_id": event_id,
                "timestamp": timestamp,
                "token_ids": sorted(
                    set((session or {}).get("token_ids", [])) | {token_id}
                ),
            }
            self.sessions[session_id] = now
            self.tokens[token_id] = {
                "token_id": token_id,
                "session_id": session_id,
                "binding_id": binding_id,
                "dependencies": list(dependencies),
                "active": True,
                "status": "active",
                "issued_event_id": event_id,
                "last_event_id": event_id,
                "timestamp": timestamp,
            }
            return self._accept(event)

        if event_type == "renew":
            assert isinstance(token_id, str)
            assert isinstance(binding_id, str)
            session = self.sessions.get(session_id)
            if session is None or not session["active"]:
                return self._reject_state(event, "renew requires an active session")
            if token_id in self.tokens:
                return self._reject_state(event, "token_id was already issued")
            binding = entries.get(binding_id)
            if binding is None or binding.kind != "role_binding":
                return self._reject_reference(
                    event, "binding_id does not resolve to a role binding"
                )
            dependencies = list(binding.runtime_dependencies())
            session.update(
                {
                    "binding_id": binding_id,
                    "subject": binding.subject,
                    "role": binding.role,
                    "dependencies": dependencies,
                    "last_event_id": event_id,
                    "timestamp": timestamp,
                }
            )
            session["token_ids"] = sorted(set(session["token_ids"]) | {token_id})
            self.tokens[token_id] = {
                "token_id": token_id,
                "session_id": session_id,
                "binding_id": binding_id,
                "dependencies": list(dependencies),
                "active": True,
                "status": "active",
                "issued_event_id": event_id,
                "last_event_id": event_id,
                "timestamp": timestamp,
            }
            return self._accept(event)

        if event_type == "role_change":
            assert isinstance(binding_id, str)
            session = self.sessions.get(session_id)
            if session is None or not session["active"]:
                return self._reject_state(
                    event, "role_change requires an active session"
                )
            binding = entries.get(binding_id)
            if binding is None or binding.kind != "role_binding":
                return self._reject_reference(
                    event, "binding_id does not resolve to a role binding"
                )
            session.update(
                {
                    "binding_id": binding_id,
                    "subject": binding.subject,
                    "role": binding.role,
                    "dependencies": list(binding.runtime_dependencies()),
                    "last_event_id": event_id,
                    "timestamp": timestamp,
                }
            )
            return self._accept(event)

        if event_type == "logout":
            session = self.sessions.get(session_id)
            if session is None or not session["active"]:
                return self._reject_state(event, "logout requires an active session")
            session["active"] = False
            session["last_event_id"] = event_id
            session["timestamp"] = timestamp
            for issued_token_id in session.get("token_ids", []):
                token = self.tokens.get(issued_token_id)
                if token is not None and token["active"]:
                    token["active"] = False
                    token["status"] = "logged_out"
                    token["last_event_id"] = event_id
            return self._accept(event)

        if event_type == "token_revoke":
            assert isinstance(token_id, str)
            token = self.tokens.get(token_id)
            if token is None:
                return self._reject_state(event, "token_id was never issued")
            if token["session_id"] != session_id:
                return self._reject_state(
                    event, "token does not belong to the supplied session"
                )
            if not token["active"]:
                return self._reject_state(event, "token is not active")
            token["active"] = False
            token["status"] = "revoked"
            token["last_event_id"] = event_id
            return self._accept(event)

        return self._reject_state(event, "unsupported event type")

    def _accept(self, event: RawEvent) -> EventOutcome:
        self.processed_keys.add(event.event_id)
        record = self._record(event, accepted=True)
        self.records.append(record)
        return EventOutcome(accepted=True, record=record)

    def _reject(
        self,
        event: RawEvent,
        diagnostics: list[Diagnostic],
    ) -> EventOutcome:
        if event.event_id:
            self.processed_keys.add(event.event_id)
        for diagnostic in diagnostics:
            self.diagnostics.append(diagnostic)
        record = self._record(
            event,
            accepted=False,
            code=diagnostics[0].code if diagnostics else "E_EVENT_STATE",
        )
        self.records.append(record)
        return EventOutcome(
            accepted=False,
            record=record,
            diagnostics=sort_diagnostics(diagnostics),
        )

    def _reject_state(self, event: RawEvent, message: str) -> EventOutcome:
        diagnostic = Diagnostic(
            code="E_EVENT_STATE",
            message=message,
            source=event.source,
            index=event.index,
            offset=event.offset,
            stable_key=event.event_id,
            details={"event_id": event.event_id, "type": event.event_type},
        )
        return self._reject(event, [diagnostic])

    def _reject_reference(self, event: RawEvent, message: str) -> EventOutcome:
        diagnostic = Diagnostic(
            code="E_EVENT_REF",
            message=message,
            source=event.source,
            index=event.index,
            offset=event.offset,
            stable_key=event.event_id,
            details={
                "event_id": event.event_id,
                "binding_id": event.event.get("binding_id"),
            },
        )
        return self._reject(event, [diagnostic])

    def _record(
        self,
        event: RawEvent,
        *,
        accepted: bool,
        code: str | None = None,
    ) -> dict[str, Any]:
        record: dict[str, Any] = {
            "event_id": event.event_id,
            "type": event.event_type,
            "source": event.source,
            "index": event.index,
            "ordinal": event.index + 1,
            "offset": event.offset,
            "status": "accepted" if accepted else "rejected",
        }
        if code is not None:
            record["code"] = code
        return record

    def active_sessions(self) -> list[dict[str, Any]]:
        return [
            dict(self.sessions[session_id])
            for session_id in sorted(self.sessions)
            if self.sessions[session_id]["active"]
        ]

    def active_tokens(self) -> list[dict[str, Any]]:
        return [
            dict(self.tokens[token_id])
            for token_id in sorted(self.tokens)
            if self.tokens[token_id]["active"]
        ]

    def to_checkpoint(self) -> dict[str, Any]:
        return {
            "sessions": _stable_mapping(self.sessions),
            "tokens": _stable_mapping(self.tokens),
            "records": list(self.records),
            "diagnostics": [
                diagnostic.to_dict() for diagnostic in sort_diagnostics(self.diagnostics)
            ],
            "processed_event_keys": sorted(self.processed_keys),
        }

    @classmethod
    def from_checkpoint(cls, value: dict[str, Any]) -> "EventState":
        state = cls()
        state.sessions = _stable_mapping(value.get("sessions", {}))
        state.tokens = _stable_mapping(value.get("tokens", {}))
        state.records = list(value.get("records", []))
        state.processed_keys = set(value.get("processed_event_keys", []))
        state.diagnostics = [
            diagnostic_from_dict(item)
            for item in value.get("diagnostics", [])
            if isinstance(item, dict)
        ]
        return state

    def canonical_events(events: Iterable[RawEvent]) -> list[RawEvent]:
        return sorted(events, key=lambda event: event.sort_key())


def diagnostic_from_dict(value: dict[str, Any]) -> Diagnostic:
    index = value.get("index")
    offset = value.get("offset")
    return Diagnostic(
        code=str(value.get("code", "E_INTERNAL")),
        message=str(value.get("message", "")),
        source=str(value.get("source", "")),
        index=index if isinstance(index, int) else None,
        offset=offset if isinstance(offset, int) else None,
        stable_key=str(value.get("stable_key", "")),
        details=value.get("details", {}) if isinstance(value.get("details"), dict) else {},
        severity=str(value.get("severity", "error")),
    )


def _stable_mapping(value: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(value, dict):
        return {}
    result: dict[str, dict[str, Any]] = {}
    for key in sorted(value):
        if isinstance(value[key], dict):
            result[str(key)] = value[key]
    return result
