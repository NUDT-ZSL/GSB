from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml

from .errors import (
    E_CLOCK,
    E_DUP_APP,
    E_DUP_APPROVAL,
    E_DUP_CLUSTER,
    E_DUP_REGION,
    E_PARSE,
    E_REF_APP,
    E_REF_CLUSTER,
    E_REF_CLUSTER_REGION,
    E_REF_REGION,
    EngineError,
    FileError,
    LimitError,
    ReferenceError,
    SchemaError,
)
from .model import (
    CONFIG_FIELDS,
    AdmissionRules,
    AppConfig,
    Approval,
    Bundle,
    ClockScript,
    ClockStep,
    ClusterMember,
    ClusterOverlay,
    RegionOverlay,
)
from .util import (
    require_bool,
    require_int,
    require_list,
    require_mapping,
    require_str,
)

DOCUMENT_NAMES = (
    "global",
    "regions",
    "clusters",
    "inventory",
    "rules",
    "approvals",
    "clock",
)

DEFAULT_DOCUMENTS = {
    "global": "global.yaml",
    "regions": "regions.json",
    "clusters": "clusters.yaml",
    "inventory": "inventory.json",
    "rules": "rules.yaml",
    "approvals": "approvals.json",
    "clock": "clock.json",
}


def _read_document(path: Path) -> Any:
    if not path.is_file():
        raise FileError("document does not exist", path=str(path))
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise FileError(
            "cannot read document", path=str(path), reason=str(exc)
        ) from exc
    suffix = path.suffix.lower()
    try:
        if suffix == ".json":
            return json.loads(text)
        if suffix in (".yaml", ".yml"):
            return yaml.safe_load(text)
    except (json.JSONDecodeError, yaml.YAMLError) as exc:
        raise FileError(
            "cannot parse document",
            path=str(path),
            reason=str(exc),
            error_code=E_PARSE,
        ) from exc
    raise FileError(
        "unsupported document extension; use .json, .yaml or .yml",
        path=str(path),
    )


def _resolve_documents(samples_dir: Path) -> dict[str, Path]:
    manifest_path = samples_dir / "manifest.json"
    if manifest_path.is_file():
        manifest = require_mapping(
            _read_document(manifest_path), f"{manifest_path}:manifest"
        )
        raw_documents = require_mapping(
            manifest.get("documents", manifest), f"{manifest_path}:documents"
        )
        documents: dict[str, Path] = {}
        for name in DOCUMENT_NAMES:
            if name not in raw_documents:
                raise SchemaError(
                    "manifest is missing a required document",
                    document=name,
                    path=str(manifest_path),
                )
            relative = require_str(
                raw_documents[name], f"{manifest_path}:documents.{name}"
            )
            path = Path(relative)
            documents[name] = path if path.is_absolute() else samples_dir / path
        return documents

    documents = {
        name: samples_dir / filename
        for name, filename in DEFAULT_DOCUMENTS.items()
    }
    missing = [str(path) for path in documents.values() if not path.is_file()]
    if missing:
        raise FileError(
            "samples directory is missing documents and has no manifest.json",
            samples_dir=str(samples_dir),
            missing=sorted(missing),
        )
    return documents


def load_bundle(samples_dir: str | Path) -> Bundle:
    root = Path(samples_dir)
    if not root.is_dir():
        raise FileError("samples directory does not exist", path=str(root))
    paths = _resolve_documents(root)
    raw = {name: _read_document(path) for name, path in paths.items()}
    return parse_bundle(raw)


