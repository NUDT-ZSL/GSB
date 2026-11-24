from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

from .util import digest_json


ATTEMPT_RESULTS = frozenset({"success", "failure", "denied"})


def normalize_key(value: str) -> str:
    return value.strip().casefold()


@dataclass(frozen=True)
class Manifest:
    sample_id: str
    seed: int
    observed_clock_ms: int
    files: dict[str, str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "sample_id": self.sample_id,
            "seed": self.seed,
            "observed_clock_ms": self.observed_clock_ms,
            "files": dict(sorted(self.files.items())),
        }


@dataclass(frozen=True)
class Session:
    session_id: str
    user_id: str
    issued_at_ms: int
    expires_at_ms: int
    device_id: str
    generation: int
    parent_session_id: str | None
    window_start_ms: int
    window_end_ms: int
    policy_key: str
    source_index: int

    @property
    def stable_key(self) -> tuple[str, int]:
        return (self.session_id, self.generation)

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "user_id": self.user_id,
            "issued_at_ms": self.issued_at_ms,
            "expires_at_ms": self.expires_at_ms,
            "device_id": self.device_id,
            "generation": self.generation,
            "parent_session_id": self.parent_session_id,
            "window_start_ms": self.window_start_ms,
            "window_end_ms": self.window_end_ms,
            "policy_key": self.policy_key,
            "source_index": self.source_index,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "Session":
        return cls(
            session_id=str(value["session_id"]),
            user_id=str(value["user_id"]),
            issued_at_ms=int(value["issued_at_ms"]),
            expires_at_ms=int(value["expires_at_ms"]),
            device_id=str(value["device_id"]),
            generation=int(value["generation"]),
            parent_session_id=value["parent_session_id"],
            window_start_ms=int(value["window_start_ms"]),
            window_end_ms=int(value["window_end_ms"]),
            policy_key=str(value["policy_key"]),
            source_index=int(value["source_index"]),
        )


