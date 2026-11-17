"""Typed in-memory models for routes, tokens and requests."""

from __future__ import annotations

from dataclasses import dataclass, field

TOKEN_POLICY = "token"
SIGNATURE_POLICY = "signature"
POLICIES = (TOKEN_POLICY, SIGNATURE_POLICY)

PATH_TOKEN_BUCKET = "token_bucket"
PATH_WINDOW_COUNTER = "sliding_window_counter"
PATH_WINDOW_LOG = "sliding_window_log"
LIMIT_PATHS = (PATH_TOKEN_BUCKET, PATH_WINDOW_COUNTER, PATH_WINDOW_LOG)
PATH_ORDER = {name: index for index, name in enumerate(LIMIT_PATHS)}


@dataclass(frozen=True)
class Route:
    name: str
    prefix: str
    auth_policy: str
    rate_per_sec: int
    burst: int
    window_ms: int
    max_in_window: int
    source_line: int = 0


@dataclass(frozen=True)
class TokenRecord:
    token: str
    client_id: str
    expires_at_ms: int
    source_line: int = 0


@dataclass(frozen=True)
class SignatureRecord:
    key_id: str
    client_id: str
    secret: str
    expires_at_ms: int
    source_line: int = 0


@dataclass(frozen=True)
class Request:
    seq: int
    route: str
    client_id: str
    token: str
    ts_ms: int
    signed_at_ms: int | None = None
    signature: str | None = None
    source_line: int = 0


@dataclass
class TokenTable:
    version: int
    tokens: dict[str, TokenRecord] = field(default_factory=dict)
    signatures: dict[str, SignatureRecord] = field(default_factory=dict)
