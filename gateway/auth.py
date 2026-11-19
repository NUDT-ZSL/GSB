from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
from dataclasses import dataclass
from typing import Mapping, Any

from .config import ApiKeyVersion, GatewayConfig, JwtKeyVersion, RotationPolicy, Route
from .errors import GatewayError


@dataclass(frozen=True)
class AuthContext:
    auth_type: str
    credential_id: str
    tenant: str
    key_version: int
    fingerprint: str
    subject: str
    permissions: tuple[str, ...]


def _unauthorized(code: str, message: str, **details: Any) -> GatewayError:
    return GatewayError(code, message, 401, "auth", details)


def _forbidden(code: str, message: str, **details: Any) -> GatewayError:
    return GatewayError(code, message, 403, "auth", details)


def _fingerprint(kind: str, credential: str) -> str:
    return hashlib.sha256(f"{kind}\0{credential}".encode("utf-8")).hexdigest()


def _check_rotation(
    kind: str,
    key_version: int,
    rotation: RotationPolicy,
    logical_time_ms: int,
) -> None:
    if key_version > rotation.active_version:
        raise _unauthorized(
            "E_AUTH_KEY_RETIRED",
            "credential key version is newer than the active rotation version",
            auth_type=kind,
            key_version=key_version,
            active_version=rotation.active_version,
        )
    if key_version == rotation.active_version:
        if logical_time_ms < rotation.effective_at_ms:
            raise _unauthorized(
                "E_AUTH_KEY_RETIRED",
                "active credential key version is not effective yet",
                auth_type=kind,
                key_version=key_version,
                effective_at_ms=rotation.effective_at_ms,
            )
        return
    if logical_time_ms >= rotation.effective_at_ms + rotation.grace_ms:
        raise _unauthorized(
            "E_AUTH_KEY_RETIRED",
            "old credential key version is outside its rotation grace window",
            auth_type=kind,
            key_version=key_version,
            active_version=rotation.active_version,
            grace_ends_ms=rotation.effective_at_ms + rotation.grace_ms,
        )


def _check_key_time(
    kind: str,
    key_id: str,
    not_before_ms: int,
    expires_at_ms: int,
    logical_time_ms: int,
) -> None:
    if logical_time_ms < not_before_ms:
        raise _unauthorized(
            "E_AUTH_NOT_YET_VALID",
            "credential key is not valid yet",
            auth_type=kind,
            key_id=key_id,
            not_before_ms=not_before_ms,
            logical_time_ms=logical_time_ms,
        )
    if logical_time_ms >= expires_at_ms:
        raise _unauthorized(
            "E_AUTH_EXPIRED",
            "credential key has expired",
            auth_type=kind,
            key_id=key_id,
            expires_at_ms=expires_at_ms,
            logical_time_ms=logical_time_ms,
        )


def _check_revocation(
    revocations: Mapping[tuple[str, str], int],
    kind: str,
    credential_id: str,
    logical_time_ms: int,
) -> None:
    revoked_at = revocations.get((kind, credential_id))
    if revoked_at is not None and logical_time_ms >= revoked_at:
        raise _forbidden(
            "E_AUTH_REVOKED",
            "credential is revoked",
            auth_type=kind,
            credential_id=credential_id,
            revoked_at_ms=revoked_at,
        )


def _parse_api_key(raw: str) -> tuple[str, str]:
    if "." not in raw:
        raise _unauthorized(
            "E_AUTH_INVALID",
            "API key must use key_id.signature format",
            auth_type="api_key",
        )
    key_id, signature = raw.rsplit(".", 1)
    if not key_id or not signature:
        raise _unauthorized(
            "E_AUTH_INVALID",
            "API key id and signature must be non-empty",
            auth_type="api_key",
        )
    return key_id, signature


