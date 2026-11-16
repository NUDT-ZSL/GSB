"""Pure, Tk-independent view model with stable render ordering."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List

from .dag_loader import parse_dag
from .snapshot import Snapshot


@dataclass
class StageRow:
    stage_id: str
    state: str
    strong_deps: str
    weak_deps: str
    resource_units: int
    output_sha256: str
    attempts: int
    missing_upstreams: str


@dataclass
class TimelineBand:
    stage_id: str
    start_mono_ms: int
    end_mono_ms: int
    resource_units: int
    state: str


@dataclass
class DashboardModel:
    title: str
    dag_version: str
    budget: int
    edges: List[tuple]
    stage_rows: List[StageRow]
    timeline: List[TimelineBand]
    waiting: List[dict]
    resource_curve: List[dict]
    snapshots: List[dict]


def build_dashboard_model(
    snapshot: Snapshot, snapshot_files: List[dict] | None = None
) -> DashboardModel:
    dag = parse_dag(snapshot.dag_definition)
    rows: List[StageRow] = []
    bands: List[TimelineBand] = []
    for stage_id in dag.stage_ids_sorted():
        stage = dag.stages[stage_id]
        result = snapshot.states.get(stage_id)
        state = result.state if result else "pending"
        rows.append(
            StageRow(
                stage_id=stage_id,
                state=state,
                strong_deps=",".join(stage.strong_deps),
                weak_deps=",".join(stage.weak_deps),
                resource_units=stage.resource_units,
                output_sha256=(result.output_sha256 or "") if result else "",
                attempts=len(result.attempts) if result else 0,
                missing_upstreams=",".join(result.missing_upstreams)
                if result
                else "",
            )
        )
        if result and result.start_mono_ms is not None and result.end_mono_ms is not None:
            bands.append(
                TimelineBand(
                    stage_id=stage_id,
                    start_mono_ms=result.start_mono_ms,
                    end_mono_ms=result.end_mono_ms,
                    resource_units=stage.resource_units,
                    state=state,
                )
            )

    edges: List[tuple] = []
    for stage_id in dag.stage_ids_sorted():
        for dep in dag.stages[stage_id].dependencies:
            edges.append((dep.stage_id, stage_id, dep.kind))
    edges.sort()

    waiting_events = [
        event.to_dict()
        for event in snapshot.trace
        if event.event == "waiting"
    ]
    waiting_events.sort(key=lambda item: (item["stage_id"], item["seq"]))

    resource_curve = _resource_curve(snapshot)
    return DashboardModel(
        title=f"Pipeline dashboard - {snapshot.session_id}",
        dag_version=snapshot.dag_version,
        budget=snapshot.global_resource_budget,
        edges=edges,
        stage_rows=rows,
        timeline=sorted(bands, key=lambda band: (band.start_mono_ms, band.stage_id)),
        waiting=waiting_events,
        resource_curve=resource_curve,
        snapshots=sorted(snapshot_files or [], key=lambda item: item["path"]),
    )


def _resource_curve(snapshot: Snapshot) -> List[dict]:
    used = 0
    points: List[dict] = []
    for event in sorted(snapshot.trace, key=lambda item: item.seq):
        if event.event == "state_change" and event.reason == "ready_to_running":
            used += event.resource_units
            points.append(
                {
                    "mono_ms": event.mono_ms,
                    "used": used,
                    "stage_id": event.stage_id,
                    "direction": "acquire",
                }
            )
        elif event.event == "state_change" and event.reason.startswith("running_to_"):
            used -= event.resource_units
            points.append(
                {
                    "mono_ms": event.mono_ms,
                    "used": used,
                    "stage_id": event.stage_id,
                    "direction": "release",
                }
            )
    return points
