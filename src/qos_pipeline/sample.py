"""Sample loading, schema validation, classifier resolution, and digests."""

from __future__ import annotations

import json
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from typing import Any, Mapping

from .errors import QosError, fail
from .models import ArrivalEvent, ClassConfig, ClassifierRule, Sample, WredConfig
from .serialization import canonical_bytes, digest_bytes


FIVE_TUPLE_FIELDS = ("src_ip", "dst_ip", "src_port", "dst_port", "protocol")


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON number: {value}")


def _load_json_bytes(data: bytes, label: str) -> Any:
    try:
        return json.loads(
            data.decode("utf-8"),
            parse_float=Decimal,
            parse_int=int,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise QosError(
            "E_JSON",
            f"{label} is not valid JSON",
            {"file": label, "reason": str(exc)},
        ) from exc


def _read_json(path: Path, label: str) -> tuple[bytes, Any]:
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise QosError(
            "E_JSON",
            f"cannot read {label}",
            {"file": label, "path": str(path), "reason": str(exc)},
        ) from exc
    return data, _load_json_bytes(data, label)


def _is_int(value: Any) -> bool:
    return type(value) is int


def _positive_int(value: Any, path: str, *, allow_zero: bool = False) -> int:
    if not _is_int(value) or (value < 0 if allow_zero else value <= 0):
        comparator = "non-negative" if allow_zero else "positive"
        fail(
            "E_CLASS_PARAM" if path.startswith("classes") else "E_MANIFEST",
            f"{path} must be a {comparator} integer",
            path=path,
            value=value,
        )
    return value


def _require_mapping(value: Any, path: str, code: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        fail(code, f"{path} must be an object", path=path)
    return value


def _require_list(value: Any, path: str, code: str) -> list[Any]:
    if not isinstance(value, list):
        fail(code, f"{path} must be an array", path=path)
    return value


def _validate_five_tuple(value: Any, path: str, code: str) -> tuple[tuple[str, Any], ...]:
    mapping = _require_mapping(value, path, code)
    missing = [field for field in FIVE_TUPLE_FIELDS if field not in mapping]
    if missing:
        fail(code, f"{path} is missing five-tuple fields", path=path, missing=missing)
    for field in ("src_ip", "dst_ip"):
        if not isinstance(mapping[field], str) or not mapping[field]:
            fail(code, f"{path}.{field} must be a non-empty string", path=path)
    for field in ("src_port", "dst_port"):
        port = mapping[field]
        if not _is_int(port) or not 0 <= port <= 65535:
            fail(code, f"{path}.{field} must be an integer in [0, 65535]", path=path)
    protocol = mapping["protocol"]
    if isinstance(protocol, str):
        if not protocol:
            fail(code, f"{path}.protocol must not be empty", path=path)
    elif not _is_int(protocol) or not 0 <= protocol <= 255:
        fail(code, f"{path}.protocol must be a string or integer in [0, 255]", path=path)
    return tuple((field, mapping[field]) for field in FIVE_TUPLE_FIELDS)


def _parse_wred(value: Any, path: str, queue_depth: int) -> WredConfig | None:
    if value is None:
        return None
    mapping = _require_mapping(value, path, "E_CLASS_PARAM")
    min_bytes = _positive_int(mapping.get("min_bytes"), f"{path}.min_bytes", allow_zero=True)
    max_bytes = _positive_int(mapping.get("max_bytes"), f"{path}.max_bytes")
    maximum = _positive_int(
        mapping.get("max_probability_ppm"), f"{path}.max_probability_ppm", allow_zero=True
    )
    if min_bytes > max_bytes:
        fail("E_CLASS_PARAM", f"{path}.min_bytes cannot exceed max_bytes", path=path)
    if max_bytes > queue_depth:
        fail(
            "E_CLASS_PARAM",
            f"{path}.max_bytes cannot exceed queue_depth_bytes",
            path=path,
        )
    if maximum > 1_000_000:
        fail(
            "E_CLASS_PARAM",
            f"{path}.max_probability_ppm must be in [0, 1000000]",
            path=path,
        )
    return WredConfig(min_bytes=min_bytes, max_bytes=max_bytes, max_probability_ppm=maximum)


def _parse_classes(config_doc: Any) -> tuple[ClassConfig, ...]:
    root = _require_mapping(config_doc, "config", "E_CLASS_SCHEMA")
    raw_classes = _require_list(root.get("classes"), "config.classes", "E_CLASS_SCHEMA")
    if not raw_classes:
        fail("E_CLASS_SCHEMA", "config.classes must not be empty", path="config.classes")

    partial: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw in enumerate(raw_classes):
        path = f"classes[{index}]"
        item = _require_mapping(raw, path, "E_CLASS_SCHEMA")
        name = item.get("name")
        if not isinstance(name, str) or not name:
            fail("E_CLASS_SCHEMA", f"{path}.name must be a non-empty string", path=path)
        if name in seen:
            fail("E_CLASS_DUPLICATE", f"duplicate class name {name!r}", path=path, class_name=name)
        seen.add(name)
        parent = item.get("parent")
        if parent is not None and (not isinstance(parent, str) or not parent):
            fail("E_CLASS_SCHEMA", f"{path}.parent must be null or a class name", path=path)
        cir = _positive_int(item.get("cir_bps"), f"{path}.cir_bps")
        pir = _positive_int(item.get("pir_bps"), f"{path}.pir_bps")
        cbs = _positive_int(
            item.get("committed_burst_bytes"), f"{path}.committed_burst_bytes"
        )
        pbs = _positive_int(item.get("peak_burst_bytes"), f"{path}.peak_burst_bytes")
        queue_depth = _positive_int(item.get("queue_depth_bytes"), f"{path}.queue_depth_bytes")
        policy = item.get("drop_policy")
        if policy not in ("tail", "wred"):
            fail(
                "E_CLASS_PARAM",
                f"{path}.drop_policy must be 'tail' or 'wred'",
                path=path,
                value=policy,
            )
        if pir < cir:
            fail(
                "E_RATE_ORDER",
                f"{path}.pir_bps must be at least cir_bps",
                path=path,
                cir_bps=cir,
                pir_bps=pir,
            )
        if pbs < cbs:
            fail(
                "E_PEAK_BURST",
                f"{path}.peak_burst_bytes must be at least committed_burst_bytes",
                path=path,
                committed_burst_bytes=cbs,
                peak_burst_bytes=pbs,
            )
        wred = _parse_wred(item.get("wred"), f"{path}.wred", queue_depth)
        if policy == "wred" and wred is None:
            fail("E_CLASS_PARAM", f"{path}.wred is required for WRED", path=path)
        weight = _positive_int(item.get("weight"), f"{path}.weight")
        guarantee_value = item.get("guarantee_bps")
        guarantee = (
            None
            if guarantee_value is None
            else _positive_int(guarantee_value, f"{path}.guarantee_bps")
        )
        partial.append(
            {
                "name": name,
                "parent": parent,
                "cir_bps": cir,
                "pir_bps": pir,
                "committed_burst_bytes": cbs,
                "peak_burst_bytes": pbs,
                "queue_depth_bytes": queue_depth,
                "drop_policy": policy,
                "wred": wred,
                "weight": weight,
                "guarantee_bps": guarantee,
                "order": index,
            }
        )

    by_name = {item["name"]: item for item in partial}
    children: dict[str, list[str]] = {item["name"]: [] for item in partial}
    for item in partial:
        parent = item["parent"]
        if parent is not None:
            if parent not in by_name:
                fail(
                    "E_PARENT_UNKNOWN",
                    f"class {item['name']!r} references unknown parent {parent!r}",
                    class_name=item["name"],
                    parent=parent,
                )
            if item["guarantee_bps"] is None:
                fail(
                    "E_CLASS_PARAM",
                    f"class {item['name']!r} must define guarantee_bps",
                    path=f"classes[{item['order']}].guarantee_bps",
                )
            children[parent].append(item["name"])
        elif item["guarantee_bps"] is not None:
            fail(
                "E_CLASS_PARAM",
                f"root class {item['name']!r} cannot define guarantee_bps",
                path=f"classes[{item['order']}].guarantee_bps",
            )

    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(name: str) -> None:
        if name in visiting:
            fail("E_HIERARCHY_CYCLE", f"cycle detected at class {name!r}", class_name=name)
        if name in visited:
            return
        visiting.add(name)
        parent = by_name[name]["parent"]
        if parent is not None:
            visit(parent)
        visiting.remove(name)
        visited.add(name)

    for item in partial:
        visit(item["name"])

    for item in partial:
        child_names = children[item["name"]]
        if child_names:
            guarantee_sum = sum(by_name[child]["guarantee_bps"] for child in child_names)
            if guarantee_sum > item["cir_bps"]:
                fail(
                    "E_GUARANTEE_SUM",
                    f"children guarantees of {item['name']!r} exceed its committed rate",
                    parent=item["name"],
                    guarantee_sum_bps=guarantee_sum,
                    parent_cir_bps=item["cir_bps"],
                )

    return tuple(
        ClassConfig(
            name=item["name"],
            parent=item["parent"],
            cir_bps=item["cir_bps"],
            pir_bps=item["pir_bps"],
            committed_burst_bytes=item["committed_burst_bytes"],
            peak_burst_bytes=item["peak_burst_bytes"],
            queue_depth_bytes=item["queue_depth_bytes"],
            drop_policy=item["drop_policy"],
            wred=item["wred"],
            weight=item["weight"],
            guarantee_bps=item["guarantee_bps"],
            order=item["order"],
            children=tuple(children[item["name"]]),
        )
        for item in partial
    )


def _parse_classifiers(config_doc: Any, class_map: Mapping[str, ClassConfig]) -> tuple[ClassifierRule, ...]:
    root = _require_mapping(config_doc, "config", "E_CLASSIFIER")
    raw_rules = _require_list(root.get("classifiers"), "config.classifiers", "E_CLASSIFIER")
    if not raw_rules:
        fail("E_CLASSIFIER", "config.classifiers must not be empty", path="config.classifiers")
    seen: set[str] = set()
    rules: list[ClassifierRule] = []
    for index, raw in enumerate(raw_rules):
        path = f"classifiers[{index}]"
        item = _require_mapping(raw, path, "E_CLASSIFIER")
        rule_id = item.get("id")
        if not isinstance(rule_id, str) or not rule_id:
            fail("E_CLASSIFIER", f"{path}.id must be a non-empty string", path=path)
        if rule_id in seen:
            fail("E_CLASSIFIER", f"duplicate classifier id {rule_id!r}", path=path)
        seen.add(rule_id)
        target = item.get("target")
        if target not in class_map:
            fail("E_CLASSIFIER", f"{path}.target references unknown class", path=path)
        if not class_map[target].is_leaf:
            fail(
                "E_CLASSIFIER",
                f"{path}.target must be a leaf shaping class",
                path=path,
                target=target,
            )
        kind = item.get("type")
        if kind == "dscp":
            dscp = item.get("dscp")
            if not _is_int(dscp) or not 0 <= dscp <= 63:
                fail("E_CLASSIFIER", f"{path}.dscp must be in [0, 63]", path=path)
            rules.append(ClassifierRule(rule_id, kind, target, dscp=dscp))
        elif kind == "five_tuple":
            five_tuple = _validate_five_tuple(
                item.get("five_tuple"), f"{path}.five_tuple", "E_CLASSIFIER"
            )
            rules.append(
                ClassifierRule(rule_id, kind, target, five_tuple=five_tuple)
            )
        else:
            fail(
                "E_CLASSIFIER",
                f"{path}.type must be 'dscp' or 'five_tuple'",
                path=path,
                value=kind,
            )
    return tuple(rules)


def _classify(
    dscp: int | None,
    five_tuple: tuple[tuple[str, Any], ...] | None,
    rules: tuple[ClassifierRule, ...],
) -> tuple[str | None, str | None]:
    five_dict = dict(five_tuple) if five_tuple is not None else None
    for rule in rules:
        if rule.kind == "dscp" and dscp is not None and dscp == rule.dscp:
            return rule.target, rule.rule_id
        if rule.kind == "five_tuple" and five_dict is not None and five_dict == dict(rule.five_tuple or ()):
            return rule.target, rule.rule_id
    return None, None


def _parse_events(
    arrivals_doc: Any,
    class_map: Mapping[str, ClassConfig],
    rules: tuple[ClassifierRule, ...],
    step_ms: int,
    total_steps: int,
) -> tuple[ArrivalEvent, ...]:
    root = _require_mapping(arrivals_doc, "arrivals", "E_EVENT_SCHEMA")
    raw_events = _require_list(root.get("events"), "arrivals.events", "E_EVENT_SCHEMA")
    if not raw_events:
        fail("E_EVENT_SCHEMA", "arrivals.events must not be empty", path="arrivals.events")
    previous_seq: int | None = None
    events: list[ArrivalEvent] = []
    for index, raw in enumerate(raw_events):
        path = f"events[{index}]"
        item = _require_mapping(raw, path, "E_EVENT_SCHEMA")
        seq = item.get("seq")
        if not _is_int(seq) or seq < 1:
            fail("E_EVENT_SCHEMA", f"{path}.seq must be a positive integer", path=path)
        if previous_seq is not None and seq <= previous_seq:
            fail(
                "E_EVENT_SEQ",
                f"{path}.seq must be strictly greater than {previous_seq}",
                path=path,
                previous_seq=previous_seq,
                seq=seq,
            )
        previous_seq = seq
        byte_count = item.get("bytes")
        if not _is_int(byte_count) or byte_count <= 0:
            fail("E_EVENT_SCHEMA", f"{path}.bytes must be a positive integer", path=path)
        arrival_ms = item.get("arrival_ms")
        if not _is_int(arrival_ms) or arrival_ms < 0:
            fail(
                "E_EVENT_TIME",
                f"{path}.arrival_ms must be a non-negative integer",
                path=path,
            )
        label = item.get("class")
        if label not in class_map:
            fail("E_EVENT_CLASS", f"{path}.class references unknown class", path=path)
        if not class_map[label].is_leaf:
            fail(
                "E_EVENT_CLASS",
                f"{path}.class must be a leaf scheduling class",
                path=path,
                class_name=label,
            )
        dscp = item.get("dscp")
        if dscp is not None and (not _is_int(dscp) or not 0 <= dscp <= 63):
            fail("E_EVENT_SCHEMA", f"{path}.dscp must be in [0, 63]", path=path)
        five_tuple = (
            _validate_five_tuple(item["five_tuple"], f"{path}.five_tuple", "E_EVENT_SCHEMA")
            if "five_tuple" in item
            else None
        )
        resolved, rule_id = _classify(dscp, five_tuple, rules)
        if resolved is not None and resolved != label:
            fail(
                "E_EVENT_CLASS_MISMATCH",
                f"{path}.class {label!r} disagrees with classifier rule {rule_id!r}",
                path=path,
                event_class=label,
                classifier_class=resolved,
                classifier_rule=rule_id,
            )
        resolved_class = resolved or label
        due_step = (arrival_ms + step_ms - 1) // step_ms
        if due_step >= total_steps:
            fail(
                "E_EVENT_HORIZON",
                f"{path} arrives after the configured horizon",
                path=path,
                arrival_ms=arrival_ms,
                total_steps=total_steps,
                step_ms=step_ms,
            )
        events.append(
            ArrivalEvent(
                seq=seq,
                bytes=byte_count,
                arrival_ms=arrival_ms,
                class_label=label,
                resolved_class=resolved_class,
                due_step=due_step,
                dscp=dscp,
                five_tuple=five_tuple,
                classifier_rule_id=rule_id,
            )
        )
    return tuple(events)


def build_sample(
    manifest_doc: Any,
    config_doc: Any,
    arrivals_doc: Any,
    *,
    config_digest: str,
    arrivals_digest: str,
    raw_package: Mapping[str, Any],
    verify_manifest_hashes: bool = True,
) -> Sample:
    manifest = _require_mapping(manifest_doc, "manifest", "E_MANIFEST")
    schema_version = manifest.get("schema_version")
    if schema_version != 1:
        fail("E_MANIFEST", "manifest.schema_version must be 1", path="manifest.schema_version")
    sample_id = manifest.get("sample_id")
    injection_id = manifest.get("injection_id")
    if not isinstance(sample_id, str) or not sample_id:
        fail("E_MANIFEST", "manifest.sample_id must be a non-empty string")
    if not isinstance(injection_id, str) or not injection_id:
        fail("E_MANIFEST", "manifest.injection_id must be a non-empty string")
    seed = manifest.get("seed")
    if not _is_int(seed) or seed < 0:
        fail("E_MANIFEST", "manifest.seed must be a non-negative integer")
    step_ms = _positive_int(manifest.get("step_ms"), "manifest.step_ms")
    total_steps = _positive_int(manifest.get("total_steps"), "manifest.total_steps")
    window_ms = _positive_int(manifest.get("window_ms"), "manifest.window_ms")
    throughput_tolerance = _positive_int(
        manifest.get("throughput_tolerance_bps"),
        "manifest.throughput_tolerance_bps",
        allow_zero=True,
    )
    schedule_tolerance = _positive_int(
        manifest.get("schedule_tolerance_bytes"),
        "manifest.schedule_tolerance_bytes",
        allow_zero=True,
    )
    initial_fill = manifest.get("initial_token_fill", "full")
    if initial_fill != "full":
        fail("E_MANIFEST", "only initial_token_fill='full' is supported")

    if verify_manifest_hashes:
        declared = _require_mapping(manifest.get("sha256"), "manifest.sha256", "E_MANIFEST")
        if declared.get("config") != config_digest:
            fail(
                "E_SAMPLE_HASH",
                "config.json digest does not match manifest.sha256.config",
                expected=declared.get("config"),
                actual=config_digest,
            )
        if declared.get("arrivals") != arrivals_digest:
            fail(
                "E_SAMPLE_HASH",
                "arrivals.json digest does not match manifest.sha256.arrivals",
                expected=declared.get("arrivals"),
                actual=arrivals_digest,
            )

    classes = _parse_classes(config_doc)
    class_map = {item.name: item for item in classes}
    classifiers = _parse_classifiers(config_doc, class_map)
    events = _parse_events(
        arrivals_doc, class_map, classifiers, step_ms=step_ms, total_steps=total_steps
    )
    event_schedule = tuple(
        sorted(events, key=lambda event: (event.due_step, event.arrival_ms, event.seq))
    )
    package = {
        "manifest": manifest_doc,
        "config": config_doc,
        "arrivals": arrivals_doc,
    }
    sample_digest = digest_bytes(canonical_bytes(package))
    return Sample(
        sample_id=sample_id,
        injection_id=injection_id,
        seed=seed,
        step_ms=step_ms,
        total_steps=total_steps,
        window_ms=window_ms,
        throughput_tolerance_bps=Fraction(throughput_tolerance, 1),
        schedule_tolerance_bytes=Fraction(schedule_tolerance, 1),
        initial_fill=initial_fill,
        classes=classes,
        classifiers=classifiers,
        events=events,
        event_schedule=event_schedule,
        sample_digest=sample_digest,
        config_digest=config_digest,
        arrivals_digest=arrivals_digest,
        raw_package=raw_package,
    )


def load_sample(manifest_path: str | Path) -> Sample:
    manifest_path = Path(manifest_path).resolve()
    manifest_bytes, manifest_doc = _read_json(manifest_path, "manifest")
    manifest = _require_mapping(manifest_doc, "manifest", "E_MANIFEST")
    files = _require_mapping(manifest.get("files"), "manifest.files", "E_MANIFEST")
    config_ref = files.get("config")
    arrivals_ref = files.get("arrivals")
    if not isinstance(config_ref, str) or not isinstance(arrivals_ref, str):
        fail("E_MANIFEST", "manifest.files must contain config and arrivals paths")
    config_path = (manifest_path.parent / config_ref).resolve()
    arrivals_path = (manifest_path.parent / arrivals_ref).resolve()
    config_bytes, config_doc = _read_json(config_path, "config")
    arrivals_bytes, arrivals_doc = _read_json(arrivals_path, "arrivals")
    config_digest = digest_bytes(config_bytes)
    arrivals_digest = digest_bytes(arrivals_bytes)
    raw_package = {
        "manifest": manifest_doc,
        "config": config_doc,
        "arrivals": arrivals_doc,
    }
    _ = manifest_bytes
    return build_sample(
        manifest_doc,
        config_doc,
        arrivals_doc,
        config_digest=config_digest,
        arrivals_digest=arrivals_digest,
        raw_package=raw_package,
    )


def sample_from_package(package: Mapping[str, Any]) -> Sample:
    manifest_doc = package.get("manifest")
    config_doc = package.get("config")
    arrivals_doc = package.get("arrivals")
    if not isinstance(manifest_doc, dict) or not isinstance(config_doc, dict) or not isinstance(arrivals_doc, dict):
        fail("E_DB_SCHEMA", "stored sample package is incomplete")
    config_digest = digest_bytes(canonical_bytes(config_doc))
    arrivals_digest = digest_bytes(canonical_bytes(arrivals_doc))
    return build_sample(
        manifest_doc,
        config_doc,
        arrivals_doc,
        config_digest=config_digest,
        arrivals_digest=arrivals_digest,
        raw_package=package,
        verify_manifest_hashes=False,
    )