def parse_bundle(raw: dict[str, Any]) -> Bundle:
    apps = _parse_apps(raw.get("global"))
    inventory = _parse_inventory(raw.get("inventory"))
    regions = _parse_regions(raw.get("regions"), apps)
    cluster_overlays = _parse_cluster_overlays(raw.get("clusters"), apps)
    rules = _parse_rules(raw.get("rules"))
    approvals = _parse_approvals(
        raw.get("approvals"), apps, inventory, rules
    )
    clock = _parse_clock(raw.get("clock"), len(inventory))

    inventory_regions = {member.region for member in inventory.values()}
    for region in sorted(regions):
        if region not in inventory_regions:
            raise ReferenceError(
                E_REF_REGION,
                "region overlay references a region absent from inventory",
                region=region,
            )

    for cluster_id in sorted(cluster_overlays):
        overlay = cluster_overlays[cluster_id]
        member = inventory.get(cluster_id)
        if member is None:
            raise ReferenceError(
                E_REF_CLUSTER,
                "cluster overlay references a cluster absent from inventory",
                cluster=cluster_id,
                declared_region=overlay.region,
            )
        if member.region != overlay.region:
            raise ReferenceError(
                E_REF_CLUSTER_REGION,
                "cluster overlay declares a region different from inventory",
                cluster=cluster_id,
                declared_region=overlay.region,
                inventory_region=member.region,
            )
        if (
            overlay.replicas_limit is not None
            and overlay.replicas_limit > member.max_replicas
        ):
            raise LimitError(
                "cluster overlay replica limit exceeds inventory capacity",
                cluster=cluster_id,
                replicas_limit=overlay.replicas_limit,
                max_replicas=member.max_replicas,
            )
        if (
            overlay.resource_quota is not None
            and overlay.resource_quota > member.max_resources
        ):
            raise LimitError(
                "cluster overlay resource quota exceeds inventory capacity",
                cluster=cluster_id,
                resource_quota=overlay.resource_quota,
                max_resources=member.max_resources,
            )

    for approval in approvals.values():
        if approval.subject not in {
            f"apps.{app}.{field_name}"
            for app in apps
            for field_name in CONFIG_FIELDS
        }:
            app, _, field_name = approval.subject.partition(".")
            del app, field_name
            raise SchemaError(
                "approval subject must be apps.<app>.<field>",
                approval=approval.approval_id,
                subject=approval.subject,
            )

    return Bundle(
        apps=apps,
        regions=regions,
        cluster_overlays=cluster_overlays,
        inventory=inventory,
        rules=rules,
        approvals=approvals,
        clock=clock,
        documents={name: raw[name] for name in sorted(raw)},
    )


def _parse_apps(raw: Any) -> dict[str, AppConfig]:
    if isinstance(raw, list):
        items = raw
    else:
        data = require_mapping(raw, "global")
        items = data.get("apps", data.get("global", data))
    if isinstance(items, dict):
        items = list(items.values())
    app_items = require_list(items, "global.apps")
    apps: dict[str, AppConfig] = {}
    for index, item in enumerate(app_items):
        where = f"global.apps[{index}]"
        app_raw = require_mapping(item, where)
        name = require_str(
            app_raw.get("name", app_raw.get("app")), f"{where}.name"
        )
        if name in apps:
            raise EngineError(
                E_DUP_APP,
                "application name must be unique in the global layer",
                details={"app": name},
            )
        image = require_str(app_raw.get("image"), f"{where}.image")
        replicas = require_int(
            app_raw.get("replicas"), f"{where}.replicas", positive=True
        )
        resources = require_int(
            app_raw.get("resources"), f"{where}.resources", positive=True
        )
        timeout_ms = require_int(
            app_raw.get("timeout_ms"), f"{where}.timeout_ms", positive=True
        )
        feature_flag = require_bool(
            app_raw.get("feature_flag"), f"{where}.feature_flag"
        )
        apps[name] = AppConfig(
            name=name,
            image=image,
            replicas=replicas,
            resources=resources,
            timeout_ms=timeout_ms,
            feature_flag=feature_flag,
        )
    if not apps:
        raise SchemaError("global layer must contain at least one application")
    return apps


def _parse_inventory(raw: Any) -> dict[str, ClusterMember]:
    if isinstance(raw, list):
        items = raw
    else:
        data = require_mapping(raw, "inventory")
        items = data.get("clusters", data)
    if isinstance(items, dict):
        items = list(items.values())
    cluster_items = require_list(items, "inventory.clusters")
    inventory: dict[str, ClusterMember] = {}
    for index, item in enumerate(cluster_items):
        where = f"inventory.clusters[{index}]"
        member_raw = require_mapping(item, where)
        cluster_id = require_str(
            member_raw.get("cluster", member_raw.get("name")), f"{where}.cluster"
        )
        if cluster_id in inventory:
            raise EngineError(
                E_DUP_CLUSTER,
                "cluster name must be unique in inventory",
                details={"cluster": cluster_id},
            )
        region = require_str(member_raw.get("region"), f"{where}.region")
        healthy = require_bool(member_raw.get("healthy"), f"{where}.healthy")
        max_replicas = require_int(
            member_raw.get("max_replicas"),
            f"{where}.max_replicas",
            positive=True,
        )
        max_resources = require_int(
            member_raw.get("max_resources"),
            f"{where}.max_resources",
            positive=True,
        )
        inventory[cluster_id] = ClusterMember(
            cluster=cluster_id,
            region=region,
            healthy=healthy,
            max_replicas=max_replicas,
            max_resources=max_resources,
        )
    if not inventory:
        raise SchemaError("inventory must contain at least one cluster")
    return inventory


