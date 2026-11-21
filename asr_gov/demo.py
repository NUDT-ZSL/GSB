"""端到端演示：合成样本、跨版本迁移、漂移检测、自适应取舍、
失效传播、追溯重放、快照并发控制与资源上限。结果输出为 JSON 报告。"""
from __future__ import annotations

import copy
import dataclasses
import json
import threading

from .adaptive import ScaleObservation, decide
from .dag import Pipeline
from .drift import adjudicate, roundtrip_drift_check
from .errors import (AsrGovError, SemanticDrift, SnapshotRollback,
                     SnapshotCorrupt, StageBusy, ResourceLimit,
                     AudioRange, EmptySlice, NoActivity)
from .formats import load_slices, dump_slices
from .formats.common import CanonicalSlice
from .framing import frame_pcm, detect_boundaries
from .migrate import migrate
from .pcm import synthesize_clip
from .sample_data import build_ground_truth, build_vocabs, default_config
from .snapshots import SnapshotStore
from .validate import validate_slices

V1_TEXT = """#format: asr-slices-v1
u1|0|1200|hello world
u2|1600|2500|good morning friends
u3|3000|4500|see you tomorrow
"""


def _err(fn):
    try:
        fn()
        return {"code": None, "raised": False}
    except AsrGovError as exc:
        return {"code": exc.code, "raised": True, **exc.to_dict()}


