"""Authentication stage with a versioned, wholesale-invalidated result cache."""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from typing import Any

from .errors import E_AUTH
from .models import Request, Route, TOKEN_POLICY, TokenTable


@dataclass(frozen=True)
class AuthResult:
    ok: bool
    code: str
    reason: str
    cache_hit: bool = False


def _cache_key(request: Request) -> tuple[str, str, str, str, str]:
    # Every credential-bearing field participates in the key so a tampered
    # signature or stale signed_at value can never hit a cached good result.
    return (
        request.route,
        request.client_id,
        request.token,
        request.signature or "",
        str(request.signed_at_ms if request.signed_at_ms is not None else ""),
    )


class Authenticator:
    """Versioned cache; a version mismatch invalidates the whole cache."""

    def __init__(self, token_table: TokenTable, routes_version: int) -> None:
        self.token_table = token_table
        self.policy_version = (routes_version, token_table.version)
        # version stored per entry; every entry shares the active policy version.
        self._cache: dict[tuple, tuple[tuple[int, int], AuthResult]] = {}
        self.evictions = 0

    def effective_policy_version(self) -> tuple[int, int]:
        return self.policy_version

    def _verify_token_policy(self, request: Request) -> AuthResult:
        record = self.token_table.tokens.get(request.token)
        if record is None:
            return AuthResult(False, E_AUTH, "token_not_found")
        if record.client_id != request.client_id:
            return AuthResult(False, E_AUTH, "client_binding_mismatch")
        if request.ts_ms > record.expires_at_ms:
            return AuthResult(False, E_AUTH, "token_expired")
        return AuthResult(True, "E_OK", "token_valid")

    def _verify_signature_policy(self, request: Request) -> AuthResult:
        record = self.token_table.signatures.get(request.token)
        if record is None:
            return AuthResult(False, E_AUTH, "key_id_not_found")
        if record.client_id != request.client_id:
            return AuthResult(False, E_AUTH, "client_binding_mismatch")
        if request.ts_ms > record.expires_at_ms:
            return AuthResult(False, E_AUTH, "signing_key_expired")
        if request.signed_at_ms is None or request.signature is None:
            return AuthResult(False, E_AUTH, "missing_signature_fields")
        if request.signed_at_ms > request.ts_ms:
            return AuthResult(False, E_AUTH, "signed_at_in_future")
        if request.ts_ms - request.signed_at_ms > 300_000:
            return AuthResult(False, E_AUTH, "signature_skew_too_large")
        expected = self.sign(record, request)
        if not hmac.compare_digest(expected, request.signature):
            return AuthResult(False, E_AUTH, "signature_mismatch")
        return AuthResult(True, "E_OK", "signature_valid")

    @staticmethod
    def sign(record, request: Request) -> str:
        payload = f"{record.key_id}|{request.client_id}|{request.route}|{request.signed_at_ms}|{request.ts_ms}"
        return hmac.new(
            record.secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256
        ).hexdigest()

    def verify_direct(self, route: Route, request: Request) -> AuthResult:
        """No-cache reference path used by the replay/consistency tests."""
        if route.auth_policy == TOKEN_POLICY:
            return self._verify_token_policy(request)
        return self._verify_signature_policy(request)

    def verify(self, route: Route, request: Request) -> AuthResult:
        key = _cache_key(request)
        cached = self._cache.get(key)
        if cached is not None:
            cached_version, cached_result = cached
            if cached_version != self.policy_version:
                # Stale entries are never patched individually: drop the entry.
                del self._cache[key]
                self.evictions += 1
            else:
                return AuthResult(cached_result.ok, cached_result.code, cached_result.reason, True)
        result = self.verify_direct(route, request)
        if result.ok:
            self._cache[key] = (self.policy_version, result)
        return result

    def replace_policy(self, token_table: TokenTable, routes_version: int) -> int:
        """Wholesale replacement: return the number of cache entries cleared."""
        cleared = len(self._cache)
        self._cache = {}
        self.token_table = token_table
        self.policy_version = (routes_version, token_table.version)
        self.evictions += cleared
        return cleared

    def cache_size(self) -> int:
        return len(self._cache)

    def cache_snapshot(self) -> dict[str, Any]:
        return {
            "policy_version": list(self.policy_version),
            "size": len(self._cache),
            "keys": sorted("|".join(part for part in key if part) for key in self._cache),
        }

