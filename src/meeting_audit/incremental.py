from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

import numpy as np

from .audio import FrameFeatures, extract_features, frame_count
from .canonical import atomic_write_bytes, atomic_write_json, canonical_bytes, content_key
from .codes import E_INCREMENTAL_MISMATCH
from .config import AuditConfig
from .pipeline import AuditPipeline
from .selfcheck import build_diagnostic_report, run_self_checks


def canonical_row(row: dict[str, Any], view: str = "common") -> bytes:
    return canonical_bytes(row[view])


def diff_sets(old_items: dict[str, Any], new_items: dict[str, Any]) -> dict[str, list[str]]:
    changed: list[str] = []
    reused: list[str] = []
    for key, new_value in new_items.items():
        if key in old_items and canonical_bytes(old_items[key]) == canonical_bytes(new_value):
            reused.append(key)
        else:
            changed.append(key)
    removed = sorted(key for key in old_items if key not in new_items)
    return {"recomputed": sorted(changed), "reused": sorted(reused), "removed": removed}


def diff_ledger(
    old_rows: list[dict[str, Any]], new_rows: list[dict[str, Any]]
) -> dict[str, Any]:
    old_by_id = {row["common"]["ledger_id"]: row for row in old_rows}
    new_by_id = {row["common"]["ledger_id"]: row for row in new_rows}
    common_diff = diff_sets(old_by_id, new_by_id)
    byte_changed = []
    byte_reused = []
    for ledger_id in sorted(set(old_by_id) & set(new_by_id)):
        same = all(
            canonical_bytes(old_by_id[ledger_id][view])
            == canonical_bytes(new_by_id[ledger_id][view])
            for view in ("common", "v1", "v2")
        )
        (byte_reused if same else byte_changed).append(ledger_id)
    new_ids = sorted(set(new_by_id) - set(old_by_id))
    return {
        "recomputed_ledger_rows": sorted(set(byte_changed) | set(new_ids)),
        "reused_ledger_rows": byte_reused,
        "removed_ledger_rows": common_diff["removed"],
        "new_ledger_rows": new_ids,
    }