def _parse_overrides(
    raw: Any,
    where: str,
    apps: dict[str, AppConfig],
) -> dict[str, dict[str, Any]]:
    overrides_raw = require_mapping(raw if raw is not None else {}, where)
    overrides: dict[str, dict[str, Any]] = {}
    for app_name in sorted(overrides_raw):
        if app_name not in apps:
            raise ReferenceError(
                E_REF_APP,
                "override references an application absent from the global layer",
                app=app_name,
                where=where,
            )
        values_raw = require_mapping(
            overrides_raw[app_name], f"{where}.{app_name}"
        )
        values: dict[str, Any] = {}
        for field_name in sorted(values_raw):
            if field_name not in CONFIG_FIELDS:
                raise SchemaError(
                    "override field is not part of the configuration contract",
                    field=field_name,
                    allowed=list(CONFIG_FIELDS),
                )
            values[field_name] = _validate_field_value(
                field_name, values_raw[field_name], f"{where}.{app_name}.{field_name}"
            )
        overrides[app_name] = values
    return overrides


def _parse_regions(
    raw: Any, apps: dict[str, AppConfig]
) -> dict[str, RegionOverlay]:
    if isinstance(raw, list):
        items = raw
    else:
        data = require_mapping(raw, "regions")
        items = data.get("regions", data.get("overlays", data))
    if isinstance(items, dict):
        items = list(items.values())
    region_items = require_list(items, "regions.regions")
    regions: dict[str, RegionOverlay] = {}
    for index, item in enumerate(region_items):
        where = f"regions.regions[{index}]"
        region_raw = require_mapping(item, where)
        region = require_str(
            region_raw.get("region", region_raw.get("name")), f"{where}.region"
        )
        if region in regions:
            raise EngineError(
                E_DUP_REGION,
                "region overlay name must be unique",
                details={"region": region},
            )
        allow_diff = require_bool(
            region_raw.get(
                "allow_diff_overrides",
                region_raw.get("allow_diff", region_raw.get("allow_region_diff")),
            ),
            f"{where}.allow_diff_overrides",
        )
        overrides = _parse_overrides(
            region_raw.get("overrides", {}), f"{where}.overrides", apps
        )
        regions[region] = RegionOverlay(
            region=region,
            allow_diff_overrides=allow_diff,
            overrides=overrides,
        )
    return regions


def _parse_cluster_overlays(
    raw: Any, apps: dict[str, AppConfig]
) -> dict[str, ClusterOverlay]:
    if isinstance(raw, list):
        items = raw
    else:
        data = require_mapping(raw, "clusters")
        items = data.get("clusters", data.get("overlays", data))
    if isinstance(items, dict):
        items = list(items.values())
    cluster_items = require_list(items, "clusters.clusters")
    overlays: dict[str, ClusterOverlay] = {}
    for index, item in enumerate(cluster_items):
        where = f"clusters.clusters[{index}]"
        cluster_raw = require_mapping(item, where)
        cluster_id = require_str(
            cluster_raw.get("cluster", cluster_raw.get("name")),
            f"{where}.cluster",
        )
        if cluster_id in overlays:
            raise EngineError(
                E_DUP_CLUSTER,
                "cluster overlay name must be unique",
                details={"cluster": cluster_id},
            )
        region = require_str(cluster_raw.get("region"), f"{where}.region")
        replicas_limit = cluster_raw.get("replicas_limit")
        if replicas_limit is not None:
            replicas_limit = require_int(
                replicas_limit, f"{where}.replicas_limit", positive=True
            )
        resource_quota = cluster_raw.get("resource_quota")
        if resource_quota is not None:
            resource_quota = require_int(
                resource_quota, f"{where}.resource_quota", positive=True
            )
        overrides = _parse_overrides(
            cluster_raw.get("overrides", {}), f"{where}.overrides", apps
        )
        overlays[cluster_id] = ClusterOverlay(
            cluster=cluster_id,
            region=region,
            overrides=overrides,
            replicas_limit=replicas_limit,
            resource_quota=resource_quota,
        )
    return overlays


