"""End-to-end deterministic diagnostic pipeline and recovery."""

from __future__ import annotations

import os
import signal
from io import BytesIO
from pathlib import Path
from typing import Any, Dict, List, Sequence

import numpy as np

from .anomalies import detect_turn_anomalies
from .cache import ContentCache
from .canonical import atomic_write_bytes, atomic_write_json, read_json, sha256_hex
from .config import EngineConfig
from .errors import DiagnosticError
from .features import extract_features, feature_matrix, load_pcm
from .ledger import read_ledger
from .migrate import (
    compare_semantics,
    downgrade_v2_to_v1,
    upgrade_v1_to_v2,
)
from .report import render_markdown_report
from .samplegen import write_jsonl
from .scheduler import run_state_machine
from .snapshots import ForcedTermination, SnapshotStore, STAGES


def read_jsonl(path: str | Path) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    with Path(path).open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                rows.append(__import__("json").loads(line))
    return rows


def load_sample_bundle(
    sample_dir: str | Path, config_path: str | Path | None = None
) -> Dict[str, Any]:
    sample_dir = Path(sample_dir)
    config = EngineConfig.load(config_path or sample_dir / "config.json")
    manifest = read_json(sample_dir / "manifest.json")
    pcm_path = sample_dir / manifest.get("pcm_path", config["pcm_filename"])
    truth_path = sample_dir / manifest.get("files", {}).get(
        "truth", config["truth_filename"]
    )
    pcm = load_pcm(pcm_path)
    truth = read_jsonl(truth_path)
    return {
        "sample_dir": sample_dir,
        "config": config,
        "manifest": manifest,
        "pcm_path": pcm_path,
        "pcm": pcm,
        "truth_path": truth_path,
        "truth": truth,
        "v1_path": sample_dir / manifest["files"]["v1"],
        "v2_path": sample_dir / manifest["files"]["v2_dir"],
    }


def _maybe_crash(
    store: SnapshotStore,
    crash: Dict[str, Any] | None,
    stage: str,
    processed_frames: int,
) -> None:
    if not crash or crash.get("stage") != stage:
        return
    after_frame = crash.get("after_frame", processed_frames)
    store.begin_work(
        stage,
        {
            "processed_frames": int(after_frame),
            "total_frames": int(processed_frames),
            "simulated": True,
        },
    )
    if crash.get("hard"):
        os.kill(os.getpid(), signal.SIGKILL)
        os._exit(137)
    raise ForcedTermination(stage, int(after_frame))


def _relative_discarded(paths: Sequence[str], output_dir: Path) -> List[str]:
    result = []
    for value in paths:
        path = Path(value)
        try:
            result.append(str(path.relative_to(output_dir.resolve())))
        except ValueError:
            result.append(str(path))
    return sorted(result)


