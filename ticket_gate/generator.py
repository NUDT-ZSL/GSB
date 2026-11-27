from __future__ import annotations

import argparse
import json
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .graph import build_plan
from .parser import load_project


TZ = timezone(timedelta(hours=8))
EVENT_COUNT = 12
ENTRANCE_COUNT = 6
GATES_PER_ENTRANCE = 8


def _event_rule(event_index: int, version: str) -> dict[str, Any]:
    event_no = event_index + 1
    group = event_index // 4
    day_offset = group
    start_minute = 16 * 60 + 30 + (event_index % 4) * 20
    end_minute = 21 * 60 + 30 + (event_index % 4) * 10

    if event_no == 7:
        start_minute = 17 * 60 + 50 if version == "v1" else 17 * 60 + 20

    day = datetime(2026, 12, 12, tzinfo=TZ) + timedelta(days=day_offset)
    start = day + timedelta(minutes=start_minute)
    end = day + timedelta(minutes=end_minute)

    whitelist = ["ADULT", "CHILD", "VIP"]
    if event_no == 1:
        whitelist = ["ADULT", "CHILD"] if version == "v1" else [
            "ADULT",
            "CHILD",
            "VIP",
        ]
    elif event_no % 5 == 0:
        whitelist.append("STAFF")

    reentry_policy = "single" if event_no % 2 == 0 else "unlimited"
    if event_no == 4:
        reentry_policy = "unlimited" if version == "v1" else "single"

    requires_adult = event_no % 2 == 1
    if event_no == 10:
        requires_adult = False if version == "v1" else True

    return {
        "ticket_whitelist": whitelist,
        "entry_window": {
            "start": start.isoformat(),
            "end": end.isoformat(),
        },
        "reentry_policy": reentry_policy,
        "child_policy": {"requires_adult": requires_adult},
    }


def _build_base_documents(seed: int) -> dict[str, Any]:
    events: dict[str, Any] = {}
    for event_index in range(EVENT_COUNT):
        event_id = f"EVT-{event_index + 1:02d}"
        events[event_id] = {
            "name": f"演出场次 {event_index + 1}",
            "current_version": "v2",
            "versions": {
                "v1": _event_rule(event_index, "v1"),
                "v2": _event_rule(event_index, "v2"),
            },
        }

    gates: list[dict[str, Any]] = []
    for gate_index in range(ENTRANCE_COUNT * GATES_PER_ENTRANCE):
        number = gate_index + 1
        entrance_index = gate_index // GATES_PER_ENTRANCE
        within_entrance = gate_index % GATES_PER_ENTRANCE
        primary_event = (gate_index % EVENT_COUNT) + 1
        subscriptions = {primary_event}
        if number % 4 == 0:
            subscriptions.add(((primary_event + 2) % EVENT_COUNT) + 1)
        if number == 10:
            subscriptions.add(11)
        if number % 7 == 0:
            subscriptions.add(((primary_event + 4) % EVENT_COUNT) + 1)
        if number % 11 == 0:
            subscriptions.add(((primary_event + 8) % EVENT_COUNT) + 1)

        gates.append(
            {
                "gate_id": f"GATE-{number:03d}",
                "name": f"闸机 {number:03d}",
                "entrance_id": f"ENT-{entrance_index:02d}",
                "seat_zone_ids": [
                    f"ZONE-{entrance_index:02d}-{(within_entrance % 2) + 1:02d}"
                ],
                "subscriptions": [
                    f"EVT-{event_no:02d}" for event_no in sorted(subscriptions)
                ],
            }
        )

    entrances: list[dict[str, Any]] = []
    for entrance_index in range(ENTRANCE_COUNT):
        dependencies: list[str] = []
        if entrance_index > 0:
            dependencies.append(f"ENT-{entrance_index - 1:02d}")
        if entrance_index == 1:
            dependencies.append("ENT-02")
        entrances.append(
            {
                "entrance_id": f"ENT-{entrance_index:02d}",
                "depends_on_entrance_ids": sorted(set(dependencies)),
            }
        )

    seat_zones: list[dict[str, Any]] = []
    for entrance_index in range(ENTRANCE_COUNT):
        for zone_index in range(2):
            dependencies = []
            if zone_index == 1:
                dependencies.append(f"ZONE-{entrance_index:02d}-01")
            seat_zones.append(
                {
                    "seat_zone_id": f"ZONE-{entrance_index:02d}-{zone_index + 1:02d}",
                    "depends_on_seat_zone_ids": dependencies,
                }
            )

    changes = [
        {
            "event_id": event_id,
            "from_version": "v1",
            "to_version": "v2",
        }
        for event_id in ("EVT-01", "EVT-04", "EVT-07", "EVT-10")
    ]

    manifest = {
        "schema_version": 1,
        "project_id": f"venue-rule-rehearsal-seed-{seed}",
        "seed": seed,
        "description": "Deterministic ticket-rule propagation rehearsal project",
        "generator": "ticket_gate.generator",
        "files": {
            "events": "config/events.json",
            "gates": "config/gates.json",
            "topology": "config/topology.json",
            "changes": "config/changes.json",
            "faults": "config/faults.json",
            "verification_requests": "config/verification_requests.json",
        },
        "conflict_definition": (
            "A gate conflicts when two subscribed events have overlapping entry "
            "windows and shared ticket types, but different reentry or child policies."
        ),
    }

    return {
        "manifest.json": manifest,
        "config/events.json": {"events": events},
        "config/gates.json": {"gates": gates},
        "config/topology.json": {
            "entrances": entrances,
            "seat_zones": seat_zones,
        },
        "config/changes.json": {"changes": changes},
        "config/faults.json": {"faults": {}},
        "config/verification_requests.json": {"requests": []},
    }