def _parse_rules(raw: Any) -> AdmissionRules:
    data = require_mapping(raw, "rules")
    approval = data.get("approval", {})
    if approval is not None:
        approval = require_mapping(approval, "rules.approval")
    else:
        approval = {}
    window = data.get("change_window", {})
    if window is not None:
        window = require_mapping(window, "rules.change_window")
    else:
        window = {}

    require_approval = data.get(
        "require_approval_for_diff",
        approval.get(
            "require_for_diff",
            approval.get(
                "require_approval",
                data.get("require_approval", False),
            ),
        ),
    )
    require_approval = require_bool(
        require_approval, "rules.require_approval_for_diff"
    )
    ttl_ms = require_int(
        data.get(
            "approval_ttl_ms",
            approval.get("ttl_ms", approval.get("validity_ms")),
        ),
        "rules.approval_ttl_ms",
        positive=True,
    )
    window_start = require_int(
        data.get("window_start_ms", window.get("start_ms")),
        "rules.change_window.start_ms",
        nonnegative=True,
    )
    window_end = require_int(
        data.get("window_end_ms", window.get("end_ms")),
        "rules.change_window.end_ms",
        positive=True,
    )
    if window_end <= window_start:
        raise LimitError(
            "change window end must be greater than start",
            start_ms=window_start,
            end_ms=window_end,
        )
    period_raw = data.get("window_period_ms", window.get("period_ms"))
    period = (
        None
        if period_raw is None
        else require_int(
            period_raw, "rules.change_window.period_ms", positive=True
        )
    )
    max_parallel = require_int(
        data.get("max_parallel"), "rules.max_parallel", positive=True
    )
    budget = require_int(
        data.get("round_budget_ms", data.get("time_budget_ms")),
        "rules.round_budget_ms",
        positive=True,
    )
    return AdmissionRules(
        require_approval_for_diff=require_approval,
        approval_ttl_ms=ttl_ms,
        window_start_ms=window_start,
        window_end_ms=window_end,
        window_period_ms=period,
        max_parallel=max_parallel,
        round_budget_ms=budget,
    )


