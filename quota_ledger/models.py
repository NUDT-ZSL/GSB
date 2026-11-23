from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import hashlib
import json
from typing import Any

from .errors import error


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _mapping(value: Any, path: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise error("INVALID_FIELD_TYPE", f"{path} 必须是对象")
    return value


def _list(value: Any, path: str) -> list[Any]:
    if not isinstance(value, list):
        raise error("INVALID_FIELD_TYPE", f"{path} 必须是数组")
    return value


def _string(value: Any, path: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise error("INVALID_FIELD_VALUE", f"{path} 必须是非空字符串")
    return value.strip()


def _int(value: Any, path: str, *, minimum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise error("INVALID_FIELD_TYPE", f"{path} 必须是整数")
    if minimum is not None and value < minimum:
        raise error(
            "INVALID_FIELD_VALUE",
            f"{path} 必须大于等于 {minimum}",
            details={"path": path, "value": value, "minimum": minimum},
        )
    return value


def _bool(value: Any, path: str) -> bool:
    if not isinstance(value, bool):
        raise error("INVALID_FIELD_TYPE", f"{path} 必须是布尔值")
    return value


def _number(value: Any, path: str, *, positive: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise error("INVALID_FIELD_TYPE", f"{path} 必须是数值")
    try:
        decimal = Decimal(str(value))
    except InvalidOperation as exc:
        raise error("INVALID_FIELD_VALUE", f"{path} 不是有效数值") from exc
    if not decimal.is_finite():
        raise error("INVALID_FIELD_VALUE", f"{path} 必须是有限数值")
    if positive and decimal <= 0:
        raise error("INVALID_FIELD_VALUE", f"{path} 必须大于 0")
    return float(decimal)


def _object_list(document: Any, key: str, path: str) -> list[Any]:
    if isinstance(document, list):
        return _list(document, path)
    root = _mapping(document, path)
    if key not in root:
        raise error("MISSING_FIELD", f"{path}.{key} 缺失")
    return _list(root[key], f"{path}.{key}")


@dataclass(frozen=True)
class PoolSpec:
    name: str
    capacity_mc: int
    capacity_mib: int


@dataclass(frozen=True)
class TenantSpec:
    name: str
    resource_pool: str
    quota_mc: int
    quota_mib: int
    allow_overdraft: bool
    borrow_limit_mc: int
    borrow_limit_mib: int
    declaration_version: int = 1

    @property
    def digest(self) -> str:
        return digest(
            {
                "resource_pool": self.resource_pool,
                "quota_mc": self.quota_mc,
                "quota_mib": self.quota_mib,
                "allow_overdraft": self.allow_overdraft,
                "borrow_limit_mc": self.borrow_limit_mc,
                "borrow_limit_mib": self.borrow_limit_mib,
            }
        )

    def payload(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "resource_pool": self.resource_pool,
            "quota": {"millicores": self.quota_mc, "mib": self.quota_mib},
            "allow_overdraft": self.allow_overdraft,
            "borrow_limit": {
                "millicores": self.borrow_limit_mc,
                "mib": self.borrow_limit_mib,
            },
            "declaration_version": self.declaration_version,
            "digest": self.digest,
        }


@dataclass(frozen=True)
class TenantBundle:
    pools: tuple[PoolSpec, ...]
    tenants: tuple[TenantSpec, ...]
    fixed_scale: int
    precision_tolerance: float


@dataclass(frozen=True)
class ServiceSpec:
    name: str
    resource_pool: str
    requests_per_round: int
    max_concurrent_reservations: int


@dataclass(frozen=True)
class ServiceBundle:
    services: tuple[ServiceSpec, ...]


@dataclass(frozen=True)
class ApplicationSpec:
    sequence: int
    application_id: str
    tenant: str
    service: str
    request_mc: int
    request_mib: int
    requested_at_ms: int
    duration_ms: int
    expires_at_ms: int

    @property
    def fingerprint(self) -> str:
        return digest(
            {
                "sequence": self.sequence,
                "application_id": self.application_id,
                "tenant": self.tenant,
                "service": self.service,
                "request_mc": self.request_mc,
                "request_mib": self.request_mib,
                "requested_at_ms": self.requested_at_ms,
                "duration_ms": self.duration_ms,
                "expires_at_ms": self.expires_at_ms,
            }
        )

    def payload(self) -> dict[str, Any]:
        return {
            "sequence": self.sequence,
            "application_id": self.application_id,
            "tenant": self.tenant,
            "service": self.service,
            "millicores": self.request_mc,
            "mib": self.request_mib,
            "requested_at_ms": self.requested_at_ms,
            "duration_ms": self.duration_ms,
            "expires_at_ms": self.expires_at_ms,
            "fingerprint": self.fingerprint,
        }


@dataclass(frozen=True)
class ApplicationBundle:
    applications: tuple[ApplicationSpec, ...]


def _resource_pair(value: Any, path: str, *, default: dict[str, int] | None = None) -> tuple[int, int]:
    if value is None and default is not None:
        return default["millicores"], default["mib"]
    obj = _mapping(value, path)
    mc = _int(obj.get("millicores"), f"{path}.millicores", minimum=0)
    mib = _int(obj.get("mib"), f"{path}.mib", minimum=0)
    return mc, mib


def parse_tenant_bundle(document: Any) -> TenantBundle:
    root = _mapping(document, "tenants_document")
    fixed_scale = _int(root.get("fixed_scale", 1000), "fixed_scale", minimum=1)
    precision_tolerance = _number(
        root.get("precision_tolerance", 0.000001),
        "precision_tolerance",
        positive=True,
    )
    pool_items = _object_list(root, "resource_pools", "tenants_document")
    tenant_items = _object_list(root, "tenants", "tenants_document")

    pools: list[PoolSpec] = []
    pool_names: set[str] = set()
    for index, item in enumerate(pool_items):
        path = f"resource_pools[{index}]"
        obj = _mapping(item, path)
        name = _string(obj.get("name"), f"{path}.name")
        if name in pool_names:
            raise error(
                "RESOURCE_POOL_DUPLICATE",
                f"资源池名称重复: {name}",
                details={"resource_pool": name},
            )
        pool_names.add(name)
        pools.append(
            PoolSpec(
                name=name,
                capacity_mc=_int(
                    obj.get("millicores"), f"{path}.millicores", minimum=0
                ),
                capacity_mib=_int(obj.get("mib"), f"{path}.mib", minimum=0),
            )
        )

    tenants: list[TenantSpec] = []
    tenant_names: set[str] = set()
    for index, item in enumerate(tenant_items):
        path = f"tenants[{index}]"
        obj = _mapping(item, path)
        name = _string(obj.get("name"), f"{path}.name")
        if name in tenant_names:
            raise error(
                "TENANT_DUPLICATE",
                f"租户名称重复: {name}",
                details={"tenant": name},
            )
        tenant_names.add(name)
        pool = _string(obj.get("resource_pool"), f"{path}.resource_pool")
        if pool not in pool_names:
            raise error(
                "RESOURCE_POOL_UNKNOWN",
                f"租户 {name} 引用了未声明资源池 {pool}",
                details={"tenant": name, "resource_pool": pool},
            )
        quota_mc, quota_mib = _resource_pair(obj.get("quota"), f"{path}.quota")
        if "borrow_limit" not in obj:
            raise error("MISSING_FIELD", f"{path}.borrow_limit 缺失")
        borrow_mc, borrow_mib = _resource_pair(
            obj.get("borrow_limit"), f"{path}.borrow_limit"
        )
        allow = _bool(obj.get("allow_overdraft"), f"{path}.allow_overdraft")
        version = _int(
            obj.get("declaration_version", 1),
            f"{path}.declaration_version",
            minimum=1,
        )
        tenants.append(
            TenantSpec(
                name=name,
                resource_pool=pool,
                quota_mc=quota_mc,
                quota_mib=quota_mib,
                allow_overdraft=allow,
                borrow_limit_mc=borrow_mc,
                borrow_limit_mib=borrow_mib,
                declaration_version=version,
            )
        )

    for pool in pools:
        pool_tenants = [t for t in tenants if t.resource_pool == pool.name]
        total_mc = sum(t.quota_mc for t in pool_tenants)
        total_mib = sum(t.quota_mib for t in pool_tenants)
        if total_mc > pool.capacity_mc or total_mib > pool.capacity_mib:
            raise error(
                "RESOURCE_POOL_CAPACITY_EXCEEDED",
                f"资源池 {pool.name} 的基础配额合计超过池容量",
                details={
                    "resource_pool": pool.name,
                    "quota_mc": total_mc,
                    "capacity_mc": pool.capacity_mc,
                    "quota_mib": total_mib,
                    "capacity_mib": pool.capacity_mib,
                },
            )

    return TenantBundle(
        pools=tuple(pools),
        tenants=tuple(tenants),
        fixed_scale=fixed_scale,
        precision_tolerance=precision_tolerance,
    )


def parse_service_bundle(document: Any) -> ServiceBundle:
    items = _object_list(document, "services", "services_document")
    services: list[ServiceSpec] = []
    names: set[str] = set()
    for index, item in enumerate(items):
        path = f"services[{index}]"
        obj = _mapping(item, path)
        name = _string(obj.get("name"), f"{path}.name")
        if name in names:
            raise error(
                "SERVICE_DUPLICATE",
                f"服务名称重复: {name}",
                details={"service": name},
            )
        names.add(name)
        services.append(
            ServiceSpec(
                name=name,
                resource_pool=_string(
                    obj.get("resource_pool"), f"{path}.resource_pool"
                ),
                requests_per_round=_int(
                    obj.get("requests_per_round"),
                    f"{path}.requests_per_round",
                    minimum=1,
                ),
                max_concurrent_reservations=_int(
                    obj.get("max_concurrent_reservations"),
                    f"{path}.max_concurrent_reservations",
                    minimum=1,
                ),
            )
        )
    return ServiceBundle(services=tuple(services))


def parse_application_bundle(document: Any) -> ApplicationBundle:
    items = _object_list(document, "applications", "applications_document")
    apps: list[ApplicationSpec] = []
    seen_ids: set[str] = set()
    seen_sequences: set[int] = set()
    previous_sequence = -1
    for index, item in enumerate(items):
        path = f"applications[{index}]"
        obj = _mapping(item, path)
        sequence = _int(obj.get("sequence"), f"{path}.sequence", minimum=1)
        if sequence in seen_sequences:
            raise error(
                "APPLICATION_SEQUENCE_DUPLICATE",
                f"申请序号重复: {sequence}",
                details={"sequence": sequence},
            )
        if sequence <= previous_sequence:
            raise error(
                "APPLICATION_SEQUENCE_ORDER",
                "申请序号必须严格递增",
                details={
                    "previous_sequence": previous_sequence,
                    "sequence": sequence,
                },
            )
        previous_sequence = sequence
        seen_sequences.add(sequence)
        application_id = obj.get("application_id", f"app-{sequence:06d}")
        application_id = _string(application_id, f"{path}.application_id")
        if application_id in seen_ids:
            raise error(
                "APPLICATION_ID_DUPLICATE",
                f"申请 ID 重复: {application_id}",
                details={"application_id": application_id},
            )
        seen_ids.add(application_id)
        tenant = _string(obj.get("tenant"), f"{path}.tenant")
        service = _string(obj.get("service"), f"{path}.service")

        if "required" in obj:
            request_mc, request_mib = _resource_pair(
                obj.get("required"), f"{path}.required"
            )
            if request_mc <= 0 or request_mib <= 0:
                raise error(
                    "INVALID_FIELD_VALUE",
                    f"{path}.required 的毫核与 MiB 必须为正整数",
                    details={
                        "millicores": request_mc,
                        "mib": request_mib,
                    },
                )
        else:
            request_mc = _int(
                obj.get("millicores"), f"{path}.millicores", minimum=1
            )
            request_mib = _int(obj.get("mib"), f"{path}.mib", minimum=1)

        requested_at_ms = _int(
            obj.get("requested_at_ms"),
            f"{path}.requested_at_ms",
            minimum=0,
        )
        expires_value = obj.get("expires_at_ms")
        duration_value = obj.get("duration_ms")
        if duration_value is not None:
            duration_ms = _int(duration_value, f"{path}.duration_ms", minimum=0)
        else:
            duration_ms = 0

        if expires_value is None:
            if duration_value is None:
                raise error(
                    "MISSING_FIELD",
                    f"{path} 必须提供 expires_at_ms 或 duration_ms",
                )
            expires_at_ms = requested_at_ms + duration_ms
        else:
            expires_at_ms = _int(
                expires_value, f"{path}.expires_at_ms", minimum=0
            )
            if expires_at_ms < requested_at_ms:
                raise error(
                    "APPLICATION_EXPIRY_BEFORE_REQUEST",
                    "到期逻辑时钟不得早于申请逻辑时钟",
                    details={
                        "application_id": application_id,
                        "requested_at_ms": requested_at_ms,
                        "expires_at_ms": expires_at_ms,
                    },
                )
            if duration_value is None:
                duration_ms = expires_at_ms - requested_at_ms
            elif expires_at_ms != requested_at_ms + duration_ms:
                raise error(
                    "APPLICATION_EXPIRY_INCONSISTENT",
                    "expires_at_ms 必须等于 requested_at_ms + duration_ms",
                    details={
                        "application_id": application_id,
                        "requested_at_ms": requested_at_ms,
                        "duration_ms": duration_ms,
                        "expires_at_ms": expires_at_ms,
                    },
                )
        apps.append(
            ApplicationSpec(
                sequence=sequence,
                application_id=application_id,
                tenant=tenant,
                service=service,
                request_mc=request_mc,
                request_mib=request_mib,
                requested_at_ms=requested_at_ms,
                duration_ms=duration_ms,
                expires_at_ms=expires_at_ms,
            )
        )
    return ApplicationBundle(applications=tuple(apps))


def validate_cross_references(
    tenants: TenantBundle,
    services: ServiceBundle,
    applications: ApplicationBundle,
) -> None:
    pool_names = {pool.name for pool in tenants.pools}
    tenant_map = {tenant.name: tenant for tenant in tenants.tenants}
    service_map: dict[str, ServiceSpec] = {}
    for service in services.services:
        if service.resource_pool not in pool_names:
            raise error(
                "RESOURCE_POOL_UNKNOWN",
                f"服务 {service.name} 引用了未声明资源池 {service.resource_pool}",
                details={
                    "service": service.name,
                    "resource_pool": service.resource_pool,
                },
            )
        service_map[service.name] = service

    for app in applications.applications:
        if app.tenant not in tenant_map:
            raise error(
                "TENANT_UNKNOWN",
                f"申请 {app.application_id} 引用了未知租户 {app.tenant}",
                details={
                    "application_id": app.application_id,
                    "tenant": app.tenant,
                },
            )
        if app.service not in service_map:
            raise error(
                "SERVICE_UNKNOWN",
                f"申请 {app.application_id} 引用了未知服务 {app.service}",
                details={
                    "application_id": app.application_id,
                    "service": app.service,
                },
            )
        tenant_pool = tenant_map[app.tenant].resource_pool
        service_pool = service_map[app.service].resource_pool
        if tenant_pool != service_pool:
            raise error(
                "RESOURCE_POOL_CLOSURE_VIOLATION",
                "申请关联的租户与服务不在同一资源池",
                details={
                    "application_id": app.application_id,
                    "tenant": app.tenant,
                    "tenant_pool": tenant_pool,
                    "service": app.service,
                    "service_pool": service_pool,
                },
            )


def parse_bundles(
    tenants_document: Any,
    services_document: Any,
    applications_document: Any,
) -> tuple[TenantBundle, ServiceBundle, ApplicationBundle]:
    tenants = parse_tenant_bundle(tenants_document)
    services = parse_service_bundle(services_document)
    applications = parse_application_bundle(applications_document)
    validate_cross_references(tenants, services, applications)
    return tenants, services, applications