@dataclass(frozen=True)
class Event:
    event_seq: int
    session_id: str
    event_time_ms: int
    attempt_result: str
    privilege_escalated: bool
    event_type: str
    user_id: str | None
    device_id: str | None
    policy_key: str | None
    source_index: int

    @property
    def canonical_key(self) -> tuple[Any, ...]:
        return (
            self.event_time_ms,
            self.event_seq,
            self.session_id,
            self.attempt_result,
            self.privilege_escalated,
            self.event_type,
            self.user_id or "",
            self.device_id or "",
            self.policy_key or "",
            digest_json(self.to_dict()),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_seq": self.event_seq,
            "session_id": self.session_id,
            "event_time_ms": self.event_time_ms,
            "attempt_result": self.attempt_result,
            "privilege_escalated": self.privilege_escalated,
            "event_type": self.event_type,
            "user_id": self.user_id,
            "device_id": self.device_id,
            "policy_key": self.policy_key,
            "source_index": self.source_index,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "Event":
        return cls(
            event_seq=int(value["event_seq"]),
            session_id=str(value["session_id"]),
            event_time_ms=int(value["event_time_ms"]),
            attempt_result=str(value["attempt_result"]),
            privilege_escalated=bool(value["privilege_escalated"]),
            event_type=str(value["event_type"]),
            user_id=value["user_id"],
            device_id=value["device_id"],
            policy_key=value["policy_key"],
            source_index=int(value["source_index"]),
        )


@dataclass(frozen=True)
class Rejection:
    source_category: str
    record_index: int
    code: str
    message: str
    source_ref: str
    record_digest: str

    @property
    def stable_key(self) -> tuple[Any, ...]:
        return (
            self.source_category,
            self.code,
            self.record_digest,
            self.record_index,
            self.message,
            self.source_ref,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_category": self.source_category,
            "record_index": self.record_index,
            "code": self.code,
            "message": self.message,
            "source_ref": self.source_ref,
            "record_digest": self.record_digest,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "Rejection":
        return cls(
            source_category=str(value["source_category"]),
            record_index=int(value["record_index"]),
            code=str(value["code"]),
            message=str(value["message"]),
            source_ref=str(value["source_ref"]),
            record_digest=str(value["record_digest"]),
        )


@dataclass(frozen=True)
class PolicyRecord:
    policy_key: str
    policy_generation: int
    effective_at_ms: int
    rotation_mode: str
    min_rotation_interval_ms: int | None
    bind_device_required: bool
    tombstone: bool
    source_format: str
    source_ref: str
    record_index: int

    @property
    def authority_key(self) -> tuple[int, int, bool, str, int, str]:
        return (
            self.policy_generation,
            self.effective_at_ms,
            self.tombstone,
            self.source_format,
            self.record_index,
            digest_json(self.to_dict()),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "policy_key": self.policy_key,
            "policy_generation": self.policy_generation,
            "effective_at_ms": self.effective_at_ms,
            "rotation_mode": self.rotation_mode,
            "min_rotation_interval_ms": self.min_rotation_interval_ms,
            "bind_device_required": self.bind_device_required,
            "tombstone": self.tombstone,
            "source_format": self.source_format,
            "source_ref": self.source_ref,
            "record_index": self.record_index,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "PolicyRecord":
        return cls(
            policy_key=str(value["policy_key"]),
            policy_generation=int(value["policy_generation"]),
            effective_at_ms=int(value["effective_at_ms"]),
            rotation_mode=str(value["rotation_mode"]),
            min_rotation_interval_ms=(
                None
                if value.get("min_rotation_interval_ms") is None
                else int(value["min_rotation_interval_ms"])
            ),
            bind_device_required=bool(value["bind_device_required"]),
            tombstone=bool(value["tombstone"]),
            source_format=str(value["source_format"]),
            source_ref=str(value["source_ref"]),
            record_index=int(value["record_index"]),
        )


@dataclass(frozen=True)
class PolicyConflict:
    policy_key: str
    field: str
    v1_value: Any
    v2_value: Any
    v1_record: dict[str, Any]
    v2_record: dict[str, Any]

    @property
    def stable_key(self) -> tuple[str, str]:
        return (self.policy_key, self.field)

    def to_dict(self) -> dict[str, Any]:
        return {
            "policy_key": self.policy_key,
            "field": self.field,
            "v1_value": self.v1_value,
            "v2_value": self.v2_value,
            "v1_record": self.v1_record,
            "v2_record": self.v2_record,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "PolicyConflict":
        return cls(
            policy_key=str(value["policy_key"]),
            field=str(value["field"]),
            v1_value=value["v1_value"],
            v2_value=value["v2_value"],
            v1_record=dict(value["v1_record"]),
            v2_record=dict(value["v2_record"]),
        )


@dataclass(frozen=True)
class PolicyView:
    policy_key: str
    policy_generation: int
    effective_at_ms: int
    rotation_mode: str | None
    min_rotation_interval_ms: int
    binding_required: bool | None
    tombstone: bool
    authority_source: str
    authority_record: dict[str, Any]
    conflicts: tuple[PolicyConflict, ...] = ()
    records: tuple[PolicyRecord, ...] = ()

    @property
    def has_conflict(self) -> bool:
        return bool(self.conflicts)

    def to_dict(self) -> dict[str, Any]:
        return {
            "policy_key": self.policy_key,
            "policy_generation": self.policy_generation,
            "effective_at_ms": self.effective_at_ms,
            "rotation_mode": self.rotation_mode,
            "min_rotation_interval_ms": self.min_rotation_interval_ms,
            "binding_required": self.binding_required,
            "tombstone": self.tombstone,
            "authority_source": self.authority_source,
            "authority_record": self.authority_record,
            "conflicts": [conflict.to_dict() for conflict in self.conflicts],
            "records": [record.to_dict() for record in self.records],
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "PolicyView":
        return cls(
            policy_key=str(value["policy_key"]),
            policy_generation=int(value["policy_generation"]),
            effective_at_ms=int(value["effective_at_ms"]),
            rotation_mode=value["rotation_mode"],
            min_rotation_interval_ms=int(value["min_rotation_interval_ms"]),
            binding_required=value["binding_required"],
            tombstone=bool(value["tombstone"]),
            authority_source=str(value["authority_source"]),
            authority_record=dict(value["authority_record"]),
            conflicts=tuple(
                PolicyConflict.from_dict(item) for item in value.get("conflicts", [])
            ),
            records=tuple(
                PolicyRecord.from_dict(item) for item in value.get("records", [])
            ),
        )


@dataclass(frozen=True)
class SessionCycle:
    session_ids: tuple[str, ...]
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {"session_ids": list(self.session_ids), "reason": self.reason}

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "SessionCycle":
        return cls(
            session_ids=tuple(str(item) for item in value["session_ids"]),
            reason=str(value["reason"]),
        )


@dataclass
class NormalizedSample:
    manifest: Manifest
    sessions: tuple[Session, ...]
    events: tuple[Event, ...]
    rejections: tuple[Rejection, ...]
    cycles: tuple[SessionCycle, ...]
    policy_views: dict[str, PolicyView]
    policy_conflicts: tuple[PolicyConflict, ...]
    sample_digest: str = field(default="")

    @property
    def sample_id(self) -> str:
        return self.manifest.sample_id

    def session_map(self) -> dict[str, Session]:
        return {session.session_id: session for session in self.sessions}

    def to_dict(self) -> dict[str, Any]:
        return {
            "manifest": self.manifest.to_dict(),
            "sessions": [item.to_dict() for item in self.sessions],
            "events": [item.to_dict() for item in self.events],
            "rejections": [item.to_dict() for item in self.rejections],
            "cycles": [item.to_dict() for item in self.cycles],
            "policy_views": [
                self.policy_views[key].to_dict()
                for key in sorted(self.policy_views)
            ],
            "policy_conflicts": [
                item.to_dict() for item in self.policy_conflicts
            ],
            "sample_digest": self.sample_digest,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "NormalizedSample":
        return cls(
            manifest=Manifest(
                sample_id=str(value["manifest"]["sample_id"]),
                seed=int(value["manifest"]["seed"]),
                observed_clock_ms=int(value["manifest"]["observed_clock_ms"]),
                files={
                    str(key): str(item)
                    for key, item in dict(value["manifest"]["files"]).items()
                },
            ),
            sessions=tuple(Session.from_dict(item) for item in value["sessions"]),
            events=tuple(Event.from_dict(item) for item in value["events"]),
            rejections=tuple(
                Rejection.from_dict(item) for item in value["rejections"]
            ),
            cycles=tuple(
                SessionCycle.from_dict(item) for item in value["cycles"]
            ),
            policy_views={
                str(item["policy_key"]): PolicyView.from_dict(item)
                for item in value["policy_views"]
            },
            policy_conflicts=tuple(
                PolicyConflict.from_dict(item)
                for item in value["policy_conflicts"]
            ),
            sample_digest=str(value["sample_digest"]),
        )


def layout_version(views: Iterable[PolicyView]) -> str:
    payload = []
    for view in sorted(views, key=lambda item: item.policy_key):
        payload.append(
            {
                "policy_key": view.policy_key,
                "policy_generation": view.policy_generation,
                "rotation_mode": view.rotation_mode,
                "binding_required": view.binding_required,
                "min_rotation_interval_ms": view.min_rotation_interval_ms,
                "tombstone": view.tombstone,
            }
        )
    return digest_json(payload)
