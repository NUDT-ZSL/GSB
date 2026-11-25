from __future__ import annotations

import threading

import numpy as np
import pytest

from audio_qc.codes import BusinessCode
from audio_qc.detect import detect_drift
from audio_qc.dsp import estimate_envelope_oneshot
from audio_qc.errors import QCError
from audio_qc.hashing import sha256_bytes
from audio_qc.pipeline import (
    CrashSimulation,
    PipelineRequest,
    build_chunk_key,
    run_pipeline,
)
from audio_qc.resources import ResourceBudget, ResourceGovernor
from audio_qc.samples import generate_bundle
from audio_qc.storage import ChunkCache, StateStore, verify_checksum
from audio_qc.thresholds import read_thresholds


@pytest.fixture(scope="module")
def bundle():
    return generate_bundle()


@pytest.fixture(scope="module")
def doc(bundle):
    return read_thresholds(bundle["v1"])


def _request(bundle, doc, state_dir, **kwargs):
    return PipelineRequest(
        pcm=bundle["pcm"],
        pcm_hash=bundle["metadata"]["pcm_sha256"],
        threshold_doc=doc,
        state_dir=state_dir,
        **kwargs,
    )


def _event_core(report):
    return [
        (event["frame_index"], event["business_code"], event["precomputed_hash"])
        for event in report["events"]
    ]


def test_cache_key_contains_required_identity_fields(tmp_path, doc):
    key, payload = build_chunk_key(
        stage="drift",
        pcm_hash="pcm-hash",
        threshold_doc=doc,
        frame_length=320,
        frame_hop=160,
        chunk_index=7,
        start_sample=1120,
        end_sample=1440,
        upstream_envelope_hash="envelope-hash",
    )
    assert len(key) == 64
    assert payload["pcm_content_hash"] == "pcm-hash"
    assert payload["threshold_version"] == doc["revision"]
    assert payload["frame_length"] == 320
    assert payload["frame_hop"] == 160
    assert payload["upstream_envelope_hash"] == "envelope-hash"

    cache = ChunkCache(tmp_path / "cache", max_entries=2)
    assert cache.get(key) is None
    cache.put(key, {"ok": True})
    assert cache.get(key) == {"ok": True}


def test_four_stage_snapshots_commit_and_resume(bundle, doc, tmp_path):
    state_dir = tmp_path / "state"
    first = run_pipeline(_request(bundle, doc, state_dir, workers=1))
    assert first["code"] == BusinessCode.DRIFT_DETECTED

    store = StateStore(state_dir)
    current = store.load_current()
    assert set(current["snapshots"]) == {"envelope", "alignment", "drift", "report"}
    for reference in current["snapshots"].values():
        snapshot = store.load_snapshot(reference)
        assert snapshot["checksum"] == reference["checksum"]
        assert verify_checksum(snapshot)
        assert snapshot["version"] >= 1

    second = run_pipeline(_request(bundle, doc, state_dir, workers=1))
    assert second["recovered"] is True
    assert second["trace"]["envelope_loaded_snapshot"] is True
    assert second["trace"]["alignment_loaded_snapshot"] is True
    assert second["trace"]["drift_loaded_snapshot"] is True
    assert second["trace"]["report_loaded_snapshot"] is True
    assert second["report"]["checksum"] == first["report"]["checksum"]