def audio_diff_interval(
    old_pcm: np.ndarray, new_pcm: np.ndarray, config: AuditConfig
) -> dict[str, Any]:
    old = np.asarray(old_pcm, dtype=np.int16)
    new = np.asarray(new_pcm, dtype=np.int16)
    common_length = min(len(old), len(new))
    if common_length:
        changed_positions = np.flatnonzero(old[:common_length] != new[:common_length])
    else:
        changed_positions = np.asarray([], dtype=np.int64)

    if changed_positions.size == 0 and len(old) == len(new):
        return {
            "changed": False,
            "changed_sample_range": None,
            "first_affected_frame": 0,
            "recompute_frame_interval": [0, 0],
            "reused_frame_intervals": [[0, len(new) // config.hop_samples]],
        }

    if changed_positions.size:
        first_sample = int(changed_positions[0])
        last_sample = int(changed_positions[-1])
    else:
        first_sample = common_length
        last_sample = max(len(old), len(new)) - 1

    frame_len = config.frame_samples
    hop = config.hop_samples
    first_frame = max(0, (first_sample - frame_len) // hop + 1)
    new_frame_count = max(0, (len(new) - frame_len) // hop + 1) if len(new) >= frame_len else 0
    first_frame = min(first_frame, new_frame_count)
    return {
        "changed": True,
        "changed_sample_range": [first_sample, last_sample],
        "changed_ms_range": [
            first_sample // config.samples_per_ms,
            last_sample // config.samples_per_ms,
        ],
        "first_affected_frame": first_frame,
        "recompute_frame_interval": [first_frame, new_frame_count],
        "reused_frame_intervals": [[0, first_frame]],
    }


class IncrementalManager:
    def __init__(self, pipeline: AuditPipeline):
        self.pipeline = pipeline

    def _current_pcm(self) -> tuple[np.ndarray, dict[str, Any]]:
        return self.pipeline.ensure_sample()

    def _finish_with_report(
        self,
        result: dict[str, Any],
        old_result: dict[str, Any],
        trigger: str,
        trigger_id: str,
        revision_id: str,
        pcm: np.ndarray,
        run_kwargs: dict[str, Any],
        invalidation: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        ledger_diff = diff_ledger(old_result.get("ledger_rows", []), result["ledger_rows"])
        old_assignments = old_result.get("assignments", {})
        assignment_diff = diff_sets(old_assignments, result["assignments"])
        verify = self.pipeline.verify_fresh_full_bytes(
            pcm=pcm,
            expected_ledger_dir=self.pipeline.ledger_dir,
            inputs={
                "revision_id": revision_id,
                "truth_overrides": run_kwargs.get("truth_overrides") or {},
                "v1_doc": run_kwargs.get("v1_doc"),
                "v2_doc": run_kwargs.get("v2_doc"),
            },
        )

        recomputed_segments = sorted(
            set(assignment_diff["recomputed"]) | set((invalidation or {}).get("segments", []))
        )
        reused_segments = [
            segment_id
            for segment_id in assignment_diff["reused"]
            if segment_id not in recomputed_segments
        ]
        report = {
            "ok": verify["ok"],
            "code": None if verify["ok"] else E_INCREMENTAL_MISMATCH,
            "trigger": trigger,
            "trigger_id": trigger_id,
            "revision_id": revision_id,
            "rule_version": (run_kwargs.get("v2_doc") or {}).get("rule_version")
            or result["keys"]["rule_version"],
            "content_keys": {
                "pcm_key": result["pcm_key"],
                "rules_v2_key": result["keys"]["rules_v2_key"],
                "config_key": result["keys"]["config_key"],
            },
            "invalidation": invalidation or {},
            "recomputed": {
                "segments": recomputed_segments,
                "ledger_rows": ledger_diff["recomputed_ledger_rows"],
                "frames": (invalidation or {}).get("recompute_frame_interval", []),
                "stages": (invalidation or {}).get(
                    "recomputed_stages",
                    ["identity_adjudication", "ledger_write", "audit_report"],
                ),
            },
            "reused": {
                "segments": reused_segments,
                "ledger_rows": ledger_diff["reused_ledger_rows"],
                "frames": (invalidation or {}).get("reused_frame_intervals", [[0, result["frame_count"]]]),
                "stages": (invalidation or {}).get(
                    "reused_stages",
                    ["frame_features", "turn_detection", "anomaly_classification"],
                ),
                "cache_stats": result.get("cache_report"),
            },
            "assignment_diff": assignment_diff,
            "ledger_diff": ledger_diff,
            "byte_equality": verify,
        }
        result["incremental_report"] = report
        refreshed_check = run_self_checks(result, self.pipeline.config, report)
        diagnostic = build_diagnostic_report(result, refreshed_check)
        result["self_check"] = refreshed_check
        result["diagnostic"] = diagnostic
        atomic_write_json(self.pipeline.reports_dir / "self_check.json", refreshed_check)
        atomic_write_json(self.pipeline.reports_dir / "diagnostic.json", diagnostic)
        audit_snapshot = self.pipeline.snapshots.commit_stage(
            "audit_report",
            [[0, 1]],
            {
                "self_check_ok": refreshed_check["ok"],
                "incremental_ok": report["ok"],
                "diagnostic_key": content_key(diagnostic),
            },
            payload=refreshed_check,
            completed_ids=["AUDIT-0001"],
        )
        result.setdefault("snapshots", {})["audit_report"] = audit_snapshot
        self.pipeline._save_result(result)
        if not report["ok"]:
            raise AssertionError(f"incremental byte mismatch: {verify}")
        if not refreshed_check["ok"]:
            # Structural injected anomalies are business findings, not harness failure.
            # Only consistency errors matter here; temporal errors are returned in report.
            consistency_codes = {
                "E_QUERY_MISMATCH",
                "E_INCREMENTAL_MISMATCH",
                "E_SNAPSHOT_MISMATCH",
                "E_RULE_MIGRATION_MISMATCH",
            }
            bad = [
                code
                for code in refreshed_check.get("error_codes", [])
                if code in consistency_codes
            ]
            if bad:
                raise AssertionError(f"consistency self-check failed: {refreshed_check}")
        return report

    def apply_truth_correction(
        self, segment_id: str, truth_key: str, revision_id: str = "truth-1"
    ) -> dict[str, Any]:
        old_result = self.pipeline.load_last_result()
        inputs_path = self.pipeline.inputs_path
        inputs = json.loads(inputs_path.read_text(encoding="utf-8")) if inputs_path.exists() else {}
        truth_overrides = dict(inputs.get("truth_overrides") or {})
        truth_overrides[segment_id] = truth_key
        pcm, _manifest = self._current_pcm()
        v1_doc, v2_doc = self.pipeline.ensure_rules()

        target = next(
            (segment for segment in old_result["segments"] if segment["segment_id"] == segment_id),
            None,
        )
        invalidation = {
            "kind": "identity_truth",
            "segments": [segment_id],
            "ledger_rows": [
                row["common"]["ledger_id"]
                for row in old_result["ledger_rows"]
                if row["common"].get("segment_id") == segment_id
            ],
            "frame_interval": target.get("frame_range") if target else None,
            "recomputed_stages": [
                "identity_adjudication",
                "ledger_write",
                "audit_report",
            ],
            "reused_stages": [
                "frame_features",
                "turn_detection",
                "anomaly_classification",
            ],
        }
        result = self.pipeline.run_full(
            revision_id=revision_id,
            pcm=pcm,
            truth_overrides=truth_overrides,
            v1_doc=v1_doc,
            v2_doc=v2_doc,
        )
        inputs["truth_overrides"] = truth_overrides
        atomic_write_json(inputs_path, inputs)
        return self._finish_with_report(
            result,
            old_result,
            "identity_truth_correction",
            f"{segment_id}={truth_key}",
            revision_id,
            pcm,
            {"truth_overrides": truth_overrides, "v1_doc": v1_doc, "v2_doc": v2_doc},
            invalidation,
        )

    def promote_rules(
        self,
        new_version: str = "v2.1.0",
        threshold_updates: dict[str, int] | None = None,
        revision_id: str = "rules-1",
    ) -> dict[str, Any]:
        old_result = self.pipeline.load_last_result()
        pcm, _manifest = self._current_pcm()
        v1_doc, v2_doc = self.pipeline.ensure_rules()
        prior_inputs = self._read_inputs()
        truth_overrides = dict(prior_inputs.get("truth_overrides") or {})
        v2_new = deepcopy(v2_doc)
        v1_new = deepcopy(v1_doc)
        v2_new["rule_version"] = new_version
        v2_new.setdefault("thresholds", {}).update(threshold_updates or {})
        v1_new.setdefault("thresholds", {}).update(threshold_updates or {})
        v1_new["rule_version"] = "v1.1.0-compatible"
        for rule in v2_new.get("rules", []):
            metric = rule.get("condition", {}).get("metric")
            threshold_key = {
                "turn_interval_ms": "rapid_switch_ms",
                "preceding_silence_ms": "silence_long_ms",
                "energy_ratio_q": "preempt_energy_ratio_q",
            }.get(metric)
            if threshold_key and threshold_key in v2_new["thresholds"]:
                rule["condition"]["threshold"] = v2_new["thresholds"][threshold_key]
            rule["advanced"] = {
                "scope": "session",
                "nested_reason": "v2 promotion adds an expression that v1 downgrade discards explicitly",
            }

        from .rules import write_v2_sharded

        atomic_write_json(self.pipeline.rules_v2_dir / "rules_v2_full.json", v2_new)
        write_v2_sharded(v2_new, self.pipeline.rules_v2_dir)
        atomic_write_json(self.pipeline.rules_v1_dir / "rules.json", v1_new)

        invalidation = {
            "kind": "rule_version_promotion",
            "rule_version": new_version,
            "segments": [],
            "ledger_rows": [row["common"]["ledger_id"] for row in old_result["ledger_rows"]],
            "recompute_frame_interval": [0, old_result["frame_count"]],
            "reused_frame_intervals": [[0, old_result["frame_count"]]],
            "recomputed_stages": ["anomaly_classification", "ledger_write", "audit_report"],
            "reused_stages": ["frame_features", "turn_detection", "identity_adjudication"],
        }
        result = self.pipeline.run_full(
            revision_id=revision_id,
            pcm=pcm,
            truth_overrides=truth_overrides,
            v1_doc=v1_new,
            v2_doc=v2_new,
        )
        return self._finish_with_report(
            result,
            old_result,
            "rule_version_promotion",
            new_version,
            revision_id,
            pcm,
            {
                "v1_doc": v1_new,
                "v2_doc": v2_new,
                "truth_overrides": truth_overrides,
            },
            invalidation,
        )

    def apply_audio_revision(
        self,
        new_pcm: np.ndarray,
        revision_id: str = "audio-1",
        change_note: str = "deterministic audio revision",
    ) -> dict[str, Any]:
        old_pcm, _manifest = self._current_pcm()
        old_result = self.pipeline.load_last_result()
        interval = audio_diff_interval(old_pcm, new_pcm, self.pipeline.config)
        v1_doc, v2_doc = self.pipeline.ensure_rules()
        prior_inputs = self._read_inputs()
        truth_overrides = dict(prior_inputs.get("truth_overrides") or {})

        boundary = int(interval["first_affected_frame"])
        reused_features = [
            FrameFeatures.from_dict(item)
            for item in old_result.get("features", [])[:boundary]
        ]
        new_frame_count = frame_count(len(new_pcm), self.pipeline.config)
        recomputed_features = []
        for frame_index in range(boundary, new_frame_count):
            start = self.pipeline.config.frame_start_sample(frame_index)
            frame = new_pcm[start : start + self.pipeline.config.frame_samples]
            recomputed_features.append(
                extract_features(frame, frame_index, self.pipeline.config, "fixed")
            )
        feature_override = reused_features + recomputed_features
        interval["reused_feature_count"] = len(reused_features)
        interval["recomputed_feature_count"] = len(recomputed_features)

        atomic_write_bytes(self.pipeline.sample_dir / "meeting.pcm", np.asarray(new_pcm, dtype="<i2").tobytes())
        result = self.pipeline.run_full(
            revision_id=revision_id,
            pcm=new_pcm,
            truth_overrides=truth_overrides,
            v1_doc=v1_doc,
            v2_doc=v2_doc,
            feature_objects_override=feature_override,
        )
        affected_segments = [
            segment["segment_id"]
            for segment in result["segments"]
            if interval.get("changed_ms_range")
            and not (
                int(segment["end_ms"]) <= interval["changed_ms_range"][0]
                or int(segment["start_ms"]) >= interval["changed_ms_range"][1]
            )
        ]
        affected_ledger_rows = [
            row["common"]["ledger_id"]
            for row in result["ledger_rows"]
            if interval.get("changed_ms_range")
            and int(row["common"]["at_ms"]) >= max(0, interval["changed_ms_range"][0] - 50)
        ]
        interval["segments"] = affected_segments
        interval["ledger_rows"] = affected_ledger_rows
        interval["change_note"] = change_note
        interval["recomputed_stages"] = [
            "frame_features",
            "turn_detection",
            "anomaly_classification",
            "identity_adjudication",
            "ledger_write",
            "audit_report",
        ]
        result["audio_invalidation"] = interval
        report = self._finish_with_report(
            result,
            old_result,
            "audio_revision",
            content_key(np.asarray(new_pcm, dtype=np.int16).tobytes())[:12],
            revision_id,
            new_pcm,
            {
                "v1_doc": v1_doc,
                "v2_doc": v2_doc,
                "truth_overrides": truth_overrides,
            },
            interval,
        )
        return report

    def _read_inputs(self) -> dict[str, Any]:
        inputs_path = self.pipeline.inputs_path
        if not inputs_path.exists():
            return {}
        return json.loads(inputs_path.read_text(encoding="utf-8"))
