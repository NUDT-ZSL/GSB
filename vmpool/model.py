"""Validation and immutable parsing of local deterministic JSON samples."""

from __future__ import annotations

import json
import tempfile
from collections import OrderedDict
from dataclasses import dataclass, replace
from decimal import Decimal
from pathlib import Path
from typing import Any

from .errors import (
    E_BUDGET,
    E_CLOCK_ROLLBACK,
    E_DEPENDENCY_MISSING,
    E_DIGEST_MISMATCH,
    E_INITIAL_STATE,
    E_INVALID_REQUEST,
    E_JSON_MALFORMED,
    E_NAME_DUPLICATE,
    E_PENDING_CONFLICT,
    E_SAMPLE_MISSING,
    E_WITHDRAW_INVALID,
    BusinessError,
)
from .fixed import (
    Conversion,
    canonical_json,
    convert_to_fixed,
    decimal_text,
    decimal_value,
    digest_bytes,
    jsonable,
    nonnegative_int,
    positive_int,
)
from .graph import build_graph, transitive_downstream


SAMPLE_FILES = ("services.json", "pool.json", "requests.json", "policy.json")


@dataclass(frozen=True)
class Units:
    cpu_per_core: int
    memory_per_mib: int
    money_per_currency: int
    percent_per_percent: int
    ratio_denominator: int


@dataclass(frozen=True)
class ServiceSpec:
    name: str
    current_replicas: int
    vcpu_per_replica: int
    memory_mib_per_replica: int
    unit_price_fixed: int
    unit_price_error: int
    cpu_fixed: int
    cpu_error_fixed: int
    memory_fixed: int
    memory_error_fixed: int
    upstream: tuple[str, ...]
    source: dict[str, Any]


@dataclass(frozen=True)
class PoolSpec:
    name: str
    available_cpu: int
    available_memory: int
    occupied_cpu: int
    occupied_memory: int
    available_cpu_error: int
    available_memory_error: int
    occupied_cpu_error: int
    occupied_memory_error: int


@dataclass(frozen=True)
class Policy:
    budget_fixed: int
    budget_error: int
    safety_percent_source: str
    safety_ratio_fixed: int
    safety_error_fixed: int
    ratio_denominator: int
    tolerance_cost_fixed: int
    tolerance_cpu_fixed: int
    tolerance_memory_fixed: int
    clock_step_ms: int
    max_clock_regression_ms: int
    units: Units
    source: dict[str, Any]


@dataclass(frozen=True)
class Request:
    seq: int
    service: str
    op: str
    clock_ms: int
    target_replicas: int | None
    reverses_seq: int | None
    request_hash: str
    source: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "seq": self.seq,
            "service": self.service,
            "op": self.op,
            "clock_ms": self.clock_ms,
        }
        if self.target_replicas is not None:
            result["target_replicas"] = self.target_replicas
        if self.reverses_seq is not None:
            result["reverses_seq"] = self.reverses_seq
        return result


@dataclass(frozen=True)
class Scenario:
    sample_dir: str
    scenario_digest: str
    services: "OrderedDict[str, ServiceSpec]"
    pool: PoolSpec
    policy: Policy
    requests: tuple[Request, ...]
    topo_order: tuple[str, ...]
    upstream: dict[str, tuple[str, ...]]
    downstream: dict[str, tuple[str, ...]]
    transitive_downstream: dict[str, tuple[str, ...]]
    conversions: tuple[Conversion, ...]
    file_hashes: dict[str, str]
    documents: dict[str, Any]

    @property
    def request_by_seq(self) -> dict[int, Request]:
        return {request.seq: request for request in self.requests}

    @property
    def next_expected_seq(self) -> int | None:
        return self.requests[-1].seq + 1 if self.requests else 1

    @property
    def units(self) -> Units:
        return self.policy.units


