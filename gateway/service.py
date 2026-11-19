from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from .auth import authenticate, authorize_route, candidate_tenant
from .config import GatewayConfig, Route, parse_config
from .errors import GatewayError
from .limiter import evaluate_limits
from .storage import Store
from .upstream import forward_request

_UINT_RE = re.compile(r"^(0|[1-9][0-9]*)$")
_DIGEST_RE = re.compile(r"^[0-9a-fA-F]{64}$")


@dataclass
class GatewayResponse:
    status: int
    headers: list[tuple[str, str]]
    body: bytes
    code: str | None = None
    decision_chain: list[dict[str, Any]] = field(default_factory=list)
    config_version: int | None = None
    config_digest: str | None = None
    effective_time_ms: int | None = None


def normalize_host(value: str) -> str:
    host = value.strip().lower()
    if host.startswith("["):
        end = host.find("]")
        if end >= 0:
            return host[1:end]
    if host.count(":") == 1:
        return host.rsplit(":", 1)[0]
    return host.rstrip(".")


def request_fingerprint_body(
    *,
    host: str,
    method: str,
    path: str,
    query_string: str,
    body: bytes,
    request_id: str,
    request_seq: int,
    logical_time_ms: int,
    tenant_hint: str,
    expected_config_version: int | None,
) -> dict[str, Any]:
    return {
        "body_sha256": hashlib.sha256(body).hexdigest(),
        "expected_config_version": expected_config_version,
        "host": host,
        "logical_time_ms": logical_time_ms,
        "method": method.upper(),
        "path": path,
        "query_string": query_string,
        "request_id": request_id,
        "request_seq": request_seq,
        "tenant_hint": tenant_hint,
    }


def compute_request_digest(
    *,
    host: str,
    method: str,
    path: str,
    query_string: str,
    body: bytes,
    request_id: str,
    request_seq: int,
    logical_time_ms: int,
    tenant_hint: str = "",
    expected_config_version: int | None = None,
) -> str:
    payload = request_fingerprint_body(
        host=normalize_host(host),
        method=method,
        path=path,
        query_string=query_string,
        body=body,
        request_id=request_id,
        request_seq=request_seq,
        logical_time_ms=logical_time_ms,
        tenant_hint=tenant_hint,
        expected_config_version=expected_config_version,
    )
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _header(headers: Mapping[str, str], name: str) -> str | None:
    lowered = name.lower()
    for key, value in headers.items():
        if key.lower() == lowered:
            return value
    return None


def _canonical_headers(headers: Mapping[str, str]) -> dict[str, str]:
    canonical: dict[str, str] = {}
    for key, value in headers.items():
        canonical[key] = value
        canonical.setdefault(key.lower(), value)
    for known in (
        "Authorization",
        "X-API-Key",
        "X-Tenant-Id",
        "X-Request-Id",
        "X-Request-Seq",
        "X-Logical-Time-Ms",
        "X-Request-Digest",
        "X-Config-Version",
    ):
        lowered = known.lower()
        if lowered in canonical:
            canonical[known] = canonical[lowered]
    return canonical


def _json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _error_stage(error: GatewayError) -> dict[str, Any]:
    return {
        "stage": error.stage,
        "decision": "deny",
        "code": error.code,
        "message": error.message,
        "details": error.details,
    }


def _parse_uint_header(raw: str | None, name: str, code: str) -> int:
    if raw is None or not _UINT_RE.fullmatch(raw):
        raise GatewayError(
            code,
            f"{name} must be a non-negative decimal integer",
            400,
            "request",
            {"header": name},
        )
    return int(raw)