def run_demo() -> dict:
    report = {"steps": {}, "errors": []}
    steps = report["steps"]

    clip_a = synthesize_clip()
    clip_b = synthesize_clip()
    cfg = default_config()
    steps["pcm_synthesis"] = {
        "clip_id": clip_a.clip_id, "seed": clip_a.seed,
        "n_samples": clip_a.n_samples, "duration_ms": clip_a.duration_ms,
        "sha256": clip_a.digest(),
        "deterministic": clip_a.digest() == clip_b.digest(),
    }

    f1 = frame_pcm(clip_a.samples, clip_a.sample_rate, cfg.frame_length_ms, cfg.frame_shift_ms)
    f2 = frame_pcm(clip_a.samples, clip_a.sample_rate, cfg.frame_length_ms, cfg.frame_shift_ms)
    candidates = detect_boundaries(f1, cfg.energy_threshold_high_mult,
                                   cfg.energy_threshold_low_mult,
                                   cfg.min_slice_ms, cfg.max_slices)
    steps["framing"] = {
        "n_frames": f1.n_frames,
        "features_sha256": f1.digest(),
        "deterministic": f1.digest() == f2.digest(),
        "candidates": [c.to_dict() for c in candidates],
    }

    gt = build_ground_truth()
    v1_set = migrate(gt, 1).slice_set
    v2_set = migrate(gt, 2).slice_set
    set1 = load_slices(dump_slices(v1_set, 1), clip_a.clip_id)
    set2 = load_slices(dump_slices(v2_set, 2), clip_a.clip_id)
    set3 = load_slices(dump_slices(gt, 3), clip_a.clip_id)
    adj = adjudicate({1: set1, 2: set2, 3: set3})
    steps["adjudication"] = adj.to_dict()
    steps["format_views"] = {
        "v1": dump_slices(v1_set, 1),
        "v2": dump_slices(v2_set, 2),
        "v3": dump_slices(gt, 3),
    }

    m_up = migrate(set1, 3)
    m_down = migrate(gt, 1)
    steps["migration"] = {
        "v1_to_v3": {"completions": len(m_up.completions),
                     "sources": sorted({c["source"] for c in m_up.completions})},
        "v3_to_v1": {"discards": len(m_down.discards),
                     "rules": sorted({d["rule"] for d in m_down.discards})},
    }

    drift = _err(lambda: roundtrip_drift_check(gt, via_version=1))
    steps["semantic_drift"] = {
        "code": drift["code"],
        "affected_slices": drift.get("details", {}).get("affected_slices"),
        "unrecoverable_fields": sorted(
            {u["field"] for u in drift.get("details", {}).get("unrecoverable", [])}),
    }

    obs_small = ScaleObservation(num_slices=3, num_frames=f1.n_frames,
                                 num_speakers=2, memory_headroom_bytes=4 << 20)
    obs_large = ScaleObservation(num_slices=5000, num_frames=9_000_000,
                                 num_speakers=40, memory_headroom_bytes=1 << 10)
    d_small = decide(obs_small, cfg)
    d_large = decide(obs_large, cfg)
    steps["adaptive"] = {
        "small": d_small.to_dict(),
        "large": d_large.to_dict(),
        "deterministic": decide(obs_large, cfg).canonical() == d_large.canonical(),
    }

    vocab1, vocab2 = build_vocabs()
    pipe = Pipeline()
    r1 = pipe.run(clip_a, gt, cfg, vocab1, annotation_revision=1)

    gt_edit = copy.deepcopy(gt)
    gt_edit.get("u2").text = "good evening friends"
    gt_edit.get("u2").tokens = []
    gt_edit.get("u2").flat_tokens = None
    r2 = pipe.run(clip_a, gt_edit, cfg, vocab1, annotation_revision=2)

    r3 = pipe.run(clip_a, gt_edit, cfg, vocab2, annotation_revision=2)

    cfg_shift = dataclasses.replace(cfg, version=2, frame_shift_ms=20)
    r4 = pipe.run(clip_a, gt_edit, cfg_shift, vocab2, annotation_revision=2)

    stale = _err(lambda: pipe.verify_lineage(
        r3.slices[0].lineage, r4.slices[0].lineage.cache_keys))
    replayed = pipe.replay(r4.slices[0].lineage)
    steps["invalidation"] = {
        "edit_slice_u2": {"affected_slices": r2.affected_slices,
                          "reused_framing": any(k.startswith("framing:") for k in r2.cache_hits)},
        "vocab_bump": {"affected_slices": r3.affected_slices},
        "frame_shift_change": {"affected_slices": r4.affected_slices},
        "timeline_stable": r1.timeline() == r2.timeline() == r3.timeline(),
        "lineage_stale_code": stale["code"],
        "stale_dependencies": [d["node"] for d in stale.get("details", {})
                               .get("stale_dependencies", [])],
        "replay_nodes": sorted(replayed),
        "replay_timeline": r4.timeline(),
        "lineage": [s.to_dict() for s in r4.slices],
    }

    store = SnapshotStore()
    payload_v1 = json.dumps(r1.snapshot, sort_keys=True).encode()
    snap1 = store.commit(store.stage("slice_set", 1, payload_v1))
    rollback = _err(lambda: store.commit(store.stage("slice_set", 1, b"older")))
    tampered = store.stage("slice_set", 2, b"corrupted-payload")
    tampered.checksum = "0" * 64
    corrupt = _err(lambda: store.commit(tampered))

    busy_result = {}

    def _slow_commit():
        store._commit_lock.acquire()
        store._commit_lock.release()

    store._commit_lock.acquire()
    try:
        busy_result = _err(lambda: store.commit(store.stage("slice_set", 3, b"blocked")))
    finally:
        store._commit_lock.release()
    _slow_commit()
    snap2 = store.commit(store.stage("slice_set", 2, json.dumps(
        r4.snapshot, sort_keys=True).encode()))
    steps["snapshots"] = {
        "committed_versions": store.versions(),
        "whole_replace": snap2.version == 2 and snap1.version == 1,
        "rollback_code": rollback["code"],
        "corrupt_code": corrupt["code"],
        "busy_code": busy_result["code"],
    }

    bad_range = copy.deepcopy(gt)
    bad_range.get("u3").end_us = 999_999_000
    e_range = _err(lambda: validate_slices(bad_range.slices, f1, clip_a.n_samples,
                                           cfg.energy_threshold_low_mult, cfg,
                                           clip_id=clip_a.clip_id))
    bad_empty = copy.deepcopy(gt)
    bad_empty.get("u1").end_us = bad_empty.get("u1").start_us
    e_empty = _err(lambda: validate_slices(bad_empty.slices, f1, clip_a.n_samples,
                                           cfg.energy_threshold_low_mult, cfg,
                                           clip_id=clip_a.clip_id))
    bad_silent = copy.deepcopy(gt)
    bad_silent.get("u1").start_us = 1_250_000
    bad_silent.get("u1").end_us = 1_550_000
    e_noact = _err(lambda: validate_slices(bad_silent.slices, f1, clip_a.n_samples,
                                           cfg.energy_threshold_low_mult, cfg,
                                           clip_id=clip_a.clip_id))
    tight = dataclasses.replace(cfg, max_slices_per_clip=2)
    e_limit = _err(lambda: validate_slices(gt.slices, f1, clip_a.n_samples,
                                           cfg.energy_threshold_low_mult, tight,
                                           clip_id=clip_a.clip_id))
    tiny_mem = dataclasses.replace(cfg, max_resident_feature_bytes=1024,
                                   max_recompute_chunks=2)
    degraded = validate_slices(gt.slices, f1, clip_a.n_samples,
                               cfg.energy_threshold_low_mult, tiny_mem,
                               requested_chunks=6, clip_id=clip_a.clip_id)
    steps["validation"] = {
        "audio_range_code": e_range["code"],
        "empty_slice_code": e_empty["code"],
        "no_activity_code": e_noact["code"],
        "resource_limit_code": e_limit["code"],
        "degradations": degraded.degradations,
        "degraded_verdicts": degraded.verdicts,
    }

    format_errors = {
        "unsupported_version": _err(lambda: load_slices("#format: asr-slices-v9\n", "c")),
        "corrupt_fields": _err(lambda: load_slices("u1|0|100\n", "c")),
        "time_not_int": _err(lambda: load_slices("u1|x|100|hi\n", "c")),
        "start_ge_end": _err(lambda: load_slices("u1|100|100|hi\n", "c")),
        "dup_utterance": _err(lambda: load_slices("u1|0|100|a\nu1|100|200|b\n", "c")),
    }
    steps["format_errors"] = {k: v["code"] for k, v in format_errors.items()}

    report["errors"] = sorted({
        steps["semantic_drift"]["code"],
        steps["invalidation"]["lineage_stale_code"],
        steps["snapshots"]["rollback_code"],
        steps["snapshots"]["corrupt_code"],
        steps["snapshots"]["busy_code"],
        steps["validation"]["audio_range_code"],
        steps["validation"]["empty_slice_code"],
        steps["validation"]["no_activity_code"],
        steps["validation"]["resource_limit_code"],
        *steps["format_errors"].values(),
    } - {None})
    report["ok"] = all([
        steps["pcm_synthesis"]["deterministic"],
        steps["framing"]["deterministic"],
        steps["adjudication"]["equal"],
        steps["semantic_drift"]["code"] == "E_SEMANTIC_DRIFT",
        steps["adaptive"]["deterministic"],
        steps["adaptive"]["small"]["path"] == "exact",
        steps["adaptive"]["large"]["path"] == "approximate",
        steps["invalidation"]["edit_slice_u2"]["affected_slices"] == ["u2"],
        steps["invalidation"]["vocab_bump"]["affected_slices"] == ["u1"],
        steps["invalidation"]["frame_shift_change"]["affected_slices"] == ["u1", "u2", "u3"],
        steps["invalidation"]["timeline_stable"],
        steps["invalidation"]["lineage_stale_code"] == "E_LINEAGE_STALE",
        steps["snapshots"]["rollback_code"] == "E_SNAPSHOT_ROLLBACK",
        steps["snapshots"]["corrupt_code"] == "E_SNAPSHOT_CORRUPT",
        steps["snapshots"]["busy_code"] == "E_STAGE_BUSY",
        steps["validation"]["audio_range_code"] == "E_AUDIO_RANGE",
        steps["validation"]["empty_slice_code"] == "E_EMPTY_SLICE",
        steps["validation"]["no_activity_code"] == "E_NO_ACTIVITY",
        steps["validation"]["resource_limit_code"] == "E_RESOURCE_LIMIT",
        steps["format_errors"]["unsupported_version"] == "E_FORMAT_VERSION_UNSUPPORTED",
        steps["format_errors"]["corrupt_fields"] == "E_FORMAT_CORRUPT",
        steps["format_errors"]["time_not_int"] == "E_TIME_INVALID",
        steps["format_errors"]["start_ge_end"] == "E_TIME_INVALID",
        steps["format_errors"]["dup_utterance"] == "E_DUP_UTTERANCE",
    ])
    return report


def main():
    report = run_demo()
    out = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
    print(out)
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