def test_in_process_crash_recovery_matches_oneshot(bundle, doc, tmp_path):
    state_dir = tmp_path / "crash-state"

    def crash_after_two():
        raise CrashSimulation("simulated hard stop after durable chunk 2")

    with pytest.raises(CrashSimulation):
        run_pipeline(
            _request(
                bundle,
                doc,
                state_dir,
                workers=1,
                crash_after_chunks=2,
                crash_function=crash_after_two,
            )
        )

    assert not (state_dir / "current.json").exists()
    entries = list((state_dir / "cache" / "entries").glob("*.json"))
    assert len(entries) == 2

    recovered = run_pipeline(_request(bundle, doc, state_dir, workers=1))
    trace = recovered["trace"]
    assert trace["envelope_reused_cache_chunks"] == [0, 1]
    assert len(trace["envelope_recomputed_chunks"]) == 1197

    oneshot_blocks = estimate_envelope_oneshot(bundle["pcm"], 320, 160)
    oneshot = detect_drift(oneshot_blocks, doc)
    assert _event_core(recovered["report"]) == _event_core(oneshot)
    assert recovered["report"]["envelope_hash"]
    assert recovered["report"]["drift_hash"] == __import__("audio_qc.hashing", fromlist=["canonical_hash"]).canonical_hash(
        oneshot["events"]
    )


def _small_threshold_doc(revision: str = "small-v1", target: float = -120.0):
    return read_thresholds(
        {
            "version": 1,
            "curve_id": "small",
            "revision": revision,
            "sample_rate": 16_000,
            "frame_length": 320,
            "frame_hop": 160,
            "table": [
                {
                    "segment_id": "all",
                    "start_sample": 0,
                    "end_sample": 16_000,
                    "energy_gate_db": -45.0,
                    "target_rms_db": target,
                    "energy_tolerance_db": 2.0,
                    "distribution_tolerance": 0.2,
                    "d1_expected": 0.0,
                    "d2_expected": 0.0,
                    "d3_expected": 0.0,
                    "a3_expected": 0.0,
                    "d1_tolerance": 0.01,
                    "d2_tolerance": 0.01,
                    "d3_tolerance": 0.01,
                    "a3_tolerance": 0.01,
                    "d1_limit": 0.01,
                    "d2_limit": 0.01,
                    "d3_limit": 0.01,
                    "a3_limit": 0.01,
                }
            ],
        }
    )


def test_threshold_change_whole_replaces_and_lists_all_chunks(tmp_path):
    pcm = np.zeros(16_000, dtype=np.float64)
    pcm_hash = sha256_bytes(np.zeros(16_000, dtype="<i2").tobytes())
    state_dir = tmp_path / "replace-state"
    first_doc = _small_threshold_doc("r1")
    first = run_pipeline(
        PipelineRequest(pcm=pcm, pcm_hash=pcm_hash, threshold_doc=first_doc, state_dir=state_dir)
    )
    assert first["code"] == BusinessCode.OK

    changed_doc = _small_threshold_doc("r2-threshold-change", target=-100.0)
    second = run_pipeline(
        PipelineRequest(pcm=pcm, pcm_hash=pcm_hash, threshold_doc=changed_doc, state_dir=state_dir)
    )
    plan = second["report"]["replacement_plan"]
    assert plan["mode"] == "whole_replace"
    assert "threshold_hash" in plan["changed_fields"]
    assert plan["recompute_chunk_count"] == 99
    assert len(plan["affected_chunks"]) == 99
    assert plan["recompute_sample_range"] == {
        "start_sample": 0,
        "end_sample": 16_000,
        "first_chunk_index": 0,
        "last_chunk_index": 98,
    }
    assert len(second["trace"]["envelope_recomputed_chunks"]) == 99

    snapshot_files = sorted((state_dir / "snapshots").glob("*.json"))
    assert len(snapshot_files) == 8
    first_snapshot = StateStore(state_dir).load_snapshot(
        {"path": "snapshots/000001-envelope-v001.json"}
    )
    assert first_snapshot["input_identity"]["threshold_version"] == "r1"
    assert (state_dir / "snapshots" / "000005-envelope-v001.json").exists()


