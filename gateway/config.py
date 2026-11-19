from __future__ import annotations

import hashlib
import ipaddress
import json
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from .errors import GatewayError, config_error

E_BAD_ROUTE = "E_BAD_ROUTE"
E_BAD_LIMIT = "E_BAD_LIMIT"
E_BAD_AUTH_MATRIX = "E_BAD_AUTH_MATRIX"
E_BAD_AUTH_KEY = "E_BAD_AUTH_KEY"
E_BAD_REVOCATION = "E_BAD_REVOCATION"
E_AUTH_VERSION_REGRESSION = "E_AUTH_VERSION_REGRESSION"

_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,96}$")


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def content_digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _require_dict(value: Any, field_name: str, code: str = E_BAD_ROUTE) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise config_error(code, "must be an object", field_name)
    return value


def _require_list(value: Any, field_name: str, code: str = E_BAD_ROUTE) -> list[Any]:
    if not isinstance(value, list):
        raise config_error(code, "must be an array", field_name)
    return value


def _require_str(
    value: Any,
    field_name: str,
    code: str = E_BAD_ROUTE,
    *,
    allow_empty: bool = False,
) -> str:
    if not isinstance(value, str) or (not allow_empty and not value):
        raise config_error(code, "must be a non-empty string", field_name)
    return value


def _require_int(
    value: Any,
    field_name: str,
    code: str = E_BAD_ROUTE,
    *,
    minimum: int = 0,
    maximum: int | None = None,
) -> int:
    if not _is_int(value) or value < minimum or (maximum is not None and value > maximum):
        bounds = f">= {minimum}" if maximum is None else f"between {minimum} and {maximum}"
        raise config_error(code, f"must be an integer {bounds}", field_name)
    return value


def _require_bool(value: Any, field_name: str, code: str = E_BAD_ROUTE) -> bool:
    if not isinstance(value, bool):
        raise config_error(code, "must be a boolean", field_name)
    return value


def _validate_id(value: Any, field_name: str, code: str) -> str:
    result = _require_str(value, field_name, code)
    if not _ID_RE.fullmatch(result):
        raise config_error(
            code,
            "must contain only letters, digits, dot, colon, underscore or hyphen",
            field_name,
        )
    return result


def _normalize_host(value: Any, field_name: str) -> str:
    host = _require_str(value, field_name).strip().lower().rstrip(".")
    if not host or "/" in host or "\\" in host or "?" in host or "#" in host:
        raise config_error(E_BAD_ROUTE, "must be a bare host name", field_name)
    if host.startswith("[") or ":" in host:
        raise config_error(E_BAD_ROUTE, "route host must not include a port", field_name)
    return host


def _validate_loopback_upstream(value: Any, field_name: str) -> str:
    upstream = _require_str(value, field_name)
    try:
        parsed = urlsplit(upstream)
        parsed_port = parsed.port
        parsed_hostname = parsed.hostname
    except ValueError as exc:
        raise config_error(
            E_BAD_ROUTE,
            "upstream URL is invalid",
            field_name,
            reason=str(exc),
        ) from exc
    if parsed.scheme != "http":
        raise config_error(E_BAD_ROUTE, "upstream scheme must be http", field_name)
    if parsed.username or parsed.password:
        raise config_error(E_BAD_ROUTE, "upstream credentials are not allowed", field_name)
    if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
        raise config_error(E_BAD_ROUTE, "upstream must be an origin URL", field_name)
    if parsed_port is None:
        raise config_error(E_BAD_ROUTE, "upstream must include an explicit port", field_name)
    host = (parsed_hostname or "").lower()
    is_loopback = host == "localhost"
    if not is_loopback:
        try:
            is_loopback = ipaddress.ip_address(host).is_loopback
        except ValueError:
            is_loopback = False
    if not is_loopback:
        raise config_error(E_BAD_ROUTE, "upstream host must be a loopback address", field_name)
    if not 1 <= parsed_port <= 65535:
        raise config_error(E_BAD_ROUTE, "upstream port is out of range", field_name)
    rendered_host = f"[{parsed_hostname}]" if ":" in parsed_hostname else parsed_hostname
    return f"http://{rendered_host}:{parsed_port}"


@dataclass(frozen=True)
class Route:
    id: str
    host: str
    path_prefix: str
    upstream: str
    permission_domain: str
    request_budget_ms: int