def _write_documents(root: Path, documents: dict[str, Any]) -> None:
    root.mkdir(parents=True, exist_ok=True)
    for relative_path, document in sorted(documents.items()):
        path = root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(document, handle, ensure_ascii=False, indent=2)
            handle.write("\n")


def _pick_faults(
    seed: int,
    batches: list[list[str]],
    plan_gate_ids: tuple[str, ...],
) -> dict[str, Any]:
    if not plan_gate_ids:
        return {}
    rng = random.Random(seed)
    first_batch = batches[0] if batches else list(plan_gate_ids)
    retry_gate = first_batch[rng.randrange(len(first_batch))]

    nonretry_candidates = [
        gate_id
        for batch in batches[1:]
        for gate_id in batch
        if gate_id != retry_gate
    ]
    if not nonretry_candidates:
        nonretry_candidates = [
            gate_id for gate_id in plan_gate_ids if gate_id != retry_gate
        ]
    nonretry_gate = (
        nonretry_candidates[rng.randrange(len(nonretry_candidates))]
        if nonretry_candidates
        else None
    )

    faults: dict[str, Any] = {
        retry_gate: {
            "attempts": [
                {
                    "attempt": 1,
                    "error_code": "GATE_LINK_TIMEOUT",
                    "retryable": True,
                },
                {
                    "attempt": 2,
                    "error_code": "GATE_LINK_TIMEOUT",
                    "retryable": True,
                },
            ]
        }
    }
    if nonretry_gate is not None:
        faults[nonretry_gate] = {
            "attempts": [
                {
                    "attempt": 1,
                    "error_code": "RULE_PACKAGE_SIGNATURE_INVALID",
                    "retryable": False,
                }
            ]
        }
    return {"faults": faults}


def _iso(moment: datetime) -> str:
    return moment.isoformat(timespec="seconds")