def _read_json(path: Path) -> tuple[Any, bytes, str]:
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        raise BusinessError(
            E_SAMPLE_MISSING,
            "样本文件缺失",
            {"file": str(path)},
        ) from None
    except OSError as exc:
        raise BusinessError(
            E_SAMPLE_MISSING,
            "样本文件不可读",
            {"file": str(path), "reason": str(exc)},
        ) from None
    try:
        parsed = json.loads(raw.decode("utf-8"), parse_float=Decimal)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BusinessError(
            E_JSON_MALFORMED,
            "JSON 样本格式错误",
            {"file": path.name, "reason": str(exc)},
        ) from None
    return parsed, raw, digest_bytes(raw)


def _expect_dict(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise BusinessError(
            E_JSON_MALFORMED,
            "JSON 节点必须是对象",
            {"field": field, "actual_type": type(value).__name__},
        )
    return value


def _expect_list(value: Any, field: str) -> list[Any]:
    if not isinstance(value, list):
        raise BusinessError(
            E_JSON_MALFORMED,
            "JSON 节点必须是数组",
            {"field": field, "actual_type": type(value).__name__},
        )
    return value


def _manifest_check(
    sample_dir: Path,
    raw_files: dict[str, bytes],
    require_manifest: bool,
) -> tuple[str, dict[str, str]]:
    if not require_manifest:
        combined = digest_bytes(
            b"\0".join(
                component
                for name in SAMPLE_FILES
                for component in (name.encode("utf-8"), raw_files[name])
            )
        )
        return combined, {
            name: digest_bytes(raw_files[name]) for name in SAMPLE_FILES
        }
    manifest_path = sample_dir / "manifest.json"
    if not manifest_path.exists():
        raise BusinessError(
            E_DIGEST_MISMATCH,
            "缺少 manifest.json，无法校验样本摘要",
            {"file": str(manifest_path)},
        )

    manifest, _, _ = _read_json(manifest_path)
    manifest = _expect_dict(manifest, "manifest")
    expected_files = manifest.get("files")
    if not isinstance(expected_files, dict):
        raise BusinessError(
            E_DIGEST_MISMATCH,
            "manifest.files 缺失或格式错误",
            {},
        )
    actual_hashes: dict[str, str] = {}
    for name in SAMPLE_FILES:
        actual = digest_bytes(raw_files[name])
        actual_hashes[name] = actual
        if expected_files.get(name) != actual:
            raise BusinessError(
                E_DIGEST_MISMATCH,
                "样本文件摘要不匹配",
                {
                    "file": name,
                    "expected": expected_files.get(name),
                    "actual": actual,
                },
            )
    combined = digest_bytes(
        b"\0".join(
            component
            for name in SAMPLE_FILES
            for component in (name.encode("utf-8"), raw_files[name])
        )
    )
    expected = manifest.get("scenario_digest")
    if expected != combined:
        raise BusinessError(
            E_DIGEST_MISMATCH,
            "场景综合摘要不匹配",
            {"expected": expected, "actual": combined},
        )
    return combined, actual_hashes


def load_sample(
    sample_dir: str | Path,
    *,
    require_manifest: bool = True,
) -> Scenario:
    directory = Path(sample_dir)
    if not directory.exists() or not directory.is_dir():
        raise BusinessError(
            E_SAMPLE_MISSING,
            "样本目录不存在",
            {"directory": str(directory)},
        )

    parsed: dict[str, Any] = {}
    raw_files: dict[str, bytes] = {}
    file_hashes: dict[str, str] = {}
    for name in SAMPLE_FILES:
        parsed[name], raw_files[name], file_hashes[name] = _read_json(directory / name)
    scenario_digest, checked_hashes = _manifest_check(
        directory, raw_files, require_manifest
    )
    file_hashes = checked_hashes

    services_doc = _expect_dict(parsed["services.json"], "services.json")
    service_items = _expect_list(
        services_doc.get("services", services_doc.get("service_list")),
        "services.json.services",
    )
    pool_doc = _expect_dict(parsed["pool.json"], "pool.json")
    policy_doc = _expect_dict(parsed["policy.json"], "policy.json")
    requests_doc = _expect_dict(parsed["requests.json"], "requests.json")
    request_items = _expect_list(
        requests_doc.get("requests"), "requests.json.requests"
    )

    units_doc = _expect_dict(policy_doc.get("units"), "policy.units")
    units = Units(
        cpu_per_core=positive_int(
            units_doc.get("cpu_fixed_per_core"),
            "policy.units.cpu_fixed_per_core",
        ),
        memory_per_mib=positive_int(
            units_doc.get("memory_fixed_per_mib"),
            "policy.units.memory_fixed_per_mib",
        ),
        money_per_currency=positive_int(
            units_doc.get("money_fixed_per_currency"),
            "policy.units.money_fixed_per_currency",
        ),
        percent_per_percent=positive_int(
            units_doc.get("percent_fixed_per_percent"),
            "policy.units.percent_fixed_per_percent",
        ),
        ratio_denominator=positive_int(
            units_doc.get("ratio_denominator"),
            "policy.units.ratio_denominator",
        ),
    )

    conversions: list[Conversion] = []

    def convert(
        value: Any,
        scale: int,
        field_path: str,
        *,
        operation: str = "decimal*scale",
    ) -> tuple[int, int]:
        conversion_id = f"conv:{len(conversions) + 1:06d}"
        fixed, record = convert_to_fixed(
            value,
            scale,
            field_path,
            conversion_id,
            operation=operation,
        )
        conversions.append(record)
        return fixed, record.error_upper_bound_fixed

    services: "OrderedDict[str, ServiceSpec]" = OrderedDict()
    for index, raw_item in enumerate(service_items):
        item = _expect_dict(raw_item, f"services[{index}]")
        name = item.get("name")
        if not isinstance(name, str) or not name:
            raise BusinessError(
                E_INVALID_REQUEST,
                "服务名必须是非空字符串",
                {"index": index},
            )
        if name in services:
            raise BusinessError(
                E_NAME_DUPLICATE,
                "服务名重复",
                {"service": name},
            )
        current_replicas = positive_int(
            item.get("current_replicas"),
            f"services[{index}].current_replicas",
        )
        vcpu_source = item.get("vcpu_per_replica")
        memory_source = item.get("memory_mib_per_replica")
        positive_int(vcpu_source, f"services[{index}].vcpu_per_replica")
        positive_int(
            memory_source, f"services[{index}].memory_mib_per_replica"
        )
        price = decimal_value(
            item.get("unit_price_per_replica"),
            f"services[{index}].unit_price_per_replica",
        )
        if price < 0:
            raise BusinessError(
                E_INVALID_REQUEST,
                "每副本单价必须非负",
                {"service": name, "value": decimal_text(price)},
            )
        cpu_fixed, cpu_error = convert(
            vcpu_source,
            units.cpu_per_core,
            f"services.{name}.vcpu_per_replica",
        )
        memory_fixed, memory_error = convert(
            memory_source,
            units.memory_per_mib,
            f"services.{name}.memory_mib_per_replica",
        )
        price_fixed, price_error = convert(
            price,
            units.money_per_currency,
            f"services.{name}.unit_price_per_replica",
        )
        upstream_value = item.get("depends_on", [])
        upstream_list = _expect_list(
            upstream_value, f"services.{name}.depends_on"
        )
        upstream_names: list[str] = []
        for dependency_index, dependency in enumerate(upstream_list):
            if not isinstance(dependency, str) or not dependency:
                raise BusinessError(
                    E_INVALID_REQUEST,
                    "依赖服务名必须是非空字符串",
                    {
                        "service": name,
                        "dependency_index": dependency_index,
                    },
                )
            if dependency == name:
                raise BusinessError(
                    E_DEPENDENCY_MISSING,
                    "服务不能依赖自身",
                    {"service": name},
                )
            if dependency in upstream_names:
                raise BusinessError(
                    E_INVALID_REQUEST,
                    "依赖列表包含重复项",
                    {"service": name, "dependency": dependency},
                )
            upstream_names.append(dependency)
        services[name] = ServiceSpec(
            name=name,
            current_replicas=current_replicas,
            vcpu_per_replica=vcpu_source,
            memory_mib_per_replica=memory_source,
            unit_price_fixed=price_fixed,
            unit_price_error=price_error,
            cpu_fixed=cpu_fixed,
            cpu_error_fixed=cpu_error,
            memory_fixed=memory_fixed,
            memory_error_fixed=memory_error,
            upstream=tuple(sorted(upstream_names)),
            source=jsonable(item),
        )
    for service in services.values():
        for dependency in service.upstream:
            if dependency not in services:
                raise BusinessError(
                    E_DEPENDENCY_MISSING,
                    "依赖边指向不存在的服务",
                    {
                        "service": service.name,
                        "dependency": dependency,
                    },
                )

    order, upstream_map, downstream_map = build_graph(
        list(services.keys()),
        {name: list(service.upstream) for name, service in services.items()},
    )
    closures = transitive_downstream(order, downstream_map)

    pools_value = pool_doc.get("pools", pool_doc)
    if isinstance(pools_value, list):
        pool_names = [
            item.get("name") if isinstance(item, dict) else None
            for item in pools_value
        ]
        named = [name for name in pool_names if isinstance(name, str) and name]
        if len(named) != len(set(named)):
            raise BusinessError(
                E_NAME_DUPLICATE,
                "池名重复",
                {"pool_names": pool_names},
            )
        if len(pools_value) != 1:
            raise BusinessError(
                E_INVALID_REQUEST,
                "当前引擎要求恰好一个虚机池",
                {"pool_count": len(pools_value)},
            )
        pool_item = _expect_dict(pools_value[0], "pool.pools[0]")
    else:
        pool_item = _expect_dict(pools_value, "pool")
    pool_name = pool_item.get("name")
    if not isinstance(pool_name, str) or not pool_name:
        raise BusinessError(
            E_INVALID_REQUEST,
            "池名必须是非空字符串",
            {},
        )
    available_cpu_source = nonnegative_int(
        pool_item.get("available_vcpu"), "pool.available_vcpu"
    )
    available_memory_source = nonnegative_int(
        pool_item.get("available_memory_mib"), "pool.available_memory_mib"
    )
    occupied_cpu_source = nonnegative_int(
        pool_item.get("occupied_vcpu"), "pool.occupied_vcpu"
    )
    occupied_memory_source = nonnegative_int(
        pool_item.get("occupied_memory_mib"), "pool.occupied_memory_mib"
    )
    if available_cpu_source <= 0 or available_memory_source <= 0:
        raise BusinessError(
            E_INVALID_REQUEST,
            "池可用总量必须为正",
            {"pool": pool_name},
        )
    available_cpu, available_cpu_error = convert(
        available_cpu_source, units.cpu_per_core, "pool.available_vcpu"
    )
    available_memory, available_memory_error = convert(
        available_memory_source,
        units.memory_per_mib,
        "pool.available_memory_mib",
    )
    occupied_cpu, occupied_cpu_error = convert(
        occupied_cpu_source, units.cpu_per_core, "pool.occupied_vcpu"
    )
    occupied_memory, occupied_memory_error = convert(
        occupied_memory_source,
        units.memory_per_mib,
        "pool.occupied_memory_mib",
    )
    if occupied_cpu > available_cpu or occupied_memory > available_memory:
        raise BusinessError(
            E_INITIAL_STATE,
            "池初始占用超过可用总量",
            {
                "occupied_cpu": occupied_cpu,
                "available_cpu": available_cpu,
                "occupied_memory": occupied_memory,
                "available_memory": available_memory,
            },
        )
    pool = PoolSpec(
        name=pool_name,
        available_cpu=available_cpu,
        available_memory=available_memory,
        occupied_cpu=occupied_cpu,
        occupied_memory=occupied_memory,
        available_cpu_error=available_cpu_error,
        available_memory_error=available_memory_error,
        occupied_cpu_error=occupied_cpu_error,
        occupied_memory_error=occupied_memory_error,
    )

    budget_source = decimal_value(policy_doc.get("budget"), "policy.budget")
    if budget_source < 0:
        raise BusinessError(
            E_INVALID_REQUEST,
            "预算必须非负",
            {"budget": decimal_text(budget_source)},
        )
    budget_fixed, budget_error = convert(
        budget_source,
        units.money_per_currency,
        "policy.budget",
    )
    safety_source = decimal_value(
        policy_doc.get("safety_margin_percent"),
        "policy.safety_margin_percent",
    )
    if safety_source < 0 or safety_source > 100:
        raise BusinessError(
            E_INVALID_REQUEST,
            "容量安全余量百分比必须在 0 到 100 之间",
            {"value": decimal_text(safety_source)},
        )
    safety_fixed, safety_error = convert(
        safety_source,
        units.percent_per_percent,
        "policy.safety_margin_percent",
    )

    tolerance_doc = _expect_dict(policy_doc.get("tolerance"), "policy.tolerance")
    tolerance_cost, _ = convert(
        tolerance_doc.get("cost"),
        units.money_per_currency,
        "policy.tolerance.cost",
    )
    tolerance_cpu, _ = convert(
        tolerance_doc.get("cpu_cores"),
        units.cpu_per_core,
        "policy.tolerance.cpu_cores",
    )
    tolerance_memory, _ = convert(
        tolerance_doc.get("memory_mib"),
        units.memory_per_mib,
        "policy.tolerance.memory_mib",
    )
    if tolerance_cost < 0 or tolerance_cpu < 0 or tolerance_memory < 0:
        raise BusinessError(
            E_INVALID_REQUEST,
            "容差必须非负",
            {},
        )
    clock_step = positive_int(
        policy_doc.get("clock_step_ms"), "policy.clock_step_ms"
    )
    max_regression = nonnegative_int(
        policy_doc.get("max_clock_regression_ms", 0),
        "policy.max_clock_regression_ms",
    )
    policy = Policy(
        budget_fixed=budget_fixed,
        budget_error=budget_error,
        safety_percent_source=format(safety_source, "f"),
        safety_ratio_fixed=safety_fixed,
        safety_error_fixed=safety_error,
        ratio_denominator=units.ratio_denominator,
        tolerance_cost_fixed=tolerance_cost,
        tolerance_cpu_fixed=tolerance_cpu,
        tolerance_memory_fixed=tolerance_memory,
        clock_step_ms=clock_step,
        max_clock_regression_ms=max_regression,
        units=units,
        source=jsonable(policy_doc),
    )

    initial_service_cpu = sum(
        service.current_replicas * service.cpu_fixed
        for service in services.values()
    )
    initial_service_memory = sum(
        service.current_replicas * service.memory_fixed
        for service in services.values()
    )
    if (
        initial_service_cpu > occupied_cpu
        or initial_service_memory > occupied_memory
    ):
        raise BusinessError(
            E_INITIAL_STATE,
            "池已占用资源小于服务清单初始资源",
            {
                "service_cpu": initial_service_cpu,
                "occupied_cpu": occupied_cpu,
                "service_memory": initial_service_memory,
                "occupied_memory": occupied_memory,
            },
        )
    initial_cost = sum(
        service.current_replicas * service.unit_price_fixed
        for service in services.values()
    )
    if initial_cost > budget_fixed:
        raise BusinessError(
            E_BUDGET,
            "初始服务成本已超过总预算",
            {"initial_cost": initial_cost, "budget": budget_fixed},
        )

    requests: list[Request] = []
    seen_seq: set[int] = set()
    seen_service_time: dict[tuple[str, int], int] = {}
    previous_seq: int | None = None
    previous_clock: int | None = None
    for index, raw_item in enumerate(request_items):
        item = _expect_dict(raw_item, f"requests[{index}]")
        seq = positive_int(item.get("seq"), f"requests[{index}].seq")
        if seq in seen_seq:
            raise BusinessError(
                E_INVALID_REQUEST,
                "请求序号重复",
                {"seq": seq},
            )
        if previous_seq is not None and seq <= previous_seq:
            raise BusinessError(
                E_INVALID_REQUEST,
                "请求序号必须严格递增",
                {"previous": previous_seq, "current": seq},
            )
        service_name = item.get("service")
        if not isinstance(service_name, str) or service_name not in services:
            raise BusinessError(
                E_INVALID_REQUEST,
                "请求引用不存在的服务",
                {"seq": seq, "service": service_name},
            )
        clock_ms = nonnegative_int(
            item.get("clock_ms"), f"requests[{index}].clock_ms"
        )
        if (
            previous_clock is not None
            and previous_clock - clock_ms > max_regression
        ):
            raise BusinessError(
                E_CLOCK_ROLLBACK,
                "逻辑时钟回拨超过策略阈值",
                {
                    "previous_clock_ms": previous_clock,
                    "clock_ms": clock_ms,
                    "max_regression_ms": max_regression,
                },
            )
        op = item.get("op", "adjust")
        if op not in ("adjust", "withdraw"):
            raise BusinessError(
                E_INVALID_REQUEST,
                "不支持的请求操作",
                {"seq": seq, "op": op},
            )
        target: int | None = None
        if op == "adjust":
            target = positive_int(
                item.get("target_replicas"),
                f"requests[{index}].target_replicas",
            )
        elif item.get("target_replicas") is not None:
            target = positive_int(
                item.get("target_replicas"),
                f"requests[{index}].target_replicas",
            )
        reverses_seq: int | None = None
        if item.get("reverses_seq") is not None:
            reverses_seq = positive_int(
                item.get("reverses_seq"),
                f"requests[{index}].reverses_seq",
            )
        service_time_key = (service_name, clock_ms)
        if service_time_key in seen_service_time:
            raise BusinessError(
                E_PENDING_CONFLICT,
                "同一服务同一逻辑时刻存在多条未裁决请求",
                {
                    "service": service_name,
                    "clock_ms": clock_ms,
                    "first_seq": seen_service_time[service_time_key],
                    "second_seq": seq,
                },
            )
        seen_service_time[service_time_key] = seq
        normalized = {
            "seq": seq,
            "service": service_name,
            "op": op,
            "clock_ms": clock_ms,
        }
        if target is not None:
            normalized["target_replicas"] = target
        if reverses_seq is not None:
            normalized["reverses_seq"] = reverses_seq
        request = Request(
            seq=seq,
            service=service_name,
            op=op,
            clock_ms=clock_ms,
            target_replicas=target,
            reverses_seq=reverses_seq,
            request_hash=digest_bytes(canonical_json(normalized).encode("utf-8")),
            source=normalized,
        )
        requests.append(request)
        seen_seq.add(seq)
        previous_seq = seq
        previous_clock = clock_ms

    request_lookup = {request.seq: request for request in requests}
    for request in requests:
        if request.op == "withdraw" and request.reverses_seq is not None:
            reversed_request = request_lookup.get(request.reverses_seq)
            if (
                reversed_request is None
                or reversed_request.seq >= request.seq
                or reversed_request.service != request.service
                or reversed_request.op != "adjust"
            ):
                raise BusinessError(
                    E_WITHDRAW_INVALID,
                    "撤销请求必须引用本服务更早的调整请求",
                    {
                        "seq": request.seq,
                        "service": request.service,
                        "reverses_seq": request.reverses_seq,
                    },
                )

    return Scenario(
        sample_dir=str(directory.resolve()),
        scenario_digest=scenario_digest,
        services=services,
        pool=pool,
        policy=policy,
        requests=tuple(requests),
        topo_order=tuple(order),
        upstream=upstream_map,
        downstream=downstream_map,
        transitive_downstream=closures,
        conversions=tuple(conversions),
        file_hashes=file_hashes,
        documents={
            name: jsonable(parsed[name]) for name in SAMPLE_FILES
        },
    )


def scenario_from_documents(
    documents: dict[str, Any],
    *,
    scenario_digest: str,
    file_hashes: dict[str, str],
    sample_dir: str,
) -> Scenario:
    """Rebuild a parsed scenario without reading the original directory.

    The stored JSON is written to a local temporary directory and passed
    through the same validator. The original digest and sample metadata are
    then restored so persisted state remains anchored to the loaded sample.
    """
    with tempfile.TemporaryDirectory(prefix="vmpool-scenario-") as temp:
        directory = Path(temp)
        for name in SAMPLE_FILES:
            content = canonical_json(documents[name]).encode("utf-8")
            (directory / name).write_bytes(content)
        loaded = load_sample(directory, require_manifest=False)
    return replace(
        loaded,
        scenario_digest=scenario_digest,
        sample_dir=sample_dir,
        file_hashes=dict(file_hashes),
        documents=jsonable(documents),
    )