@dataclass(frozen=True)
class BucketPolicy:
    capacity: int
    refill_per_second: int

    @property
    def capacity_units(self) -> int:
        return self.capacity * 1000

    @property
    def refill_units_per_ms(self) -> int:
        return self.refill_per_second


@dataclass(frozen=True)
class SlidingWindowPolicy:
    length_ms: int
    step_ms: int
    max_requests: int
    max_rejections: int


@dataclass(frozen=True)
class LimitPolicy:
    clock_tolerance_ms: int
    route_buckets: dict[str, BucketPolicy]
    tenant_buckets: dict[str, BucketPolicy]
    window: SlidingWindowPolicy

    def route_bucket(self, route_id: str) -> BucketPolicy:
        return self.route_buckets[route_id]

    def tenant_bucket(self, tenant: str) -> BucketPolicy:
        return self.tenant_buckets.get(tenant, self.tenant_buckets["*"])


@dataclass(frozen=True)
class ApiKeyVersion:
    key_id: str
    secret: str
    version: int
    tenant: str
    not_before_ms: int
    expires_at_ms: int


@dataclass(frozen=True)
class JwtKeyVersion:
    kid: str
    secret: str
    version: int
    not_before_ms: int
    expires_at_ms: int


@dataclass(frozen=True)
class RotationPolicy:
    active_version: int
    effective_at_ms: int
    grace_ms: int


@dataclass(frozen=True)
class AuthPolicy:
    api_keys: dict[str, ApiKeyVersion]
    jwt_keys: dict[str, JwtKeyVersion]
    api_key_rotation: RotationPolicy
    jwt_rotation: RotationPolicy
    permission_matrix: dict[str, tuple[str, ...]]
    tenant_grants: dict[str, tuple[str, ...]]
    revocations: tuple[dict[str, Any], ...]

    @property
    def max_api_key_version(self) -> int:
        return max(key.version for key in self.api_keys.values())

    @property
    def max_jwt_version(self) -> int:
        return max(key.version for key in self.jwt_keys.values())


@dataclass(frozen=True)
class GatewayConfig:
    version: int
    raw: dict[str, Any]
    digest: str
    routes: dict[str, Route]
    limits: LimitPolicy
    auth: AuthPolicy

    def match_route(self, host: str, path: str) -> Route | None:
        normalized_host = host.lower().rstrip(".")
        candidates = [
            route
            for route in self.routes.values()
            if route.host == normalized_host and path.startswith(route.path_prefix)
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda route: (len(route.path_prefix), route.id))


def _parse_bucket(value: Any, field_name: str) -> BucketPolicy:
    item = _require_dict(value, field_name, E_BAD_LIMIT)
    capacity = _require_int(item.get("capacity"), f"{field_name}.capacity", E_BAD_LIMIT, minimum=1)
    refill = _require_int(
        item.get("refill_per_second"),
        f"{field_name}.refill_per_second",
        E_BAD_LIMIT,
        minimum=1,
    )
    return BucketPolicy(capacity=capacity, refill_per_second=refill)


def _parse_routes(raw_routes: Any) -> dict[str, Route]:
    routes: dict[str, Route] = {}
    host_prefixes: set[tuple[str, str]] = set()
    for index, value in enumerate(_require_list(raw_routes, "routes")):
        field_name = f"routes[{index}]"
        item = _require_dict(value, field_name)
        route_id = _validate_id(item.get("id"), f"{field_name}.id", E_BAD_ROUTE)
        if route_id in routes:
            raise config_error(E_BAD_ROUTE, "duplicate route id", f"{field_name}.id", route_id=route_id)
        host = _normalize_host(item.get("host"), f"{field_name}.host")
        prefix = _require_str(item.get("path_prefix"), f"{field_name}.path_prefix")
        if not prefix.startswith("/") or "?" in prefix or "#" in prefix or any(ch.isspace() for ch in prefix):
            raise config_error(
                E_BAD_ROUTE,
                "path_prefix must start with / and contain no query, fragment or whitespace",
                f"{field_name}.path_prefix",
            )
        host_prefix = (host, prefix)
        if host_prefix in host_prefixes:
            raise config_error(
                E_BAD_ROUTE,
                "duplicate host and path_prefix",
                f"{field_name}.path_prefix",
                host=host,
                path_prefix=prefix,
            )
        host_prefixes.add(host_prefix)
        upstream = _validate_loopback_upstream(item.get("upstream"), f"{field_name}.upstream")
        domain = _require_str(
            item.get("permission_domain"), f"{field_name}.permission_domain"
        )
        budget = _require_int(
            item.get("request_budget_ms", 1000),
            f"{field_name}.request_budget_ms",
            E_BAD_ROUTE,
            minimum=1,
            maximum=600000,
        )
        routes[route_id] = Route(
            id=route_id,
            host=host,
            path_prefix=prefix,
            upstream=upstream,
            permission_domain=domain,
            request_budget_ms=budget,
        )
    if not routes:
        raise config_error(E_BAD_ROUTE, "at least one route is required", "routes")
    return routes


