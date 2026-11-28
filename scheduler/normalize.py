"""三版本施工进度工程的装载与归一。

v1: 扁平施工段表 + 时刻秒 + 工期小时
v2: 嵌套施工段树 + 时刻毫秒 + 工期小时
v3: 扁平施工段表 + 时刻秒 + 工期天（按整日折算为小时）
"""
from .errors import (BusinessError, E_SCHEMA_STRUCTURE, E_SCHEMA_UNKNOWN,
                     E_UNIT_MISSING, E_UNIT_UNKNOWN, E_VALIDATION)
from .model import Activity, Project

TIME_UNITS = {"s": 1000, "ms": 1}
DURATION_UNITS = {"h": 1.0, "d": 24.0}
SCHEMAS = ("v1", "v2", "v3")


def _read_meta(doc):
    meta = doc.get("meta")
    if not isinstance(meta, dict):
        raise BusinessError(E_SCHEMA_UNKNOWN, "缺少 meta 段，无法识别 schema 版本")
    schema = meta.get("schema")
    if schema not in SCHEMAS:
        raise BusinessError(E_SCHEMA_UNKNOWN,
                            f"无法识别的 schema 版本: {schema!r}",
                            {"schema": schema})
    time_unit = meta.get("time_unit")
    duration_unit = meta.get("duration_unit")
    missing = [k for k, v in (("time_unit", time_unit),
                              ("duration_unit", duration_unit)) if v is None]
    if missing:
        raise BusinessError(E_UNIT_MISSING,
                            f"单位字段缺失: {', '.join(missing)}",
                            {"missing": missing})
    if time_unit not in TIME_UNITS:
        raise BusinessError(E_UNIT_UNKNOWN, f"无法识别的时刻单位: {time_unit!r}")
    if duration_unit not in DURATION_UNITS:
        raise BusinessError(E_UNIT_UNKNOWN, f"无法识别的工期单位: {duration_unit!r}")
    return schema, time_unit, duration_unit


def _flatten_table(doc):
    """扁平表版本（v1/v3）：segments 数组携带 zone/building/segment 层级字段。"""
    segments = doc.get("segments")
    if not isinstance(segments, list):
        raise BusinessError(E_SCHEMA_STRUCTURE, "扁平表版本缺少 segments 数组")
    out = []
    for i, seg in enumerate(segments):
        for f in ("zone", "building", "segment"):
            if f not in seg:
                raise BusinessError(E_SCHEMA_STRUCTURE,
                                    f"segments[{i}] 缺少层级字段 {f}")
        seg_id = f"{seg['zone']}/{seg['building']}/{seg['segment']}"
        acts = seg.get("activities")
        if not isinstance(acts, list):
            raise BusinessError(E_SCHEMA_STRUCTURE,
                                f"segments[{i}] 缺少 activities 数组")
        for j, a in enumerate(acts):
            out.append((seg_id, a, f"$.segments[{i}].activities[{j}]"))
    return out


def _flatten_tree(doc):
    """嵌套树版本（v2）：zones -> buildings -> segments -> activities。"""
    zones = doc.get("zones")
    if not isinstance(zones, list):
        raise BusinessError(E_SCHEMA_STRUCTURE, "嵌套树版本缺少 zones 数组")
    out = []
    for i, zone in enumerate(zones):
        zid = zone.get("id")
        buildings = zone.get("buildings")
        if zid is None or not isinstance(buildings, list):
            raise BusinessError(E_SCHEMA_STRUCTURE, f"zones[{i}] 结构不完整")
        for j, building in enumerate(buildings):
            bid = building.get("id")
            segments = building.get("segments")
            if bid is None or not isinstance(segments, list):
                raise BusinessError(E_SCHEMA_STRUCTURE,
                                    f"zones[{i}].buildings[{j}] 结构不完整")
            for k, segment in enumerate(segments):
                sid = segment.get("id")
                acts = segment.get("activities")
                if sid is None or not isinstance(acts, list):
                    raise BusinessError(
                        E_SCHEMA_STRUCTURE,
                        f"zones[{i}].buildings[{j}].segments[{k}] 结构不完整")
                seg_id = f"{zid}/{bid}/{sid}"
                for l, a in enumerate(acts):
                    out.append((seg_id, a,
                                f"$.zones[{i}].buildings[{j}].segments[{k}]"
                                f".activities[{l}]"))
    return out


def _build_activity(rec, schema, duration_unit):
    seg_id, raw, path = rec
    required = ("id", "name", "order", "duration", "resources")
    for f in required:
        if f not in raw:
            raise BusinessError(E_VALIDATION, f"工序记录缺少字段 {f}: {path}")
    duration_raw = raw["duration"]
    if duration_unit == "d" and float(duration_raw) != int(duration_raw):
        raise BusinessError(E_VALIDATION,
                            f"工序 {raw['id']} 工期以天表达时须为整数日: {path}")
    duration_hours = float(duration_raw) * DURATION_UNITS[duration_unit]
    if duration_hours <= 0:
        raise BusinessError(E_VALIDATION, f"工序 {raw['id']} 工期必须为正数")
    prov = {f: {"path": f"{path}.{f}", "schema": schema} for f in required}
    prov["predecessors"] = {"path": f"{path}.predecessors", "schema": schema}
    return Activity(activity_id=raw["id"], segment_id=seg_id,
                    name=raw["name"], order=int(raw["order"]),
                    duration_hours=duration_hours,
                    resources={k: float(v) for k, v in raw["resources"].items()},
                    predecessors=list(raw.get("predecessors", [])),
                    provenance=prov)


def normalize(doc):
    """把任一版本文档归一为 Project；无法识别时抛业务错误码，不猜测。"""
    schema, time_unit, duration_unit = _read_meta(doc)
    proj = doc.get("project")
    if not isinstance(proj, dict) or "id" not in proj or "planned_start" not in proj:
        raise BusinessError(E_VALIDATION, "project 段缺少 id 或 planned_start")
    planned_start_ms = int(proj["planned_start"]) * TIME_UNITS[time_unit]
    caps = doc.get("resource_caps")
    if not isinstance(caps, dict) or not caps:
        raise BusinessError(E_VALIDATION, "缺少 resource_caps 工种供应上限")
    flat = _flatten_tree(doc) if schema == "v2" else _flatten_table(doc)
    activities = {}
    for rec in flat:
        act = _build_activity(rec, schema, duration_unit)
        if act.activity_id in activities:
            raise BusinessError(E_VALIDATION, f"工序 id 重复: {act.activity_id}")
        activities[act.activity_id] = act
    for act in activities.values():
        for p in act.predecessors:
            if p not in activities:
                raise BusinessError(E_VALIDATION,
                                    f"工序 {act.activity_id} 的前驱不存在: {p}")
    return Project(project_id=proj["id"], name=proj.get("name", proj["id"]),
                   schema=schema, planned_start_ms=planned_start_ms,
                   activities=activities, caps=dict(caps),
                   provenance={"planned_start": {"path": "$.project.planned_start",
                                                 "schema": schema}})