def _parse_approvals(
    raw: Any,
    apps: dict[str, AppConfig],
    inventory: dict[str, ClusterMember],
    rules: AdmissionRules,
) -> dict[str, Approval]:
    if isinstance(raw, list):
        items = raw
    else:
        data = require_mapping(raw, "approvals")
        items = data.get("approvals", data)
    if isinstance(items, dict):
        items = list(items.values())
    approval_items = require_list(items, "approvals.approvals")
    approvals: dict[str, Approval] = {}
    subject_fields = {
        f"apps.{app}.{field_name}": (app, field_name)
        for app in apps
        for field_name in CONFIG_FIELDS
    }
    for index, item in enumerate(approval_items):
        where = f"approvals.approvals[{index}]"
        approval_raw = require_mapping(item, where)
        approval_id = require_str(
            approval_raw.get("id", approval_raw.get("approval_id")),
            f"{where}.id",
        )
        if approval_id in approvals:
            raise EngineError(
                E_DUP_APPROVAL,
                "approval id must be unique",
                details={"approval": approval_id},
            )
        subject = require_str(
            approval_raw.get("subject", approval_raw.get("target")),
            f"{where}.subject",
        )
        if subject not in subject_fields:
            raise ReferenceError(
                E_REF_APP,
                "approval subject references an unknown app or key",
                approval=approval_id,
                subject=subject,
            )
        scope = require_mapping(approval_raw.get("scope"), f"{where}.scope")
        if not scope or ("region" not in scope and "cluster" not in scope):
            raise SchemaError(
                "approval scope must contain region and/or cluster",
                approval=approval_id,
            )
        scope_region = require_str(
            scope.get("region", "*"), f"{where}.scope.region"
        )
        scope_cluster = require_str(
            scope.get("cluster", "*"), f"{where}.scope.cluster"
        )
        if scope_region != "*" and scope_region not in {
            member.region for member in inventory.values()
        }:
            raise ReferenceError(
                E_REF_REGION,
                "approval scope references an unknown region",
                approval=approval_id,
                region=scope_region,
            )
        if scope_cluster != "*":
            member = inventory.get(scope_cluster)
            if member is None:
                raise ReferenceError(
                    E_REF_CLUSTER,
                    "approval scope references an unknown cluster",
                    approval=approval_id,
                    cluster=scope_cluster,
                )
            if scope_region != "*" and member.region != scope_region:
                raise ReferenceError(
                    E_REF_CLUSTER_REGION,
                    "approval scope cluster belongs to another region",
                    approval=approval_id,
                    cluster=scope_cluster,
                    declared_region=scope_region,
                    inventory_region=member.region,
                )
        if "value" not in approval_raw:
            raise SchemaError(
                "approval must pin the approved value",
                approval=approval_id,
            )
        app_name, field_name = subject_fields[subject]
        value = _validate_field_value(
            field_name, approval_raw["value"], f"{where}.value"
        )
        issued_ms = require_int(
            approval_raw.get("issued_ms", approval_raw.get("valid_from_ms")),
            f"{where}.issued_ms",
            nonnegative=True,
        )
        valid_until_ms = require_int(
            approval_raw.get("valid_until_ms"),
            f"{where}.valid_until_ms",
            positive=True,
        )
        if valid_until_ms <= issued_ms:
            raise LimitError(
                "approval valid_until_ms must be greater than issued_ms",
                approval=approval_id,
                issued_ms=issued_ms,
                valid_until_ms=valid_until_ms,
            )
        # The configured TTL caps the approval's own validity interval.
        if valid_until_ms > issued_ms + rules.approval_ttl_ms:
            valid_until_ms = issued_ms + rules.approval_ttl_ms
        approvals[approval_id] = Approval(
            approval_id=approval_id,
            subject=subject,
            scope_region=scope_region,
            scope_cluster=scope_cluster,
            value=value,
            issued_ms=issued_ms,
            valid_until_ms=valid_until_ms,
        )
        del app_name
    return approvals


def _parse_clock(raw: Any, cluster_count: int) -> ClockScript:
    data = require_mapping(raw, "clock")
    start_ms = require_int(
        data.get("start_ms"), "clock.start_ms", nonnegative=True
    )
    steps_raw = require_list(data.get("steps"), "clock.steps")
    steps: list[ClockStep] = []
    for index, item in enumerate(steps_raw):
        where = f"clock.steps[{index}]"
        step_raw = require_mapping(item, where)
        event = require_str(step_raw.get("event"), f"{where}.event")
        if event not in {"dispatch_start", "dispatch_finish"}:
            raise SchemaError(
                "clock step event must be dispatch_start or dispatch_finish",
                where=where,
                event=event,
            )
        advance_ms = require_int(
            step_raw.get("advance_ms"),
            f"{where}.advance_ms",
            nonnegative=True,
        )
        steps.append(ClockStep(event=event, advance_ms=advance_ms))
    if not steps:
        raise SchemaError("clock step table must not be empty")
    if len(steps) % 2 != 0:
        raise SchemaError(
            "clock step table must contain complete start/finish pairs",
            step_count=len(steps),
        )
    for index in range(0, len(steps), 2):
        if steps[index].event != "dispatch_start":
            raise SchemaError(
                "clock step pair must begin with dispatch_start",
                index=index,
                event=steps[index].event,
            )
        if steps[index + 1].event != "dispatch_finish":
            raise SchemaError(
                "clock step pair must end with dispatch_finish",
                index=index + 1,
                event=steps[index + 1].event,
            )
    needed = max(2, cluster_count * 2)
    if len(steps) < needed:
        raise EngineError(
            E_CLOCK,
            "clock step table is too short for all member clusters",
            details={"step_count": len(steps), "required": needed},
        )
    return ClockScript(start_ms=start_ms, steps=tuple(steps))


def _validate_field_value(field_name: str, value: Any, where: str) -> Any:
    if field_name == "image":
        return require_str(value, where)
    if field_name in {"replicas", "resources", "timeout_ms"}:
        return require_int(value, where, positive=True)
    if field_name == "feature_flag":
        return require_bool(value, where)
    raise SchemaError("unknown configuration field", field=field_name)