def _parse_limits(raw_limits: Any, routes: dict[str, Route]) -> LimitPolicy:
    limits = _require_dict(raw_limits, "limits", E_BAD_LIMIT)
    tolerance = _require_int(
        limits.get("clock_tolerance_ms"),
        "limits.clock_tolerance_ms",
        E_BAD_LIMIT,
        minimum=0,
        maximum=600000,
    )

    raw_route_buckets = _require_dict(limits.get("route_buckets"), "limits.route_buckets", E_BAD_LIMIT)
    route_buckets = {
        str(route_id): _parse_bucket(value, f"limits.route_buckets.{route_id}")
        for route_id, value in raw_route_buckets.items()
    }
    missing_routes = sorted(set(routes) - set(route_buckets))
    unknown_routes = sorted(set(route_buckets) - set(routes))
    if missing_routes or unknown_routes:
        raise config_error(
            E_BAD_LIMIT,
            "route bucket references must close over routes",
            "limits.route_buckets",
            missing=missing_routes,
            unknown=unknown_routes,
        )
    if not route_buckets:
        raise config_error(E_BAD_LIMIT, "at least one route bucket is required", "limits.route_buckets")

    raw_tenant_buckets = _require_dict(
        limits.get("tenant_buckets"), "limits.tenant_buckets", E_BAD_LIMIT
    )
    tenant_buckets: dict[str, BucketPolicy] = {}
    for tenant, value in raw_tenant_buckets.items():
        if not isinstance(tenant, str) or not tenant:
            raise config_error(E_BAD_LIMIT, "tenant key must be a non-empty string", "limits.tenant_buckets")
        tenant_buckets[tenant] = _parse_bucket(value, f"limits.tenant_buckets.{tenant}")
    if "*" not in tenant_buckets:
        raise config_error(
            E_BAD_LIMIT,
            "a wildcard tenant bucket is required for JWT tenants",
            "limits.tenant_buckets",
        )

    window = _require_dict(limits.get("sliding_window"), "limits.sliding_window", E_BAD_LIMIT)
    length_ms = _require_int(
        window.get("length_ms"), "limits.sliding_window.length_ms", E_BAD_LIMIT, minimum=1
    )
    step_ms = _require_int(
        window.get("step_ms"), "limits.sliding_window.step_ms", E_BAD_LIMIT, minimum=1
    )
    if step_ms > length_ms:
        raise config_error(
            E_BAD_LIMIT,
            "sliding window step must not exceed window length",
            "limits.sliding_window.step_ms",
            step_ms=step_ms,
            length_ms=length_ms,
        )
    max_requests = _require_int(
        window.get("max_requests"),
        "limits.sliding_window.max_requests",
        E_BAD_LIMIT,
        minimum=1,
    )
    max_rejections = _require_int(
        window.get("max_rejections"),
        "limits.sliding_window.max_rejections",
        E_BAD_LIMIT,
        minimum=1,
    )
    return LimitPolicy(
        clock_tolerance_ms=tolerance,
        route_buckets=route_buckets,
        tenant_buckets=tenant_buckets,
        window=SlidingWindowPolicy(
            length_ms=length_ms,
            step_ms=step_ms,
            max_requests=max_requests,
            max_rejections=max_rejections,
        ),
    )