def api_key_signature(secret: str, key_id: str) -> str:
    return hmac.new(
        secret.encode("utf-8"),
        f"api-key:{key_id}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _authenticate_api_key(
    raw: str,
    config: GatewayConfig,
    revocations: Mapping[tuple[str, str], int],
    logical_time_ms: int,
) -> AuthContext:
    key_id, signature = _parse_api_key(raw)
    configured: ApiKeyVersion | None = config.auth.api_keys.get(key_id)
    if configured is None:
        raise _unauthorized(
            "E_AUTH_INVALID",
            "API key id is invalid",
            auth_type="api_key",
            key_id=key_id,
        )
    expected_signature = api_key_signature(configured.secret, key_id)
    if not hmac.compare_digest(expected_signature, signature.lower()):
        raise _unauthorized(
            "E_AUTH_INVALID",
            "API key signature digest is invalid",
            auth_type="api_key",
            key_id=key_id,
        )
    _check_revocation(revocations, "api_key", key_id, logical_time_ms)
    _check_key_time(
        "api_key",
        key_id,
        configured.not_before_ms,
        configured.expires_at_ms,
        logical_time_ms,
    )
    _check_rotation(
        "api_key", configured.version, config.auth.api_key_rotation, logical_time_ms
    )
    permissions = config.auth.tenant_grants.get(configured.tenant, ())
    return AuthContext(
        auth_type="api_key",
        credential_id=key_id,
        tenant=configured.tenant,
        key_version=configured.version,
        fingerprint=_fingerprint("api_key", raw),
        subject=key_id,
        permissions=permissions,
    )


def _b64url_decode(segment: str) -> bytes:
    if not segment or not isinstance(segment, str):
        raise ValueError("empty base64url segment")
    padding = "=" * (-len(segment) % 4)
    try:
        return base64.b64decode(
            (segment + padding).encode("ascii"),
            altchars=b"-_",
            validate=True,
        )
    except (binascii.Error, UnicodeEncodeError) as exc:
        raise ValueError("invalid base64url segment") from exc


