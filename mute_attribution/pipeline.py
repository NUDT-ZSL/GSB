from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Callable

from .attribution import (
    compute_attribution_record,
    validate_attribution_record,
)
from .config import STAGE_ORDER, section_revision
from .errors import BusinessError, fail
from .features import (
    CallMeta,
    compute_feature_record,
    feature_input_fingerprint,
    load_manifest,
    validate_feature_record,
)
from .noise import compute_noise_record, validate_noise_record
from .profile import compute_profile_record
from .store import Snapshot, SnapshotStore
from .synth import load_truth, truth_frame_kinds
from .util import integer_ranges, sha256_json


def _parse_crash_point(value: str | None) -> tuple[str, int] | None:
    if not value:
        return None
    try:
        stage, count = value.split(":", 1)
        parsed = (stage, int(count))
    except (ValueError, TypeError):
        fail(
            "E_CONFIG_INVALID",
            detail="crash point must use stage:count syntax",
            crash_at=value,
        )
    if parsed[0] not in STAGE_ORDER or parsed[1] < 0:
        fail(
            "E_CONFIG_INVALID",
            detail="invalid stage or count in crash point",
            crash_at=value,
        )
    return parsed


def _dependency_equal(
    snapshot: Snapshot | None,
    call_id: str,
    expected: dict[str, Any],
) -> bool:
    if snapshot is None:
        return False
    record = snapshot.record_map().get(call_id)
    return bool(record) and record.get("dependencies") == expected


def _affected_calls(
    calls: list[CallMeta],
    snapshot: Snapshot | None,
    expected_by_call: dict[str, dict[str, Any]],
) -> list[str]:
    if snapshot is None:
        return [call.call_id for call in calls]
    return [
        call.call_id
        for call in calls
        if not _dependency_equal(
            snapshot, call.call_id, expected_by_call[call.call_id]
        )
    ]


def _full_ranges(
    calls: list[CallMeta], affected: list[str]
) -> dict[str, list[list[int]]]:
    by_id = {call.call_id: call for call in calls}
    return {
        call_id: [
            [
                0,
                by_id[call_id].duration_ms // by_id[call_id].frame_ms,
            ]
        ]
        for call_id in affected
    }


def _truth_changed_ranges(
    old_record: dict[str, Any] | None,
    new_kinds: list[str],
) -> list[list[int]]:
    if not old_record:
        return [[0, len(new_kinds)]]
    old_kinds = old_record.get("truth_frame_kinds")
    if not isinstance(old_kinds, list) or len(old_kinds) != len(new_kinds):
        return [[0, len(new_kinds)]]
    changed = [
        index
        for index, (old, new) in enumerate(zip(old_kinds, new_kinds))
        if old != new
    ]
    return integer_ranges(changed)


def _overlapping_segment_ids(
    old_record: dict[str, Any] | None, ranges: list[list[int]]
) -> list[str]:
    if not old_record:
        return []
    affected: list[str] = []
    for segment in old_record.get("silence_segments", []):
        start = int(segment["start_frame"])
        end = int(segment["end_frame"])
        if any(left < end and right > start for left, right in ranges):
            affected.append(str(segment["segment_id"]))
    return affected


