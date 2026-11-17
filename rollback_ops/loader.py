"""三版本变更单加载、强校验与归一。任何格式问题整体拒绝并抛 E_FORMAT，附带字段路径。"""

from __future__ import annotations

import json

from .errors import E_FORMAT, OpsError
from .models import (
    CURRENT_FORMAT_VERSION,
    DEFAULT_CATEGORY_V1,
    DEFAULT_RISK_WEIGHT,
    DEFAULT_SOURCE_SYSTEM,
    SUPPORTED_CATEGORIES,
    SUPPORTED_FORMAT_VERSIONS,
    ChangeItem,
    NormalizedTicket,
    RollbackPlan,
    RollbackStep,
)

_SCALAR_TYPES = (str, int, float, bool, type(None))


def _err(message, path, **details):
    return OpsError(E_FORMAT, message, {"path": path, **details})


def _require(cond, message, path, **details):
    if not cond:
        raise _err(message, path, **details)


def _check_scalar(value, path):
    _require(isinstance(value, _SCALAR_TYPES), "value must be a JSON scalar", path,
             actual=type(value).__name__)


def detect_format_version(raw) -> int:
    _require(isinstance(raw, dict), "ticket document must be a JSON object", "/")
    _require("format_version" in raw, "missing required field", "/format_version")
    version = raw["format_version"]
    _require(isinstance(version, int) and not isinstance(version, bool),
             "format_version must be an integer", "/format_version")
    _require(version in SUPPORTED_FORMAT_VERSIONS, "unknown format_version",
             "/format_version", actual=version, supported=list(SUPPORTED_FORMAT_VERSIONS))
    return version


def _normalize_item(raw_item, index, version) -> ChangeItem:
    base = f"/changes/{index}"
    _require(isinstance(raw_item, dict), "change item must be an object", base)

    for name in ("key", "old_value", "new_value"):
        _require(name in raw_item, "missing required field", f"{base}/{name}")
    key = raw_item["key"]
    _require(isinstance(key, str) and key, "key must be a non-empty string", f"{base}/key")
    _check_scalar(raw_item["old_value"], f"{base}/old_value")
    _check_scalar(raw_item["new_value"], f"{base}/new_value")

    if version >= 2:
        _require("category" in raw_item, "missing required field", f"{base}/category")
        category = raw_item["category"]
        _require(category in SUPPORTED_CATEGORIES, "unsupported category",
                 f"{base}/category", actual=category, supported=list(SUPPORTED_CATEGORIES))
        _require("impacted_services" in raw_item, "missing required field",
                 f"{base}/impacted_services")
        services = raw_item["impacted_services"]
        _require(isinstance(services, list)
                 and all(isinstance(s, str) for s in services),
                 "impacted_services must be a list of strings", f"{base}/impacted_services")
        impacted = tuple(sorted(services))
    else:
        category = DEFAULT_CATEGORY_V1
        impacted = ()

    if version >= 3:
        _require("source_system" in raw_item, "missing required field", f"{base}/source_system")
        source_system = raw_item["source_system"]
        _require(isinstance(source_system, str) and source_system,
                 "source_system must be a non-empty string", f"{base}/source_system")
        _require("risk_weight" in raw_item, "missing required field", f"{base}/risk_weight")
        weight = raw_item["risk_weight"]
        _require(isinstance(weight, int) and not isinstance(weight, bool) and weight >= 0,
                 "risk_weight must be a non-negative integer", f"{base}/risk_weight")
    else:
        source_system = DEFAULT_SOURCE_SYSTEM[version]
        weight = DEFAULT_RISK_WEIGHT

    return ChangeItem(
        key=key,
        old_value=raw_item["old_value"],
        new_value=raw_item["new_value"],
        category=category,
        impacted_services=impacted,
        source_system=source_system,
        risk_weight=weight,
        source_format_version=version,
    )


def normalize_ticket(raw) -> NormalizedTicket:
    version = detect_format_version(raw)
    _require("ticket_id" in raw, "missing required field", "/ticket_id")
    _require(isinstance(raw["ticket_id"], str) and raw["ticket_id"],
             "ticket_id must be a non-empty string", "/ticket_id")
    revision = raw.get("revision", 1)
    _require(isinstance(revision, int) and not isinstance(revision, bool) and revision >= 1,
             "revision must be a positive integer", "/revision")
    _require("changes" in raw, "missing required field", "/changes")
    _require(isinstance(raw["changes"], list), "changes must be a list", "/changes")

    items = [_normalize_item(it, i, version) for i, it in enumerate(raw["changes"])]
    seen = {}
    for i, it in enumerate(items):
        if it.key in seen:
            raise _err("duplicate change key", f"/changes/{i}/key",
                       key=it.key, first_index=seen[it.key])
        seen[it.key] = i
    items.sort(key=lambda it: it.key)
    return NormalizedTicket(
        ticket_id=raw["ticket_id"],
        revision=revision,
        source_format_version=version,
        items=tuple(items),
    )


def load_ticket(path) -> NormalizedTicket:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except json.JSONDecodeError as exc:
        raise _err(f"invalid JSON: {exc.msg}", "/",
                   line=exc.lineno, column=exc.colno) from exc
    return normalize_ticket(raw)


def load_plan(path) -> RollbackPlan:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except json.JSONDecodeError as exc:
        raise _err(f"invalid JSON: {exc.msg}", "/",
                   line=exc.lineno, column=exc.colno) from exc
    return normalize_plan(raw)


def normalize_plan(raw) -> RollbackPlan:
    _require(isinstance(raw, dict), "plan document must be a JSON object", "/")
    _require("plan_version" in raw, "missing required field", "/plan_version")
    _require(isinstance(raw["plan_version"], int)
             and not isinstance(raw["plan_version"], bool) and raw["plan_version"] >= 1,
             "plan_version must be a positive integer", "/plan_version")
    _require("steps" in raw, "missing required field", "/steps")
    _require(isinstance(raw["steps"], list), "steps must be a list", "/steps")

    steps = []
    for i, raw_step in enumerate(raw["steps"]):
        base = f"/steps/{i}"
        _require(isinstance(raw_step, dict), "step must be an object", base)
        for name in ("step_id", "change_key", "action"):
            _require(name in raw_step, "missing required field", f"{base}/{name}")
            _require(isinstance(raw_step[name], str) and raw_step[name],
                     f"{name} must be a non-empty string", f"{base}/{name}")
        deps = raw_step.get("depends_on", [])
        _require(isinstance(deps, list) and all(isinstance(d, str) for d in deps),
                 "depends_on must be a list of strings", f"{base}/depends_on")
        steps.append(RollbackStep(
            step_id=raw_step["step_id"],
            change_key=raw_step["change_key"],
            action=raw_step["action"],
            depends_on=tuple(sorted(set(deps))),
        ))
    steps.sort(key=lambda s: s.step_id)
    return RollbackPlan(plan_version=raw["plan_version"], steps=tuple(steps))
