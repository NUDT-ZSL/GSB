from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Optional


JsonDict = dict[str, Any]


def normalize_identifier(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def normalize_scope(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [item for item in value.split() if item]
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    return [str(value).strip()]


def source_ref_dict(
    source_version: str,
    path: str,
    record_no: int,
    record_id: Optional[str] = None,
    generation: Optional[int] = None,
    collection: Optional[str] = None,
) -> JsonDict:
    return {
        "source_version": source_version,
        "path": path,
        "record_no": record_no,
        "record_index": max(record_no - 1, 0),
        "record_id": record_id,
        "generation": generation,
        "collection": collection,
    }


@dataclass(frozen=True)
class SourceRef:
    source_version: str
    path: str
    record_no: int
    record_id: Optional[str] = None
    generation: Optional[int] = 0
    collection: Optional[str] = None

    def to_dict(self) -> JsonDict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: JsonDict) -> "SourceRef":
        return cls(
            source_version=data.get("source_version", "unknown"),
            path=data.get("path", ""),
            record_no=int(data.get("record_no", data.get("record_index", 0) + 1)),
            record_id=data.get("record_id"),
            generation=data.get("generation", 0),
            collection=data.get("collection"),
        )


@dataclass
class Rejection:
    code: str
    message: str
    source_version: str
    path: str
    record_no: int
    record_id: Optional[str] = None
    collection: Optional[str] = None

    def to_dict(self) -> JsonDict:
        return asdict(self)


@dataclass
class Conflict:
    code: str
    entity_key: str
    field: str
    left: Any
    right: Any
    left_source: JsonDict
    right_source: JsonDict
    message: str = ""

    def to_dict(self) -> JsonDict:
        return asdict(self)


@dataclass
class NormalizedRecord:
    record_type: str
    record_id: str
    source: SourceRef
    client_id: Optional[str] = None
    code: Optional[str] = None
    session_id: Optional[str] = None
    redirect_uri: Optional[str] = None
    scope: list[str] = field(default_factory=list)
    issued_at_ms: Optional[int] = None
    expires_at_ms: Optional[int] = None
    not_before_ms: Optional[int] = None
    used: bool = False
    used_at_ms: Optional[int] = None
    generation: int = 0
    tombstoned: bool = False
    pkce_challenge: Optional[str] = None
    pkce_method: Optional[str] = None
    code_verifier: Optional[str] = None
    chain_id: Optional[str] = None
    token_id: Optional[str] = None
    access_token_id: Optional[str] = None
    refresh_token_id: Optional[str] = None
    rotates_from_id: Optional[str] = None
    successor_id: Optional[str] = None
    batch_id: Optional[str] = None
    arrival_order: int = 0
    raw: JsonDict = field(default_factory=dict)

    @property
    def as_of_ms(self) -> int:
        candidate = self.raw.get("updated_at_ms")
        if isinstance(candidate, int) and not isinstance(candidate, bool):
            return candidate
        if self.issued_at_ms is not None:
            return self.issued_at_ms
        return 0

    def source_dict(self) -> JsonDict:
        data = self.source.to_dict()
        data["generation"] = self.generation
        return data

    def to_dict(self) -> JsonDict:
        data = asdict(self)
        data["source"] = self.source.to_dict()
        data["as_of_ms"] = self.as_of_ms
        return data

    @classmethod
    def from_dict(cls, data: JsonDict) -> "NormalizedRecord":
        source = SourceRef.from_dict(data.get("source", {}))
        known = {
            key: value
            for key, value in data.items()
            if key
            in {
                "record_type",
                "record_id",
                "client_id",
                "code",
                "session_id",
                "redirect_uri",
                "scope",
                "issued_at_ms",
                "expires_at_ms",
                "not_before_ms",
                "used",
                "used_at_ms",
                "generation",
                "tombstoned",
                "pkce_challenge",
                "pkce_method",
                "code_verifier",
                "chain_id",
                "token_id",
                "access_token_id",
                "refresh_token_id",
                "rotates_from_id",
                "successor_id",
                "batch_id",
                "arrival_order",
                "raw",
            }
        }
        return cls(source=source, **known)


def client_key(client_id: Any) -> str:
    return f"client:{normalize_identifier(client_id)}"


def code_key(client_id: Any, code: Any) -> str:
    return f"code:{normalize_identifier(client_id)}:{normalize_identifier(code)}"


def session_key(session_id: Any) -> str:
    return f"session:{normalize_identifier(session_id)}"


def token_key(token_id: Any, token_type: Optional[str] = None) -> str:
    prefix = token_type or "token"
    return f"{prefix}:{normalize_identifier(token_id)}"


def record_entity_key(record: NormalizedRecord) -> str:
    if record.record_type == "authorization_code":
        return code_key(record.client_id, record.code)
    if record.record_type == "session":
        return session_key(record.session_id or record.record_id)
    if record.record_type in {"access_token", "refresh_token"}:
        token_type = "access_token" if record.record_type == "access_token" else "refresh_token"
        return token_key(record.token_id or record.record_id, token_type)
    if record.record_type == "client":
        return client_key(record.client_id or record.record_id)
    return f"{record.record_type}:{record.record_id}"