class GatewayService:
    def __init__(
        self,
        database_path: str | os.PathLike[str],
        *,
        lock_dir: str | os.PathLike[str] | None = None,
        initial_config_path: str | os.PathLike[str] | None = None,
        install_initial_config: bool = True,
    ):
        self.store = Store(database_path, lock_dir)
        self.store.initialize()
        self.recovery_report = self.store.recover_pending()
        if (
            initial_config_path is not None
            and install_initial_config
            and self.store.current_version() is None
        ):
            raw = json.loads(Path(initial_config_path).read_text(encoding="utf-8"))
            self.update_config(
                raw,
                expected_current_version=0,
                provided_digest=None,
                logical_time_ms=0,
            )

    def _load_config(self) -> tuple[GatewayConfig, int, str, dict[tuple[str, str], int]]:
        loaded = self.store.load_active_snapshot()
        if loaded is None:
            raise GatewayError(
                "E_NO_CONFIG",
                "no active gateway configuration is installed",
                503,
                "config",
            )
        version, digest, raw, revocations = loaded
        try:
            config = parse_config(raw)
        except GatewayError as exc:
            raise GatewayError(
                "E_CONFIG_CORRUPT",
                "persisted configuration failed validation",
                500,
                "config",
                {"persisted_error": exc.as_dict(), "version": version},
            ) from exc
        if config.version != version or config.digest != digest:
            raise GatewayError(
                "E_CONFIG_CORRUPT",
                "persisted configuration metadata does not match its content",
                500,
                "config",
                {
                    "row_version": version,
                    "parsed_version": config.version,
                    "row_digest": digest,
                    "parsed_digest": config.digest,
                },
            )
        return config, version, digest, revocations

    def _error_response(
        self,
        error: GatewayError,
        *,
        request_id: str | None,
        config: GatewayConfig | None,
        effective_time_ms: int | None,
        chain: list[dict[str, Any]],
    ) -> GatewayResponse:
        decision_chain = list(chain)
        decision_chain.append(_error_stage(error))
        payload = {
            "code": error.code,
            "message": error.message,
            "stage": error.stage,
            "details": error.details,
            "request_id": request_id,
            "config_version": None if config is None else config.version,
            "logical_time_ms": effective_time_ms,
            "decision_chain": decision_chain,
        }
        headers = [("Content-Type", "application/json")]
        return GatewayResponse(
            status=error.http_status,
            headers=headers,
            body=_json_bytes(payload),
            code=error.code,
            decision_chain=decision_chain,
            config_version=None if config is None else config.version,
            config_digest=None if config is None else config.digest,
            effective_time_ms=effective_time_ms,
        )

    def handle_request(
        self,
        *,
        method: str,
        host: str,
        path: str,
        query_string: str,
        headers: Mapping[str, str],
        body: bytes,
    ) -> GatewayResponse:
        canonical_headers = _canonical_headers(headers)
        chain: list[dict[str, Any]] = []
        request_id: str | None = None
        config: GatewayConfig | None = None
        effective_time_ms: int | None = None
        try:
            request_id = _header(canonical_headers, "X-Request-Id")
            if not request_id or len(request_id) > 200 or any(ord(ch) < 32 for ch in request_id):
                raise GatewayError(
                    "E_REQUEST_ID_REQUIRED",
                    "X-Request-Id must be a non-empty printable string of at most 200 characters",
                    400,
                    "request",
                )
            request_seq = _parse_uint_header(
                _header(canonical_headers, "X-Request-Seq"),
                "X-Request-Seq",
                "E_REQUEST_SEQ_REQUIRED",
            )
            requested_time = _parse_uint_header(
                _header(canonical_headers, "X-Logical-Time-Ms"),
                "X-Logical-Time-Ms",
                "E_CLOCK_REQUIRED",
            )
            tenant_hint = _header(canonical_headers, "X-Tenant-Id") or ""
            expected_version_raw = _header(canonical_headers, "X-Config-Version")
            expected_config_version: int | None = None
            if expected_version_raw is not None:
                if not _UINT_RE.fullmatch(expected_version_raw):
                    raise GatewayError(
                        "E_CONFIG_VERSION_MISMATCH",
                        "X-Config-Version must be a non-negative integer",
                        409,
                        "config",
                        {"header": "X-Config-Version", "value": expected_version_raw},
                    )
                expected_config_version = int(expected_version_raw)
            normalized_host = normalize_host(host)
            expected_digest = _header(canonical_headers, "X-Request-Digest")
            if not expected_digest:
                raise GatewayError(
                    "E_REQUEST_DIGEST_REQUIRED",
                    "X-Request-Digest is required",
                    400,
                    "request",
                )
            if expected_digest.lower().startswith("sha256:"):
                expected_digest = expected_digest[7:]
            if not _DIGEST_RE.fullmatch(expected_digest):
                raise GatewayError(
                    "E_REQUEST_DIGEST_MISMATCH",
                    "X-Request-Digest must be a SHA-256 hex digest",
                    409,
                    "request",
                )
            actual_digest = compute_request_digest(
                host=normalized_host,
                method=method,
                path=path,
                query_string=query_string,
                body=body,
                request_id=request_id,
                request_seq=request_seq,
                logical_time_ms=requested_time,
                tenant_hint=tenant_hint,
                expected_config_version=expected_config_version,
            )
            if not hmac.compare_digest(actual_digest, expected_digest.lower()):
                raise GatewayError(
                    "E_REQUEST_DIGEST_MISMATCH",
                    "request metadata or body does not match X-Request-Digest",
                    409,
                    "request",
                    {"expected": expected_digest.lower(), "actual": actual_digest},
                )
            chain.append(
                {
                    "stage": "request",
                    "decision": "allow",
                    "request_id": request_id,
                    "request_seq": request_seq,
                    "request_digest": actual_digest,
                    "host": normalized_host,
                    "method": method.upper(),
                    "path": path,
                    "logical_time_ms": requested_time,
                }
            )

            config, _, _, revocations = self._load_config()
            chain.append(
                {
                    "stage": "config",
                    "decision": "allow",
                    "config_version": config.version,
                    "config_digest": config.digest,
                    "snapshot": "bound_at_request_start",
                }
            )
            if (
                expected_config_version is not None
                and expected_config_version != config.version
            ):
                raise GatewayError(
                    "E_CONFIG_VERSION_MISMATCH",
                    "active config version does not match the request expectation",
                    409,
                    "config",
                    {
                        "expected": expected_config_version,
                        "active": config.version,
                    },
                )
            route = config.match_route(normalized_host, path)
            if route is None:
                raise GatewayError(
                    "E_ROUTE_NOT_FOUND",
                    "no route matches the request host and path",
                    404,
                    "route",
                    {"host": normalized_host, "path": path},
                )
            chain.append(
                {
                    "stage": "route",
                    "decision": "allow",
                    "route_id": route.id,
                    "host": route.host,
                    "path_prefix": route.path_prefix,
                    "upstream": route.upstream,
                    "request_budget_ms": route.request_budget_ms,
                }
            )
            candidate = candidate_tenant(canonical_headers, config)
            if not candidate:
                candidate = tenant_hint or "_unauthenticated"
            with self.store.tenant_lock(candidate):
                return self._handle_locked(
                    method=method,
                    path=path,
                    query_string=query_string,
                    headers=canonical_headers,
                    body=body,
                    request_id=request_id,
                    request_seq=request_seq,
                    requested_time=requested_time,
                    tenant_hint=tenant_hint,
                    config=config,
                    route=route,
                    revocations=revocations,
                    chain=chain,
                )
        except GatewayError as error:
            return self._error_response(
                error,
                request_id=request_id,
                config=config,
                effective_time_ms=effective_time_ms,
                chain=chain,
            )
        except Exception as error:  # keep the gateway loop alive and observable
            internal = GatewayError(
                "E_INTERNAL",
                "unexpected gateway failure",
                500,
                "internal",
                {"error_type": type(error).__name__},
            )
            return self._error_response(
                internal,
                request_id=request_id,
                config=config,
                effective_time_ms=effective_time_ms,
                chain=chain,
            )

    def _handle_locked(
        self,
        *,
        method: str,
        path: str,
        query_string: str,
        headers: Mapping[str, str],
        body: bytes,
        request_id: str,
        request_seq: int,
        requested_time: int,
        tenant_hint: str,
        config: GatewayConfig,
        route: Route,
        revocations: dict[tuple[str, str], int],
        chain: list[dict[str, Any]],
    ) -> GatewayResponse:
        request_digest = None
        for item in chain:
            if item.get("stage") == "request":
                request_digest = item["request_digest"]
                break
        assert request_digest is not None

        pending: dict[str, Any] | None = None
        try:
            with self.store.transaction() as conn:
                effective_time, adjustment = self.store.advance_logical_clock(
                    conn, requested_time, config.limits.clock_tolerance_ms
                )
                chain.append(
                    {
                        "stage": "clock",
                        "decision": "allow",
                        "requested_ms": requested_time,
                        "effective_ms": effective_time,
                        "within_tolerance_adjustment_ms": adjustment,
                        "tolerance_ms": config.limits.clock_tolerance_ms,
                    }
                )
                try:
                    auth_context = authenticate(
                        headers, config, revocations, effective_time
                    )
                except GatewayError as auth_error:
                    error_chain = chain + [_error_stage(auth_error)]
                    self.store.insert_audit(
                        conn,
                        request_id=request_id,
                        tenant=tenant_hint or None,
                        config_version=config.version,
                        logical_time_ms=effective_time,
                        code=auth_error.code,
                        stage=auth_error.stage,
                        decision_chain=error_chain,
                        details=auth_error.details,
                    )
                    return self._error_response(
                        auth_error,
                        request_id=request_id,
                        config=config,
                        effective_time_ms=effective_time,
                        chain=chain,
                    )
                chain.append(
                    {
                        "stage": "auth",
                        "decision": "allow",
                        "auth_type": auth_context.auth_type,
                        "credential_id": auth_context.credential_id,
                        "subject": auth_context.subject,
                        "tenant": auth_context.tenant,
                        "key_version": auth_context.key_version,
                        "logical_time_ms": effective_time,
                    }
                )
                if tenant_hint and tenant_hint != auth_context.tenant:
                    error = GatewayError(
                        "E_AUTH_FORBIDDEN",
                        "X-Tenant-Id does not match the authenticated credential tenant",
                        403,
                        "auth",
                        {"claimed_tenant": tenant_hint, "credential_tenant": auth_context.tenant},
                    )
                    self.store.insert_audit(
                        conn,
                        request_id=request_id,
                        tenant=auth_context.tenant,
                        config_version=config.version,
                        logical_time_ms=effective_time,
                        code=error.code,
                        stage=error.stage,
                        decision_chain=chain + [_error_stage(error)],
                        details=error.details,
                    )
                    return self._error_response(
                        error,
                        request_id=request_id,
                        config=config,
                        effective_time_ms=effective_time,
                        chain=chain,
                    )
                try:
                    authorize_route(auth_context, route, config)
                except GatewayError as authz_error:
                    self.store.insert_audit(
                        conn,
                        request_id=request_id,
                        tenant=auth_context.tenant,
                        config_version=config.version,
                        logical_time_ms=effective_time,
                        code=authz_error.code,
                        stage=authz_error.stage,
                        decision_chain=chain + [_error_stage(authz_error)],
                        details=authz_error.details,
                    )
                    return self._error_response(
                        authz_error,
                        request_id=request_id,
                        config=config,
                        effective_time_ms=effective_time,
                        chain=chain,
                    )
                chain.append(
                    {
                        "stage": "authorization",
                        "decision": "allow",
                        "tenant": auth_context.tenant,
                        "route_id": route.id,
                        "domain": route.permission_domain,
                        "granted_domains": list(auth_context.permissions),
                    }
                )

                existing = self.store.find_request(
                    conn, auth_context.tenant, request_id
                )
                if existing is not None:
                    if not hmac.compare_digest(
                        str(existing["request_digest"]), request_digest
                    ):
                        raise GatewayError(
                            "E_REQUEST_DIGEST_MISMATCH",
                            "request id was previously used with a different digest",
                            409,
                            "idempotency",
                            {
                                "request_id": request_id,
                                "persisted_digest": str(existing["request_digest"]),
                                "actual_digest": request_digest,
                            },
                        )
                    if not hmac.compare_digest(
                        str(existing["credential_fingerprint"]),
                        auth_context.fingerprint,
                    ):
                        raise GatewayError(
                            "E_IDEMPOTENCY_CONFLICT",
                            "request id was previously used with different credentials",
                            409,
                            "idempotency",
                            {"request_id": request_id},
                        )
                    if str(existing["state"]) == "pending":
                        settled = self.store.finalize_pending_as_recovered(conn, existing)
                        cached = settled["result"]
                        cached_chain = settled["decision_chain"]
                        existing_version = int(existing["config_version"])
                    else:
                        final_json = existing["final_result"]
                        if final_json is None:
                            final_json = existing["admission_result"]
                        cached = self.store.decode_result(str(final_json))
                        cached_chain = json.loads(existing["decision_chain"])
                        existing_version = int(existing["config_version"])
                    replay_chain = cached_chain + [
                        {
                            "stage": "idempotency",
                            "decision": "replay",
                            "request_id": request_id,
                            "original_config_version": existing_version,
                        }
                    ]
                    return GatewayResponse(
                        status=int(cached["http_status"]),
                        headers=list(cached["headers"]),
                        body=cached["body"],
                        code=cached.get("code"),
                        decision_chain=replay_chain,
                        config_version=existing_version,
                        config_digest=str(existing["config_digest"]),
                        effective_time_ms=int(existing["logical_time_ms"]),
                    )

                sequence_row = self.store.find_sequence(
                    conn, auth_context.tenant, request_seq
                )
                if sequence_row is not None:
                    raise GatewayError(
                        "E_REQUEST_SEQUENCE_CONFLICT",
                        "request sequence was already used for another request",
                        409,
                        "idempotency",
                        {
                            "request_seq": request_seq,
                            "existing_request_id": str(sequence_row["request_id"]),
                        },
                    )

                limit = evaluate_limits(
                    conn, config, route, auth_context.tenant, effective_time
                )
                chain.append(
                    {
                        "stage": "limit",
                        "decision": "allow" if limit["allowed"] else "deny",
                        "code": limit["code"],
                        "reason": limit["reason"],
                        "route_bucket": limit["route_bucket"],
                        "tenant_bucket": limit["tenant_bucket"],
                        "window": limit["window"],
                    }
                )
                if not limit["allowed"]:
                    error = GatewayError(
                        str(limit["code"]),
                        str(limit["reason"]),
                        429,
                        "limit",
                        {
                            "route_bucket": limit["route_bucket"],
                            "tenant_bucket": limit["tenant_bucket"],
                            "window": limit["window"],
                        },
                    )
                    response = self._error_response(
                        error,
                        request_id=request_id,
                        config=config,
                        effective_time_ms=effective_time,
                        chain=chain,
                    )
                    self.store.commit_direct_request(
                        conn,
                        tenant=auth_context.tenant,
                        request_seq=request_seq,
                        request_id=request_id,
                        config=config,
                        request_digest=request_digest,
                        credential_fingerprint=auth_context.fingerprint,
                        logical_time_ms=effective_time,
                        method=method,
                        path=path,
                        query_string=query_string,
                        result={
                            "http_status": response.status,
                            "headers": response.headers,
                            "body": response.body,
                            "code": response.code,
                        },
                        decision_chain=response.decision_chain,
                    )
                    return response

                admission_placeholder = {
                    "http_status": 200,
                    "headers": [],
                    "body": b"",
                    "code": None,
                }
                self.store.insert_pending_request(
                    conn,
                    tenant=auth_context.tenant,
                    request_seq=request_seq,
                    request_id=request_id,
                    config=config,
                    request_digest=request_digest,
                    credential_fingerprint=auth_context.fingerprint,
                    logical_time_ms=effective_time,
                    method=method,
                    path=path,
                    query_string=query_string,
                    admission_result=admission_placeholder,
                    decision_chain=chain,
                )
                pending = {
                    "tenant": auth_context.tenant,
                    "request_seq": request_seq,
                    "request_id": request_id,
                    "effective_time": effective_time,
                    "chain": list(chain),
                }
        except GatewayError as error:
            return self._error_response(
                error,
                request_id=request_id,
                config=config,
                effective_time_ms=None,
                chain=chain,
            )

        assert pending is not None
        if os.environ.get("GATEWAY_FAULT_AFTER_RESERVATION") == "1":
            os._exit(86)
        return self._forward_and_finalize(
            method=method,
            path=path,
            query_string=query_string,
            headers=headers,
            body=body,
            config=config,
            route=route,
            pending=pending,
        )

    def _forward_and_finalize(
        self,
        *,
        method: str,
        path: str,
        query_string: str,
        headers: Mapping[str, str],
        body: bytes,
        config: GatewayConfig,
        route: Route,
        pending: dict[str, Any],
    ) -> GatewayResponse:
        chain = list(pending["chain"])
        try:
            upstream = forward_request(
                upstream=route.upstream,
                method=method,
                path=path,
                query_string=query_string,
                headers=headers,
                body=body,
                budget_ms=route.request_budget_ms,
                request_id=pending["request_id"],
                tenant=pending["tenant"],
                config_version=config.version,
                logical_time_ms=pending["effective_time"],
            )
            chain.append(
                {
                    "stage": "upstream",
                    "decision": "forward",
                    "route_id": route.id,
                    "upstream": route.upstream,
                    "status": upstream.status,
                    "response_bytes": len(upstream.body),
                }
            )
            result = {
                "http_status": upstream.status,
                "headers": list(upstream.headers),
                "body": upstream.body,
                "code": None,
            }
            response = GatewayResponse(
                status=upstream.status,
                headers=list(upstream.headers),
                body=upstream.body,
                code=None,
                decision_chain=chain,
                config_version=config.version,
                config_digest=config.digest,
                effective_time_ms=pending["effective_time"],
            )
        except GatewayError as error:
            error.details = dict(error.details)
            error.details.setdefault("route_id", route.id)
            error.details.setdefault("upstream", route.upstream)
            response = self._error_response(
                error,
                request_id=pending["request_id"],
                config=config,
                effective_time_ms=pending["effective_time"],
                chain=chain,
            )
            result = {
                "http_status": response.status,
                "headers": response.headers,
                "body": response.body,
                "code": response.code,
            }
        except Exception as error:
            internal = GatewayError(
                "E_INTERNAL",
                "unexpected upstream or finalization failure",
                500,
                "upstream",
                {"error_type": type(error).__name__},
            )
            response = self._error_response(
                internal,
                request_id=pending["request_id"],
                config=config,
                effective_time_ms=pending["effective_time"],
                chain=chain,
            )
            result = {
                "http_status": response.status,
                "headers": response.headers,
                "body": response.body,
                "code": response.code,
            }
            chain = response.decision_chain
        with self.store.transaction() as conn:
            self.store.finalize_request(
                conn,
                tenant=pending["tenant"],
                request_seq=pending["request_seq"],
                result=result,
                decision_chain=response.decision_chain,
                logical_time_ms=pending["effective_time"],
            )
        return response

    def update_config(
        self,
        raw: dict[str, Any],
        *,
        expected_current_version: int,
        provided_digest: str | None,
        logical_time_ms: int,
    ) -> dict[str, Any]:
        if (
            not isinstance(expected_current_version, int)
            or isinstance(expected_current_version, bool)
            or expected_current_version < 0
        ):
            raise GatewayError(
                "E_CONFIG_VERSION_MISMATCH",
                "expected_current_version must be a non-negative integer",
                409,
                "config",
            )
        if (
            not isinstance(logical_time_ms, int)
            or isinstance(logical_time_ms, bool)
            or logical_time_ms < 0
        ):
            raise GatewayError(
                "E_BAD_CLOCK",
                "logical_time_ms must be a non-negative integer",
                400,
                "config",
            )
        config = parse_config(raw)
        if provided_digest is not None:
            digest = provided_digest.strip().lower()
            if digest.startswith("sha256:"):
                digest = digest[7:]
            if not _DIGEST_RE.fullmatch(digest):
                raise GatewayError(
                    "E_CONFIG_DIGEST_MISMATCH",
                    "content digest must be a SHA-256 hex digest",
                    409,
                    "config",
                )
            if not hmac.compare_digest(digest, config.digest):
                raise GatewayError(
                    "E_CONFIG_DIGEST_MISMATCH",
                    "submitted content digest does not match the canonical config",
                    409,
                    "config",
                    {"submitted": digest, "actual": config.digest},
                )
        return self.store.activate_config(
            config,
            expected_current_version=expected_current_version,
            logical_time_ms=logical_time_ms,
        )

    def state_snapshot(self, tenant: str | None = None) -> dict[str, Any]:
        snapshot = self.store.state_snapshot(tenant)
        snapshot["startup_recovery"] = self.recovery_report
        return snapshot
