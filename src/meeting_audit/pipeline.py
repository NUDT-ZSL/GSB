from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np

from .audio import extract_all_features
from .canonical import atomic_write_bytes, atomic_write_json, canonical_bytes, content_key
from .config import AuditConfig, DEFAULT_CONFIG
from .detector import detect_turns
from .identity import IdentityAdjudicator, build_speech_segments
from .ledger import build_ledger_rows, write_dual_ledger
from .rules import (
    build_default_v1_rules,
    classify_turns,
    compare_rule_views,
    migrate_v1_to_v2,
    write_v1_rules,
    write_v2_sharded,
    read_v2_sharded,
)
from .sample_generator import generate_sample, load_manifest, write_sample_bundle
from .selfcheck import build_diagnostic_report, run_self_checks
from .state import ContentAddressedCache, SnapshotStore, STAGES


def assignment_content_key(assignment: dict[str, Any]) -> str:
    return content_key(
        {
            "segment_id": assignment["segment_id"],
            "speaker_id": assignment["speaker_id"],
            "status": assignment["status"],
            "truth_key": assignment.get("truth_key"),
            "feature": assignment["feature"],
        }
    )


def process_audio(
    pcm: np.ndarray,
    config: AuditConfig = DEFAULT_CONFIG,
    v1_doc: dict[str, Any] | None = None,
    v2_doc: dict[str, Any] | None = None,
    truth_overrides: dict[str, str] | None = None,
    segments: list[dict[str, Any]] | None = None,
    prior_adjudicator: IdentityAdjudicator | None = None,
    arithmetic_path: str = "fixed",
    revision_id: str = "r0",
    pcm_key: str | None = None,
    feature_objects_override: list[Any] | None = None,
) -> dict[str, Any]:
    if arithmetic_path not in {"fixed", "float"}:
        raise ValueError("arithmetic_path must be fixed or float")
    v1_doc = v1_doc or build_default_v1_rules(config)
    if v2_doc is None:
        v2_doc, _migration = migrate_v1_to_v2(v1_doc)

    pcm_key = pcm_key or content_key(np.asarray(pcm, dtype=np.int16).tobytes())
    config_key = content_key(config.to_dict())
    if feature_objects_override is not None:
        feature_objects = feature_objects_override
    else:
        feature_objects = extract_all_features(pcm, config, arithmetic_path)  # type: ignore[arg-type]
    features = [item.to_dict() for item in feature_objects]
    features_key = content_key(features)

    detection = detect_turns(feature_objects, config)
    detection_key = content_key(detection)

    if segments is None:
        segments = build_speech_segments(detection, pcm, feature_objects, config)
    segments_key = content_key(segments)

    adjudicator = prior_adjudicator or IdentityAdjudicator(config)
    truth_overrides = truth_overrides or {}
    for segment in segments:
        segment_id = str(segment["segment_id"])
        if segment_id in truth_overrides:
            adjudicator.correct_truth(
                segment_id, truth_overrides[segment_id], segment["speaker_feature"]
            )
        else:
            adjudicator.assign(segment)

    assignments = {
        str(segment["segment_id"]): adjudicator.assignments[str(segment["segment_id"])]
        for segment in segments
        if str(segment["segment_id"]) in adjudicator.assignments
    }
    assignment_keys = {
        segment_id: assignment_content_key(assignment)
        for segment_id, assignment in assignments.items()
    }
    event_provenance: dict[str, dict[str, Any]] = {}
    for turn in detection["turn_points"]:
        frame_index = int(turn["frame_index"])
        feature_object = feature_objects[frame_index]
        frame_bytes = np.asarray(
            pcm[feature_object.start_sample : feature_object.end_sample], dtype=np.int16
        ).tobytes()
        event_provenance[str(turn["event_id"])] = {
            "frame_index": frame_index,
            "frame_content_key": content_key(frame_bytes),
            "feature_key": content_key(feature_object.to_dict()),
        }
    identity_key = content_key(
        {"speakers": adjudicator.speakers, "assignments": assignments}
    )

    classified_turns = classify_turns(detection["turn_points"], v2_doc, config)
    classification_key = content_key(classified_turns)
    rule_view_check = compare_rule_views(v1_doc, v2_doc, detection["turn_points"], config)

    context = {
        "pcm_key": pcm_key,
        "config_key": config_key,
        "features_key": features_key,
        "detection_key": detection_key,
        "classification_key": classification_key,
        "identity_key": identity_key,
        "assignment_keys_by_segment": assignment_keys,
        "event_provenance_by_event": event_provenance,
        "rules_v1_key": content_key(v1_doc),
        "rules_v2_key": content_key(v2_doc),
        "rule_version": v2_doc.get("rule_version"),
        "revision_id": revision_id,
    }
    ledger_rows = build_ledger_rows(
        classified_turns, segments, assignments, context
    )
    business_by_start = {
        int(turn["at_ms"]): str(turn["business_code"]) for turn in classified_turns
    }
    identity_frames: list[dict[str, Any]] = []
    for segment in segments:
        segment_id = str(segment["segment_id"])
        assignment = assignments.get(segment_id)
        speaker_id = assignment["speaker_id"] if assignment else None
        business_code = business_by_start.get(int(segment["start_ms"]))
        for feature_object in feature_objects:
            if int(segment["start_ms"]) <= feature_object.at_ms < int(segment["end_ms"]):
                identity_frames.append(
                    {
                        "frame_index": feature_object.frame_index,
                        "at_ms": feature_object.at_ms,
                        "segment_id": segment_id,
                        "speaker_id": speaker_id,
                        "business_code": business_code,
                    }
                )

    return {
        "schema": "meeting-audit/result/v1",
        "revision_id": revision_id,
        "arithmetic_path": arithmetic_path,
        "config": config.to_dict(),
        "keys": context,
        "pcm_key": pcm_key,
        "frame_count": len(features),
        "duration_ms": len(pcm) // config.samples_per_ms,
        "features": features,
        "detection": detection,
        "turn_points": detection["turn_points"],
        "classified_turns": classified_turns,
        "segments": segments,
        "assignments": assignments,
        "identity_frames": identity_frames,
        "assignment_keys": assignment_keys,
        "identity_registry": adjudicator.to_dict(),
        "identity_key": identity_key,
        "v1_rules": v1_doc,
        "v2_rules": v2_doc,
        "rule_view_check": rule_view_check,
        "ledger_rows": ledger_rows,
        "feature_objects": feature_objects,
    }