def _json_object(raw: bytes) -> dict[str, Any]:
    value = json.loads(raw.decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError("JWT segment must be a JSON object")
    return value


def _authenticate_jwt(
    token: str,
    config: GatewayConfig,
    revocations: Mapping[tuple[str, str], int],
    logical_time_ms: int,
) -> AuthContext:
    parts = token.split(".")
    if len(parts) != 3:
        raise _unauthorized("E_AUTH_INVALID", "JWT must have three segments", auth_type="jwt")
    try:
        header = _json_object(_b64url_decode(parts[0]))
        payload = _json_object(_b64url_decode(parts[1]))
        signature = _b64url_decode(parts[2])
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
        raise _unauthorized(
            "E_AUTH_INVALID", "JWT encoding or JSON payload is invalid", auth_type="jwt"
        ) from None
    if header.get("alg") != "HS256":
        raise _unauthorized(
            "E_AUTH_INVALID",
            "only HS256 JWTs are accepted",
            auth_type="jwt",
            alg=header.get("alg"),
        )
    kid = header.get("kid")
    if not isinstance(kid, str) or not kid:
        raise _unauthorized("E_AUTH_INVALID", "JWT kid is required", auth_type="jwt")
    configured: JwtKeyVersion | None = config.auth.jwt_keys.get(kid)
    if configured is None:
        raise _unauthorized(
            "E_AUTH_INVALID",
            "JWT kid is unknown",
            auth_type="jwt",
            kid=kid,
        )
    expected = hmac.new(
        configured.secret.encode("utf-8"),
        f"{parts[0]}.{parts[1]}".encode("ascii"),
        hashlib.sha256,
    ).digest()
    if not hmac.compare_digest(expected, signature):
        raise _unauthorized(
            "E_AUTH_INVALID",
            "JWT signature is invalid",
            auth_type="jwt",
            kid=kid,
        )

    tenant = payload.get("tenant")
    subject = payload.get("sub")
    jti = payload.get("jti")
    exp_ms = payload.get("exp_ms")
    iat_ms = payload.get("iat_ms", 0)
    nbf_ms = payload.get("nbf_ms", 0)
    if (
        not isinstance(tenant, str)
        or not tenant
        or not isinstance(subject, str)
        or not subject
        or not isinstance(jti, str)
        or not jti
        or not isinstance(exp_ms, int)
        or isinstance(exp_ms, bool)
        or not isinstance(iat_ms, int)
        or isinstance(iat_ms, bool)
        or not isinstance(nbf_ms, int)
        or isinstance(nbf_ms, bool)
    ):
        raise _unauthorized(
            "E_AUTH_INVALID",
            "JWT requires tenant, sub, jti and integer logical-time claims",
            auth_type="jwt",
            kid=kid,
        )
    if logical_time_ms < nbf_ms or logical_time_ms < iat_ms:
        raise _unauthorized(
            "E_AUTH_NOT_YET_VALID",
            "JWT is not valid yet",
            auth_type="jwt",
            kid=kid,
            nbf_ms=nbf_ms,
            iat_ms=iat_ms,
            logical_time_ms=logical_time_ms,
        )
    if logical_time_ms >= exp_ms:
        raise _unauthorized(
            "E_AUTH_EXPIRED",
            "JWT has expired",
            auth_type="jwt",
            kid=kid,
            exp_ms=exp_ms,
            logical_time_ms=logical_time_ms,
        )
    _check_revocation(revocations, "jwt", jti, logical_time_ms)
    _check_key_time(
        "jwt",
        kid,
        configured.not_before_ms,
        configured.expires_at_ms,
        logical_time_ms,
    )
    _check_rotation("jwt", configured.version, config.auth.jwt_rotation, logical_time_ms)
    permissions = config.auth.tenant_grants.get(tenant, ())
    return AuthContext(
        auth_type="jwt",
        credential_id=jti,
        tenant=tenant,
        key_version=configured.version,
        fingerprint=_fingerprint("jwt", token),
        subject=subject,
        permissions=permissions,
    )


def authenticate(
    headers: Mapping[str, str],
    config: GatewayConfig,
    revocations: Mapping[tuple[str, str], int],
    logical_time_ms: int,
) -> AuthContext:
    authorization = headers.get("Authorization", "")
    api_key_header = headers.get("X-API-Key", "")
    credential_kind: str | None = None
    credential_value: str | None = None
    authorization_used = False
    if authorization:
        if authorization.startswith("Bearer "):
            credential_kind = "jwt"
            credential_value = authorization[len("Bearer ") :]
        elif authorization.startswith("ApiKey "):
            credential_kind = "api_key"
            credential_value = authorization[len("ApiKey ") :]
        else:
            raise _unauthorized(
                "E_AUTH_INVALID",
                "Authorization must use Bearer or ApiKey scheme",
                auth_type="authorization",
            )
        authorization_used = True
    if api_key_header:
        if authorization_used:
            raise _unauthorized(
                "E_AUTH_INVALID",
                "multiple authentication credentials were supplied",
                auth_type="multiple",
            )
        credential_kind = "api_key"
        credential_value = api_key_header
    if credential_kind is None or not credential_value:
        raise _unauthorized(
            "E_AUTH_REQUIRED",
            "an API key or JWT bearer credential is required",
            auth_type="none",
        )
    if credential_kind == "api_key":
        return _authenticate_api_key(
            credential_value, config, revocations, logical_time_ms
        )
    return _authenticate_jwt(credential_value, config, revocations, logical_time_ms)


def authorize_route(context: AuthContext, route: Route, config: GatewayConfig) -> None:
    if route.permission_domain not in context.permissions:
        raise _forbidden(
            "E_AUTH_FORBIDDEN",
            "tenant is not granted the route's permission domain",
            tenant=context.tenant,
            route_id=route.id,
            domain=route.permission_domain,
        )
    route_ids = config.auth.permission_matrix.get(route.permission_domain, ())
    if route.id not in route_ids:
        raise _forbidden(
            "E_AUTH_FORBIDDEN",
            "permission domain does not authorize the route",
            tenant=context.tenant,
            route_id=route.id,
            domain=route.permission_domain,
        )


def candidate_tenant(headers: Mapping[str, str], config: GatewayConfig) -> str | None:
    """Best-effort tenant extraction used only to select the serialization lock."""

    token = headers.get("Authorization", "")
    if token.startswith("Bearer "):
        parts = token[len("Bearer ") :].split(".")
        if len(parts) == 3:
            try:
                payload = _json_object(_b64url_decode(parts[1]))
                tenant = payload.get("tenant")
                if isinstance(tenant, str) and tenant:
                    return tenant
            except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
                pass
    raw_key = headers.get("X-API-Key")
    if raw_key is None and token.startswith("ApiKey "):
        raw_key = token[len("ApiKey ") :]
    if raw_key:
        try:
            key_id, _ = _parse_api_key(raw_key)
        except GatewayError:
            key_id = ""
        configured = config.auth.api_keys.get(key_id)
        if configured is not None:
            return configured.tenant
    hint = headers.get("X-Tenant-Id")
    if hint:
        return hint
    return None
