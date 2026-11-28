"""Placement, capacity control, timeline simulation, and reconciliation."""

from __future__ import annotations

import copy
import math
from collections import defaultdict
from typing import Any, Iterable

from .models import (
    Incident,
    Project,
    Region,
    StorageObject,
    TimelineEvent,
)


def _round(value: float) -> float:
    return round(float(value), 6)


def _ratio(value: int, capacity: int) -> float:
    return _round(value / capacity) if capacity else 0.0


class Planner:
    """Deterministic storage planner for one validated exchange project."""

    def __init__(self, project: Project, seed: int | None = None) -> None:
        self.project = project
        self.seed = project.source_seed if seed is None else int(seed)
        self.policy = project.policy
        self.regions: dict[str, Region] = copy.deepcopy(project.regions)
        self.objects = project.objects
        self.institutions = project.institutions

        self.refcounts: dict[str, int] = {
            object_id: obj.reference_count
            for object_id, obj in self.objects.items()
        }
        self.assignments: dict[str, str] = {}
        self.region_refs: dict[str, dict[str, dict[str, Any]]] = {
            region_id: {} for region_id in self.regions
        }
        self.reference_operations: list[dict[str, Any]] = []
        self.event_log: list[dict[str, Any]] = []
        self.incident_log: list[dict[str, Any]] = []
        self.relocation_batches: list[dict[str, Any]] = []
        self.timeline: list[dict[str, Any]] = []

        self.capacity_repair_moves: list[dict[str, Any]] = []
        self.local_adjustment_moves: list[dict[str, Any]] = []
        self.preferred_assignment: dict[str, str] = {}
        self.initial_unresolved_regions: list[dict[str, Any]] = []
        self.initial_assignment_report: list[dict[str, Any]] = []
        self.initial_unassigned_ids: list[str] = []
        self.initial_audit: dict[str, Any] = {}
        self.initial_distributions: dict[str, Any] = {}

        self._batch_counter = 0
        self._current_time: float = 0.0

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def run(self) -> dict[str, Any]:
        self._initial_place()
        initial_audit = self._reconcile("initial_assignment")
        self.initial_audit = initial_audit

        self.initial_distributions = {
            "before_placement": self._distribution(
                "before_placement",
                {},
                self._active_ids(),
                include_satisfaction=True,
            ),
            "preferred_unconstrained": self._distribution(
                "preferred_unconstrained",
                self.preferred_assignment,
                self._active_ids(),
                include_satisfaction=True,
            ),
            "after_capacity_assignment": self._distribution(
                "after_capacity_assignment",
                self.assignments,
                self._active_ids(),
                include_satisfaction=True,
            ),
        }

        self.timeline.append(
            self._snapshot(
                at=0.0,
                phase="initial_placement",
                events=[],
                incidents=[],
                batch_ids=[],
                reconciliation_ok=initial_audit["ok"],
            )
        )

        times = sorted(
            {event.at for event in self.project.events}
            | {incident.at for incident in self.project.incidents}
        )
        for at in times:
            self._process_time(at)

        final_audit = self._reconcile("final")
        final_events = [
            entry
            for entry in self.event_log
            if entry["at"] == (times[-1] if times else 0.0)
        ]
        if not self.timeline or self.timeline[-1]["at"] != (
            times[-1] if times else 0.0
        ):
            self.timeline.append(
                self._snapshot(
                    at=(times[-1] if times else 0.0),
                    phase="final",
                    events=final_events,
                    incidents=[],
                    batch_ids=[],
                    reconciliation_ok=final_audit["ok"],
                )
            )

        final_distribution = self._distribution(
            "after_timeline",
            self.assignments,
            self._active_ids(),
            include_satisfaction=True,
        )
        comparison = self._distribution_comparison(
            self.initial_distributions,
            final_distribution,
        )
        progress = self._relocation_progress()
        execution_audit = self._execution_audit(final_audit)

        return {
            "schema_version": 1,
            "metadata": {
                "project_id": self.project.project_id,
                "seed": self.seed,
                "object_count": len(self.objects),
                "region_count": len(self.regions),
                "institution_count": len(self.institutions),
                "source_metadata": self.project.metadata,
            },
            "validation": {"valid": True, "error_count": 0, "errors": []},
            "formula": self._formula_description(),
            "assignment": {
                "objects": self.initial_assignment_report,
                # Frozen at the initial capacity-aware placement. Later
                # expirations must not rewrite the initial assignment report.
                "region_totals": self.initial_distributions[
                    "after_capacity_assignment"
                ]["regions"],
                "unassigned_object_ids": self.initial_unassigned_ids,
            },
            "capacity_control": self._capacity_control_report(),
            "events_processed": self.event_log,
            "incidents_processed": self.incident_log,
            "timeline": self.timeline,
            "relocations": self.relocation_batches,
            "relocation_progress": progress,
            "reconciliation": final_audit,
            "initial_reconciliation": initial_audit,
            "distribution_comparison": comparison,
            "execution_audit": execution_audit,
            "region_reference_lists": self._region_reference_lists(),
        }

    # ------------------------------------------------------------------
    # Initial placement and capacity control
    # ------------------------------------------------------------------

    def _initial_place(self) -> None:
        ordered = sorted(
            self.objects.values(),
            key=lambda obj: (
                -self._weight(obj),
                -obj.bytes,
                obj.id,
            ),
        )
        for obj in ordered:
            target = self._best_region(obj, enforce_capacity=False)
            if target is not None:
                self.assignments[obj.id] = target[0]

        self.preferred_assignment = dict(self.assignments)
        self._repair_capacity()
        self._local_adjust()
        self._rebuild_initial_references()
        self.initial_assignment_report = self._build_assignment_report()
        self.initial_unassigned_ids = sorted(
            set(self.objects) - set(self.assignments)
        )
        self.initial_unresolved_regions = self._unresolved_capacity_report()

    def _repair_capacity(self) -> None:
        loads = self._loads(self.assignments)
        move_counts: dict[str, int] = defaultdict(int)
        max_iterations = max(100, len(self.objects) * 20)

        for _ in range(max_iterations):
            overfull = [
                region_id
                for region_id in sorted(self.regions)
                if loads[region_id] > self._limit(region_id)
            ]
            if not overfull:
                break

            choices: list[tuple[tuple[float, float, int, str, str], str, str, float, float]] = []
            for source_id in overfull:
                source_region = self.regions[source_id]
                residents = sorted(
                    self._residents(source_id),
                    key=lambda object_id: (
                        -self.objects[object_id].bytes,
                        object_id,
                    ),
                )
                for object_id in residents:
                    obj = self.objects[object_id]
                    if move_counts[object_id] >= 3:
                        continue
                    source_score = self._score(obj, source_region)
                    for target_id in sorted(self.regions):
                        if target_id == source_id:
                            continue
                        target_region = self.regions[target_id]
                        if not self._eligible(obj, target_region):
                            continue
                        if (
                            loads[target_id] + obj.bytes
                            > self._limit(target_id)
                        ):
                            continue
                        target_score = self._score(obj, target_region)
                        loss = source_score - target_score
                        cost = (
                            loss / max(1, obj.bytes),
                            loss,
                            -obj.bytes,
                            object_id,
                            target_id,
                        )
                        choices.append(
                            (
                                cost,
                                object_id,
                                target_id,
                                source_score,
                                target_score,
                            )
                        )
            if not choices:
                break

            _, object_id, target_id, source_score, target_score = min(choices)
            source_id = self.assignments[object_id]
            obj = self.objects[object_id]
            loads[source_id] -= obj.bytes
            loads[target_id] += obj.bytes
            self.assignments[object_id] = target_id
            move_counts[object_id] += 1
            self.capacity_repair_moves.append(
                {
                    "object_id": object_id,
                    "institution_id": obj.institution_id,
                    "source_region_id": source_id,
                    "target_region_id": target_id,
                    "bytes": obj.bytes,
                    "score_before": _round(source_score),
                    "score_after": _round(target_score),
                    "score_loss": _round(source_score - target_score),
                }
            )

    def _local_adjust(self) -> None:
        loads = self._loads(self.assignments)
        for _ in range(8):
            changed = False
            for object_id in sorted(self.assignments):
                obj = self.objects[object_id]
                source_id = self.assignments[object_id]
                current_score = self._score(obj, self.regions[source_id])
                best: tuple[float, str] | None = None
                for target_id in sorted(self.regions):
                    if target_id == source_id:
                        continue
                    target = self.regions[target_id]
                    if not self._eligible(obj, target):
                        continue
                    if loads[target_id] + obj.bytes > self._limit(target_id):
                        continue
                    target_score = self._score(obj, target)
                    if target_score <= current_score:
                        continue
                    if best is None or target_score > best[0]:
                        best = (target_score, target_id)
                if best is None:
                    continue
                target_score, target_id = best
                loads[source_id] -= obj.bytes
                loads[target_id] += obj.bytes
                self.assignments[object_id] = target_id
                changed = True
                self.local_adjustment_moves.append(
                    {
                        "object_id": object_id,
                        "institution_id": obj.institution_id,
                        "source_region_id": source_id,
                        "target_region_id": target_id,
                        "bytes": obj.bytes,
                        "score_before": _round(current_score),
                        "score_after": _round(target_score),
                    }
                )
            if not changed:
                break

    def _rebuild_initial_references(self) -> None:
        self.region_refs = {region_id: {} for region_id in self.regions}
        self.reference_operations = []
        for object_id in sorted(self.assignments):
            self._ref_add(
                0.0,
                object_id,
                self.assignments[object_id],
                "initial_placement",
            )

    # ------------------------------------------------------------------
    # Scoring and filtering
    # ------------------------------------------------------------------

    def _weight(self, obj: StorageObject) -> float:
        return self.policy.urgency_weights[obj.urgency]

    def _latency_limit(self, obj: StorageObject) -> float:
        institution = self.institutions[obj.institution_id]
        return min(self.policy.max_latency_ms, institution.max_latency_ms)

    def _score(self, obj: StorageObject, region: Region) -> float:
        return _round(
            self._weight(obj) * 1000.0 / (float(region.latency_ms) + 1.0)
        )

    def _screen(
        self, obj: StorageObject, region: Region
    ) -> tuple[bool, str | None, str | None]:
        if region.health != "healthy":
            return (
                False,
                f"EXCLUDED_HEALTH_{region.health.upper()}",
                f"区域健康状态为 {region.health}",
            )
        latency_limit = self._latency_limit(obj)
        if region.latency_ms > latency_limit:
            return (
                False,
                "EXCLUDED_LATENCY_LIMIT",
                (
                    f"区域时延 {region.latency_ms:g} ms 超过上限 "
                    f"{latency_limit:g} ms"
                ),
            )
        required = self.institutions[obj.institution_id].required_compliance_level
        if region.compliance_level < required:
            return (
                False,
                "EXCLUDED_COMPLIANCE_LEVEL",
                (
                    f"区域合规级别 {region.compliance_level} 低于机构要求 "
                    f"{required}"
                ),
            )
        return True, None, None

    def _eligible(self, obj: StorageObject, region: Region) -> bool:
        return self._screen(obj, region)[0]

    def _best_region(
        self,
        obj: StorageObject,
        *,
        excluded: Iterable[str] = (),
        loads: dict[str, int] | None = None,
        enforce_capacity: bool = False,
    ) -> tuple[str, float] | None:
        excluded_set = set(excluded)
        options: list[tuple[float, str]] = []
        for region_id in sorted(self.regions):
            if region_id in excluded_set:
                continue
            region = self.regions[region_id]
            if not self._eligible(obj, region):
                continue
            if (
                enforce_capacity
                and loads is not None
                and loads[region_id] + obj.bytes > self._limit(region_id)
            ):
                continue
            options.append((self._score(obj, region), region_id))
        if not options:
            return None
        score, region_id = min(options, key=lambda item: (-item[0], item[1]))
        return region_id, score

    def _best_region_with_reason(
        self,
        obj: StorageObject,
        *,
        excluded: Iterable[str] = (),
        loads: dict[str, int],
    ) -> tuple[str | None, float | None, str | None]:
        excluded_set = set(excluded)
        eligible_without_capacity: list[tuple[float, str]] = []
        fitting: list[tuple[float, str]] = []
        for region_id in sorted(self.regions):
            if region_id in excluded_set:
                continue
            region = self.regions[region_id]
            if not self._eligible(obj, region):
                continue
            score = self._score(obj, region)
            eligible_without_capacity.append((score, region_id))
            if loads[region_id] + obj.bytes <= self._limit(region_id):
                fitting.append((score, region_id))
        if fitting:
            score, region_id = min(
                fitting, key=lambda item: (-item[0], item[1])
            )
            return region_id, score, None
        if eligible_without_capacity:
            return None, None, "TARGET_CAPACITY_EXCEEDED"
        return None, None, "NO_ELIGIBLE_TARGET"

    # ------------------------------------------------------------------
    # Timeline processing
    # ------------------------------------------------------------------

    def _process_time(self, at: float) -> None:
        self._current_time = at
        events_at = sorted(
            (event for event in self.project.events if event.at == at),
            key=lambda event: (
                -self._weight(self.objects[event.object_id]),
                0 if event.type == "expire" else 1,
                event.object_id,
                event.index,
            ),
        )
        event_rows = [self._process_event(event) for event in events_at]

        incidents_at = sorted(
            (incident for incident in self.project.incidents if incident.at == at),
            key=lambda incident: incident.index,
        )
        immediate = [
            incident
            for incident in incidents_at
            if incident.after_migration_step is None
        ]
        deferred = [
            incident
            for incident in incidents_at
            if incident.after_migration_step is not None
        ]
        immediate_rows = [self._apply_incident(at, incident) for incident in immediate]

        health_changed = {
            row["region_id"]
            for row in immediate_rows
            if row["kind"] == "health" and row["health"] != "healthy"
        }
        watermark_changed = {
            row["region_id"]
            for row in immediate_rows
            if row["kind"] == "watermark"
        }

        affected = self._derive_affected(health_changed, watermark_changed)
        batch_ids: list[str] = []
        deferred_health_changed: set[str] = set()
        deferred_watermark_changed: set[str] = set()

        if affected:
            batch = self._execute_relocation_batch(
                at,
                affected,
                triggers=[
                    *immediate_rows,
                ],
                deferred=deferred,
            )
            batch_ids.append(batch["batch_id"])
            deferred_health_changed.update(batch["deferred_health_changed"])
            deferred_watermark_changed.update(
                batch["deferred_watermark_changed"]
            )

        # Incidents deferred past the last migration step (or with no migration
        # plan) still take effect at this logical time.
        if deferred:
            remaining = [incident for incident in deferred if incident.index not in {
                row["incident_index"] for row in self.incident_log if row["at"] == at
            }]
            for incident in remaining:
                row = self._apply_incident(at, incident)
                if row["kind"] == "health" and row["health"] != "healthy":
                    deferred_health_changed.add(row["region_id"])
                elif row["kind"] == "watermark":
                    deferred_watermark_changed.add(row["region_id"])

        # A target can become unhealthy while an earlier migration is running.
        # Objects already accepted by that target are affected by the health
        # downgrade and get one focused follow-up pass.
        followup = self._derive_affected(
            deferred_health_changed, deferred_watermark_changed
        )
        if followup:
            batch = self._execute_relocation_batch(
                at,
                followup,
                triggers=[
                    {
                        "kind": "followup",
                        "reason": "TARGET_OR_THRESHOLD_CHANGED_DURING_MIGRATION",
                    }
                ],
                deferred=[],
            )
            batch_ids.append(batch["batch_id"])

        # Local capacity state can also cross a threshold because of an
        # externally supplied baseline update; report one final focused pass.
        overfull = [
            region_id
            for region_id in sorted(self.regions)
            if self._effective_used(region_id) > self._limit(region_id)
        ]
        if overfull:
            already_affected = {
                entry["object_id"]
                for batch in self.relocation_batches
                if batch["at"] == at
                for entry in batch["plan"]
            }
            overflow_affected = [
                item
                for item in self._derive_affected(set(), set(overfull))
                if item["object_id"] not in already_affected
            ]
            if overflow_affected:
                batch = self._execute_relocation_batch(
                    at,
                    overflow_affected,
                    triggers=[
                        {
                            "kind": "auto",
                            "reason": "REGION_WATERMARK_EXCEEDED",
                            "region_ids": overfull,
                        }
                    ],
                    deferred=[],
                )
                batch_ids.append(batch["batch_id"])

        audit = self._reconcile(f"timeline:{at:g}")
        self.timeline.append(
            self._snapshot(
                at=at,
                phase="event_time",
                events=event_rows,
                incidents=[
                    row
                    for row in self.incident_log
                    if row["at"] == at
                ],
                batch_ids=batch_ids,
                reconciliation_ok=audit["ok"],
            )
        )

    def _process_event(self, event: TimelineEvent) -> dict[str, Any]:
        obj = self.objects[event.object_id]
        before = self.refcounts[event.object_id]
        removed = False
        state = "retained"
        if event.type == "access":
            after = before + event.delta
            self.refcounts[event.object_id] = after
            if obj.id in self.assignments:
                region_id = self.assignments[obj.id]
                self.region_refs[region_id][obj.id][
                    "reference_count"
                ] = after
                state = "updated_reference_count"
            else:
                state = "unassigned"
        else:
            after = max(0, before - event.delta)
            self.refcounts[event.object_id] = after
            if after == 0 and obj.id in self.assignments:
                region_id = self.assignments.pop(obj.id)
                self._ref_remove(
                    self._current_time,
                    obj.id,
                    region_id,
                    "expiration_zero_reference",
                )
                removed = True
                state = "removed_zero_reference"
            elif obj.id in self.assignments:
                region_id = self.assignments[obj.id]
                self.region_refs[region_id][obj.id][
                    "reference_count"
                ] = after
                state = "updated_reference_count"
            elif before == 0:
                state = "already_released"
            else:
                state = "unassigned"

        row = {
            "at": event.at,
            "sequence": event.index,
            "type": event.type,
            "object_id": obj.id,
            "institution_id": obj.institution_id,
            "urgency": obj.urgency,
            "urgency_weight": _round(self._weight(obj)),
            "delta": event.delta,
            "reference_count_before": before,
            "reference_count_after": self.refcounts[obj.id],
            "removed_from_region": removed,
            "state": state,
        }
        self.event_log.append(row)
        return row

    def _apply_incident(
        self, at: float, incident: Incident
    ) -> dict[str, Any]:
        region = self.regions[incident.region_id]
        if incident.type in {"health_degraded", "health_change"}:
            before = region.health
            region.health = incident.health or "degraded"
            row = {
                "at": at,
                "incident_index": incident.index,
                "kind": "health",
                "type": incident.type,
                "region_id": region.id,
                "health_before": before,
                "health": region.health,
                "after_migration_step": incident.after_migration_step,
            }
        else:
            before_ratio = region.watermark_ratio
            if incident.watermark_ratio is not None:
                region.watermark_ratio = float(incident.watermark_ratio)
            row = {
                "at": at,
                "incident_index": incident.index,
                "kind": "watermark",
                "type": incident.type,
                "region_id": region.id,
                "watermark_before": before_ratio,
                "watermark_ratio": region.watermark_ratio,
                "after_migration_step": incident.after_migration_step,
            }
        self.incident_log.append(row)
        return row

    # ------------------------------------------------------------------
    # Relocation planning and execution
    # ------------------------------------------------------------------

    def _derive_affected(
        self,
        health_changed: set[str],
        watermark_changed: set[str],
    ) -> list[dict[str, Any]]:
        affected: list[dict[str, Any]] = []
        seen: set[str] = set()

        health_rows = []
        for region_id in sorted(health_changed):
            if self.regions[region_id].health == "healthy":
                continue
            for object_id in self._residents(region_id):
                health_rows.append(object_id)
        health_rows.sort(
            key=lambda object_id: (
                -self._weight(self.objects[object_id]),
                object_id,
            )
        )
        for object_id in health_rows:
            if object_id in seen:
                continue
            seen.add(object_id)
            affected.append(
                {
                    "object_id": object_id,
                    "source_region_id": self.assignments[object_id],
                    "reason_code": "REGION_HEALTH_DEGRADED",
                }
            )

        overload_rows: list[str] = []
        for region_id in sorted(watermark_changed):
            effective = self._effective_used(region_id)
            limit = self._limit(region_id)
            if effective <= limit:
                continue
            required = effective - limit
            freed = 0
            residents = sorted(
                self._residents(region_id),
                key=lambda object_id: (
                    self._weight(self.objects[object_id]),
                    -self.objects[object_id].bytes,
                    object_id,
                ),
            )
            for object_id in residents:
                if freed >= required:
                    break
                if self.objects[object_id].bytes == 0:
                    continue
                overload_rows.append(object_id)
                freed += self.objects[object_id].bytes
        for object_id in overload_rows:
            if object_id in seen:
                continue
            seen.add(object_id)
            affected.append(
                {
                    "object_id": object_id,
                    "source_region_id": self.assignments[object_id],
                    "reason_code": "REGION_WATERMARK_EXCEEDED",
                }
            )
        return affected

    def _execute_relocation_batch(
        self,
        at: float,
        affected: list[dict[str, Any]],
        *,
        triggers: list[dict[str, Any]],
        deferred: list[Incident],
    ) -> dict[str, Any]:
        self._batch_counter += 1
        batch_id = f"RELOC-{self._batch_counter:04d}"
        temp_loads = self._loads(self.assignments)
        entries: list[dict[str, Any]] = []

        for sequence, item in enumerate(affected, start=1):
            object_id = item["object_id"]
            if object_id not in self.assignments:
                continue
            obj = self.objects[object_id]
            source_id = self.assignments[object_id]
            target_id, target_score, failure_reason = (
                self._best_region_with_reason(
                    obj,
                    excluded={source_id},
                    loads=temp_loads,
                )
            )
            if target_id is not None:
                temp_loads[target_id] += obj.bytes
                predicted_target = temp_loads[target_id]
            else:
                predicted_target = None
            entries.append(
                {
                    "sequence": sequence,
                    "object_id": obj.id,
                    "institution_id": obj.institution_id,
                    "urgency": obj.urgency,
                    "bytes": obj.bytes,
                    "source_region_id": source_id,
                    "target_region_id": target_id,
                    "target_score": (
                        _round(target_score) if target_score is not None else None
                    ),
                    "estimated_byte_delta": obj.bytes if target_id else 0,
                    "source_byte_delta": -obj.bytes if target_id else 0,
                    "target_effective_bytes_after": predicted_target,
                    "target_watermark_after": (
                        _ratio(predicted_target, self.regions[target_id].capacity_bytes)
                        if target_id is not None
                        else None
                    ),
                    "status": "pending",
                    "failure_reason_code": failure_reason,
                    "failure_message": self._failure_message(failure_reason),
                    "status_history": [
                        {"at": at, "status": "pending", "reason_code": None}
                    ],
                    "source_retained": True,
                }
            )

        deferred = sorted(
            deferred,
            key=lambda incident: (
                incident.after_migration_step or 0,
                incident.index,
            ),
        )
        deferred_applied: list[dict[str, Any]] = []
        deferred_health_changed: set[str] = set()
        deferred_watermark_changed: set[str] = set()

        for entry in entries:
            if entry["failure_reason_code"] is not None:
                entry["status"] = "failed"
                entry["status_history"].append(
                    {
                        "at": at,
                        "status": "failed",
                        "reason_code": entry["failure_reason_code"],
                    }
                )
                self._apply_deferred_for_step(
                    at,
                    entry["sequence"],
                    deferred,
                    deferred_applied,
                    deferred_health_changed,
                    deferred_watermark_changed,
                )
                continue

            object_id = entry["object_id"]
            target_id = entry["target_region_id"]
            source_id = entry["source_region_id"]
            obj = self.objects[object_id]
            if self.assignments.get(object_id) != source_id:
                entry["status"] = "failed"
                entry["failure_reason_code"] = "SOURCE_REFERENCE_CHANGED"
                entry["failure_message"] = self._failure_message(
                    "SOURCE_REFERENCE_CHANGED"
                )
                entry["status_history"].append(
                    {
                        "at": at,
                        "status": "failed",
                        "reason_code": "SOURCE_REFERENCE_CHANGED",
                    }
                )
                self._apply_deferred_for_step(
                    at,
                    entry["sequence"],
                    deferred,
                    deferred_applied,
                    deferred_health_changed,
                    deferred_watermark_changed,
                )
                continue

            target = self.regions[target_id]
            if target.health != "healthy":
                entry["status"] = "failed"
                entry["failure_reason_code"] = "TARGET_HEALTH_DEGRADED"
                entry["failure_message"] = (
                    f"迁移途中目标区域健康状态为 {target.health}"
                )
                entry["status_history"].append(
                    {
                        "at": at,
                        "status": "failed",
                        "reason_code": "TARGET_HEALTH_DEGRADED",
                    }
                )
            elif (
                self._effective_used(target_id) + obj.bytes
                > self._limit(target_id)
            ):
                entry["status"] = "failed"
                entry["failure_reason_code"] = "TARGET_CAPACITY_EXCEEDED"
                entry["failure_message"] = self._failure_message(
                    "TARGET_CAPACITY_EXCEEDED"
                )
                entry["status_history"].append(
                    {
                        "at": at,
                        "status": "failed",
                        "reason_code": "TARGET_CAPACITY_EXCEEDED",
                    }
                )
            else:
                self._ref_remove(
                    at,
                    object_id,
                    source_id,
                    f"{batch_id}:migration_source_remove",
                )
                self.assignments[object_id] = target_id
                self._ref_add(
                    at,
                    object_id,
                    target_id,
                    f"{batch_id}:migration_target_add",
                )
                entry["status"] = "migrated"
                entry["failure_reason_code"] = None
                entry["source_retained"] = False
                entry["status_history"].append(
                    {
                        "at": at,
                        "status": "migrated",
                        "reason_code": None,
                    }
                )

            self._apply_deferred_for_step(
                at,
                entry["sequence"],
                deferred,
                deferred_applied,
                deferred_health_changed,
                deferred_watermark_changed,
            )

        # Deferred incidents whose step was never reached still apply now.
        for incident in deferred:
            if incident.index in {
                row["incident_index"] for row in deferred_applied
            }:
                continue
            row = self._apply_incident(at, incident)
            deferred_applied.append(row)
            if row["kind"] == "health" and row["health"] != "healthy":
                deferred_health_changed.add(row["region_id"])
            elif row["kind"] == "watermark":
                deferred_watermark_changed.add(row["region_id"])

        summary = {"pending": 0, "migrated": 0, "failed": 0}
        for entry in entries:
            summary[entry["status"]] += 1
        batch = {
            "batch_id": batch_id,
            "at": at,
            "triggers": triggers,
            "affected_object_ids": [
                entry["object_id"] for entry in entries
            ],
            "plan": entries,
            "progress_summary": summary,
            "deferred_incidents_applied": deferred_applied,
            "deferred_health_changed": sorted(deferred_health_changed),
            "deferred_watermark_changed": sorted(deferred_watermark_changed),
        }
        self.relocation_batches.append(batch)
        return batch

    def _apply_deferred_for_step(
        self,
        at: float,
        step: int,
        deferred: list[Incident],
        applied: list[dict[str, Any]],
        health_changed: set[str],
        watermark_changed: set[str],
    ) -> None:
        applied_indices = {row["incident_index"] for row in applied}
        for incident in deferred:
            if incident.index in applied_indices:
                continue
            if incident.after_migration_step != step:
                continue
            row = self._apply_incident(at, incident)
            applied.append(row)
            if row["kind"] == "health" and row["health"] != "healthy":
                health_changed.add(row["region_id"])
            elif row["kind"] == "watermark":
                watermark_changed.add(row["region_id"])

    @staticmethod
    def _failure_message(reason: str | None) -> str | None:
        messages = {
            "NO_ELIGIBLE_TARGET": "不存在满足健康、合规与时延要求的其他区域",
            "TARGET_CAPACITY_EXCEEDED": "目标区域迁移后水位将超过阈值",
            "TARGET_HEALTH_DEGRADED": "迁移途中目标区域健康状态降级",
            "SOURCE_REFERENCE_CHANGED": "迁移途中源区域引用已变化",
        }
        return messages.get(reason) if reason else None

    # ------------------------------------------------------------------
    # State loading and reference maintenance
    # ------------------------------------------------------------------

    def _active_ids(self) -> set[str]:
        return {
            object_id
            for object_id, count in self.refcounts.items()
            if count > 0
        }

    def _limit(self, region_id: str) -> int:
        region = self.regions[region_id]
        return int(math.floor(region.capacity_bytes * region.watermark_ratio))

    def _residents(self, region_id: str) -> list[str]:
        return sorted(
            object_id
            for object_id, assigned_region in self.assignments.items()
            if assigned_region == region_id and self.refcounts[object_id] > 0
        )

    def _assigned_bytes(self, region_id: str) -> int:
        return sum(
            self.objects[object_id].bytes
            for object_id in self._residents(region_id)
        )

    def _effective_used(self, region_id: str) -> int:
        return self.regions[region_id].baseline_used_bytes + self._assigned_bytes(
            region_id
        )

    def _loads(self, assignments: dict[str, str]) -> dict[str, int]:
        loads = {
            region_id: region.baseline_used_bytes
            for region_id, region in self.regions.items()
        }
        for object_id, region_id in assignments.items():
            if self.refcounts.get(object_id, 0) > 0:
                loads[region_id] += self.objects[object_id].bytes
        return loads

    def _ref_add(
        self,
        at: float,
        object_id: str,
        region_id: str,
        reason: str,
    ) -> None:
        obj = self.objects[object_id]
        self.region_refs[region_id][object_id] = {
            "digest": obj.digest,
            "reference_count": self.refcounts[object_id],
            "bytes": obj.bytes,
        }
        self.reference_operations.append(
            {
                "at": at,
                "operation": "add",
                "object_id": object_id,
                "region_id": region_id,
                "digest": obj.digest,
                "reference_count": self.refcounts[object_id],
                "reason": reason,
            }
        )

    def _ref_remove(
        self,
        at: float,
        object_id: str,
        region_id: str,
        reason: str,
    ) -> None:
        entry = self.region_refs[region_id].pop(object_id, None)
        self.reference_operations.append(
            {
                "at": at,
                "operation": "remove",
                "object_id": object_id,
                "region_id": region_id,
                "digest": entry["digest"] if entry else None,
                "reason": reason,
            }
        )

    # ------------------------------------------------------------------
    # Reports
    # ------------------------------------------------------------------

    def _region_totals(
        self, assignments: dict[str, str], object_ids: set[str]
    ) -> list[dict[str, Any]]:
        rows = []
        for region_id in sorted(self.regions):
            region = self.regions[region_id]
            ids = [
                object_id
                for object_id, assigned in assignments.items()
                if assigned == region_id and object_id in object_ids
            ]
            assigned_bytes = sum(self.objects[object_id].bytes for object_id in ids)
            effective = region.baseline_used_bytes + assigned_bytes
            rows.append(
                {
                    "region_id": region_id,
                    "object_count": len(ids),
                    "assigned_object_bytes": assigned_bytes,
                    "baseline_used_bytes": region.baseline_used_bytes,
                    "effective_used_bytes": effective,
                    "capacity_bytes": region.capacity_bytes,
                    "limit_bytes": self._limit(region_id),
                    "watermark": _ratio(effective, region.capacity_bytes),
                    "health": region.health,
                    "over_limit": effective > self._limit(region_id),
                }
            )
        return rows

    def _distribution(
        self,
        phase: str,
        assignments: dict[str, str],
        object_ids: set[str],
        *,
        include_satisfaction: bool,
    ) -> dict[str, Any]:
        regions = self._region_totals(assignments, object_ids)
        result: dict[str, Any] = {
            "phase": phase,
            "active_object_count": len(object_ids),
            "regions": regions,
            "over_limit_region_ids": [
                row["region_id"] for row in regions if row["over_limit"]
            ],
            "total_assigned_object_bytes": sum(
                row["assigned_object_bytes"] for row in regions
            ),
        }
        if include_satisfaction:
            result["latency_satisfaction"] = self._latency_satisfaction(
                assignments, object_ids
            )
        return result

    def _latency_satisfaction(
        self, assignments: dict[str, str], object_ids: set[str]
    ) -> dict[str, Any]:
        total_weight = 0.0
        satisfied_weight = 0.0
        by_urgency: dict[str, dict[str, float]] = {}
        for object_id in sorted(object_ids):
            obj = self.objects[object_id]
            weight = self._weight(obj)
            total_weight += weight
            bucket = by_urgency.setdefault(
                obj.urgency,
                {
                    "total_weight": 0.0,
                    "satisfied_weight": 0.0,
                    "object_count": 0,
                    "satisfied_object_count": 0,
                },
            )
            bucket["total_weight"] += weight
            bucket["object_count"] += 1
            region_id = assignments.get(object_id)
            satisfied = False
            if region_id is not None:
                region = self.regions[region_id]
                satisfied = region.latency_ms <= self._latency_limit(obj)
            if satisfied:
                satisfied_weight += weight
                bucket["satisfied_weight"] += weight
                bucket["satisfied_object_count"] += 1

        for bucket in by_urgency.values():
            bucket["total_weight"] = _round(bucket["total_weight"])
            bucket["satisfied_weight"] = _round(bucket["satisfied_weight"])
            bucket["rate_percent"] = _round(
                (
                    100.0
                    * bucket["satisfied_weight"]
                    / bucket["total_weight"]
                )
                if bucket["total_weight"]
                else 0.0
            )
        return {
            "total_weight": _round(total_weight),
            "satisfied_weight": _round(satisfied_weight),
            "rate_percent": _round(
                (100.0 * satisfied_weight / total_weight)
                if total_weight
                else 0.0
            ),
            "by_urgency": {
                key: by_urgency[key] for key in sorted(by_urgency)
            },
        }

    def _build_assignment_report(self) -> list[dict[str, Any]]:
        rows = []
        for object_id in sorted(self.objects):
            obj = self.objects[object_id]
            selected_id = self.assignments.get(object_id)
            selected_score = (
                self._score(obj, self.regions[selected_id])
                if selected_id is not None
                else None
            )
            candidates = []
            for region_id in sorted(self.regions):
                region = self.regions[region_id]
                eligible, filter_code, filter_message = self._screen(obj, region)
                score = self._score(obj, region)
                status = "not_selected"
                reason_code: str | None = None
                reason: str | None = None
                if region_id == selected_id:
                    status = "selected"
                    reason_code = "SELECTED"
                    reason = "按得分与容量约束选定"
                elif not eligible:
                    status = "filtered"
                    reason_code = filter_code
                    reason = filter_message
                elif selected_id is None:
                    status = "unassigned"
                    reason_code = "NO_ELIGIBLE_REGION"
                    reason = "无满足过滤条件的区域"
                elif score > (selected_score or 0):
                    status = "not_selected_capacity"
                    reason_code = "CAPACITY_WATERMARK"
                    reason = "得分更高，但容量水位约束下未入选"
                elif score == selected_score:
                    status = "not_selected_tie_break"
                    reason_code = "TIE_BREAK_REGION_ID"
                    reason = "得分相同，按区域 id 稳定决胜"
                else:
                    reason_code = "LOWER_SCORE"
                    reason = "候选得分低于已选区域"
                headroom = self._limit(region_id) - self._effective_used(region_id)
                candidates.append(
                    {
                        "region_id": region_id,
                        "score": score,
                        "eligible": eligible,
                        "status": status,
                        "reason_code": reason_code,
                        "reason": reason,
                        "capacity_headroom_bytes_at_final_assignment": headroom,
                    }
                )
            rows.append(
                {
                    "object_id": obj.id,
                    "institution_id": obj.institution_id,
                    "test_category": obj.test_category,
                    "urgency": obj.urgency,
                    "urgency_weight": _round(self._weight(obj)),
                    "bytes": obj.bytes,
                    "latency_limit_ms": _round(self._latency_limit(obj)),
                    "assigned_region_id": selected_id,
                    "selected_score": selected_score,
                    "candidates": candidates,
                }
            )
        return rows

    def _unresolved_capacity_report(self) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for region_id in sorted(self.regions):
            effective = self._effective_used(region_id)
            limit = self._limit(region_id)
            if effective <= limit:
                continue
            required = effective - limit
            freed = 0
            evictions = []
            residents = sorted(
                self._residents(region_id),
                key=lambda object_id: (
                    self._weight(self.objects[object_id]),
                    -self.objects[object_id].bytes,
                    object_id,
                ),
            )
            for object_id in residents:
                if freed >= required:
                    break
                obj = self.objects[object_id]
                if obj.bytes == 0:
                    continue
                evictions.append(object_id)
                freed += obj.bytes
            predicted = effective - freed
            result.append(
                {
                    "region_id": region_id,
                    "capacity_bytes": self.regions[region_id].capacity_bytes,
                    "limit_ratio": self.regions[region_id].watermark_ratio,
                    "limit_bytes": limit,
                    "effective_used_bytes": effective,
                    "overflow_bytes": required,
                    "eviction_object_ids": evictions,
                    "evicted_bytes": freed,
                    "predicted_effective_used_bytes": predicted,
                    "predicted_watermark": _ratio(
                        predicted, self.regions[region_id].capacity_bytes
                    ),
                    "still_over_limit": predicted > limit,
                }
            )
        return result

    def _capacity_control_report(self) -> dict[str, Any]:
        initial_after = self.initial_distributions[
            "after_capacity_assignment"
        ]
        final_regions = self._region_totals(
            self.assignments, self._active_ids()
        )
        return {
            "algorithm": (
                "descending-urgency preferred-score assignment, "
                "capacity-watermark repair, then score-improving local moves"
            ),
            "repair_moves": self.capacity_repair_moves,
            "local_adjustment_moves": self.local_adjustment_moves,
            "initial_over_limit_region_ids": initial_after[
                "over_limit_region_ids"
            ],
            "initial_unresolved_regions": self.initial_unresolved_regions,
            "final_over_limit_region_ids": [
                row["region_id"] for row in final_regions if row["over_limit"]
            ],
            "final_unresolved_regions": self._unresolved_capacity_report(),
            "final_regions": final_regions,
        }

    def _distribution_comparison(
        self,
        initial: dict[str, Any],
        final: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "before_placement": initial["before_placement"],
            "preferred_unconstrained": initial["preferred_unconstrained"],
            "after_capacity_assignment": initial[
                "after_capacity_assignment"
            ],
            "after_timeline": final,
            "over_limit_change": {
                "before_placement": initial["before_placement"][
                    "over_limit_region_ids"
                ],
                "preferred_unconstrained": initial["preferred_unconstrained"][
                    "over_limit_region_ids"
                ],
                "after_capacity_assignment": initial[
                    "after_capacity_assignment"
                ]["over_limit_region_ids"],
                "after_timeline": final["over_limit_region_ids"],
            },
            "weighted_latency_satisfaction_comparison": {
                phase: initial[phase]["latency_satisfaction"]
                for phase in (
                    "before_placement",
                    "preferred_unconstrained",
                    "after_capacity_assignment",
                )
            }
            | {"after_timeline": final["latency_satisfaction"]},
        }

    def _snapshot(
        self,
        *,
        at: float,
        phase: str,
        events: list[dict[str, Any]],
        incidents: list[dict[str, Any]],
        batch_ids: list[str],
        reconciliation_ok: bool,
    ) -> dict[str, Any]:
        region_rows = []
        for region_id in sorted(self.regions):
            region = self.regions[region_id]
            effective = self._effective_used(region_id)
            region_rows.append(
                {
                    "region_id": region_id,
                    "object_count": len(self._residents(region_id)),
                    "assigned_object_bytes": self._assigned_bytes(region_id),
                    "baseline_used_bytes": region.baseline_used_bytes,
                    "effective_used_bytes": effective,
                    "capacity_bytes": region.capacity_bytes,
                    "limit_bytes": self._limit(region_id),
                    "watermark": _ratio(effective, region.capacity_bytes),
                    "health": region.health,
                    "over_limit": effective > self._limit(region_id),
                    "reference_count_held": sum(
                        entry["reference_count"]
                        for entry in self.region_refs[region_id].values()
                    ),
                }
            )
        return {
            "at": at,
            "phase": phase,
            "events": events,
            "incidents": incidents,
            "relocation_batch_ids": batch_ids,
            "active_object_count": len(self._active_ids()),
            "regions": region_rows,
            "over_limit_region_ids": [
                row["region_id"] for row in region_rows if row["over_limit"]
            ],
            "latency_satisfaction": self._latency_satisfaction(
                self.assignments, self._active_ids()
            ),
            "reconciliation_ok": reconciliation_ok,
        }

    def _relocation_progress(self) -> dict[str, Any]:
        entries = []
        summary = {"pending": 0, "migrated": 0, "failed": 0}
        for batch in self.relocation_batches:
            for item in batch["plan"]:
                entries.append(
                    {
                        "batch_id": batch["batch_id"],
                        "at": batch["at"],
                        "sequence": item["sequence"],
                        "object_id": item["object_id"],
                        "institution_id": item["institution_id"],
                        "source_region_id": item["source_region_id"],
                        "target_region_id": item["target_region_id"],
                        "status": item["status"],
                        "initial_status": "pending",
                        "failure_reason_code": item["failure_reason_code"],
                        "failure_message": item["failure_message"],
                        "status_history": item["status_history"],
                    }
                )
                summary[item["status"]] += 1
        return {
            "summary": summary,
            "entries": entries,
        }

    # ------------------------------------------------------------------
    # Reconciliation and execution audit
    # ------------------------------------------------------------------

    def _reconcile(self, checkpoint: str) -> dict[str, Any]:
        records: list[dict[str, Any]] = []
        first: dict[str, Any] | None = None
        active_ids = self._active_ids()

        for object_id in sorted(self.objects):
            obj = self.objects[object_id]
            expected_region = self.assignments.get(object_id)
            actual_regions = sorted(
                region_id
                for region_id, entries in self.region_refs.items()
                if object_id in entries
            )
            # An active but unassignable object has no region-held reference.
            # Its logical usage counter remains visible in events/timeline, but
            # the reference ledger correctly expects zero entries.
            expected_count = (
                self.refcounts[object_id]
                if expected_region is not None
                else 0
            )
            actual_sum = sum(
                self.region_refs[region_id][object_id]["reference_count"]
                for region_id in actual_regions
            )
            issues: list[dict[str, Any]] = []
            if expected_count > 0:
                if expected_region is not None and not actual_regions:
                    issues.append(
                        {
                            "code": "MISSING_REFERENCE",
                            "region_id": expected_region,
                            "message": "对象缺少区域引用",
                        }
                    )
                elif expected_region is not None and len(actual_regions) > 1:
                    issues.append(
                        {
                            "code": "DUPLICATE_REFERENCE",
                            "region_id": actual_regions[0],
                            "message": "对象在多个区域存在副本归属",
                        }
                    )
                elif (
                    expected_region is not None
                    and actual_regions
                    and actual_regions[0] != expected_region
                ):
                    issues.append(
                        {
                            "code": "OWNERSHIP_DIFFERENCE",
                            "region_id": actual_regions[0],
                            "message": "引用清单区域与指派区域不同",
                        }
                    )
                for region_id in actual_regions:
                    entry = self.region_refs[region_id][object_id]
                    if entry["digest"] != obj.digest:
                        issues.append(
                            {
                                "code": "DIGEST_MISMATCH",
                                "region_id": region_id,
                                "message": (
                                    f"区域摘要 {entry['digest']!r} 与对象摘要 "
                                    f"{obj.digest!r} 不一致"
                                ),
                            }
                        )
                    if entry["reference_count"] != expected_count:
                        issues.append(
                            {
                                "code": "REFERENCE_COUNT_MISMATCH",
                                "region_id": region_id,
                                "message": (
                                    f"区域引用计数 {entry['reference_count']} "
                                    f"与对象计数 {expected_count} 不一致"
                                ),
                            }
                        )
                if actual_sum != expected_count:
                    issues.append(
                        {
                            "code": "REFERENCE_COUNT_SUM_MISMATCH",
                            "region_id": (
                                actual_regions[0] if actual_regions else None
                            ),
                            "message": (
                                f"引用计数合计 {actual_sum} 与对象计数 "
                                f"{expected_count} 不一致"
                            ),
                        }
                    )
            elif actual_regions:
                issues.append(
                    {
                        "code": "ORPHAN_REFERENCE",
                        "region_id": actual_regions[0],
                        "message": "引用计数归零但区域仍保留引用",
                    }
                )

            if issues:
                record = {
                    "object_id": object_id,
                    "institution_id": obj.institution_id,
                    "expected_region_id": expected_region,
                    "actual_region_ids": actual_regions,
                    "object_reference_count": self.refcounts[object_id],
                    "expected_reference_count": expected_count,
                    "actual_reference_count_sum": actual_sum,
                    "canonical_digest": obj.digest,
                    "actual_digests": {
                        region_id: self.region_refs[region_id][object_id]["digest"]
                        for region_id in actual_regions
                    },
                    "issues": issues,
                }
                records.append(record)
                if first is None:
                    first = {
                        "checkpoint": checkpoint,
                        "object_id": object_id,
                        "institution_id": obj.institution_id,
                        "region_id": issues[0]["region_id"],
                        "code": issues[0]["code"],
                        "message": issues[0]["message"],
                    }

        for region_id in sorted(self.region_refs):
            for object_id in sorted(self.region_refs[region_id]):
                if object_id not in self.objects:
                    issues = [
                        {
                            "code": "UNKNOWN_OBJECT_REFERENCE",
                            "region_id": region_id,
                            "message": "区域引用了未知对象",
                        }
                    ]
                    record = {
                        "object_id": object_id,
                        "institution_id": None,
                        "expected_region_id": None,
                        "actual_region_ids": [region_id],
                        "object_reference_count": 0,
                        "expected_reference_count": 0,
                        "actual_reference_count_sum": self.region_refs[region_id][
                            object_id
                        ]["reference_count"],
                        "canonical_digest": None,
                        "actual_digests": {
                            region_id: self.region_refs[region_id][object_id][
                                "digest"
                            ]
                        },
                        "issues": issues,
                    }
                    records.append(record)
                    if first is None:
                        first = {
                            "checkpoint": checkpoint,
                            "object_id": object_id,
                            "institution_id": None,
                            "region_id": region_id,
                            "code": issues[0]["code"],
                            "message": issues[0]["message"],
                        }

        return {
            "checkpoint": checkpoint,
            "ok": not records,
            "active_object_count": len(active_ids),
            "checked_object_count": len(self.objects),
            "region_reference_count": sum(
                len(entries) for entries in self.region_refs.values()
            ),
            "inconsistency_count": sum(
                len(record["issues"]) for record in records
            ),
            "inconsistent_object_count": len(records),
            "first_inconsistency": first,
            "inconsistencies": records,
        }

    def _execution_audit(self, final_audit: dict[str, Any]) -> dict[str, Any]:
        migrated = 0
        failed = 0
        pending = 0
        failures_retained_source = True
        for batch in self.relocation_batches:
            for entry in batch["plan"]:
                if entry["status"] == "migrated":
                    migrated += 1
                elif entry["status"] == "failed":
                    failed += 1
                    if entry["failure_reason_code"] in {
                        "TARGET_HEALTH_DEGRADED",
                        "TARGET_CAPACITY_EXCEEDED",
                        "NO_ELIGIBLE_TARGET",
                        "SOURCE_REFERENCE_CHANGED",
                    }:
                        failures_retained_source = (
                            failures_retained_source
                            and entry["source_retained"]
                        )
                else:
                    pending += 1
        return {
            "pending_entries": pending,
            "migrated_entries": migrated,
            "failed_entries": failed,
            "failed_entries_kept_source_reference": failures_retained_source,
            "final_reference_lists_match_assignments": final_audit["ok"],
            "reference_operation_count": len(self.reference_operations),
        }

    def _region_reference_lists(self) -> list[dict[str, Any]]:
        rows = []
        for region_id in sorted(self.region_refs):
            entries = []
            for object_id in sorted(self.region_refs[region_id]):
                entry = self.region_refs[region_id][object_id]
                obj = self.objects.get(object_id)
                entries.append(
                    {
                        "object_id": object_id,
                        "institution_id": obj.institution_id if obj else None,
                        "digest": entry["digest"],
                        "reference_count": entry["reference_count"],
                        "bytes": entry["bytes"],
                    }
                )
            rows.append(
                {
                    "region_id": region_id,
                    "object_count": len(entries),
                    "reference_count": sum(
                        entry["reference_count"] for entry in entries
                    ),
                    "bytes": sum(entry["bytes"] for entry in entries),
                    "objects": entries,
                }
            )
        return rows

    def _formula_description(self) -> dict[str, Any]:
        return {
            "placement_score": (
                "urgency_weight * 1000 / (region.latency_ms + 1), "
                "rounded to 6 decimal places; higher is better"
            ),
            "latency_limit_ms": (
                "min(policy.max_latency_ms, institution.max_latency_ms)"
            ),
            "eligibility": (
                "region.health == healthy AND region.latency_ms <= "
                "latency_limit_ms AND region.compliance_level >= "
                "institution.required_compliance_level"
            ),
            "capacity_limit_bytes": (
                "floor(region.capacity_bytes * region.watermark_ratio)"
            ),
            "effective_used_bytes": (
                "region.used_bytes + sum(bytes of active assigned objects)"
            ),
            "weighted_latency_satisfaction": (
                "100 * sum(urgency_weight of active objects whose assigned "
                "region meets latency_limit) / sum(urgency_weight of all "
                "active objects)"
            ),
            "tie_break": "ascending region id",
        }