def _parse_api_keys(raw_keys: list[Any], tenant_grants: dict[str, tuple[str, ...]]) -> dict[str, ApiKeyVersion]:
    keys: dict[str, ApiKeyVersion] = {}
    versions: list[int] = []
    for index, value in enumerate(raw_keys):
        field_name = f"auth.api_key_versions[{index}]"
        item = _require_dict(value, field_name, E_BAD_AUTH_KEY)
        key_id = _validate_id(item.get("key_id"), f"{field_name}.key_id", E_BAD_AUTH_KEY)
        if key_id in keys:
            raise config_error(E_BAD_AUTH_KEY, "duplicate API key id", f"{field_name}.key_id")
        secret = _require_str(item.get("secret"), f"{field_name}.secret", E_BAD_AUTH_KEY)
        version = _require_int(item.get("version"), f"{field_name}.version", E_BAD_AUTH_KEY, minimum=1)
        tenant = _require_str(item.get("tenant"), f"{field_name}.tenant", E_BAD_AUTH_KEY)
        if tenant not in tenant_grants:
            raise config_error(
                E_BAD_AUTH_MATRIX,
                "API key tenant has no tenant grant entry",
                f"{field_name}.tenant",
                tenant=tenant,
            )
        not_before = _require_int(
            item.get("not_before_ms", 0), f"{field_name}.not_before_ms", E_BAD_AUTH_KEY, minimum=0
        )
        expires = _require_int(
            item.get("expires_at_ms"), f"{field_name}.expires_at_ms", E_BAD_AUTH_KEY, minimum=1
        )
        if expires <= not_before:
            raise config_error(
                E_BAD_AUTH_KEY,
                "expires_at_ms must be greater than not_before_ms",
                f"{field_name}.expires_at_ms",
            )
        keys[key_id] = ApiKeyVersion(
            key_id=key_id,
            secret=secret,
            version=version,
            tenant=tenant,
            not_before_ms=not_before,
            expires_at_ms=expires,
        )
        versions.append(version)
    if not keys:
        raise config_error(E_BAD_AUTH_KEY, "at least one API key version is required", "auth.api_key_versions")
    if versions != sorted(versions):
        raise config_error(
            E_BAD_AUTH_KEY,
            "API key versions must be monotonically non-decreasing",
            "auth.api_key_versions",
        )
    return keys


def _parse_jwt_keys(raw_keys: list[Any]) -> dict[str, JwtKeyVersion]:
    keys: dict[str, JwtKeyVersion] = {}
    versions: list[int] = []
    for index, value in enumerate(raw_keys):
        field_name = f"auth.jwt_key_versions[{index}]"
        item = _require_dict(value, field_name, E_BAD_AUTH_KEY)
        kid = _validate_id(item.get("kid"), f"{field_name}.kid", E_BAD_AUTH_KEY)
        if kid in keys:
            raise config_error(E_BAD_AUTH_KEY, "duplicate JWT kid", f"{field_name}.kid")
        secret = _require_str(item.get("secret"), f"{field_name}.secret", E_BAD_AUTH_KEY)
        version = _require_int(item.get("version"), f"{field_name}.version", E_BAD_AUTH_KEY, minimum=1)
        not_before = _require_int(
            item.get("not_before_ms", 0), f"{field_name}.not_before_ms", E_BAD_AUTH_KEY, minimum=0
        )
        expires = _require_int(
            item.get("expires_at_ms"), f"{field_name}.expires_at_ms", E_BAD_AUTH_KEY, minimum=1
        )
        if expires <= not_before:
            raise config_error(
                E_BAD_AUTH_KEY,
                "expires_at_ms must be greater than not_before_ms",
                f"{field_name}.expires_at_ms",
            )
        keys[kid] = JwtKeyVersion(
            kid=kid,
            secret=secret,
            version=version,
            not_before_ms=not_before,
            expires_at_ms=expires,
        )
        versions.append(version)
    if not keys:
        raise config_error(E_BAD_AUTH_KEY, "at least one JWT key version is required", "auth.jwt_key_versions")
    if versions != sorted(versions):
        raise config_error(
            E_BAD_AUTH_KEY,
            "JWT key versions must be monotonically non-decreasing",
            "auth.jwt_key_versions",
        )
    return keys


def _parse_rotation(
    value: Any,
    field_name: str,
    max_version: int,
) -> RotationPolicy:
    item = _require_dict(value, field_name, E_BAD_AUTH_KEY)
    active = _require_int(item.get("active_version"), f"{field_name}.active_version", E_BAD_AUTH_KEY, minimum=1)
    if active != max_version:
        raise config_error(
            E_BAD_AUTH_KEY,
            "active_version must equal the highest configured key version",
            f"{field_name}.active_version",
            active_version=active,
            max_version=max_version,
        )
    effective = _require_int(
        item.get("effective_at_ms"),
        f"{field_name}.effective_at_ms",
        E_BAD_AUTH_KEY,
        minimum=0,
    )
    grace = _require_int(
        item.get("grace_ms"), f"{field_name}.grace_ms", E_BAD_AUTH_KEY, minimum=0
    )
    return RotationPolicy(active_version=active, effective_at_ms=effective, grace_ms=grace)