def test_restart_uses_latest_committed_stage_snapshot(tmp_path):
    pcm = np.zeros(16_000, dtype=np.float64)
    pcm_hash = sha256_bytes(np.zeros(16_000, dtype="<i2").tobytes())
    state_dir = tmp_path / "stage-crash-state"

    def stop_after_alignment():
        raise CrashSimulation("stop after alignment commit")

    with pytest.raises(CrashSimulation):
        run_pipeline(
            PipelineRequest(
                pcm=pcm,
                pcm_hash=pcm_hash,
                threshold_doc=_small_threshold_doc("stage-crash"),
                state_dir=state_dir,
                crash_after_stage="alignment",
                crash_function=stop_after_alignment,
            )
        )

    current = StateStore(state_dir).load_current()
    assert set(current["snapshots"]) == {"envelope", "alignment"}

    recovered = run_pipeline(
        PipelineRequest(
            pcm=pcm,
            pcm_hash=pcm_hash,
            threshold_doc=_small_threshold_doc("stage-crash"),
            state_dir=state_dir,
        )
    )
    trace = recovered["trace"]
    assert trace["envelope_loaded_snapshot"] is True
    assert trace["alignment_loaded_snapshot"] is True
    assert trace["drift_loaded_snapshot"] is False
    assert recovered["code"] == BusinessCode.OK


def test_resource_budget_priority_and_serial_fallback(tmp_path):
    governor = ResourceGovernor(
        ResourceBudget(max_resident_blocks=10, max_parallel_chunks=2, requested_workers=1)
    )
    with pytest.raises(QCError) as memory_exc:
        governor.check_resident(11)
    assert memory_exc.value.code == BusinessCode.E_MEMORY_BUDGET

    with pytest.raises(QCError) as resource_exc:
        governor.check_parallel(4)
    assert resource_exc.value.code == BusinessCode.E_RESOURCE_LIMIT

    priority_plan = governor.plan(100, requested_workers=8)
    assert priority_plan.code == BusinessCode.E_MEMORY_BUDGET
    assert priority_plan.mode == "serial_streaming"

    resource_governor = ResourceGovernor(ResourceBudget(max_resident_blocks=10_000, max_parallel_chunks=2))
    resource_plan = resource_governor.plan(20, requested_workers=4)
    assert resource_plan.code == BusinessCode.E_RESOURCE_LIMIT
    assert resource_plan.mode == "serial"

    pcm = np.zeros(16_000, dtype=np.float64)
    pcm_hash = sha256_bytes(np.zeros(16_000, dtype="<i2").tobytes())
    result = run_pipeline(
        PipelineRequest(
            pcm=pcm,
            pcm_hash=pcm_hash,
            threshold_doc=_small_threshold_doc("budget"),
            state_dir=tmp_path / "budget-state",
            workers=4,
            budget=ResourceBudget(
                max_resident_blocks=1,
                max_cache_entries=3,
                max_parallel_chunks=8,
                requested_workers=4,
            ),
        )
    )
    assert result["report"]["budget_plan"]["business_code"] == BusinessCode.E_MEMORY_BUDGET
    assert result["report"]["budget_plan"]["mode"] == "serial_streaming"
    assert result["cache_stats"]["entry_count"] <= 3


def test_commit_lock_returns_stage_busy_nonblocking(tmp_path):
    store = StateStore(tmp_path / "lock-state")
    errors: list[str] = []

    def competing_commit():
        try:
            store.commit_snapshot("drift", {"generation": "g1", "drift_hash": "x"})
        except QCError as error:
            errors.append(error.code)

    with store.coordinator.lease("external-owner"):
        thread = threading.Thread(target=competing_commit)
        thread.start()
        thread.join(timeout=2)

    assert errors == [BusinessCode.E_STAGE_BUSY]
    snapshot = store.commit_snapshot("drift", {"generation": "g1", "drift_hash": "x"})
    assert snapshot["stage"] == "drift"
    assert snapshot["checksum"]


def test_actual_acceptance_subprocess_passes(tmp_path):
    from audio_qc.acceptance import run_acceptance

    result = run_acceptance(tmp_path / "acceptance")
    assert result["code"] == BusinessCode.VERIFY_OK
    assert result["ok"] is True
    assert result["equivalence"]["recovered_vs_oneshot_events_equal"] is True
