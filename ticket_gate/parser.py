from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from .model import (
    Change,
    EventDefinition,
    Fault,
    Gate,
    Project,
    RuleSet,
    Topology,
    ValidationIssue,
    VerificationRequest,
)


def _pointer_part(value: str) -> str:
    return value.replace("~", "~0").replace("/", "~1")


def _join_pointer(base: str, *parts: str) -> str:
    for part in parts:
        base += "/" + _pointer_part(str(part))
    return base


def _source_path(relative_file: str, *parts: str) -> str:
    return relative_file + "#" + _join_pointer("", *parts)


def parse_datetime(value: Any, path: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{path}: expected a non-empty ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{path}: invalid ISO-8601 timestamp {value!r}") from exc
    return parsed


class _Loader:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.issues: list[ValidationIssue] = []
        self.source_files: dict[str, str] = {}

    def read_json(self, relative: str, required: bool = True) -> Any:
        path = self.root / relative
        try:
            with path.open("r", encoding="utf-8") as handle:
                data = json.load(handle)
        except FileNotFoundError:
            if required:
                self.issues.append(
                    ValidationIssue(
                        code="FILE_NOT_FOUND",
                        path=relative,
                        message=f"required file does not exist: {path}",
                    )
                )
            return None
        except json.JSONDecodeError as exc:
            self.issues.append(
                ValidationIssue(
                    code="INVALID_JSON",
                    path=relative,
                    message=f"invalid JSON at line {exc.lineno}, column {exc.colno}: {exc.msg}",
                )
            )
            return None
        self.source_files[relative] = str(path)
        return data

    def issue(
        self,
        code: str,
        path: str,
        message: str,
        *,
        event_id: str | None = None,
        version: str | None = None,
        gate_id: str | None = None,
    ) -> None:
        self.issues.append(
            ValidationIssue(
                code=code,
                path=path,
                message=message,
                event_id=event_id,
                version=version,
                gate_id=gate_id,
            )
        )


def _parse_rule(
    loader: _Loader,
    events_file: str,
    event_id: str,
    version: str,
    raw_rule: Any,
) -> RuleSet | None:
    base = ("events", event_id, "versions", version, "rules")
    source_root = _source_path(events_file, *base)
    if not isinstance(raw_rule, dict):
        loader.issue(
            "RULE_MISSING",
            source_root,
            "event rule object is missing or not an object",
            event_id=event_id,
            version=version,
        )
        return None

    ok = True
    sources = {
        "ticket_whitelist": _source_path(events_file, *base, "ticket_whitelist"),
        "entry_window.start": _source_path(events_file, *base, "entry_window", "start"),
        "entry_window.end": _source_path(events_file, *base, "entry_window", "end"),
        "reentry_policy": _source_path(events_file, *base, "reentry_policy"),
        "child_policy.requires_adult": _source_path(
            events_file, *base, "child_policy", "requires_adult"
        ),
    }

    whitelist = raw_rule.get("ticket_whitelist")
    normalized_whitelist: tuple[str, ...] = ()
    if not isinstance(whitelist, list):
        loader.issue(
            "TICKET_WHITELIST_MISSING",
            sources["ticket_whitelist"],
            "ticket_whitelist must be a non-empty array",
            event_id=event_id,
            version=version,
        )
        ok = False
    else:
        values = [item for item in whitelist if isinstance(item, str) and item]
        if not values or len(values) != len(whitelist):
            loader.issue(
                "TICKET_WHITELIST_INVALID",
                sources["ticket_whitelist"],
                "ticket_whitelist must contain only non-empty strings",
                event_id=event_id,
                version=version,
            )
            ok = False
        else:
            normalized_whitelist = tuple(sorted(set(values)))

    window = raw_rule.get("entry_window")
    window_start = ""
    window_end = ""
    start_at: datetime | None = None
    end_at: datetime | None = None
    if not isinstance(window, dict):
        loader.issue(
            "ENTRY_WINDOW_MISSING",
            _source_path(events_file, *base, "entry_window"),
            "entry_window must be an object with start and end",
            event_id=event_id,
            version=version,
        )
        ok = False
    else:
        for key in ("start", "end"):
            field_source = sources[f"entry_window.{key}"]
            if key not in window:
                loader.issue(
                    "ENTRY_WINDOW_FIELD_MISSING",
                    field_source,
                    f"entry_window.{key} is required",
                    event_id=event_id,
                    version=version,
                )
                ok = False
                continue
            try:
                parsed = parse_datetime(window[key], field_source)
            except ValueError as exc:
                loader.issue(
                    "ENTRY_WINDOW_INVALID",
                    field_source,
                    str(exc),
                    event_id=event_id,
                    version=version,
                )
                ok = False
                continue
            if key == "start":
                window_start = window[key]
                start_at = parsed
            else:
                window_end = window[key]
                end_at = parsed
        if start_at is not None and end_at is not None:
            try:
                inverted = start_at > end_at
            except TypeError:
                loader.issue(
                    "ENTRY_WINDOW_TIMEZONE_MISMATCH",
                    _source_path(events_file, *base, "entry_window"),
                    "entry_window.start and entry_window.end must both be timezone-aware or both be naive",
                    event_id=event_id,
                    version=version,
                )
                ok = False
            else:
                if inverted:
                    loader.issue(
                        "ENTRY_WINDOW_INVERTED",
                        _source_path(events_file, *base, "entry_window"),
                        "entry_window.start is later than entry_window.end",
                        event_id=event_id,
                        version=version,
                    )
                    ok = False

    reentry = raw_rule.get("reentry_policy")
    if reentry not in {"single", "unlimited"}:
        loader.issue(
            "REENTRY_POLICY_INVALID",
            sources["reentry_policy"],
            "reentry_policy must be 'single' or 'unlimited'",
            event_id=event_id,
            version=version,
        )
        ok = False

    child = raw_rule.get("child_policy")
    requires_adult = False
    if not isinstance(child, dict):
        loader.issue(
            "CHILD_POLICY_MISSING",
            _source_path(events_file, *base, "child_policy"),
            "child_policy must be an object",
            event_id=event_id,
            version=version,
        )
        ok = False
    elif not isinstance(child.get("requires_adult"), bool):
        loader.issue(
            "CHILD_POLICY_INVALID",
            sources["child_policy.requires_adult"],
            "child_policy.requires_adult must be a boolean",
            event_id=event_id,
            version=version,
        )
        ok = False
    else:
        requires_adult = child["requires_adult"]

    if not ok or start_at is None or end_at is None:
        return None

    return RuleSet(
        event_id=event_id,
        version=version,
        ticket_whitelist=normalized_whitelist,
        window_start=window_start,
        window_end=window_end,
        reentry_policy=reentry,
        child_requires_adult=requires_adult,
        sources=sources,
        start_at=start_at,
        end_at=end_at,
    )


def load_project(root: str | Path) -> Project:
    root_path = Path(root).resolve()
    loader = _Loader(root_path)
    manifest = loader.read_json("manifest.json")
    if not isinstance(manifest, dict):
        if not loader.issues:
            loader.issue("MANIFEST_INVALID", "manifest.json", "manifest must be an object")
        return Project(
            root=str(root_path),
            project_id="<invalid>",
            seed=0,
            schema_version=0,
            manifest={},
            events={},
            gates={},
            changes={},
            topology=Topology({}, {}),
            faults={},
            requests=(),
            source_files=loader.source_files,
            issues=loader.issues,
        )

    project_id = manifest.get("project_id")
    seed = manifest.get("seed")
    schema_version = manifest.get("schema_version")
    if not isinstance(project_id, str) or not project_id:
        loader.issue(
            "PROJECT_ID_INVALID", "manifest.json#/project_id", "project_id must be a non-empty string"
        )
        project_id = "<invalid>"
    if not isinstance(seed, int):
        loader.issue("SEED_INVALID", "manifest.json#/seed", "seed must be an integer")
        seed = 0
    if schema_version != 1:
        loader.issue(
            "SCHEMA_VERSION_UNSUPPORTED",
            "manifest.json#/schema_version",
            "only schema_version 1 is supported",
        )

    files = manifest.get("files", {})
    if not isinstance(files, dict):
        loader.issue("MANIFEST_FILES_INVALID", "manifest.json#/files", "files must be an object")
        files = {}

    def required_file(key: str) -> str:
        value = files.get(key)
        if not isinstance(value, str) or not value:
            loader.issue(
                "MANIFEST_FILE_PATH_MISSING",
                f"manifest.json#/files/{key}",
                f"files.{key} must be a non-empty relative path",
            )
            return f"<missing-{key}>"
        return value

    events_file = required_file("events")
    gates_file = required_file("gates")
    topology_file = required_file("topology")
    changes_file = required_file("changes")
    faults_file = required_file("faults")
    requests_file = required_file("verification_requests")

    events_raw = loader.read_json(events_file)
    gates_raw = loader.read_json(gates_file)
    topology_raw = loader.read_json(topology_file)
    changes_raw = loader.read_json(changes_file)
    faults_raw = loader.read_json(faults_file)
    requests_raw = loader.read_json(requests_file)

    events: dict[str, EventDefinition] = {}
    if isinstance(events_raw, dict) and isinstance(events_raw.get("events"), dict):
        for event_id, raw_event in sorted(events_raw["events"].items()):
            event_path = _source_path(events_file, "events", event_id)
            if not isinstance(raw_event, dict):
                loader.issue(
                    "EVENT_INVALID", event_path, "event must be an object", event_id=event_id
                )
                continue
            current_version = raw_event.get("current_version")
            versions_raw = raw_event.get("versions")
            if not isinstance(current_version, str) or not current_version:
                loader.issue(
                    "CURRENT_VERSION_MISSING",
                    _source_path(events_file, "events", event_id, "current_version"),
                    "current_version is required",
                    event_id=event_id,
                )
                continue
            if not isinstance(versions_raw, dict) or not versions_raw:
                loader.issue(
                    "EVENT_VERSIONS_MISSING",
                    _source_path(events_file, "events", event_id, "versions"),
                    "versions must be a non-empty object",
                    event_id=event_id,
                )
                continue
            versions: dict[str, RuleSet] = {}
            valid_event = True
            for version, raw_rule in sorted(versions_raw.items()):
                if not isinstance(version, str) or not version:
                    loader.issue(
                        "RULE_VERSION_INVALID",
                        _source_path(events_file, "events", event_id, "versions"),
                        "rule version keys must be non-empty strings",
                        event_id=event_id,
                    )
                    valid_event = False
                    continue
                parsed = _parse_rule(loader, events_file, event_id, version, raw_rule)
                if parsed is None:
                    valid_event = False
                else:
                    versions[version] = parsed
            if current_version not in versions_raw:
                loader.issue(
                    "CURRENT_VERSION_NOT_FOUND",
                    _source_path(events_file, "events", event_id, "current_version"),
                    f"current_version {current_version!r} is not present in versions",
                    event_id=event_id,
                    version=current_version,
                )
                valid_event = False
            if valid_event:
                events[event_id] = EventDefinition(
                    event_id=event_id,
                    name=str(raw_event.get("name", event_id)),
                    current_version=current_version,
                    versions=versions,
                )
    elif events_raw is not None:
        loader.issue(
            "EVENTS_INVALID",
            f"{events_file}#/events",
            "expected an object with an 'events' object",
        )

    gates: dict[str, Gate] = {}
    if isinstance(gates_raw, dict) and isinstance(gates_raw.get("gates"), list):
        for index, raw_gate in enumerate(gates_raw["gates"]):
            gate_path = _source_path(gates_file, "gates", str(index))
            if not isinstance(raw_gate, dict):
                loader.issue("GATE_INVALID", gate_path, "gate must be an object")
                continue
            gate_id = raw_gate.get("gate_id")
            if not isinstance(gate_id, str) or not gate_id:
                loader.issue(
                    "GATE_ID_INVALID",
                    _source_path(gates_file, "gates", str(index), "gate_id"),
                    "gate_id must be a non-empty string",
                )
                continue
            if gate_id in gates:
                loader.issue(
                    "GATE_ID_DUPLICATE",
                    _source_path(gates_file, "gates", str(index), "gate_id"),
                    f"duplicate gate_id {gate_id!r}",
                    gate_id=gate_id,
                )
                continue
            entrance_id = raw_gate.get("entrance_id")
            zones = raw_gate.get("seat_zone_ids")
            subscriptions = raw_gate.get("subscriptions")
            valid_gate = True
            if not isinstance(entrance_id, str) or not entrance_id:
                loader.issue(
                    "GATE_ENTRANCE_MISSING",
                    _source_path(gates_file, "gates", str(index), "entrance_id"),
                    "entrance_id must be a non-empty string",
                    gate_id=gate_id,
                )
                valid_gate = False
            if (
                not isinstance(zones, list)
                or not zones
                or any(not isinstance(item, str) or not item for item in zones)
            ):
                loader.issue(
                    "GATE_ZONES_INVALID",
                    _source_path(gates_file, "gates", str(index), "seat_zone_ids"),
                    "seat_zone_ids must be a non-empty array of strings",
                    gate_id=gate_id,
                )
                valid_gate = False
            if (
                not isinstance(subscriptions, list)
                or not subscriptions
                or any(not isinstance(item, str) or not item for item in subscriptions)
            ):
                loader.issue(
                    "GATE_SUBSCRIPTIONS_INVALID",
                    _source_path(gates_file, "gates", str(index), "subscriptions"),
                    "subscriptions must be a non-empty array of event ids",
                    gate_id=gate_id,
                )
                valid_gate = False
            if valid_gate:
                gates[gate_id] = Gate(
                    gate_id=gate_id,
                    name=str(raw_gate.get("name", gate_id)),
                    entrance_id=entrance_id,
                    seat_zone_ids=tuple(sorted(set(zones))),
                    subscriptions=tuple(sorted(set(subscriptions))),
                )
    elif gates_raw is not None:
        loader.issue(
            "GATES_INVALID",
            f"{gates_file}#/gates",
            "expected an object with a 'gates' array",
        )

    topology = Topology({}, {})
    if isinstance(topology_raw, dict):
        entrance_deps: dict[str, tuple[str, ...]] = {}
        zone_deps: dict[str, tuple[str, ...]] = {}
        for index, raw_entrance in enumerate(topology_raw.get("entrances", [])):
            path = _source_path(topology_file, "entrances", str(index))
            if not isinstance(raw_entrance, dict):
                loader.issue("TOPOLOGY_ENTRANCE_INVALID", path, "entrance must be an object")
                continue
            entrance_id = raw_entrance.get("entrance_id")
            deps = raw_entrance.get("depends_on_entrance_ids", [])
            if not isinstance(entrance_id, str) or not entrance_id:
                loader.issue(
                    "TOPOLOGY_ENTRANCE_ID_INVALID",
                    path + "/entrance_id",
                    "entrance_id must be a non-empty string",
                )
                continue
            if not isinstance(deps, list) or any(not isinstance(item, str) for item in deps):
                loader.issue(
                    "TOPOLOGY_ENTRANCE_DEPS_INVALID",
                    path + "/depends_on_entrance_ids",
                    "depends_on_entrance_ids must be an array of strings",
                )
                continue
            entrance_deps[entrance_id] = tuple(sorted(set(deps)))
        for index, raw_zone in enumerate(topology_raw.get("seat_zones", [])):
            path = _source_path(topology_file, "seat_zones", str(index))
            if not isinstance(raw_zone, dict):
                loader.issue("TOPOLOGY_ZONE_INVALID", path, "seat zone must be an object")
                continue
            zone_id = raw_zone.get("seat_zone_id")
            deps = raw_zone.get("depends_on_seat_zone_ids", [])
            if not isinstance(zone_id, str) or not zone_id:
                loader.issue(
                    "TOPOLOGY_ZONE_ID_INVALID",
                    path + "/seat_zone_id",
                    "seat_zone_id must be a non-empty string",
                )
                continue
            if not isinstance(deps, list) or any(not isinstance(item, str) for item in deps):
                loader.issue(
                    "TOPOLOGY_ZONE_DEPS_INVALID",
                    path + "/depends_on_seat_zone_ids",
                    "depends_on_seat_zone_ids must be an array of strings",
                )
                continue
            zone_deps[zone_id] = tuple(sorted(set(deps)))
        topology = Topology(entrance_deps, zone_deps)
    elif topology_raw is not None:
        loader.issue(
            "TOPOLOGY_INVALID",
            topology_file,
            "topology must be an object",
        )

    for gate in gates.values():
        if topology.entrance_dependencies and gate.entrance_id not in topology.entrance_dependencies:
            loader.issue(
                "GATE_ENTRANCE_NOT_IN_TOPOLOGY",
                _source_path(gates_file, "gates", gate.gate_id, "entrance_id"),
                f"entrance {gate.entrance_id!r} is not declared in topology",
                gate_id=gate.gate_id,
            )
        for zone_id in gate.seat_zone_ids:
            if topology.seat_zone_dependencies and zone_id not in topology.seat_zone_dependencies:
                loader.issue(
                    "GATE_ZONE_NOT_IN_TOPOLOGY",
                    _source_path(gates_file, "gates", gate.gate_id, "seat_zone_ids"),
                    f"seat zone {zone_id!r} is not declared in topology",
                    gate_id=gate.gate_id,
                )
        for event_id in gate.subscriptions:
            if event_id not in events:
                loader.issue(
                    "GATE_SUBSCRIPTION_UNKNOWN_EVENT",
                    _source_path(gates_file, "gates", gate.gate_id, "subscriptions"),
                    f"subscribed event {event_id!r} is missing or invalid",
                    gate_id=gate.gate_id,
                )

    changes: dict[str, Change] = {}
    if isinstance(changes_raw, dict) and isinstance(changes_raw.get("changes"), list):
        for index, raw_change in enumerate(changes_raw["changes"]):
            path = _source_path(changes_file, "changes", str(index))
            if not isinstance(raw_change, dict):
                loader.issue("CHANGE_INVALID", path, "change must be an object")
                continue
            event_id = raw_change.get("event_id")
            from_version = raw_change.get("from_version")
            to_version = raw_change.get("to_version")
            if not all(isinstance(item, str) and item for item in (event_id, from_version, to_version)):
                loader.issue(
                    "CHANGE_FIELDS_INVALID",
                    path,
                    "event_id, from_version and to_version must be non-empty strings",
                )
                continue
            if event_id in changes:
                loader.issue(
                    "CHANGE_DUPLICATE", path, f"duplicate change for event {event_id!r}"
                )
                continue
            changes[event_id] = Change(event_id, from_version, to_version)
    elif changes_raw is not None:
        loader.issue(
            "CHANGES_INVALID",
            f"{changes_file}#/changes",
            "expected an object with a 'changes' array",
        )

    for change in changes.values():
        event = events.get(change.event_id)
        if event is None:
            loader.issue(
                "CHANGE_EVENT_UNKNOWN",
                _source_path(changes_file, "changes", change.event_id),
                f"changed event {change.event_id!r} is missing or invalid",
                event_id=change.event_id,
            )
            continue
        for label, version in (
            ("from_version", change.from_version),
            ("to_version", change.to_version),
        ):
            if version not in event.versions:
                loader.issue(
                    "CHANGE_VERSION_UNKNOWN",
                    _source_path(changes_file, "changes", change.event_id, label),
                    f"{label} {version!r} is not defined for event {change.event_id!r}",
                    event_id=change.event_id,
                    version=version,
                )
        if change.from_version == change.to_version:
            loader.issue(
                "CHANGE_VERSION_IDENTICAL",
                _source_path(changes_file, "changes", change.event_id),
                "from_version and to_version must differ",
                event_id=change.event_id,
            )

    faults: dict[str, tuple[Fault, ...]] = {}
    if isinstance(faults_raw, dict):
        fault_items = faults_raw.get("faults", {})
        if not isinstance(fault_items, dict):
            loader.issue(
                "FAULTS_INVALID", f"{faults_file}#/faults", "faults must be an object"
            )
        else:
            for gate_id, raw_fault in sorted(fault_items.items()):
                path = _source_path(faults_file, "faults", gate_id)
                if gate_id not in gates:
                    loader.issue(
                        "FAULT_GATE_UNKNOWN",
                        path,
                        f"fault references unknown gate {gate_id!r}",
                        gate_id=gate_id,
                    )
                    continue
                attempts_raw = raw_fault.get("attempts") if isinstance(raw_fault, dict) else None
                if not isinstance(attempts_raw, list):
                    loader.issue(
                        "FAULT_ATTEMPTS_INVALID",
                        path + "/attempts",
                        "attempts must be an array",
                        gate_id=gate_id,
                    )
                    continue
                attempts: dict[int, Fault] = {}
                for index, raw_attempt in enumerate(attempts_raw):
                    attempt_path = path + f"/attempts/{index}"
                    if not isinstance(raw_attempt, dict):
                        loader.issue(
                            "FAULT_INVALID",
                            attempt_path,
                            "fault attempt must be an object",
                            gate_id=gate_id,
                        )
                        continue
                    attempt_no = raw_attempt.get("attempt")
                    error_code = raw_attempt.get("error_code")
                    retryable = raw_attempt.get("retryable")
                    if (
                        not isinstance(attempt_no, int)
                        or attempt_no < 1
                        or not isinstance(error_code, str)
                        or not error_code
                        or not isinstance(retryable, bool)
                    ):
                        loader.issue(
                            "FAULT_INVALID",
                            attempt_path,
                            "attempt must be >=1, error_code non-empty and retryable boolean",
                            gate_id=gate_id,
                        )
                        continue
                    if attempt_no in attempts:
                        loader.issue(
                            "FAULT_DUPLICATE_ATTEMPT",
                            attempt_path,
                            f"duplicate attempt {attempt_no}",
                            gate_id=gate_id,
                        )
                        continue
                    attempts[attempt_no] = Fault(attempt_no, error_code, retryable)
                faults[gate_id] = tuple(attempts[key] for key in sorted(attempts))
    elif faults_raw is not None:
        loader.issue("FAULTS_FILE_INVALID", faults_file, "faults file must be an object")

    requests: list[VerificationRequest] = []
    if isinstance(requests_raw, dict) and isinstance(requests_raw.get("requests"), list):
        seen_request_ids: set[str] = set()
        for index, raw_request in enumerate(requests_raw["requests"]):
            path = _source_path(requests_file, "requests", str(index))
            if not isinstance(raw_request, dict):
                loader.issue("VERIFICATION_REQUEST_INVALID", path, "request must be an object")
                continue
            request_id = raw_request.get("request_id")
            gate_id = raw_request.get("gate_id")
            event_id = raw_request.get("event_id")
            ticket_type = raw_request.get("ticket_type")
            at = raw_request.get("at")
            valid_request = True
            if not isinstance(request_id, str) or not request_id:
                loader.issue(
                    "VERIFICATION_REQUEST_ID_INVALID",
                    path + "/request_id",
                    "request_id must be a non-empty string",
                )
                valid_request = False
            elif request_id in seen_request_ids:
                loader.issue(
                    "VERIFICATION_REQUEST_ID_DUPLICATE",
                    path + "/request_id",
                    f"duplicate request_id {request_id!r}",
                )
                valid_request = False
            else:
                seen_request_ids.add(request_id)
            if not isinstance(gate_id, str) or gate_id not in gates:
                loader.issue(
                    "VERIFICATION_GATE_UNKNOWN",
                    path + "/gate_id",
                    "gate_id must reference a declared gate",
                    gate_id=gate_id if isinstance(gate_id, str) else None,
                )
                valid_request = False
            if not isinstance(event_id, str) or event_id not in events:
                loader.issue(
                    "VERIFICATION_EVENT_UNKNOWN",
                    path + "/event_id",
                    "event_id must reference a declared event",
                    event_id=event_id if isinstance(event_id, str) else None,
                )
                valid_request = False
            elif isinstance(gate_id, str) and gate_id in gates and event_id not in gates[gate_id].subscriptions:
                loader.issue(
                    "VERIFICATION_EVENT_NOT_SUBSCRIBED",
                    path + "/event_id",
                    f"gate {gate_id!r} does not subscribe event {event_id!r}",
                    event_id=event_id,
                    gate_id=gate_id,
                )
                valid_request = False
            if not isinstance(ticket_type, str) or not ticket_type:
                loader.issue(
                    "VERIFICATION_TICKET_INVALID",
                    path + "/ticket_type",
                    "ticket_type must be a non-empty string",
                )
                valid_request = False
            at_time: datetime | None = None
            try:
                at_time = parse_datetime(at, path + "/at")
            except ValueError:
                loader.issue(
                    "VERIFICATION_TIME_INVALID",
                    path + "/at",
                    "at must be a valid ISO-8601 timestamp",
                )
                valid_request = False
            prior_entries = raw_request.get("prior_entries", 0)
            if not isinstance(prior_entries, int) or prior_entries < 0:
                loader.issue(
                    "VERIFICATION_PRIOR_ENTRIES_INVALID",
                    path + "/prior_entries",
                    "prior_entries must be a non-negative integer",
                )
                valid_request = False
            is_child = raw_request.get("is_child", ticket_type == "CHILD")
            if not isinstance(is_child, bool):
                loader.issue(
                    "VERIFICATION_IS_CHILD_INVALID",
                    path + "/is_child",
                    "is_child must be a boolean",
                )
                valid_request = False
            accompanied = raw_request.get("accompanied_adult_count", 0)
            if not isinstance(accompanied, int) or accompanied < 0:
                loader.issue(
                    "VERIFICATION_ADULT_COUNT_INVALID",
                    path + "/accompanied_adult_count",
                    "accompanied_adult_count must be a non-negative integer",
                )
                valid_request = False
            after_batch = raw_request.get("after_batch")
            if after_batch is not None and (not isinstance(after_batch, int) or after_batch < 0):
                loader.issue(
                    "VERIFICATION_AFTER_BATCH_INVALID",
                    path + "/after_batch",
                    "after_batch must be null or a non-negative integer",
                )
                valid_request = False
            if valid_request:
                requests.append(
                    VerificationRequest(
                        request_id=request_id,
                        gate_id=gate_id,
                        event_id=event_id,
                        at=at,
                        at_time=at_time,
                        ticket_type=ticket_type,
                        prior_entries=prior_entries,
                        is_child=is_child,
                        accompanied_adult_count=accompanied,
                        after_batch=after_batch,
                        source_path=path,
                    )
                )
    elif requests_raw is not None:
        loader.issue(
            "VERIFICATION_REQUESTS_INVALID",
            f"{requests_file}#/requests",
            "expected an object with a 'requests' array",
        )

    if not changes:
        loader.issue(
            "NO_CHANGES",
            f"{changes_file}#/changes",
            "at least one event rule change is required",
        )
    if not gates:
        loader.issue("NO_GATES", f"{gates_file}#/gates", "at least one gate is required")

    requests_tuple = tuple(sorted(requests, key=lambda request: request.request_id))
    return Project(
        root=str(root_path),
        project_id=project_id,
        seed=seed,
        schema_version=schema_version if isinstance(schema_version, int) else 0,
        manifest=manifest,
        events=events,
        gates=gates,
        changes=changes,
        topology=topology,
        faults=faults,
        requests=requests_tuple,
        source_files=loader.source_files,
        issues=loader.issues,
    )
