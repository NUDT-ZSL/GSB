"""Strict, standard-library-only project manifest validation."""

from __future__ import annotations

import math
from typing import Any

from .models import (
    EVENT_TYPES,
    HEALTH_STATES,
    INCIDENT_TYPES,
    Incident,
    Institution,
    Policy,
    Project,
    Region,
    StorageObject,
    TimelineEvent,
    ValidationError,
)


DEFAULT_URGENCY_WEIGHTS = {
    "routine": 1.0,
    "urgent": 2.0,
    "critical": 4.0,
}


def _is_number(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


class _Collector:
    def __init__(self) -> None:
        self.errors: list[ValidationError] = []

    def add(
        self,
        code: str,
        message: str,
        path: str,
        *,
        object_id: str | None = None,
        institution_id: str | None = None,
        region_id: str | None = None,
        event_index: int | None = None,
    ) -> None:
        self.errors.append(
            ValidationError(
                code=code,
                message=message,
                path=path,
                object_id=object_id,
                institution_id=institution_id,
                region_id=region_id,
                event_index=event_index,
            )
        )


def parse_project(data: Any) -> tuple[Project | None, list[ValidationError]]:
    """Parse and validate a manifest.

    The parser deliberately keeps inspecting after individual record failures so
    operators receive one consolidated error list. No planner is run when the
    returned project is ``None``.
    """

    collector = _Collector()
    if not isinstance(data, dict):
        collector.add("INVALID_ROOT", "工程清单根节点必须是 JSON 对象", "$")
        return None, collector.errors

    project_id = data.get("project_id", "unnamed-project")
    if not _text(project_id):
        collector.add("INVALID_PROJECT_ID", "project_id 必须是非空字符串", "$.project_id")
        project_id = "unnamed-project"

    policy = _parse_policy(data.get("policy"), collector)
    institutions = _parse_institutions(data.get("institutions"), collector)
    regions = _parse_regions(data.get("regions"), policy, collector)
    objects = _parse_objects(
        data.get("objects"), institutions, policy, collector
    )
    events = _parse_events(data.get("events"), objects, collector)
    incidents = _parse_incidents(data.get("incidents", []), regions, collector)

    # Manifest ordering is part of the input contract. Sorting inside the engine
    # is still deterministic for equal timestamps.
    previous_at: float | None = None
    for index, event in enumerate(events):
        if previous_at is not None and event.at < previous_at:
            collector.add(
                "EVENTS_NOT_TIME_ORDERED",
                "events 必须按逻辑时刻非降序排列",
                f"$.events[{index}].at",
                object_id=event.object_id,
                event_index=event.index,
            )
        previous_at = event.at

    if collector.errors:
        return None, collector.errors

    if policy is None or any(
        mapping is None for mapping in (institutions, regions, objects)
    ):
        collector.add("INTERNAL_PARSE_ERROR", "工程清单解析未产生完整模型", "$")
        return None, collector.errors

    metadata = data.get("metadata", {})
    if not isinstance(metadata, dict):
        collector.add("INVALID_METADATA", "metadata 必须是 JSON 对象", "$.metadata")
        metadata = {}

    source_seed = data.get("seed")
    if source_seed is not None and not _is_int(source_seed):
        collector.add("INVALID_SEED", "seed 必须是整数", "$.seed")

    if collector.errors:
        return None, collector.errors

    project = Project(
        project_id=project_id,
        policy=policy,
        institutions=institutions,
        regions=regions,
        objects=objects,
        events=events,
        incidents=incidents,
        source_seed=source_seed,
        metadata=metadata,
    )
    return project, collector.errors


def _parse_policy(value: Any, collector: _Collector) -> Policy | None:
    if not isinstance(value, dict):
        collector.add("INVALID_POLICY", "policy 必须是 JSON 对象", "$.policy")
        return None

    watermark = value.get("watermark_ratio")
    if not _is_number(watermark) or not (0 < float(watermark) <= 1):
        collector.add(
            "INVALID_WATERMARK_RATIO",
            "policy.watermark_ratio 必须满足 0 < ratio <= 1",
            "$.policy.watermark_ratio",
        )
        watermark = None

    max_latency = value.get("max_latency_ms")
    if not _is_number(max_latency) or float(max_latency) < 0:
        collector.add(
            "INVALID_MAX_LATENCY",
            "policy.max_latency_ms 必须是非负有限数",
            "$.policy.max_latency_ms",
        )
        max_latency = None

    raw_weights = value.get("urgency_weights", DEFAULT_URGENCY_WEIGHTS)
    weights: dict[str, float] = {}
    if not isinstance(raw_weights, dict) or not raw_weights:
        collector.add(
            "INVALID_URGENCY_WEIGHTS",
            "policy.urgency_weights 必须是非空对象",
            "$.policy.urgency_weights",
        )
    else:
        for key, weight in raw_weights.items():
            if not _text(key):
                collector.add(
                    "INVALID_URGENCY_NAME",
                    "急缓等级名称必须是非空字符串",
                    "$.policy.urgency_weights",
                )
                continue
            if not _is_number(weight) or float(weight) <= 0:
                collector.add(
                    "INVALID_URGENCY_WEIGHT",
                    f"急缓等级 {key} 的权重必须是正有限数",
                    f"$.policy.urgency_weights.{key}",
                )
                continue
            weights[key] = float(weight)

    if watermark is None or max_latency is None or not weights:
        return None
    return Policy(
        watermark_ratio=float(watermark),
        max_latency_ms=float(max_latency),
        urgency_weights=weights,
    )


def _parse_institutions(
    value: Any, collector: _Collector
) -> dict[str, Institution] | None:
    if not isinstance(value, list):
        collector.add(
            "INVALID_INSTITUTIONS", "institutions 必须是数组", "$.institutions"
        )
        return None

    result: dict[str, Institution] = {}
    for index, item in enumerate(value):
        path = f"$.institutions[{index}]"
        if not isinstance(item, dict):
            collector.add(
                "INVALID_INSTITUTION", "机构条目必须是 JSON 对象", path
            )
            continue
        institution_id = item.get("id")
        if not _text(institution_id):
            collector.add(
                "INVALID_INSTITUTION_ID",
                "机构 id 必须是非空字符串",
                f"{path}.id",
            )
            continue
        if institution_id in result:
            collector.add(
                "DUPLICATE_INSTITUTION_ID",
                f"机构 id 重复: {institution_id}",
                f"{path}.id",
                institution_id=institution_id,
            )
            continue
        name = item.get("name", institution_id)
        if not _text(name):
            collector.add(
                "INVALID_INSTITUTION_NAME",
                "机构 name 必须是非空字符串",
                f"{path}.name",
                institution_id=institution_id,
            )
            name = institution_id
        compliance = item.get("required_compliance_level")
        if not _is_int(compliance) or compliance < 0:
            collector.add(
                "INVALID_INSTITUTION_COMPLIANCE",
                "required_compliance_level 必须是非负整数",
                f"{path}.required_compliance_level",
                institution_id=institution_id,
            )
            continue
        max_latency = item.get("max_latency_ms")
        if max_latency is not None and (
            not _is_number(max_latency) or float(max_latency) < 0
        ):
            collector.add(
                "INVALID_INSTITUTION_LATENCY",
                "机构 max_latency_ms 必须是非负有限数",
                f"{path}.max_latency_ms",
                institution_id=institution_id,
            )
            continue
        result[institution_id] = Institution(
            id=institution_id,
            name=name,
            required_compliance_level=int(compliance),
            max_latency_ms=(
                float(max_latency) if max_latency is not None else math.inf
            ),
        )
    return result


def _parse_regions(
    value: Any, policy: Policy | None, collector: _Collector
) -> dict[str, Region] | None:
    if not isinstance(value, list):
        collector.add("INVALID_REGIONS", "regions 必须是数组", "$.regions")
        return None

    result: dict[str, Region] = {}
    for index, item in enumerate(value):
        path = f"$.regions[{index}]"
        if not isinstance(item, dict):
            collector.add("INVALID_REGION", "区域条目必须是 JSON 对象", path)
            continue
        region_id = item.get("id")
        if not _text(region_id):
            collector.add(
                "INVALID_REGION_ID", "区域 id 必须是非空字符串", f"{path}.id"
            )
            continue
        if region_id in result:
            collector.add(
                "DUPLICATE_REGION_ID",
                f"区域 id 重复: {region_id}",
                f"{path}.id",
                region_id=region_id,
            )
            continue
        name = item.get("name", region_id)
        if not _text(name):
            collector.add(
                "INVALID_REGION_NAME",
                "区域 name 必须是非空字符串",
                f"{path}.name",
                region_id=region_id,
            )
            name = region_id
        capacity = item.get("capacity_bytes")
        if not _is_int(capacity) or capacity <= 0:
            collector.add(
                "INVALID_REGION_CAPACITY",
                "capacity_bytes 必须是正整数",
                f"{path}.capacity_bytes",
                region_id=region_id,
            )
            continue
        used = item.get("used_bytes")
        if not _is_int(used) or used < 0:
            collector.add(
                "INVALID_REGION_USED_BYTES",
                "used_bytes 必须是非负整数",
                f"{path}.used_bytes",
                region_id=region_id,
            )
            continue
        health = item.get("health")
        if health not in HEALTH_STATES:
            collector.add(
                "INVALID_REGION_HEALTH",
                f"health 必须是 {sorted(HEALTH_STATES)} 之一",
                f"{path}.health",
                region_id=region_id,
            )
            continue
        latency = item.get("latency_ms")
        if not _is_number(latency) or float(latency) < 0:
            collector.add(
                "INVALID_REGION_LATENCY",
                "latency_ms 必须是非负有限数",
                f"{path}.latency_ms",
                region_id=region_id,
            )
            continue
        compliance = item.get("compliance_level")
        if not _is_int(compliance) or compliance < 0:
            collector.add(
                "INVALID_REGION_COMPLIANCE",
                "compliance_level 必须是非负整数",
                f"{path}.compliance_level",
                region_id=region_id,
            )
            continue
        ratio_value = item.get("watermark_ratio")
        if ratio_value is None and policy is not None:
            ratio_value = policy.watermark_ratio
        if not _is_number(ratio_value) or not (0 < float(ratio_value) <= 1):
            collector.add(
                "INVALID_REGION_WATERMARK",
                "区域 watermark_ratio 必须满足 0 < ratio <= 1",
                f"{path}.watermark_ratio",
                region_id=region_id,
            )
            continue
        result[region_id] = Region(
            id=region_id,
            name=name,
            capacity_bytes=int(capacity),
            baseline_used_bytes=int(used),
            health=health,
            latency_ms=float(latency),
            compliance_level=int(compliance),
            watermark_ratio=float(ratio_value),
        )
    return result


def _parse_objects(
    value: Any,
    institutions: dict[str, Institution] | None,
    policy: Policy | None,
    collector: _Collector,
) -> dict[str, StorageObject] | None:
    if not isinstance(value, list):
        collector.add("INVALID_OBJECTS", "objects 必须是数组", "$.objects")
        return None

    result: dict[str, StorageObject] = {}
    for index, item in enumerate(value):
        path = f"$.objects[{index}]"
        if not isinstance(item, dict):
            collector.add("INVALID_OBJECT", "对象条目必须是 JSON 对象", path)
            continue
        object_id = item.get("id")
        if not _text(object_id):
            collector.add(
                "INVALID_OBJECT_ID", "对象 id 必须是非空字符串", f"{path}.id"
            )
            continue
        if object_id in result:
            collector.add(
                "DUPLICATE_OBJECT_ID",
                f"对象 id 重复: {object_id}",
                f"{path}.id",
                object_id=object_id,
            )
            continue
        institution_id = item.get("institution_id")
        if not _text(institution_id):
            collector.add(
                "INVALID_OBJECT_INSTITUTION",
                "对象 institution_id 必须是非空字符串",
                f"{path}.institution_id",
                object_id=object_id,
            )
            continue
        if institutions is not None and institution_id not in institutions:
            collector.add(
                "UNKNOWN_INSTITUTION",
                f"对象引用了不存在的机构: {institution_id}",
                f"{path}.institution_id",
                object_id=object_id,
                institution_id=institution_id,
            )
            continue
        category = item.get("test_category")
        if not _text(category):
            collector.add(
                "INVALID_TEST_CATEGORY",
                "test_category 必须是非空字符串",
                f"{path}.test_category",
                object_id=object_id,
                institution_id=institution_id,
            )
            continue
        urgency = item.get("urgency")
        if policy is None or urgency not in policy.urgency_weights:
            collector.add(
                "INVALID_URGENCY",
                f"急缓等级不合法: {urgency!r}",
                f"{path}.urgency",
                object_id=object_id,
                institution_id=institution_id,
            )
            continue
        obj_bytes = item.get("bytes")
        if not _is_int(obj_bytes) or obj_bytes < 0:
            collector.add(
                "INVALID_OBJECT_BYTES",
                "对象 bytes 必须是非负整数",
                f"{path}.bytes",
                object_id=object_id,
                institution_id=institution_id,
            )
            obj_bytes = 0
        created_at = item.get("created_at")
        expires_at = item.get("expires_at")
        if not _is_number(created_at):
            collector.add(
                "INVALID_CREATED_AT",
                "created_at 必须是有限数",
                f"{path}.created_at",
                object_id=object_id,
                institution_id=institution_id,
            )
            created_at = 0.0
        if not _is_number(expires_at):
            collector.add(
                "INVALID_EXPIRES_AT",
                "expires_at 必须是有限数",
                f"{path}.expires_at",
                object_id=object_id,
                institution_id=institution_id,
            )
            expires_at = 0.0
        if _is_number(created_at) and _is_number(expires_at):
            if float(expires_at) < float(created_at):
                collector.add(
                    "EXPIRES_BEFORE_CREATED",
                    "expires_at 不得早于 created_at",
                    f"{path}.expires_at",
                    object_id=object_id,
                    institution_id=institution_id,
                )
        digest = item.get("digest")
        if not _text(digest):
            collector.add(
                "INVALID_DIGEST",
                "digest 必须是非空字符串",
                f"{path}.digest",
                object_id=object_id,
                institution_id=institution_id,
            )
            digest = ""
        reference_count = item.get("reference_count", 1)
        if not _is_int(reference_count) or reference_count < 1:
            collector.add(
                "INVALID_REFERENCE_COUNT",
                "reference_count 必须是正整数",
                f"{path}.reference_count",
                object_id=object_id,
                institution_id=institution_id,
            )
            reference_count = 1
        result[object_id] = StorageObject(
            id=object_id,
            institution_id=institution_id,
            test_category=category,
            urgency=urgency,
            bytes=int(obj_bytes),
            created_at=float(created_at),
            expires_at=float(expires_at),
            digest=digest,
            reference_count=int(reference_count),
        )
    return result


def _parse_events(
    value: Any,
    objects: dict[str, StorageObject] | None,
    collector: _Collector,
) -> list[TimelineEvent]:
    if not isinstance(value, list):
        collector.add("INVALID_EVENTS", "events 必须是数组", "$.events")
        return []

    result: list[TimelineEvent] = []
    for index, item in enumerate(value):
        path = f"$.events[{index}]"
        if not isinstance(item, dict):
            collector.add(
                "INVALID_EVENT", "事件条目必须是 JSON 对象", path, event_index=index
            )
            continue
        at = item.get("at")
        event_type = item.get("type")
        object_id = item.get("object_id")
        institution_id = None
        if _text(object_id) and objects is not None and object_id in objects:
            institution_id = objects[object_id].institution_id
        if not _is_number(at):
            collector.add(
                "INVALID_EVENT_TIME",
                "事件 at 必须是有限数",
                f"{path}.at",
                object_id=object_id if _text(object_id) else None,
                institution_id=institution_id,
                event_index=index,
            )
            continue
        if event_type not in EVENT_TYPES:
            collector.add(
                "INVALID_EVENT_TYPE",
                "事件 type 必须是 access 或 expire",
                f"{path}.type",
                object_id=object_id if _text(object_id) else None,
                institution_id=institution_id,
                event_index=index,
            )
            continue
        if not _text(object_id):
            collector.add(
                "INVALID_EVENT_OBJECT",
                "事件 object_id 必须是非空字符串",
                f"{path}.object_id",
                event_index=index,
            )
            continue
        if objects is not None and object_id not in objects:
            collector.add(
                "UNKNOWN_EVENT_OBJECT",
                f"事件引用了不存在的对象: {object_id}",
                f"{path}.object_id",
                object_id=object_id,
                event_index=index,
            )
            continue
        delta = item.get("delta", 1)
        if not _is_int(delta) or delta < 1:
            collector.add(
                "INVALID_EVENT_DELTA",
                "事件 delta 必须是正整数",
                f"{path}.delta",
                object_id=object_id,
                institution_id=institution_id,
                event_index=index,
            )
            continue
        if objects is not None and object_id in objects:
            obj = objects[object_id]
            if float(at) < obj.created_at:
                collector.add(
                    "EVENT_BEFORE_CREATION",
                    "事件时刻不得早于对象创建时刻",
                    f"{path}.at",
                    object_id=object_id,
                    institution_id=institution_id,
                    event_index=index,
                )
                continue
        result.append(
            TimelineEvent(
                at=float(at),
                type=event_type,
                object_id=object_id,
                delta=int(delta),
                index=index,
            )
        )
    return result


def _parse_incidents(
    value: Any,
    regions: dict[str, Region] | None,
    collector: _Collector,
) -> list[Incident]:
    if not isinstance(value, list):
        collector.add("INVALID_INCIDENTS", "incidents 必须是数组", "$.incidents")
        return []

    result: list[Incident] = []
    for index, item in enumerate(value):
        path = f"$.incidents[{index}]"
        if not isinstance(item, dict):
            collector.add(
                "INVALID_INCIDENT", "故障条目必须是 JSON 对象", path, event_index=index
            )
            continue
        at = item.get("at")
        incident_type = item.get("type")
        region_id = item.get("region_id")
        if not _is_number(at):
            collector.add(
                "INVALID_INCIDENT_TIME",
                "incident.at 必须是有限数",
                f"{path}.at",
                region_id=region_id if _text(region_id) else None,
                event_index=index,
            )
            continue
        if incident_type not in INCIDENT_TYPES:
            collector.add(
                "INVALID_INCIDENT_TYPE",
                f"incident.type 必须是 {sorted(INCIDENT_TYPES)} 之一",
                f"{path}.type",
                region_id=region_id if _text(region_id) else None,
                event_index=index,
            )
            continue
        if not _text(region_id):
            collector.add(
                "INVALID_INCIDENT_REGION",
                "incident.region_id 必须是非空字符串",
                f"{path}.region_id",
                event_index=index,
            )
            continue
        if regions is not None and region_id not in regions:
            collector.add(
                "UNKNOWN_INCIDENT_REGION",
                f"故障引用了不存在的区域: {region_id}",
                f"{path}.region_id",
                region_id=region_id,
                event_index=index,
            )
            continue
        health = None
        watermark_ratio = None
        if incident_type in {"health_degraded", "health_change"}:
            health = item.get("health", "degraded")
            if health not in HEALTH_STATES:
                collector.add(
                    "INVALID_INCIDENT_HEALTH",
                    f"health 必须是 {sorted(HEALTH_STATES)} 之一",
                    f"{path}.health",
                    region_id=region_id,
                    event_index=index,
                )
                continue
        elif incident_type == "watermark_change":
            watermark_ratio = item.get("watermark_ratio")
            if not _is_number(watermark_ratio) or not (
                0 < float(watermark_ratio) <= 1
            ):
                collector.add(
                    "INVALID_INCIDENT_WATERMARK",
                    "watermark_ratio 必须满足 0 < ratio <= 1",
                    f"{path}.watermark_ratio",
                    region_id=region_id,
                    event_index=index,
                )
                continue
        else:
            # ``watermark_exceeded`` can be a pure trigger at the current
            # threshold. Supplying a ratio upgrades it to a threshold change.
            watermark_ratio = item.get("watermark_ratio")
            if watermark_ratio is not None and (
                not _is_number(watermark_ratio)
                or not (0 < float(watermark_ratio) <= 1)
            ):
                collector.add(
                    "INVALID_INCIDENT_WATERMARK",
                    "watermark_ratio 若提供则必须满足 0 < ratio <= 1",
                    f"{path}.watermark_ratio",
                    region_id=region_id,
                    event_index=index,
                )
                continue
        after_step = item.get("after_migration_step")
        if after_step is not None and (
            not _is_int(after_step) or after_step < 1
        ):
            collector.add(
                "INVALID_INCIDENT_STEP",
                "after_migration_step 必须是正整数",
                f"{path}.after_migration_step",
                region_id=region_id,
                event_index=index,
            )
            continue
        result.append(
            Incident(
                at=float(at),
                type=incident_type,
                region_id=region_id,
                health=health,
                watermark_ratio=(
                    float(watermark_ratio)
                    if watermark_ratio is not None
                    else None
                ),
                after_migration_step=(
                    int(after_step) if after_step is not None else None
                ),
                index=index,
            )
        )
    return result