def _parse_auth(raw_auth: Any) -> AuthPolicy:
    auth = _require_dict(raw_auth, "auth", E_BAD_AUTH_MATRIX)

    raw_matrix = _require_dict(auth.get("permission_matrix"), "auth.permission_matrix", E_BAD_AUTH_MATRIX)
    matrix: dict[str, tuple[str, ...]] = {}
    for domain, route_ids in raw_matrix.items():
        if not isinstance(domain, str) or not domain:
            raise config_error(E_BAD_AUTH_MATRIX, "domain must be non-empty", "auth.permission_matrix")
        values = _require_list(route_ids, f"auth.permission_matrix.{domain}", E_BAD_AUTH_MATRIX)
        normalized = tuple(
            _require_str(value, f"auth.permission_matrix.{domain}", E_BAD_AUTH_MATRIX)
            for value in values
        )
        if not normalized or len(normalized) != len(set(normalized)):
            raise config_error(
                E_BAD_AUTH_MATRIX,
                "route references must be non-empty and unique",
                f"auth.permission_matrix.{domain}",
            )
        matrix[domain] = normalized

    raw_grants = _require_dict(auth.get("tenant_grants"), "auth.tenant_grants", E_BAD_AUTH_MATRIX)
    grants: dict[str, tuple[str, ...]] = {}
    for tenant, domains in raw_grants.items():
        tenant_name = _require_str(tenant, "auth.tenant_grants", E_BAD_AUTH_MATRIX)
        values = _require_list(domains, f"auth.tenant_grants.{tenant}", E_BAD_AUTH_MATRIX)
        normalized = tuple(
            _require_str(value, f"auth.tenant_grants.{tenant}", E_BAD_AUTH_MATRIX)
            for value in values
        )
        if not normalized or len(normalized) != len(set(normalized)):
            raise config_error(
                E_BAD_AUTH_MATRIX,
                "tenant permission domains must be non-empty and unique",
                f"auth.tenant_grants.{tenant}",
            )
        unknown = sorted(set(normalized) - set(matrix))
        if unknown:
            raise config_error(
                E_BAD_AUTH_MATRIX,
                "tenant grants reference unknown permission domains",
                f"auth.tenant_grants.{tenant}",
                unknown=unknown,
            )
        grants[tenant_name] = normalized
    if not grants:
        raise config_error(E_BAD_AUTH_MATRIX, "at least one tenant grant is required", "auth.tenant_grants")

    raw_revocations = _require_list(auth.get("revocations", []), "auth.revocations", E_BAD_REVOCATION)
    revocations: list[dict[str, Any]] = []
    seen_revocations: set[tuple[str, str]] = set()
    for index, value in enumerate(raw_revocations):
        field_name = f"auth.revocations[{index}]"
        item = _require_dict(value, field_name, E_BAD_REVOCATION)
        kind = _require_str(item.get("type"), f"{field_name}.type", E_BAD_REVOCATION)
        if kind not in {"api_key", "jwt"}:
            raise config_error(
                E_BAD_REVOCATION,
                "type must be api_key or jwt",
                f"{field_name}.type",
                value=kind,
            )
        credential_id = _require_str(item.get("id"), f"{field_name}.id", E_BAD_REVOCATION)
        revoked_at = _require_int(
            item.get("revoked_at_ms", 0),
            f"{field_name}.revoked_at_ms",
            E_BAD_REVOCATION,
            minimum=0,
        )
        key = (kind, credential_id)
        if key in seen_revocations:
            raise config_error(
                E_BAD_REVOCATION,
                "duplicate revocation entry",
                field_name,
                type=kind,
                id=credential_id,
            )
        seen_revocations.add(key)
        normalized = {"type": kind, "id": credential_id, "revoked_at_ms": revoked_at}
        if "reason" in item:
            normalized["reason"] = _require_str(
                item["reason"], f"{field_name}.reason", E_BAD_REVOCATION
            )
        revocations.append(normalized)

    api_keys = _parse_api_keys(
        _require_list(auth.get("api_key_versions"), "auth.api_key_versions", E_BAD_AUTH_KEY),
        grants,
    )
    jwt_keys = _parse_jwt_keys(
        _require_list(auth.get("jwt_key_versions"), "auth.jwt_key_versions", E_BAD_AUTH_KEY)
    )
    raw_rotation = _require_dict(auth.get("rotation"), "auth.rotation", E_BAD_AUTH_KEY)
    api_rotation = _parse_rotation(
        raw_rotation.get("api_key"),
        "auth.rotation.api_key",
        max(key.version for key in api_keys.values()),
    )
    jwt_rotation = _parse_rotation(
        raw_rotation.get("jwt"),
        "auth.rotation.jwt",
        max(key.version for key in jwt_keys.values()),
    )
    return AuthPolicy(
        api_keys=api_keys,
        jwt_keys=jwt_keys,
        api_key_rotation=api_rotation,
        jwt_rotation=jwt_rotation,
        permission_matrix=matrix,
        tenant_grants=grants,
        revocations=tuple(revocations),
    )


