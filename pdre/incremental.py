"""Dependency-aware incremental rejudgment."""

from __future__ import annotations

from dataclasses import asdict
from dataclasses import dataclass, field
from typing import Any, Optional

from .codes import BusinessCode as Codes
from .config import EngineConfig
from .engine import analyze, assemble_report, compare_semantic_reports
from .lineage import LineageView, build_lineage_view
from .models import EventRecord, LineageRecord, PolicyRecord, Rejection
from .models import SourceRef
from .policies import PolicyView, build_policy_view
from .runtime import UnitJob, run_jobs


@dataclass
class ChangeSet:
    policy_keys: set[str] = field(default_factory=set)
    lineage_guids: set[str] = field(default_factory=set)
    event_guids: set[str] = field(default_factory=set)


class IncrementalEngine:
    def __init__(
        self,
        config: EngineConfig | None = None,
        *,
        rejections: list[Rejection] | None = None,
        read_errors: list[dict[str, Any]] | None = None,
    ) -> None:
        self.config = config or EngineConfig()
        self.rejections = rejections or []
        self.read_errors = read_errors or []
        self.events_by_uid: dict[str, EventRecord] = {}
        self.lineage_by_uid: dict[str, LineageRecord] = {}
        self.policies_by_uid: dict[str, PolicyRecord] = {}
        self.unit_cache: dict[str, dict[str, Any]] = {}
        self.unit_versions: dict[str, int] = {}
        self.previous_view: Optional[LineageView] = None
        self.previous_policy_view: Optional[PolicyView] = None
        self.processed_batches: list[str] = []
        self.changes = ChangeSet()
        self.last_report: Optional[dict[str, Any]] = None

    @property
    def events(self) -> list[EventRecord]:
        return list(self.events_by_uid.values())

    @property
    def lineage_records(self) -> list[LineageRecord]:
        return list(self.lineage_by_uid.values())

    @property
    def policy_records(self) -> list[PolicyRecord]:
        return list(self.policies_by_uid.values())

    def add_events(self, records: list[EventRecord]) -> None:
        for record in records:
            self.events_by_uid[record.uid] = record
            self.changes.event_guids.add(record.process_guid)

    def add_lineage(self, records: list[LineageRecord]) -> None:
        for record in records:
            self.lineage_by_uid[record.uid] = record
            self.changes.lineage_guids.add(record.process_guid)

    def add_policies(self, records: list[PolicyRecord]) -> None:
        for record in records:
            self.policies_by_uid[record.uid] = record
            self.changes.policy_keys.add(record.policy_key)

    def remove_event(self, uid: str) -> None:
        record = self.events_by_uid.pop(uid, None)
        if record:
            self.changes.event_guids.add(record.process_guid)

    def remove_lineage(self, uid: str) -> None:
        record = self.lineage_by_uid.pop(uid, None)
        if record:
            self.changes.lineage_guids.add(record.process_guid)

    def remove_policy(self, uid: str) -> None:
        record = self.policies_by_uid.pop(uid, None)
        if record:
            self.changes.policy_keys.add(record.policy_key)

    def apply_batch(
        self,
        *,
        batch_id: str,
        events: list[EventRecord] | None = None,
        lineage: list[LineageRecord] | None = None,
        policies: list[PolicyRecord] | None = None,
        verify: bool = True,
    ) -> dict[str, Any]:
        self.add_events(events or [])
        self.add_lineage(lineage or [])
        self.add_policies(policies or [])
        return self.recompute(batch_id=batch_id, verify=verify)

    def _policy_dependency_guids(
        self,
        new_policy_view: PolicyView,
    ) -> set[str]:
        guids: set[str] = set()
        changed_keys = self.changes.policy_keys
        if not changed_keys:
            return guids

        digests: set[str] = set()
        for key in changed_keys:
            for record in new_policy_view.records_by_key.get(key, []):
                if record.match_digest:
                    digests.add(record.match_digest)
            if self.previous_policy_view:
                for record in self.previous_policy_view.records_by_key.get(key, []):
                    if record.match_digest:
                        digests.add(record.match_digest)

        for event in self.events:
            if event.policy_key in changed_keys or event.payload_digest in digests:
                guids.add(event.process_guid)
        return guids

    def _affected_unit_ids(
        self,
        new_view: LineageView,
        new_policy_view: PolicyView,
        current_unit_ids: set[str],
    ) -> set[str]:
        if self.previous_view is None:
            return set(current_unit_ids)

        affected_guids: set[str] = set(self.changes.event_guids)
        affected_guids.update(self._policy_dependency_guids(new_policy_view))

        for guid in self.changes.lineage_guids:
            affected_guids.add(guid)
            affected_guids.update(self.previous_view.descendants_of(guid))
            affected_guids.update(new_view.descendants_of(guid))

        affected_unit_ids: set[str] = set()
        for guid in affected_guids:
            old_assignment = self.previous_view.assignments.get(guid)
            if old_assignment:
                affected_unit_ids.add(old_assignment.unit_id)
            new_assignment = new_view.assignments.get(guid)
            if new_assignment:
                affected_unit_ids.add(new_assignment.unit_id)

        for unit_id in current_unit_ids:
            if unit_id not in self.unit_cache:
                affected_unit_ids.add(unit_id)
        return affected_unit_ids

    def recompute(self, *, batch_id: str | None = None, verify: bool = True) -> dict[str, Any]:
        events = self.events
        lineage_records = self.lineage_records
        policy_records = self.policy_records

        lineage_view = build_lineage_view(events, lineage_records)
        policy_view = build_policy_view(policy_records)
        all_jobs = self._build_jobs(events, lineage_view, policy_view)
        current_unit_ids = {job.unit_id for job in all_jobs}
        affected_unit_ids = self._affected_unit_ids(
            lineage_view, policy_view, current_unit_ids
        )
        target_jobs = [job for job in all_jobs if job.unit_id in affected_unit_ids]

        units, queue_rejections, timings = run_jobs(target_jobs, self.config)
        succeeded_ids = {unit["unit_id"] for unit in units}
        rejected_ids = {item["unit_id"] for item in queue_rejections}

        for unit in units:
            unit_id = unit["unit_id"]
            self.unit_cache[unit_id] = unit
            self.unit_versions[unit_id] = self.unit_versions.get(unit_id, 0) + 1

        for unit_id in rejected_ids:
            self.unit_cache.pop(unit_id, None)

        for cached_unit_id in list(self.unit_cache):
            if cached_unit_id not in current_unit_ids:
                self.unit_cache.pop(cached_unit_id, None)

        cached_units = [
            self.unit_cache[unit_id]
            for unit_id in sorted(current_unit_ids)
            if unit_id in self.unit_cache
        ]
        all_timings = {
            unit_id: timings.get(unit_id, 0.0)
            for unit_id in sorted(succeeded_ids)
        }

        recomputed_ids = succeeded_ids
        reused_ids = {
            unit["unit_id"]
            for unit in cached_units
            if unit["unit_id"] not in recomputed_ids
        }

        incremental_info: dict[str, Any] = {
            "mode": "incremental_closure",
            "batch_id": batch_id,
            "changed_policy_keys": sorted(self.changes.policy_keys),
            "changed_lineage_guids": sorted(self.changes.lineage_guids),
            "changed_event_guids": sorted(self.changes.event_guids),
            "affected_unit_ids": sorted(affected_unit_ids & current_unit_ids),
            "target_unit_ids": sorted(job.unit_id for job in target_jobs),
            "recomputed_unit_ids": sorted(recomputed_ids),
            "reused_unit_ids": sorted(reused_ids),
            "queue_rejected_unit_ids": sorted(rejected_ids),
            "consistency": "not_checked",
            "differences": [],
        }

        report = assemble_report(
            events=events,
            lineage_records=lineage_records,
            policy_records=policy_records,
            lineage_view=lineage_view,
            policy_view=policy_view,
            units=cached_units,
            queue_rejections=queue_rejections,
            timings=all_timings,
            config=self.config,
            rejections=self.rejections,
            read_errors=self.read_errors,
            extra={"incremental": incremental_info},
        )

        rejudge_errors: list[dict[str, Any]] = []
        if verify:
            differences = self.verify_report_against_full(report)
            if differences:
                incremental_info["consistency"] = "mismatch"
                incremental_info["differences"] = differences[:20]
                rejudge_errors.append(
                    {
                        "code": Codes.REJUDGE_MISMATCH,
                        "message": "incremental result differs from full recomputation",
                        "difference_count": len(differences),
                        "differences": differences[:20],
                    }
                )
                report["rejudge_errors"] = rejudge_errors
                report["summary"]["rejudge_mismatch_count"] = 1
            else:
                incremental_info["consistency"] = "matched"
                report["summary"]["rejudge_mismatch_count"] = 0

        self.last_report = report
        self.previous_view = lineage_view
        self.previous_policy_view = policy_view
        if batch_id:
            self.processed_batches.append(batch_id)
        self.changes = ChangeSet()
        return report

    def _build_jobs(self, events, lineage_view, policy_view) -> list[UnitJob]:
        groups: dict[str, tuple[Any, list[EventRecord]]] = {}
        for event in events:
            assignment = lineage_view.assignments[event.process_guid]
            if assignment.unit_id not in groups:
                groups[assignment.unit_id] = (assignment, [])
            groups[assignment.unit_id][1].append(event)
        return [
            UnitJob(assignment=assignment, events=group_events, policies=policy_view.policies)
            for _, (assignment, group_events) in sorted(groups.items())
        ]

    def verify_current_against_full(self) -> list[dict[str, Any]]:
        if self.last_report is None:
            return []
        return self.verify_report_against_full(self.last_report)

    def export_state(self) -> dict[str, Any]:
        return {
            "events": [asdict(record) for record in self.events],
            "lineage": [asdict(record) for record in self.lineage_records],
            "policies": [asdict(record) for record in self.policy_records],
            "rejections": [
                item.to_dict() if isinstance(item, Rejection) else dict(item)
                for item in self.rejections
            ],
            "read_errors": list(self.read_errors),
            "unit_cache": self.unit_cache,
            "unit_versions": self.unit_versions,
            "processed_batches": self.processed_batches,
        }

    def load_state(self, state: dict[str, Any]) -> None:
        self.events_by_uid = {}
        self.lineage_by_uid = {}
        self.policies_by_uid = {}

        for item in state.get("events", []):
            data = dict(item)
            data["source"] = SourceRef(**data["source"])
            record = EventRecord(**data)
            self.events_by_uid[record.uid] = record

        for item in state.get("lineage", []):
            data = dict(item)
            data["source"] = SourceRef(**data["source"])
            record = LineageRecord(**data)
            self.lineage_by_uid[record.uid] = record

        for item in state.get("policies", []):
            data = dict(item)
            data["source"] = SourceRef(**data["source"])
            record = PolicyRecord(**data)
            self.policies_by_uid[record.uid] = record

        self.rejections = list(state.get("rejections", []))
        self.read_errors = list(state.get("read_errors", []))
        self.unit_cache = dict(state.get("unit_cache", {}))
        self.unit_versions = {
            str(key): int(value)
            for key, value in state.get("unit_versions", {}).items()
        }
        self.processed_batches = list(state.get("processed_batches", []))
        self.previous_view = build_lineage_view(
            self.events, self.lineage_records
        )
        self.previous_policy_view = build_policy_view(self.policy_records)
        self.changes = ChangeSet()

    @classmethod
    def from_state(
        cls,
        state: dict[str, Any],
        config: EngineConfig | None = None,
    ) -> "IncrementalEngine":
        engine = cls(config=config)
        engine.load_state(state)
        return engine

    def verify_report_against_full(self, report: dict[str, Any]) -> list[dict[str, Any]]:
        full_report = analyze(
            self.events,
            self.lineage_records,
            self.policy_records,
            self.config,
            rejections=self.rejections,
            read_errors=self.read_errors,
        )
        return compare_semantic_reports(report, full_report)