class Pipeline:
    def __init__(
        self,
        data_dir: Path,
        store: SnapshotStore,
        config: dict[str, Any],
        crash_at: str | None = None,
    ):
        self.data_dir = data_dir.resolve()
        self.store = store
        self.config = config
        self.calls = load_manifest(self.data_dir)
        self.call_map = {call.call_id: call for call in self.calls}
        self.crash_point = _parse_crash_point(crash_at)
        self.discarded_pending: list[str] = []
        self.recovery: dict[str, Any] = {}
        self.stage_reports: list[dict[str, Any]] = []

    def _maybe_crash(self, stage: str, computed_count: int) -> None:
        if self.crash_point is None:
            return
        crash_stage, crash_count = self.crash_point
        if stage == crash_stage and computed_count >= crash_count:
            os._exit(137)

    def _stage_commit(
        self,
        stage: str,
        current: Snapshot | None,
        affected: list[str],
        build_record: Callable[[str, int], dict[str, Any]],
        complete_record: Callable[[dict[str, Any]], None],
        dependency_map: dict[str, dict[str, Any]],
        config_revision: str,
        stage_config: dict[str, Any],
        affected_frame_ranges: dict[str, list[list[int]]],
        affected_segment_ids: dict[str, list[str]],
        active_snapshot: bool,
    ) -> tuple[Snapshot | None, dict[str, Any]]:
        affected_set = set(affected)
        all_call_ids = [call.call_id for call in self.calls]
        reused = [call_id for call_id in all_call_ids if call_id not in affected_set]
        if not affected and active_snapshot:
            report = {
                "stage": stage,
                "status": "reused",
                "version": current.version if current else None,
                "reused_calls": reused,
                "recomputed_calls": [],
                "affected_calls": [],
                "affected_frame_ranges": {},
                "affected_segment_ids": {},
            }
            self.stage_reports.append(report)
            return current, report

        new_version = self.store.next_version(stage)
        current_records = current.record_map() if current else {}
        records: list[dict[str, Any]] = []
        existing_pending = self.store.pending_stage_dir(stage)
        if existing_pending.exists():
            import shutil

            shutil.rmtree(existing_pending)
        computed_count = 0
        for call_id in all_call_ids:
            if call_id in affected_set:
                record = build_record(call_id, new_version)
                complete_record(record)
                self.store.write_pending(stage, call_id, record)
                computed_count += 1
                self._maybe_crash(stage, computed_count)
            else:
                record = current_records[call_id]
            records.append(record)
        invalidation = {
            "affected_calls": affected,
            "recomputed_calls": affected,
            "reused_calls": reused,
            "coverage_calls": sorted(reused + affected),
            "affected_frame_ranges": affected_frame_ranges,
            "affected_segment_ids": affected_segment_ids,
        }
        snapshot = self.store.commit_stage(
            stage=stage,
            records=records,
            completed_call_ids=all_call_ids,
            config_revision=config_revision,
            input_signature=dependency_map,
            stage_config=stage_config,
            invalidation=invalidation,
        )
        self.store.finish_stage_work(stage)
        report = {
            "stage": stage,
            "status": "replaced_or_built",
            "version": snapshot.version,
            "reused_calls": reused,
            "recomputed_calls": affected,
            "affected_calls": affected,
            "affected_frame_ranges": affected_frame_ranges,
            "affected_segment_ids": affected_segment_ids,
        }
        self.stage_reports.append(report)
        return snapshot, report

    def _run_features(self) -> Snapshot:
        current = self.store.get_current("features")
        active = current is not None
        fingerprints = {
            call.call_id: feature_input_fingerprint(self.data_dir, call)
            for call in self.calls
        }
        config_revision = section_revision(self.config, "features")
        expected = {
            call.call_id: {
                "feature_config_revision": config_revision,
                "input_fingerprint": fingerprints[call.call_id],
            }
            for call in self.calls
        }
        affected = _affected_calls(self.calls, current, expected)

        def build(call_id: str, version: int) -> dict[str, Any]:
            return compute_feature_record(
                self.call_map[call_id],
                self.data_dir,
                self.config,
                version,
                fingerprints[call_id],
            )

        snapshot, _ = self._stage_commit(
            "features",
            current,
            affected,
            build,
            validate_feature_record,
            expected,
            config_revision,
            self.config["features"],
            _full_ranges(self.calls, affected),
            {call_id: [] for call_id in affected},
            active,
        )
        assert snapshot is not None
        return snapshot

    def _run_noise(
        self, feature_snapshot: Snapshot
    ) -> Snapshot:
        active_snapshot = self.store.get_current("noise_floor")
        active = active_snapshot is not None
        current = active_snapshot or self.initial_snapshots.get("noise_floor")
        feature_records = feature_snapshot.record_map()
        config_revision = section_revision(self.config, "noise_floor")
        expected = {
            call.call_id: {
                "source_feature_checksum": feature_records[call.call_id][
                    "record_checksum"
                ],
                "noise_config_revision": config_revision,
                "frame_count": feature_records[call.call_id]["frame_count"],
            }
            for call in self.calls
        }
        affected = _affected_calls(self.calls, current, expected)

        def build(call_id: str, version: int) -> dict[str, Any]:
            return compute_noise_record(
                feature_records[call_id],
                self.config["noise_floor"],
                self.config,
                version,
            )

        snapshot, _ = self._stage_commit(
            "noise_floor",
            current,
            affected,
            build,
            validate_noise_record,
            expected,
            config_revision,
            self.config["noise_floor"],
            _full_ranges(self.calls, affected),
            {call_id: [] for call_id in affected},
            active,
        )
        assert snapshot is not None
        return snapshot

    def _run_attribution(
        self, feature_snapshot: Snapshot, noise_snapshot: Snapshot
    ) -> Snapshot:
        active_snapshot = self.store.get_current("attribution")
        active = active_snapshot is not None
        current = active_snapshot or self.initial_snapshots.get("attribution")
        feature_records = feature_snapshot.record_map()
        noise_records = noise_snapshot.record_map()
        config_revision = section_revision(self.config, "attribution")
        truths = {
            call.call_id: load_truth(self.data_dir, call.call_id)
            for call in self.calls
        }
        truth_hashes = {
            call_id: sha256_json(truth) for call_id, truth in truths.items()
        }
        expected = {
            call.call_id: {
                "source_noise_checksum": noise_records[call.call_id][
                    "record_checksum"
                ],
                "truth_fingerprint": truth_hashes[call.call_id],
                "truth_revision": truths[call.call_id].get("revision"),
                "attribution_config_revision": config_revision,
                "frame_count": feature_records[call.call_id]["frame_count"],
            }
            for call in self.calls
        }
        affected = _affected_calls(self.calls, current, expected)
        current_records = current.record_map() if current else {}
        ranges_by_call: dict[str, list[list[int]]] = {}
        segments_by_call: dict[str, list[str]] = {}
        for call_id in affected:
            old_record = current_records.get(call_id)
            frame_count = int(feature_records[call_id]["frame_count"])
            if current is None or old_record is None:
                ranges = [[0, frame_count]]
            elif (
                old_record.get("attribution_config_revision") != config_revision
                or old_record.get("source_noise_checksum")
                != noise_records[call_id]["record_checksum"]
            ):
                ranges = [[0, frame_count]]
            else:
                try:
                    new_kinds = truth_frame_kinds(truths[call_id], frame_count)
                except BusinessError:
                    new_kinds = []
                ranges = _truth_changed_ranges(
                    old_record,
                    new_kinds if new_kinds else ["unknown"] * frame_count,
                )
            ranges_by_call[call_id] = ranges
            segments_by_call[call_id] = _overlapping_segment_ids(
                old_record, ranges
            )

        def build(call_id: str, version: int) -> dict[str, Any]:
            revision = (
                f"attrib:{config_revision[:16]}:"
                f"noise:{noise_records[call_id]['record_checksum'][:16]}:"
                f"features:{feature_records[call_id]['record_checksum'][:16]}:"
                f"truth:{truth_hashes[call_id][:16]}:v{version}"
            )
            return compute_attribution_record(
                self.call_map[call_id],
                feature_records[call_id],
                noise_records[call_id],
                truths[call_id],
                self.config,
                version,
                revision,
            )

        snapshot, _ = self._stage_commit(
            "attribution",
            current,
            affected,
            build,
            validate_attribution_record,
            expected,
            config_revision,
            self.config["attribution"],
            ranges_by_call,
            segments_by_call,
            active,
        )
        assert snapshot is not None
        return snapshot

    def _run_profile(self, attr_snapshot: Snapshot) -> Snapshot:
        active_snapshot = self.store.get_current("profile")
        active = active_snapshot is not None
        current = active_snapshot or self.initial_snapshots.get("profile")
        attr_records = attr_snapshot.record_map()
        config_revision = section_revision(self.config, "profile")
        controlled = list(self.config["attribution"]["controlled_labels"])
        expected = {
            call.call_id: {
                "source_attribution_checksum": attr_records[call.call_id][
                    "record_checksum"
                ],
                "profile_config_revision": config_revision,
                "duration_ms": call.duration_ms,
                "controlled_labels": controlled,
            }
            for call in self.calls
        }
        affected = _affected_calls(self.calls, current, expected)

        def build(call_id: str, version: int) -> dict[str, Any]:
            return compute_profile_record(
                self.call_map[call_id],
                attr_records[call_id],
                self.config,
                version,
            )

        snapshot, _ = self._stage_commit(
            "profile",
            current,
            affected,
            build,
            lambda record: None,
            expected,
            config_revision,
            self.config["profile"],
            _full_ranges(self.calls, affected),
            {call_id: [] for call_id in affected},
            active,
        )
        assert snapshot is not None
        return snapshot

    def run(self) -> dict[str, Any]:
        self.discarded_pending = self.store.discard_pending()
        committed_before = self.store.committed_stage_names()
        self.initial_snapshots = {
            stage: self.store.get_current(stage) for stage in STAGE_ORDER
        }
        last_committed = committed_before[-1] if committed_before else None
        features = self._run_features()
        noise = self._run_noise(features)
        attribution = self._run_attribution(features, noise)
        profile = self._run_profile(attribution)
        from .consistency import run_consistency

        consistency = run_consistency(self.store, self.calls)
        after = self.store.committed_stage_names()
        rebuilt = [
            report["stage"]
            for report in self.stage_reports
            if report["status"] != "reused"
        ]
        self.recovery = {
            "resumed_from": last_committed,
            "committed_stages_before_run": committed_before,
            "committed_stages_after_run": after,
            "rebuilt_stages": rebuilt,
            "discarded_pending": self.discarded_pending,
        }
        return {
            "code": "OK",
            "call_ids": [call.call_id for call in self.calls],
            "versions": {
                "features": features.version,
                "noise_floor": noise.version,
                "attribution": attribution.version,
                "profile": profile.version,
            },
            "checksums": {
                "features": features.checksum,
                "noise_floor": noise.checksum,
                "attribution": attribution.checksum,
                "profile": profile.checksum,
            },
            "stages": self.stage_reports,
            "recovery": self.recovery,
            "consistency": consistency,
        }


