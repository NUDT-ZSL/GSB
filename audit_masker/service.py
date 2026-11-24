from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from .checkpoint import (
    CheckpointError,
    create_checkpoint,
    restore_latest_checkpoint,
    verify_checkpoints,
)
from .engine import decide_events
from .ledger import LedgerStore
from .locking import LockError, WorkspaceLock
from .masking import (
    DEFAULT_MASK_METHODS,
    field_matches_path,
    mask_method_allowed,
)
from .models import Event, EventResult, LayoutPolicy, Rejection, RuleRecord
from .oracle import reference_expected_fields
from .parsing import (
    ParsedPatch,
    ParsedSnapshot,
    parse_events_file,
    parse_patch_file,
    parse_rules_snapshot,
)
from .rules import (
    build_unified_view,
    defaults_fingerprint,
    layout_fingerprint,
)
from .util import (
    atomic_write_json,
    canonical_digest,
    canonical_json,
    ensure_dir,
    sha256_bytes,
)


def _codes(values: Iterable[str]) -> Dict[str, int]:
    return dict(sorted(Counter(values).items()))


def _event_key_dict(event_id: str, seq: int) -> Dict[str, Any]:
    return {"event_id": event_id, "seq": seq}


class WorkspaceService:
    def __init__(self, workspace: str | Path):
        self.workspace = Path(workspace)

    def _lock(self) -> WorkspaceLock:
        return WorkspaceLock(self.workspace)

    def import_events(self, paths: Sequence[str | Path]) -> Dict[str, Any]:
        with self._lock():
            with LedgerStore(self.workspace) as store:
                seen = store.event_keys()
                seen_event_ids = {
                    event.event_id for event in store.load_events()
                }
                accepted: List[Event] = []
                rejections: List[Rejection] = []
                per_file: List[Dict[str, Any]] = []
                for path in paths:
                    events, file_rejections = parse_events_file(
                        path,
                        seen_event_keys=seen,
                        seen_event_ids=seen_event_ids,
                    )
                    accepted.extend(events)
                    rejections.extend(file_rejections)
                    per_file.append(
                        {
                            "source_ref": str(path),
                            "accepted": len(events),
                            "rejected": len(file_rejections),
                        }
                    )
                with store.transaction():
                    inserted = store.insert_events(accepted)
                    store.insert_rejections(rejections)
                codes = _codes(rejection.code for rejection in rejections)
                if rejections:
                    codes["E_PARSE_BATCH"] = codes.get("E_PARSE_BATCH", 0) + 1
                return {
                    "command": "import-events",
                    "success": not rejections,
                    "exit_code": 0 if not rejections else 1,
                    "accepted": len(accepted),
                    "inserted": inserted,
                    "rejected": len(rejections),
                    "rejections": [item.to_dict() for item in rejections],
                    "files": per_file,
                    "business_codes": codes,
                }

    def import_rules(
        self,
        snapshot_path: Optional[str | Path],
        patch_paths: Sequence[str | Path] = (),
    ) -> Dict[str, Any]:
        with self._lock():
            with LedgerStore(self.workspace) as store:
                snapshot: Optional[ParsedSnapshot] = None
                snapshot_error = False
                rejections: List[Rejection] = []
                if snapshot_path:
                    snapshot = parse_rules_snapshot(snapshot_path)
                    rejections.extend(snapshot.rejections)
                    snapshot_error = not snapshot.valid

                current_layout = store.load_layout() or LayoutPolicy(
                    DEFAULT_MASK_METHODS
                )
                effective_layout = (
                    snapshot.layout
                    if snapshot is not None and snapshot.valid and snapshot.layout
                    else current_layout
                )
                existing_patch_records = [
                    record
                    for record in store.load_rule_records()
                    if record.source_kind == "patch"
                ]
                if snapshot is not None and snapshot.valid:
                    invalid_existing = [
                        record
                        for record in existing_patch_records
                        if record.mask
                        and not mask_method_allowed(
                            record.mask,
                            effective_layout.mask_methods,
                        )
                    ]
                    if invalid_existing:
                        rejection = Rejection(
                            source_format="rules-metadata",
                            source_ref=str(snapshot_path),
                            record_index=0,
                            code="E_MASK_NOT_ALLOWED",
                            message=(
                                "new layout whitelist would invalidate existing "
                                "patch rule(s): "
                                + ", ".join(
                                    sorted(
                                        {
                                            record.rule_name
                                            for record in invalid_existing
                                        }
                                    )
                                )
                            ),
                        )
                        rejections.append(rejection)
                        snapshot_error = True

                parsed_patch_files = []
                patches_to_apply: List[ParsedPatch] = []
                for path in patch_paths:
                    parsed_file = parse_patch_file(path)
                    parsed_patch_files.append(parsed_file)
                    rejections.extend(parsed_file.rejections)
                    for patch in parsed_file.patches:
                        prior_rejection_count = len(patch.rejections)
                        filtered_records: List[RuleRecord] = []
                        for record in patch.records:
                            if record.mask and not mask_method_allowed(
                                record.mask,
                                effective_layout.mask_methods,
                            ):
                                patch.rejections.append(
                                    Rejection(
                                        source_format=record.source_format,
                                        source_ref=record.source_ref,
                                        record_index=record.record_index,
                                        code="E_MASK_NOT_ALLOWED",
                                        message=(
                                            f"mask {record.mask!r} is not allowed "
                                            "by the active layout"
                                        ),
                                        raw=record.raw,
                                    )
                                )
                            else:
                                filtered_records.append(record)
                        patch.records = filtered_records
                        patches_to_apply.append(patch)
                        rejections.extend(
                            patch.rejections[prior_rejection_count:]
                        )

                seen_patch_ids: Dict[str, str] = {}
                to_insert: List[ParsedPatch] = []
                skipped_patch_ids: List[str] = []
                for patch in patches_to_apply:
                    existing = store.patch_by_id(patch.patch_id)
                    if existing is not None:
                        if existing["digest"] == patch.digest:
                            skipped_patch_ids.append(patch.patch_id)
                            continue
                        rejections.append(
                            Rejection(
                                source_format="rules-patch",
                                source_ref=patch.source_ref,
                                record_index=0,
                                code="E_PATCH_DUPLICATE",
                                message=(
                                    f"patch_id {patch.patch_id!r} already exists "
                                    "with different content"
                                ),
                                raw=patch.raw,
                            )
                        )
                        continue
                    prior_digest = seen_patch_ids.get(patch.patch_id)
                    if prior_digest is not None:
                        if prior_digest != patch.digest:
                            rejections.append(
                                Rejection(
                                    source_format="rules-patch",
                                    source_ref=patch.source_ref,
                                    record_index=0,
                                    code="E_PATCH_DUPLICATE",
                                    message=(
                                        f"patch_id {patch.patch_id!r} appears "
                                        "more than once with different content"
                                    ),
                                    raw=patch.raw,
                                )
                            )
                        else:
                            skipped_patch_ids.append(patch.patch_id)
                        continue
                    seen_patch_ids[patch.patch_id] = patch.digest
                    to_insert.append(patch)

                snapshot_imported = False
                patches_imported = 0
                with store.transaction():
                    if (
                        snapshot is not None
                        and snapshot.valid
                        and not snapshot_error
                    ):
                        source_fingerprint = canonical_digest(
                            {
                                "layout": snapshot.layout.to_dict()
                                if snapshot.layout
                                else None,
                                "defaults": {
                                    key: value.to_dict()
                                    for key, value in sorted(
                                        snapshot.defaults.items()
                                    )
                                },
                                "clock_ms": snapshot.observation_clock_ms,
                                "records": [
                                    record.to_dict()
                                    for record in snapshot.records
                                ],
                            }
                        )
                        store.replace_snapshot_rules(
                            snapshot.records,
                            layout=snapshot.layout or current_layout,
                            defaults=snapshot.defaults,
                            observation_clock_ms=snapshot.observation_clock_ms,
                            snapshot_id=snapshot.snapshot_id,
                            source_fingerprint=source_fingerprint,
                        )
                        snapshot_imported = True
                    for patch in to_insert:
                        store.insert_patch(
                            patch.patch_id,
                            patch.source_ref,
                            patch.changed_rules,
                            patch.tombstones,
                            patch.digest,
                            patch.raw,
                            patch.records,
                        )
                        patches_imported += 1
                    store.insert_rejections(rejections)

                codes = _codes(rejection.code for rejection in rejections)
                if rejections:
                    codes["E_PARSE_BATCH"] = codes.get("E_PARSE_BATCH", 0) + 1
                return {
                    "command": "import-rules",
                    "success": not rejections,
                    "exit_code": 0 if not rejections else 1,
                    "snapshot_imported": snapshot_imported,
                    "snapshot_rejected": bool(snapshot_path and not snapshot_imported),
                    "snapshot_records": (
                        len(snapshot.records) if snapshot is not None else 0
                    ),
                    "patches_imported": patches_imported,
                    "patches_skipped_idempotent": sorted(skipped_patch_ids),
                    "rejected": len(rejections),
                    "rejections": [item.to_dict() for item in rejections],
                    "business_codes": codes,
                }

    def _load_view(self, store: LedgerStore):
        layout = store.load_layout() or LayoutPolicy(DEFAULT_MASK_METHODS)
        defaults = store.load_defaults()
        records = store.load_rule_records()
        return build_unified_view(records, layout, defaults)

    def recompute(
        self,
        *,
        full: bool = False,
    ) -> Dict[str, Any]:
        with self._lock():
            with LedgerStore(self.workspace) as store:
                return self._recompute_locked(store, full=full)

    def _recompute_locked(
        self,
        store: LedgerStore,
        *,
        full: bool,
    ) -> Dict[str, Any]:
        events = store.load_events()
        event_map = {event.sort_key: event for event in events}
        view = self._load_view(store)
        layout_fp = layout_fingerprint(view.layout)
        defaults_fp = defaults_fingerprint(view.defaults)
        current_state = view.state()
        current_state["layout_config_fingerprint"] = layout_fp
        current_state["defaults_fingerprint"] = defaults_fp

        previous_state = store.meta_get("applied_view_state")
        previous_layout = store.meta_get("applied_layout_fingerprint")
        first_run = not isinstance(previous_state, dict)
        layout_rebuilt = bool(
            previous_layout is not None and previous_layout != layout_fp
        )

        previous_rules = (
            previous_state.get("rules", {})
            if isinstance(previous_state, dict)
            else {}
        )
        current_rules = current_state.get("rules", {})
        changed_rule_names = sorted(
            name
            for name in set(previous_rules) | set(current_rules)
            if previous_rules.get(name) != current_rules.get(name)
        )
        if first_run or layout_rebuilt:
            changed_rule_names = sorted(current_rules)

        affected_paths: Set[str] = set()
        for name in changed_rule_names:
            previous_rule = previous_rules.get(name, {})
            current_rule = current_rules.get(name, {})
            affected_paths.update(previous_rule.get("dependency_paths", []))
            affected_paths.update(current_rule.get("dependency_paths", []))
            previous_field = previous_rule.get("field_path")
            current_field = current_rule.get("field_path")
            if previous_field:
                affected_paths.add(previous_field)
            if current_field:
                affected_paths.add(current_field)

        previous_defaults = (
            previous_state.get("defaults", {})
            if isinstance(previous_state, dict)
            else {}
        )
        current_defaults = current_state.get("defaults", {})
        affected_categories: Set[str] = {
            category
            for category in set(previous_defaults) | set(current_defaults)
            if previous_defaults.get(category) != current_defaults.get(category)
        }
        if first_run or layout_rebuilt:
            affected_categories = set(view.defaults) | {
                event.category for event in events
            }

        impacted: Set[Tuple[str, int]] = set(store.incomplete_event_keys())
        if (
            full
            or first_run
            or layout_rebuilt
            or not previous_state
        ):
            impacted = set(event_map)
        else:
            for event in events:
                if event.category in affected_categories:
                    impacted.add(event.sort_key)
                    continue
                if any(
                    field_matches_path(path, field_name)
                    for field_name in event.payload
                    for path in affected_paths
                ):
                    impacted.add(event.sort_key)

        state_changed = canonical_json(current_state) != canonical_json(previous_state)
        if not impacted and not state_changed:
            return {
                "command": "recompute",
                "success": True,
                "exit_code": 0,
                "layout_rebuilt": False,
                "full_rejudge": bool(full),
                "decision_revision": store.latest_decision_revision(),
                "impacted_event_count": 0,
                "impacted_events": [],
                "changed_rule_names": [],
                "affected_field_paths": [],
                "affected_categories": [],
                "conflicts": [item.to_dict() for item in view.conflicts],
                "business_codes": {},
            }

        next_revision = store.latest_decision_revision() + 1
        impacted_events = [
            event_map[key] for key in sorted(impacted) if key in event_map
        ]
        decided = decide_events(
            impacted_events,
            view,
            decision_watermark=next_revision,
        )
        # All decisions are computed before the write transaction begins.
        with store.transaction():
            if store.count_events() != len(events):
                raise RuntimeError(
                    "event set changed while preparing recompute; retry"
                )
            if layout_rebuilt:
                store.clear_decisions()
                store.delete_all_checkpoints()
            store.replace_event_decisions(
                [decided[event.sort_key] for event in impacted_events],
                layout_fp,
                next_revision,
            )
            store.meta_set("decision_revision", next_revision)
            store.meta_set("rules_fingerprint", view.fingerprint)
            store.meta_set("applied_view_state", current_state)
            store.meta_set("applied_layout_fingerprint", layout_fp)
            store.meta_set("applied_defaults_fingerprint", defaults_fp)
            store.meta_set("last_recompute", {
                "decision_revision": next_revision,
                "impacted_events": [
                    _event_key_dict(event.event_id, event.seq)
                    for event in impacted_events
                ],
            })

        codes: Dict[str, int] = {}
        if view.conflicts:
            codes["E_RULE_CONFLICT"] = len(view.conflicts)
        if layout_rebuilt:
            codes["I_LAYOUT_REBUILD"] = 1
        return {
            "command": "recompute",
            "success": not view.conflicts,
            "exit_code": 0 if not view.conflicts else 1,
            "layout_rebuilt": layout_rebuilt,
            "full_rejudge": bool(full),
            "decision_revision": next_revision,
            "impacted_event_count": len(impacted_events),
            "impacted_events": [
                _event_key_dict(event.event_id, event.seq)
                for event in impacted_events
            ],
            "changed_rule_names": changed_rule_names,
            "affected_field_paths": sorted(affected_paths),
            "affected_categories": sorted(affected_categories),
            "missing_events": [
                _event_key_dict(event_id, seq)
                for event_id, seq in sorted(store.incomplete_event_keys())
            ],
            "conflicts": [item.to_dict() for item in view.conflicts],
            "business_codes": codes,
        }

    def verify_full_consistency(self) -> Dict[str, Any]:
        with self._lock():
            with LedgerStore(self.workspace) as store:
                events = store.load_events()
                view = self._load_view(store)
                expected = decide_events(events, view, decision_watermark=0)
                ledger = store.load_ledger_rows()
                ledger_by_event: Dict[Tuple[str, int], Dict[str, Any]] = {}
                for row in ledger:
                    key = (row["event_id"], row["seq"])
                    ledger_by_event.setdefault(key, {})[row["field_path"]] = row
                mismatches: List[Dict[str, Any]] = []
                for event in events:
                    result = expected[event.sort_key]
                    actual_fields = ledger_by_event.get(event.sort_key, {})
                    for field_name in sorted(result.fields):
                        decision = result.fields[field_name]
                        actual = actual_fields.get(decision.field_path)
                        if actual is None:
                            mismatches.append(
                                {
                                    "event_id": event.event_id,
                                    "seq": event.seq,
                                    "field_path": decision.field_path,
                                    "reason": "missing ledger row",
                                }
                            )
                            continue
                        expected_row = decision.ledger_row()
                        for key in (
                            "decision",
                            "masked_value",
                            "winning_rule_name",
                            "winning_generation",
                            "winning_threshold",
                            "basis",
                            "matches",
                            "suppressed",
                        ):
                            if actual.get(key) != expected_row.get(key):
                                mismatches.append(
                                    {
                                        "event_id": event.event_id,
                                        "seq": event.seq,
                                        "field_path": decision.field_path,
                                        "key": key,
                                        "expected": expected_row.get(key),
                                        "actual": actual.get(key),
                                    }
                                )
                expected_keys = {
                    (
                        event.event_id,
                        event.seq,
                        expected[event.sort_key].fields[field_name].field_path,
                    )
                    for event in events
                    for field_name in expected[event.sort_key].fields
                }
                for row in ledger:
                    key = (row["event_id"], row["seq"], row["field_path"])
                    if key not in expected_keys:
                        mismatches.append(
                            {
                                "event_id": row["event_id"],
                                "seq": row["seq"],
                                "field_path": row["field_path"],
                                "reason": "stale ledger row",
                            }
                        )
                return {
                    "command": "verify-full-consistency",
                    "success": not mismatches,
                    "exit_code": 0 if not mismatches else 1,
                    "event_count": len(events),
                    "field_count": sum(
                        len(result.fields) for result in expected.values()
                    ),
                    "mismatch_count": len(mismatches),
                    "mismatches": mismatches,
                    "business_codes": (
                        {"E_FULL_INCONSISTENCY": len(mismatches)}
                        if mismatches
                        else {}
                    ),
                }

    def export_report(
        self,
        output_path: str | Path,
        *,
        graph_path: Optional[str | Path] = None,
        truth_path: Optional[str | Path] = None,
    ) -> Dict[str, Any]:
        with self._lock():
            with LedgerStore(self.workspace) as store:
                events = store.load_events()
                view = self._load_view(store)
                result_rows = store.load_event_results()
                results_by_key = {
                    (item["event_id"], int(item["seq"])): item
                    for item in result_rows
                }
                event_reports: List[Dict[str, Any]] = []
                pending = 0
                all_codes: List[str] = []
                field_count = 0
                for event in events:
                    result = results_by_key.get(event.sort_key)
                    if result is None:
                        pending += 1
                        event_reports.append(
                            {
                                "event_id": event.event_id,
                                "seq": event.seq,
                                "sync_marker": event.sync_marker,
                                "category": event.category,
                                "status": "pending",
                                "business_codes": ["E_NOT_RECOMPUTED"],
                                "masked_payload": {},
                                "fields": {},
                            }
                        )
                        all_codes.append("E_NOT_RECOMPUTED")
                        continue
                    event_reports.append(result)
                    all_codes.extend(result.get("business_codes", []))
                    field_count += len(result.get("fields", {}))

                rejections = store.load_rejections()
                all_codes.extend(item["code"] for item in rejections)
                all_codes.extend(
                    "E_RULE_CONFLICT" for _ in view.conflicts
                )
                applied_rules_fingerprint = store.meta_get("rules_fingerprint")
                stale_rule_view = bool(
                    applied_rules_fingerprint is not None
                    and applied_rules_fingerprint != view.fingerprint
                )
                if stale_rule_view:
                    all_codes.append("W_RECOMPUTE_REQUIRED")
                truth_validation = None
                inconsistent_count = 0
                if truth_path is not None:
                    truth_validation, inconsistent_count = self._validate_truth(
                        Path(truth_path),
                        event_reports,
                        view,
                    )
                    if inconsistent_count:
                        all_codes.extend(
                            ["E_TRUTH_MISMATCH"] * inconsistent_count
                        )

                graph = self._build_dependency_graph(events, view)
                summary = {
                    "event_count": len(events),
                    "decided_event_count": len(events) - pending,
                    "pending_event_count": pending,
                    "stale_rule_view": stale_rule_view,
                    "field_count": field_count,
                    "rejection_count": len(rejections),
                    "rule_conflict_count": len(view.conflicts),
                    "inconsistent_count": inconsistent_count,
                }
                report = {
                    "schema": "audit-mask-report/v1",
                    "summary": summary,
                    "inconsistent_count": inconsistent_count,
                    "decision_revision": store.latest_decision_revision(),
                    "layout": view.layout.to_dict(),
                    "layout_fingerprint": layout_fingerprint(view.layout),
                    "rules_fingerprint": view.fingerprint,
                    "applied_rules_fingerprint": applied_rules_fingerprint,
                    "business_codes": _codes(all_codes),
                    "events": event_reports,
                    "rules": {
                        "active": [
                            rule.to_dict() for rule in view.active_rules
                        ],
                        "tombstoned": [
                            rule.to_dict()
                            for rule in sorted(
                                view.rules_by_name.values(),
                                key=lambda item: item.rule_name,
                            )
                            if rule.state == "tombstoned"
                        ],
                        "conflicts": [
                            rule.to_dict()
                            for rule in sorted(
                                view.rules_by_name.values(),
                                key=lambda item: item.rule_name,
                            )
                            if rule.state == "conflict"
                        ],
                    },
                    "conflicts": [
                        item.to_dict() for item in view.conflicts
                    ],
                    "import_rejections": rejections,
                    "truth_validation": truth_validation,
                }
                ensure_dir(Path(output_path).parent)
                report_digest = atomic_write_json(Path(output_path), report)
                graph_digest = None
                if graph_path is not None:
                    ensure_dir(Path(graph_path).parent)
                    graph_digest = atomic_write_json(Path(graph_path), graph)

                exit_code = 1 if inconsistent_count else 0
                if pending or stale_rule_view:
                    exit_code = 1
                return {
                    "command": "export-report",
                    "success": exit_code == 0,
                    "exit_code": exit_code,
                    "output": str(Path(output_path)),
                    "report_digest": report_digest,
                    "graph_output": (
                        str(Path(graph_path)) if graph_path is not None else None
                    ),
                    "graph_digest": graph_digest,
                    "summary": summary,
                    "business_codes": report["business_codes"],
                }

    def _validate_truth(
        self,
        truth_path: Path,
        event_reports: Sequence[Dict[str, Any]],
        view,
    ) -> Tuple[Dict[str, Any], int]:
        try:
            truth = json.loads(truth_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            return (
                {
                    "success": False,
                    "error": f"cannot parse truth file: {exc}",
                    "mismatch_count": 1,
                },
                1,
            )
        truth_events = (
            truth.get("events", [])
            if isinstance(truth, dict)
            else truth
        )
        if not isinstance(truth_events, list):
            return (
                {
                    "success": False,
                    "error": "truth events must be an array",
                    "mismatch_count": 1,
                },
                1,
            )
        by_marker: Dict[str, Dict[str, Any]] = {}
        mismatches: List[Dict[str, Any]] = []
        for index, item in enumerate(truth_events, start=1):
            if not isinstance(item, dict):
                mismatches.append(
                    {"reason": "truth record is not an object", "index": index}
                )
                continue
            marker = item.get("sync_marker")
            if not isinstance(marker, str) or not marker:
                mismatches.append(
                    {"reason": "truth sync_marker is missing", "index": index}
                )
                continue
            if marker in by_marker:
                mismatches.append(
                    {
                        "reason": "duplicate truth sync_marker",
                        "sync_marker": marker,
                    }
                )
                continue
            by_marker[marker] = item

        matched_markers = set()
        for event_report in sorted(
            event_reports,
            key=lambda item: (item["event_id"], int(item["seq"])),
        ):
            marker = event_report.get("sync_marker")
            if not isinstance(marker, str) or not marker:
                mismatches.append(
                    {
                        "event_id": event_report["event_id"],
                        "seq": event_report["seq"],
                        "reason": "output event has no sync_marker",
                    }
                )
                continue
            truth_item = by_marker.get(marker)
            if truth_item is None:
                mismatches.append(
                    {
                        "event_id": event_report["event_id"],
                        "seq": event_report["seq"],
                        "sync_marker": marker,
                        "reason": "sync_marker is absent from truth file",
                    }
                )
                continue
            matched_markers.add(marker)
            try:
                truth_seq = int(truth_item.get("seq", -1))
            except (TypeError, ValueError):
                truth_seq = -1
            if (
                truth_item.get("event_id") != event_report["event_id"]
                or truth_seq != int(event_report["seq"])
            ):
                mismatches.append(
                    {
                        "sync_marker": marker,
                        "reason": "truth marker points to a different event key",
                    }
                )
                continue
            raw_hex = truth_item.get("raw_payload_utf8_hex")
            if not isinstance(raw_hex, str):
                mismatches.append(
                    {
                        "sync_marker": marker,
                        "reason": "truth raw payload bytes are missing",
                    }
                )
                continue
            try:
                raw_bytes = bytes.fromhex(raw_hex)
                payload = json.loads(raw_bytes.decode("utf-8"))
            except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                mismatches.append(
                    {
                        "sync_marker": marker,
                        "reason": f"truth raw payload is invalid: {exc}",
                    }
                )
                continue
            expected_raw_digest = truth_item.get("raw_payload_sha256")
            if (
                isinstance(expected_raw_digest, str)
                and expected_raw_digest != sha256_bytes(raw_bytes)
            ):
                mismatches.append(
                    {
                        "sync_marker": marker,
                        "reason": "truth raw payload SHA-256 does not match bytes",
                    }
                )
                continue
            if not isinstance(payload, dict) or any(
                not isinstance(key, str) or not isinstance(value, str)
                for key, value in payload.items()
            ):
                mismatches.append(
                    {
                        "sync_marker": marker,
                        "reason": "truth raw payload is not a string map",
                    }
                )
                continue
            expected = reference_expected_fields(
                payload,
                event_report["category"],
                view,
            )
            actual_payload = event_report.get("masked_payload", {})
            expected_payload = {
                field_name: item["masked_value"]
                for field_name, item in expected.items()
            }
            if actual_payload != expected_payload:
                mismatches.append(
                    {
                        "sync_marker": marker,
                        "reason": "masked payload mismatch",
                        "expected": expected_payload,
                        "actual": actual_payload,
                    }
                )
                continue
            actual_fields = event_report.get("fields", {})
            for field_name, expected_item in expected.items():
                actual_field = actual_fields.get(field_name)
                if not isinstance(actual_field, dict):
                    mismatches.append(
                        {
                            "sync_marker": marker,
                            "field": field_name,
                            "reason": "field decision is missing",
                        }
                    )
                    continue
                if (
                    actual_field.get("winning_rule_name")
                    != expected_item["winning_rule_name"]
                    or int(actual_field.get("winning_generation", -1))
                    != int(expected_item["winning_generation"])
                ):
                    mismatches.append(
                        {
                            "sync_marker": marker,
                            "field": field_name,
                            "reason": "winning rule mismatch",
                            "expected": expected_item,
                            "actual": {
                                "winning_rule_name": actual_field.get(
                                    "winning_rule_name"
                                ),
                                "winning_generation": actual_field.get(
                                    "winning_generation"
                                ),
                            },
                        }
                    )
        missing_markers = sorted(set(by_marker) - matched_markers)
        for marker in missing_markers:
            mismatches.append(
                {"sync_marker": marker, "reason": "truth event has no output"}
            )
        return (
            {
                "success": not mismatches,
                "truth_event_count": len(by_marker),
                "matched_marker_count": len(matched_markers),
                "mismatch_count": len(mismatches),
                "mismatches": mismatches,
            },
            len(mismatches),
        )

    def _build_dependency_graph(
        self,
        events: Sequence[Event],
        view,
    ) -> Dict[str, Any]:
        rule_edges: List[Dict[str, Any]] = []
        field_to_rules: Dict[str, Set[str]] = {}
        field_to_events: Dict[str, List[Dict[str, Any]]] = {}
        for rule in view.active_rules:
            if not rule.field_path:
                continue
            matched_events: List[Dict[str, Any]] = []
            for event in events:
                if any(
                    field_matches_path(rule.field_path, field_name)
                    for field_name in event.payload
                ):
                    matched_events.append(
                        _event_key_dict(event.event_id, event.seq)
                    )
            rule_edges.append(
                {
                    "rule_name": rule.rule_name,
                    "field_path": rule.field_path,
                    "generation": rule.generation,
                    "effective_ms": rule.effective_ms,
                    "mask": rule.mask,
                    "threshold": rule.threshold,
                    "event_keys": matched_events,
                }
            )
            field_to_rules.setdefault(rule.field_path, set()).add(rule.rule_name)
            field_to_events.setdefault(rule.field_path, [])
            known = {
                (item["event_id"], item["seq"])
                for item in field_to_events[rule.field_path]
            }
            for item in matched_events:
                key = (item["event_id"], item["seq"])
                if key not in known:
                    field_to_events[rule.field_path].append(item)
                    known.add(key)
        field_edges = [
            {
                "field_path": field_path,
                "rule_names": sorted(rule_names),
                "event_keys": sorted(
                    field_to_events.get(field_path, []),
                    key=lambda item: (item["event_id"], item["seq"]),
                ),
            }
            for field_path, rule_names in sorted(field_to_rules.items())
        ]
        default_edges: List[Dict[str, Any]] = []
        for category, default in sorted(view.defaults.items()):
            event_keys = [
                _event_key_dict(event.event_id, event.seq)
                for event in events
                if event.category == category
                and any(
                    field_matches_path(pattern, field_name)
                    for field_name in event.payload
                    for pattern in default.fields
                )
            ]
            default_edges.append(
                {
                    "category": category,
                    "rule_name": default.rule_name,
                    "generation": default.generation,
                    "mask": default.mask,
                    "fields": list(default.fields),
                    "event_keys": event_keys,
                }
            )
        return {
            "schema": "audit-mask-dependency-graph/v1",
            "layout_fingerprint": layout_fingerprint(view.layout),
            "rules_fingerprint": view.fingerprint,
            "rules": rule_edges,
            "fields": field_edges,
            "category_default_edges": default_edges,
            "category_defaults": {
                name: item.to_dict()
                for name, item in sorted(view.defaults.items())
            },
            "conflicts": [item.to_dict() for item in view.conflicts],
        }

    def create_checkpoint(
        self,
        snapshot_path: Optional[str | Path] = None,
    ) -> Dict[str, Any]:
        with self._lock():
            with LedgerStore(self.workspace) as store:
                result = create_checkpoint(
                    store,
                    snapshot_path=str(snapshot_path) if snapshot_path else None,
                )
                result["command"] = "checkpoint-create"
                result["success"] = True
                result["exit_code"] = 0
                result["business_codes"] = {}
                return result

    def verify_checkpoints(self) -> Dict[str, Any]:
        with self._lock():
            with LedgerStore(self.workspace) as store:
                view = self._load_view(store)
                verification = verify_checkpoints(
                    store,
                    current_rules_fingerprint=view.fingerprint,
                )
                verification.pop("latest_valid_snapshot", None)
                invalid_count = int(verification["invalid_count"])
                verification["command"] = "checkpoint-verify"
                verification["success"] = invalid_count == 0
                verification["exit_code"] = 0 if invalid_count == 0 else 1
                codes = {}
                if invalid_count:
                    codes["E_CHECKPOINT"] = invalid_count
                if (
                    verification["latest_valid"] is not None
                    and not verification["current_ledger_matches_latest"]
                ):
                    codes["W_LEDGER_DIVERGED"] = 1
                if (
                    verification["latest_valid"] is not None
                    and not verification["latest_valid"]["current_rules_match"]
                ):
                    codes["W_RECOMPUTE_REQUIRED"] = 1
                verification["business_codes"] = codes
                return verification

    def restore_checkpoint(self) -> Dict[str, Any]:
        with self._lock():
            with LedgerStore(self.workspace) as store:
                view = self._load_view(store)
                try:
                    result = restore_latest_checkpoint(
                        store,
                        current_rules_fingerprint=view.fingerprint,
                    )
                except CheckpointError as exc:
                    return {
                        "command": "checkpoint-restore",
                        "success": False,
                        "exit_code": 1,
                        "error": str(exc),
                        "business_codes": {"E_CHECKPOINT": 1},
                    }
                result["command"] = "checkpoint-restore"
                result["success"] = True
                result["exit_code"] = 0
                result["business_codes"] = (
                    {"W_RECOMPUTE_REQUIRED": 1}
                    if result.get("rules_changed_since_checkpoint")
                    else {}
                )
                return result