def _build_requests(project: Any) -> dict[str, Any]:
    plan = build_plan(project)
    affected = list(plan.affected_gate_ids)
    request_number = 0
    requests: list[dict[str, Any]] = []

    def add(
        gate_id: str,
        event_id: str,
        at: datetime,
        ticket_type: str,
        *,
        prior_entries: int = 0,
        is_child: bool = False,
        accompanied_adult_count: int = 0,
        after_batch: int | None = None,
        scenario: str,
    ) -> None:
        nonlocal request_number
        request_number += 1
        requests.append(
            {
                "request_id": f"REQ-{request_number:04d}",
                "scenario": scenario,
                "gate_id": gate_id,
                "event_id": event_id,
                "at": _iso(at),
                "ticket_type": ticket_type,
                "prior_entries": prior_entries,
                "is_child": is_child,
                "accompanied_adult_count": accompanied_adult_count,
                "after_batch": after_batch,
            }
        )

    for gate_id in affected:
        gate = project.gates[gate_id]
        changed_subscriptions = sorted(set(gate.subscriptions).intersection(project.changes))
        event_id = changed_subscriptions[0] if changed_subscriptions else gate.subscriptions[0]
        version = project.target_version(event_id)
        rule = project.rule(event_id, version)
        midpoint = rule.start_at + (rule.end_at - rule.start_at) / 2
        primary_ticket = rule.ticket_whitelist[0]
        child_ticket = "CHILD" if "CHILD" in rule.ticket_whitelist else primary_ticket

        add(gate_id, event_id, midpoint, primary_ticket, scenario="allowed-current")
        add(
            gate_id,
            event_id,
            midpoint,
            "NOT-A-TICKET",
            scenario="ticket-not-whitelisted",
        )
        add(
            gate_id,
            event_id,
            rule.start_at - timedelta(minutes=15),
            primary_ticket,
            scenario="before-window",
        )
        add(
            gate_id,
            event_id,
            rule.end_at + timedelta(minutes=15),
            primary_ticket,
            scenario="after-window",
        )
        add(
            gate_id,
            event_id,
            midpoint,
            primary_ticket,
            prior_entries=2,
            scenario="reentry",
        )
        add(
            gate_id,
            event_id,
            midpoint,
            child_ticket,
            is_child=True,
            accompanied_adult_count=0,
            scenario="child-without-adult",
        )
        add(
            gate_id,
            event_id,
            midpoint,
            primary_ticket,
            after_batch=0,
            scenario="after-first-batch-snapshot",
        )

    return {"requests": requests}


def generate_project(
    output_dir: str | Path,
    seed: int = 42,
    overwrite: bool = False,
) -> Path:
    root = Path(output_dir).resolve()
    if root.exists() and any(root.iterdir()) and not overwrite:
        raise FileExistsError(
            f"output directory is not empty: {root}; pass --overwrite to replace generated files"
        )

    documents = _build_base_documents(seed)
    _write_documents(root, documents)

    project = load_project(root)
    if not project.valid:
        details = "\n".join(
            f"{issue.code} {issue.path}: {issue.message}" for issue in project.issues
        )
        raise RuntimeError(f"generated project failed validation:\n{details}")

    plan = build_plan(project)
    fault_document = _pick_faults(seed, plan.batch_gate_ids(), plan.affected_gate_ids)
    documents["config/faults.json"] = fault_document
    _write_documents(root, {"config/faults.json": fault_document})

    project = load_project(root)
    if not project.valid:
        details = "\n".join(
            f"{issue.code} {issue.path}: {issue.message}" for issue in project.issues
        )
        raise RuntimeError(f"project with faults failed validation:\n{details}")
    requests = _build_requests(project)
    documents["config/verification_requests.json"] = requests
    _write_documents(root, {"config/verification_requests.json": requests})

    final_project = load_project(root)
    if not final_project.valid:
        details = "\n".join(
            f"{issue.code} {issue.path}: {issue.message}" for issue in final_project.issues
        )
        raise RuntimeError(f"generated project failed final validation:\n{details}")
    return root


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate a deterministic ticket-rule project")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--out", required=True)
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args(argv)
    root = generate_project(args.out, args.seed, args.overwrite)
    print(root)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