def run_pipeline(
    sample_dir: str | Path,
    output_dir: str | Path,
    *,
    config_path: str | Path | None = None,
    workers: int | None = None,
    use_process_pool: bool | None = None,
    crash: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    bundle = load_sample_bundle(sample_dir, config_path)
    config: EngineConfig = bundle["config"]
    if workers is not None:
        config.values["workers"] = int(workers)
        config.validate()

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    store = SnapshotStore(output_dir / "state")
    discarded_absolute = store.discard_uncommitted()
    discarded = _relative_discarded(discarded_absolute, output_dir)
    cache = ContentCache(output_dir / "cache")

    pcm = bundle["pcm"]
    truth = bundle["truth"]
    frame_count = len(truth)
    pcm_hash = sha256_hex((bundle["pcm_path"]).read_bytes())
    truth_hash = sha256_hex(truth)
    frame_params_hash = sha256_hex(
        {
            "sample_rate": int(config["sample_rate"]),
            "frame_ms": int(config["frame_ms"]),
            "hop_ms": int(config["hop_ms"]),
            "frame_samples": config.frame_samples,
            "hop_samples": config.hop_samples,
        }
    )

    cache_index: Dict[str, Any] = {
        "pcm_sha256": pcm_hash,
        "frame_params_sha256": frame_params_hash,
        "annotation_generation": int(config["default_generation"]),
        "key_inputs": [
            "category",
            "pcm_sha256",
            "frame_params_sha256",
            "annotation_generation",
            "upstream_hashes",
            "frame_index",
        ],
        "features": {},
        "states": {},
        "ledger_entries": {},
        "anomaly_events": [],
    }
    stage_reuse: Dict[str, Dict[str, Any]] = {}

    feature_path = output_dir / "frame_features.jsonl"
    feature_snapshot = store.load_latest(STAGES[0])
    if (
        feature_snapshot
        and feature_path.exists()
        and feature_snapshot.get("metadata", {}).get("pcm_sha256") == pcm_hash
    ):
        features = read_jsonl(feature_path)
        stage_reuse[STAGES[0]] = {
            "recomputed_ranges": [],
            "reused_ranges": [[0, frame_count]],
            "committed": True,
        }
    else:
        features = extract_features(
            pcm,
            config,
            pcm_hash=pcm_hash,
            expected_frame_count=frame_count,
        )
        _maybe_crash(store, crash, STAGES[0], len(features))
        write_jsonl(feature_path, features)
        feature_snapshot = store.commit(
            STAGES[0],
            features,
            [[0, frame_count]],
            frame_count=frame_count,
            artifact_checksums={"frame_features.jsonl": sha256_hex(feature_path.read_bytes())},
            metadata={"pcm_sha256": pcm_hash, "truth_sha256": truth_hash},
        )
        stage_reuse[STAGES[0]] = {
            "recomputed_ranges": [[0, frame_count]],
            "reused_ranges": [],
            "committed": True,
        }

    feature_hashes: Dict[int, str] = {}
    matrix_buffer = BytesIO()
    np.save(matrix_buffer, feature_matrix(features), allow_pickle=False)
    matrix_path = output_dir / "frame_feature_matrix.npy"
    atomic_write_bytes(matrix_path, matrix_buffer.getvalue())
    for frame in features:
        index = int(frame["frame_index"])
        content_hash = cache.put(frame)
        feature_hashes[index] = content_hash
        key = ContentCache.make_cache_key(
            category="frame_feature",
            pcm_hash=pcm_hash,
            frame_params_hash=frame_params_hash,
            annotation_generation=config["default_generation"],
            frame_index=index,
        )
        cache_index["features"][str(index)] = {
            "cache_key": key,
            "content_hash": content_hash,
        }

    state_path = output_dir / "emotion_states.jsonl"
    anomaly_path = output_dir / "anomalies.json"
    state_snapshot = store.load_latest(STAGES[1])
    if (
        state_snapshot
        and state_path.exists()
        and anomaly_path.exists()
        and state_snapshot.get("metadata", {}).get("truth_hash") == truth_hash
    ):
        state_records = read_jsonl(state_path)
        anomaly_result = read_json(anomaly_path)
        scheduling_summary = state_snapshot["metadata"].get("scheduling", {})
        admission = scheduling_summary.get("admission")
        if isinstance(admission, dict):
            admission.setdefault("degradation_category", "none")
        stage_reuse[STAGES[1]] = {
            "recomputed_ranges": [],
            "reused_ranges": [[0, frame_count]],
            "committed": True,
        }
    else:
        scheduling = run_state_machine(
            features,
            truth,
            config,
            use_process_pool=use_process_pool,
        )
        state_records = scheduling["records"]
        _maybe_crash(store, crash, STAGES[1], len(state_records))
        anomaly_result = detect_turn_anomalies(truth, features, config)
        write_jsonl(state_path, state_records)
        atomic_write_json(anomaly_path, anomaly_result)
        scheduling_summary = {
            "used_process_pool": scheduling["used_process_pool"],
            "worker_count": scheduling["worker_count"],
            "final_state": scheduling["final_state"],
            "dispatch_order": scheduling["dispatch_order"],
            "merge_order": scheduling["merge_order"],
            "admission": scheduling["admission"],
            "batch_count": len(scheduling["batches"]),
        }
        state_snapshot = store.commit(
            STAGES[1],
            {"states": state_records, "anomalies": anomaly_result},
            [[0, frame_count]],
            frame_count=frame_count,
            artifact_checksums={
                "emotion_states.jsonl": sha256_hex(state_path.read_bytes()),
                "anomalies.json": sha256_hex(anomaly_path.read_bytes()),
            },
            metadata={
                "truth_hash": truth_hash,
                "feature_content_checksum": state_snapshot_content_hash(store, STAGES[0]),
                "scheduling": scheduling_summary,
            },
        )
        stage_reuse[STAGES[1]] = {
            "recomputed_ranges": [[0, frame_count]],
            "reused_ranges": [],
            "committed": True,
        }

    for record in state_records:
        index = int(record["frame_index"])
        content_hash = cache.put(record)
        upstream = [feature_hashes.get(index, "")]
        key = ContentCache.make_cache_key(
            category="emotion_state",
            pcm_hash=pcm_hash,
            frame_params_hash=frame_params_hash,
            annotation_generation=config["default_generation"],
            upstream_hashes=upstream,
            frame_index=index,
        )
        cache_index["states"][str(index)] = {
            "cache_key": key,
            "content_hash": content_hash,
            "upstream_hashes": upstream,
        }

    for event in anomaly_result.get("events", []):
        content_hash = cache.put(event)
        cache_index["anomaly_events"].append(
            {
                "event_id": event["event_id"],
                "content_hash": content_hash,
                "cache_key": ContentCache.make_cache_key(
                    category="anomaly_event",
                    pcm_hash=pcm_hash,
                    frame_params_hash=frame_params_hash,
                    annotation_generation=config["default_generation"],
                    upstream_hashes=[
                        cache_index["states"]
                        .get(str(event["start_frame"]), {})
                        .get("content_hash", "")
                    ],
                    extra={"event_id": event["event_id"], "type": event["type"]},
                ),
            }
        )

    unified_path = output_dir / "unified_ledger.jsonl"
    migration_summary_path = output_dir / "migration_summary.json"
    migration_snapshot = store.load_latest(STAGES[2])
    if (
        migration_snapshot
        and unified_path.exists()
        and migration_summary_path.exists()
    ):
        unified_records = read_jsonl(unified_path)
        migration_summary = read_json(migration_summary_path)
        stage_reuse[STAGES[2]] = {
            "recomputed_ranges": [],
            "reused_ranges": [[0, frame_count]],
            "committed": True,
        }
    else:
        v1_ledger = read_ledger(bundle["v1_path"])
        v2_ledger = read_ledger(bundle["v2_path"])
        upgrade_result = upgrade_v1_to_v2(
            v1_ledger,
            truth,
            config,
            output_dir=output_dir / "migration" / "v1_to_v2",
        )
        downgrade_result = downgrade_v2_to_v1(
            v2_ledger,
            config,
            output_path=output_dir / "migration" / "v2_to_v1.json",
        )
        from .migrate import roundtrip_check

        roundtrip = roundtrip_check(v2_ledger, truth, config)
        cross_equivalence = compare_semantics(
            v2_ledger["records"], upgrade_result["unified_records"]
        )
        unified_records = []
        for record in v2_ledger["records"]:
            slim = {
                key: value
                for key, value in record.items()
                if key not in {"raw", "source_shard"}
            }
            unified_records.append(slim)
        write_jsonl(unified_path, unified_records)

        migration_summary = {
            "v1_path": str(bundle["v1_path"].relative_to(bundle["sample_dir"])),
            "v2_path": str(bundle["v2_path"].relative_to(bundle["sample_dir"])),
            "cross_version_equivalent": cross_equivalence["equivalent"],
            "cross_version_detail": cross_equivalence,
            "completion_count": upgrade_result["completion_count"],
            "completion_sources_preview": upgrade_result["completion_sources"][:12],
            "discard_count": downgrade_result["discard_count"],
            "discard_records_preview": downgrade_result["discard_records"][:20],
            "discard_rule": downgrade_result["discard_rule"],
            "roundtrip_equivalent": roundtrip["roundtrip_equivalence"]["equivalent"],
            "against_original_equivalent": roundtrip["against_original"]["equivalent"],
            "metadata_restorable": roundtrip["metadata_restorable"],
        }
        atomic_write_json(migration_summary_path, migration_summary)

        migration_snapshot = store.commit(
            STAGES[2],
            {
                "migration": migration_summary,
                "unified_count": len(unified_records),
            },
            [[0, frame_count]],
            frame_count=frame_count,
            artifact_checksums={
                "unified_ledger.jsonl": sha256_hex(unified_path.read_bytes()),
                "migration_summary.json": sha256_hex(
                    migration_summary_path.read_bytes()
                ),
            },
            metadata={
                "v1_record_count": len(v1_ledger["records"]),
                "v2_record_count": len(v2_ledger["records"]),
            },
        )
        stage_reuse[STAGES[2]] = {
            "recomputed_ranges": [[0, frame_count]],
            "reused_ranges": [],
            "committed": True,
        }

    for record in unified_records:
        index = int(record.get("frame_index", record.get("frame_start", 0)))
        content_hash = cache.put(record)
        key = ContentCache.make_cache_key(
            category="ledger_entry",
            pcm_hash=pcm_hash,
            frame_params_hash=frame_params_hash,
            annotation_generation=int(record.get("generation", 1)),
            upstream_hashes=[str(record.get("checksum", ""))],
            frame_index=index,
        )
        cache_index["ledger_entries"][str(index)] = {
            "cache_key": key,
            "content_hash": content_hash,
            "generation": int(record.get("generation", 1)),
            "revision": int(record.get("revision", 0)),
        }

    cache_manifest_path = output_dir / "cache" / "cache_manifest.json"
    atomic_write_json(cache_manifest_path, cache_index)

    snapshots = store.list_latest()
    stage_summaries = [
        {
            "stage": stage,
            "version": snapshots[stage]["version"],
            "stage_version": snapshots[stage]["stage_version"],
            "completed_ranges": snapshots[stage]["completed_ranges"],
            "content_checksum": snapshots[stage]["content_checksum"],
        }
        for stage in STAGES[:3]
        if stage in snapshots
    ]

    report: Dict[str, Any] = {
        "ok": True,
        "dialogue_id": config["dialogue_id"],
        "seed": int(config["seed"]),
        "sample_rate": int(config["sample_rate"]),
        "frame_count": frame_count,
        "pcm_samples": int(pcm.size),
        "pcm_sha256": pcm_hash,
        "frame_params_hash": frame_params_hash,
        "stages": stage_summaries,
        "stage_reuse": stage_reuse,
        "recovery": {
            "discarded_uncommitted": discarded,
            "resumed_after_uncommitted_discard": bool(discarded),
        },
        "state_summary": {
            "final_state": scheduling_summary.get("final_state"),
            "changed_frame_count": sum(1 for r in state_records if r.get("changed")),
            "state_counts": count_states(state_records),
        },
        "scheduling": scheduling_summary,
        "anomalies": anomaly_result,
        "migration": migration_summary,
        "cache": {
            "root": "cache",
            "manifest": "cache/cache_manifest.json",
            "feature_entries": len(cache_index["features"]),
            "state_entries": len(cache_index["states"]),
            "ledger_entries": len(cache_index["ledger_entries"]),
            "anomaly_event_entries": len(cache_index["anomaly_events"]),
        },
    }

    previous_report_snapshot = store.load_latest(STAGES[3])
    report_version = store.current_version() + 1
    report_stage_version = (
        int(previous_report_snapshot["stage_version"]) + 1
        if previous_report_snapshot
        else 1
    )
    report_stage_summary = {
        "stage": STAGES[3],
        "version": report_version,
        "stage_version": report_stage_version,
        "completed_ranges": [[0, frame_count]],
        "content_checksum": None,
        "snapshot_checksum_field": "state/report_export.latest.json",
    }
    report["stages"].append(report_stage_summary)
    report_json_path = output_dir / "diagnostic_report.json"
    report_md_path = output_dir / "diagnostic_report.md"
    report_checksum = atomic_write_json(report_json_path, report)
    report_md_path.write_text(
        render_markdown_report(report), encoding="utf-8"
    )
    store.commit(
        STAGES[3],
        report,
        [[0, frame_count]],
        frame_count=frame_count,
        artifact_checksums={
            "diagnostic_report.json": report_checksum,
            "diagnostic_report.md": sha256_hex(report_md_path.read_bytes()),
        },
        metadata={"report_checksum": report_checksum},
    )
    atomic_write_json(output_dir / "result.json", {"ok": True, "report": "diagnostic_report.json"})
    return report


def state_snapshot_content_hash(store: SnapshotStore, stage: str) -> str | None:
    snapshot = store.load_latest(stage)
    return snapshot["content_checksum"] if snapshot else None


def count_states(records: Sequence[Dict[str, Any]]) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for record in records:
        state = str(record["state"])
        counts[state] = counts.get(state, 0) + 1
    return dict(sorted(counts.items()))