class AuditPipeline:
    def __init__(self, workspace: str | Path, config: AuditConfig = DEFAULT_CONFIG):
        self.workspace = Path(workspace)
        self.config = config
        self.sample_dir = self.workspace / "sample"
        self.rules_v1_dir = self.workspace / "rules" / "v1"
        self.rules_v2_dir = self.workspace / "rules" / "v2"
        self.ledger_dir = self.workspace / "ledger"
        self.artifacts_dir = self.workspace / "artifacts"
        self.reports_dir = self.workspace / "reports"
        for directory in (
            self.sample_dir,
            self.rules_v1_dir,
            self.rules_v2_dir,
            self.ledger_dir,
            self.artifacts_dir,
            self.reports_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)
        self.snapshots = SnapshotStore(self.workspace / "state")
        self.cache = ContentAddressedCache(self.workspace / "state" / "cache")
        self.last_result_path = self.artifacts_dir / "last_result.json"
        self.inputs_path = self.artifacts_dir / "inputs.json"

    def ensure_sample(self) -> tuple[np.ndarray, dict[str, Any]]:
        pcm_path = self.sample_dir / "meeting.pcm"
        manifest_path = self.sample_dir / "sample_manifest.json"
        if not pcm_path.exists() or not manifest_path.exists():
            write_sample_bundle(self.sample_dir, self.config)
        manifest = load_manifest(manifest_path)
        pcm = np.frombuffer(pcm_path.read_bytes(), dtype="<i2").copy()
        return pcm, manifest

    def ensure_rules(self) -> tuple[dict[str, Any], dict[str, Any]]:
        v1_path = self.rules_v1_dir / "rules.json"
        if v1_path.exists():
            v1_doc = json.loads(v1_path.read_text(encoding="utf-8"))
        else:
            v1_doc = build_default_v1_rules(self.config)
            write_v1_rules(v1_doc, v1_path)
        v2_path = self.rules_v2_dir / "rules_v2.sqlite"
        if (self.rules_v2_dir / "index.json").exists():
            v2_doc = read_v2_sharded(self.rules_v2_dir, v2_path)
            # Full shard rules are needed for later promotion/output.
            full_path = self.rules_v2_dir / "rules_v2_full.json"
            if full_path.exists():
                v2_doc = json.loads(full_path.read_text(encoding="utf-8"))
        else:
            v2_doc, _report = migrate_v1_to_v2(v1_doc)
            write_v2_sharded(v2_doc, self.rules_v2_dir, v2_path)
            atomic_write_json(self.rules_v2_dir / "rules_v2_full.json", v2_doc)
        return v1_doc, v2_doc

    def _save_result(self, result: dict[str, Any]) -> None:
        serializable = {k: v for k, v in result.items() if k != "feature_objects"}
        atomic_write_json(self.last_result_path, serializable)

    def load_last_result(self) -> dict[str, Any]:
        return json.loads(self.last_result_path.read_text(encoding="utf-8"))

    def resume_plan(self) -> dict[str, Any]:
        latest = self.snapshots.latest_all()
        committed: list[str] = []
        versions: dict[str, int] = {}
        for stage in STAGES:
            snapshot = latest.get(stage)
            if snapshot and snapshot.get("committed") is True:
                committed.append(stage)
                versions[stage] = int(snapshot["version"])
            else:
                break
        next_stage = STAGES[len(committed)] if len(committed) < len(STAGES) else None
        return {
            "committed_stages": committed,
            "next_stage": next_stage,
            "versions": versions,
            "resume_from": committed[-1] if committed else None,
        }

    def _commit_pipeline_snapshots(
        self, result: dict[str, Any], ledger_info: dict[str, Any]
    ) -> dict[str, Any]:
        frame_count = int(result["frame_count"])
        turn_ids = [item["event_id"] for item in result["turn_points"]]
        segment_ids = [item["segment_id"] for item in result["segments"]]
        ledger_ids = [row["common"]["ledger_id"] for row in result["ledger_rows"]]
        snapshots: dict[str, Any] = {}
        specs = [
            (
                STAGES[0],
                [[0, frame_count]],
                {"features_key": result["keys"]["features_key"]},
                [f"frame:{i:06d}" for i in range(frame_count)],
            ),
            (
                STAGES[1],
                [[0, len(turn_ids)]],
                {"detection_key": result["keys"]["detection_key"]},
                turn_ids,
            ),
            (
                STAGES[2],
                [[0, len(turn_ids)]],
                {"classification_key": result["keys"]["classification_key"]},
                turn_ids,
            ),
            (
                STAGES[3],
                [[0, len(segment_ids)]],
                {"identity_key": result["identity_key"]},
                segment_ids,
            ),
            (
                STAGES[4],
                [[0, len(ledger_ids)]],
                ledger_info,
                ledger_ids,
            ),
        ]
        for stage, intervals, keys, ids in specs:
            snapshots[stage] = self.snapshots.commit_stage(stage, intervals, keys, completed_ids=ids)

        result["snapshots"] = snapshots
        audit_report = run_self_checks(result, self.config)
        diagnostic = build_diagnostic_report(result, audit_report)
        atomic_write_json(self.reports_dir / "self_check.json", audit_report)
        atomic_write_json(self.reports_dir / "diagnostic.json", diagnostic)
        snapshots[STAGES[5]] = self.snapshots.commit_stage(
            STAGES[5],
            [[0, 1]],
            {
                "self_check_ok": audit_report["ok"],
                "diagnostic_key": content_key(diagnostic),
            },
            payload=audit_report,
            completed_ids=["AUDIT-0001"],
        )
        atomic_write_json(self.reports_dir / "audit_report.json", audit_report)
        result["snapshots"] = snapshots
        result["self_check"] = audit_report
        result["diagnostic"] = diagnostic
        return snapshots

    def run_full(
        self,
        revision_id: str = "r0",
        pcm: np.ndarray | None = None,
        truth_overrides: dict[str, str] | None = None,
        v1_doc: dict[str, Any] | None = None,
        v2_doc: dict[str, Any] | None = None,
        segments: list[dict[str, Any]] | None = None,
        arithmetic_path: str = "fixed",
        feature_objects_override: list[Any] | None = None,
    ) -> dict[str, Any]:
        recovered = self.snapshots.recover()
        if pcm is None:
            pcm, manifest = self.ensure_sample()
            pcm_key = manifest["pcm_sha256"]
        else:
            pcm_key = content_key(np.asarray(pcm, dtype=np.int16).tobytes())
        if v1_doc is None or v2_doc is None:
            disk_v1, disk_v2 = self.ensure_rules()
            v1_doc = v1_doc or disk_v1
            v2_doc = v2_doc or disk_v2

        inputs = {
            "revision_id": revision_id,
            "truth_overrides": truth_overrides or {},
            "rule_version": v2_doc.get("rule_version"),
            "pcm_key": pcm_key,
        }
        atomic_write_json(self.inputs_path, inputs)

        result = process_audio(
            pcm=pcm,
            config=self.config,
            v1_doc=v1_doc,
            v2_doc=v2_doc,
            truth_overrides=truth_overrides,
            segments=segments,
            arithmetic_path=arithmetic_path,
            revision_id=revision_id,
            pcm_key=pcm_key,
            feature_objects_override=feature_objects_override,
        )
        result["recovery"] = recovered
        result["resume_plan"] = self.resume_plan()
        ledger_info = write_dual_ledger(self.ledger_dir, result["ledger_rows"])
        result["ledger"] = ledger_info
        result["cache_report"] = self.cache_stages(result)
        self._commit_pipeline_snapshots(result, ledger_info)
        self._save_result(result)
        return result

    def cache_stages(self, result: dict[str, Any]) -> dict[str, Any]:
        stages = [
            ("frame_features", result["features"], result["keys"]["features_key"]),
            ("turn_detection", result["detection"], result["keys"]["detection_key"]),
            (
                "anomaly_classification",
                result["classified_turns"],
                result["keys"]["classification_key"],
            ),
            (
                "identity_adjudication",
                result["identity_registry"],
                result["identity_key"],
            ),
        ]
        for _stage, value, key in stages:
            cached = self.cache.get(key)
            if cached is None:
                self.cache.put(value, key)
        return self.cache.stats()

    def verify_fresh_full_bytes(
        self,
        pcm: np.ndarray,
        inputs: dict[str, Any],
        expected_ledger_dir: Path,
    ) -> dict[str, Any]:
        fresh = process_audio(
            pcm=pcm,
            config=self.config,
            truth_overrides=inputs.get("truth_overrides") or {},
            v1_doc=inputs.get("v1_doc"),
            v2_doc=inputs.get("v2_doc"),
            revision_id=str(inputs.get("revision_id", "verify")),
        )
        matches = {}
        ok = True
        for name in ("ledger_v1.jsonl", "ledger_v2.jsonl", "ledger_common.jsonl"):
            expected = (expected_ledger_dir / name).read_bytes()
            if name == "ledger_v1.jsonl":
                actual = b"".join(
                    canonical_bytes(row["v1"]) + b"\n" for row in fresh["ledger_rows"]
                )
            elif name == "ledger_v2.jsonl":
                actual = b"".join(
                    canonical_bytes(row["v2"]) + b"\n" for row in fresh["ledger_rows"]
                )
            else:
                actual = b"".join(
                    canonical_bytes(row["common"]) + b"\n" for row in fresh["ledger_rows"]
                )
            same = actual == expected
            matches[name] = same
            ok = ok and same
        return {"ok": ok, "byte_matches": matches}