def _validate_matrix_closure(routes: dict[str, Route], auth: AuthPolicy) -> None:
    matrix_routes = {route_id for route_ids in auth.permission_matrix.values() for route_id in route_ids}
    unknown = sorted(matrix_routes - set(routes))
    if unknown:
        raise config_error(
            E_BAD_AUTH_MATRIX,
            "permission matrix references unknown routes",
            "auth.permission_matrix",
            unknown=unknown,
        )
    for route in routes.values():
        if route.permission_domain not in auth.permission_matrix:
            raise config_error(
                E_BAD_AUTH_MATRIX,
                "route permission domain is not present in the matrix",
                "auth.permission_matrix",
                route_id=route.id,
                domain=route.permission_domain,
            )
        if route.id not in auth.permission_matrix[route.permission_domain]:
            raise config_error(
                E_BAD_AUTH_MATRIX,
                "route's own domain does not authorize the route",
                "auth.permission_matrix",
                route_id=route.id,
                domain=route.permission_domain,
            )
    unused_routes = sorted(set(routes) - matrix_routes)
    if unused_routes:
        raise config_error(
            E_BAD_AUTH_MATRIX,
            "every route must be referenced by at least one domain",
            "auth.permission_matrix",
            unused_routes=unused_routes,
        )


def parse_config(raw: dict[str, Any]) -> GatewayConfig:
    if not isinstance(raw, dict):
        raise config_error(E_BAD_ROUTE, "config must be an object", "$")
    version = _require_int(raw.get("version"), "version", E_BAD_ROUTE, minimum=1)
    routes = _parse_routes(raw.get("routes"))
    limits = _parse_limits(raw.get("limits"), routes)
    auth = _parse_auth(raw.get("auth"))
    _validate_matrix_closure(routes, auth)
    return GatewayConfig(
        version=version,
        raw=raw,
        digest=content_digest(raw),
        routes=routes,
        limits=limits,
        auth=auth,
    )


def version_watermarks(config: GatewayConfig) -> dict[str, dict[str, int]]:
    return {
        "api_key": {
            "max_version": config.auth.max_api_key_version,
            "active_version": config.auth.api_key_rotation.active_version,
            "effective_at_ms": config.auth.api_key_rotation.effective_at_ms,
        },
        "jwt": {
            "max_version": config.auth.max_jwt_version,
            "active_version": config.auth.jwt_rotation.active_version,
            "effective_at_ms": config.auth.jwt_rotation.effective_at_ms,
        },
    }


def validate_against_watermarks(
    config: GatewayConfig, persisted: dict[str, tuple[int, int, int]]
) -> None:
    for kind, proposed in version_watermarks(config).items():
        if kind not in persisted:
            continue
        old_max, old_active, old_effective = persisted[kind]
        if proposed["max_version"] < old_max:
            raise config_error(
                E_AUTH_VERSION_REGRESSION,
                "key max version must not decrease across config versions",
                f"auth.{kind}_key_versions",
                kind=kind,
                persisted=old_max,
                proposed=proposed["max_version"],
            )
        if proposed["active_version"] < old_active:
            raise config_error(
                E_AUTH_VERSION_REGRESSION,
                "active key version must not decrease across config versions",
                f"auth.rotation.{kind}.active_version",
                kind=kind,
                persisted=old_active,
                proposed=proposed["active_version"],
            )
        if proposed["effective_at_ms"] < old_effective:
            raise config_error(
                E_AUTH_VERSION_REGRESSION,
                "rotation effective logical clock must not decrease",
                f"auth.rotation.{kind}.effective_at_ms",
                kind=kind,
                persisted=old_effective,
                proposed=proposed["effective_at_ms"],
            )
