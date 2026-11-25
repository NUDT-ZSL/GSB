"""End-to-end deterministic diagnostics and report generation."""

from __future__ import annotations

import json
import platform
import shutil
from dataclasses import asdict
from pathlib import Path
from typing import Any

import numpy as np

from .adjudicate import adjudicate
from .attribution import attribute_difference
from .audio import FrameConfig, align_interval, changed_vad_intervals, extract_features
from .cache import CacheKey, ContentAddressedCache
from .codes import Code
from .consistency import TimelineIndex, check_consistency
from .formats_v1 import read_v1
from .formats_v2 import read_v2_dataset
from .hashing import digest_json
from .migrate import (
    V1ToV2Policy,
    V2ToV1Policy,
    migrate_v1_to_v2,
    migrate_v2_to_v1,
)
from .models import Utterance, Word
from .refresh import AdjudicationRefreshService
from .samples import generate_sample_tree
from .snapshot import SnapshotStore, build_snapshot


class DiagnosticRecorder:
    def __init__(self) -> None:
        self.checks: list[dict[str, Any]] = []

    def check(self, name: str, condition: bool, code: str = Code.OK, **details: Any) -> bool:
        self.checks.append(
            {
                "name": name,
                "ok": bool(condition),
                "code": code,
                **details,
            }
        )
        return bool(condition)

    @property
    def ok(self) -> bool:
        return all(item["ok"] for item in self.checks)


def _invalid_v2_fixtures(directory: Path) -> dict[str, Path]:
    directory.mkdir(parents=True, exist_ok=True)
    payloads = {
        "missing_field.json": {
            "format": "transcript-v2-shard",
            "format_version": "2.0",
            "utterances": [{"utterance_id": "V2-MISSING"}],
        },
        "duplicate_id.json": {
            "format": "transcript-v2-shard",
            "format_version": "2.0",
            "utterances": [
                {"utterance_id": "V2-DUP", "start_ms": 100, "end_ms": 200, "text": "a"},
                {"utterance_id": "V2-DUP", "start_ms": 210, "end_ms": 300, "text": "b"},
            ],
        },
        "time_regression.json": {
            "format": "transcript-v2-shard",
            "format_version": "2.0",
            "utterances": [
                {"utterance_id": "V2-T2", "start_ms": 300, "end_ms": 400, "text": "later"},
                {"utterance_id": "V2-T1", "start_ms": 100, "end_ms": 200, "text": "earlier"},
            ],
        },
        "invalid_interval.json": {
            "format": "transcript-v2-shard",
            "format_version": "2.0",
            "utterances": [
                {"utterance_id": "V2-RANGE", "start_ms": 300, "end_ms": 200, "text": "bad"},
            ],
        },
    }
    paths: dict[str, Path] = {}
    for name, payload in payloads.items():
        path = directory / name
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        paths[name] = path
    return paths


def run_diagnostic(
    sample_dir: str | Path = "samples",
    report_dir: str | Path = "reports",
) -> dict[str, Any]:
    sample_dir = Path(sample_dir)
    report_dir = Path(report_dir)
    report_dir.mkdir(parents=True, exist_ok=True)
    rec = DiagnosticRecorder()

    paths = generate_sample_tree(sample_dir)
    pcm_bytes = paths.pcm.read_bytes()
    v1_load = read_v1(paths.v1)
    v2_load = read_v2_dataset(paths.v2_dir)

    rec.check("v1_sample_loads", v1_load.ok and len(v1_load.utterances) == 5, v1_load.code)
    rec.check("v2_sample_loads_with_sqlite", v2_load.ok and len(v2_load.utterances) == 5, v2_load.code)

    frame_config = FrameConfig()
    features = extract_features(pcm_bytes, frame_config)
    rec.check(
        "pcm_is_deterministic_16k_mono",
        features.pcm_sha256 == paths.pcm_sha256 and features.sample_count > 0,
        pcm_hash=features.pcm_sha256,
        sample_count=features.sample_count,
        duration_ms=features.duration_ms,
        frame_count=len(features.frames),
        speech_segments=list(features.speech_segments_ms),
        energy_quantiles=features.energy_quantiles,
    )

    alignments = [align_interval(features, u.start_ms, u.end_ms, u.utterance_id).to_dict() for u in v2_load.utterances]
    rec.check("frame_grid_alignment", all(item["first_frame"] is not None for item in alignments), alignments=alignments)
    v1_supplement = next(u for u in v1_load.utterances if u.utterance_id == "UTT-050")
    v2_supplement = next(u for u in v2_load.utterances if u.utterance_id == "UTT-050")
    attribution = attribute_difference(features, v1_supplement, v2_supplement).to_dict()
    rec.check(
        "difference_attribution_reuses_frame_grid",
        attribution["start_delta_ms"] == 0
        and attribution["end_delta_ms"] == 630
        and "boundary_shift" in attribution["reasons"],
        attribution=attribution,
    )

    invalid_results: dict[str, dict[str, Any]] = {}
    expected_v1 = {
        "missing_field.v1.txt": Code.E_FIELD_MISSING,
        "duplicate_id.v1.txt": Code.E_DUPLICATE_UTTERANCE_ID,
        "time_regression.v1.txt": Code.E_TIME_REGRESSION,
        "invalid_interval.v1.txt": Code.E_INVALID_INTERVAL,
        "bad_header.v1.txt": Code.E_FORMAT_HEADER,
    }
    for filename, expected_code in expected_v1.items():
        result = read_v1(paths.invalid_dir / filename)
        invalid_results[filename] = result.to_dict()
        rec.check(
            f"v1_{filename}_business_code",
            (not result.ok) and result.code == expected_code,
            result.code,
            expected_code=expected_code,
        )

    invalid_v2_paths = _invalid_v2_fixtures(report_dir / "invalid_v2")
    expected_v2 = {
        "missing_field.json": Code.E_FIELD_MISSING,
        "duplicate_id.json": Code.E_DUPLICATE_UTTERANCE_ID,
        "time_regression.json": Code.E_TIME_REGRESSION,
        "invalid_interval.json": Code.E_INVALID_INTERVAL,
    }
    for filename, expected_code in expected_v2.items():
        result = read_v2_dataset(invalid_v2_paths[filename])
        invalid_results[filename] = result.to_dict()
        rec.check(
            f"v2_{filename}_business_code",
            (not result.ok) and result.code == expected_code,
            result.code,
            expected_code=expected_code,
        )

    migrated_v2 = migrate_v1_to_v2(v1_load, V1ToV2Policy())
    migrated_v1 = migrate_v2_to_v1(v2_load, V2ToV1Policy())
    flattened_v1 = migrate_v2_to_v1(
        v2_load,
        V2ToV1Policy(
            speaker_rule="flatten_prefix",
            words_rule="flatten_timing_tokens",
            revision_rule="flatten_id_suffix",
        ),
    )
    rec.check(
        "migration_boundaries_preserved",
        migrated_v2.boundaries_preserved and migrated_v1.boundaries_preserved and flattened_v1.boundaries_preserved,
        v1_to_v2_fills=len(migrated_v2.filled_defaults),
        v2_to_v1_losses=len(migrated_v1.loss_records),
        flatten_example=flattened_v1.utterances[0].to_dict(),
    )

    adjudication = adjudicate(v1_load, v2_load)
    pair_by_key = {pair.pair_key: pair for pair in adjudication.pairs}
    lex_pair = pair_by_key["V1-LEX-040__x__V2-LEX-040"]
    expected_adjudication = {
        "UTT-001__x__UTT-001": ("covered", None),
        "UTT-002__x__UTT-002": ("conflict", "v1"),
        "UTT-003__x__UTT-003": ("conflict", "v2"),
        "V1-LEX-040__x__V2-LEX-040": ("conflict", "v1"),
        "UTT-050__x__UTT-050": ("supplement", None),
    }
    adjudication_ok = True
    for pair_key, (conclusion, winner_side) in expected_adjudication.items():
        pair = pair_by_key[pair_key]
        if pair.decision.conclusion != conclusion:
            adjudication_ok = False
        if winner_side is not None and pair.decision.winner_side != winner_side:
            adjudication_ok = False
    adjudication_ok = adjudication_ok and lex_pair.decision.priority_applied == "utterance_id"
    rec.check(
        "cross_version_adjudication_rules",
        adjudication_ok,
        conclusions=adjudication.conclusions,
        pairs=[pair.to_dict() for pair in adjudication.pairs],
    )

    refresh_service = AdjudicationRefreshService(
        v1_load.utterances,
        v2_load.utterances,
        region=(min(u.start_ms for u in v1_load.utterances), max(u.end_ms for u in v2_load.utterances)),
    )
    old_first = next(u for u in v2_load.utterances if u.utterance_id == "UTT-001")
    revised_words = list(old_first.words)
    revised_words[1] = Word("speech", 560, 700, revised_words[1].confidence)
    refresh_plan, incremental_report, ms_consistent = refresh_service.revise_v2(
        "UTT-001",
        text="deterministic revised refresh wording",
        words=tuple(revised_words),
    )
    rec.check(
        "refresh_invalidation_and_millisecond_equality",
        ms_consistent
        and refresh_plan.invalidated_nodes == ["v1:UTT-001", "v2:UTT-001"]
        and len(refresh_service.last_recomputed_pair_keys) == 1
        and len(refresh_service.last_reused_pair_keys) == 4,
        invalidated_nodes=refresh_plan.invalidated_nodes,
        reused_node_count=len(refresh_plan.reused_nodes),
        recomputed_pairs=refresh_service.last_recomputed_pair_keys,
        reused_pairs=refresh_service.last_reused_pair_keys,
        changed_fields=refresh_plan.changed_fields,
        triggers=[trigger.to_dict() for trigger in refresh_plan.triggers],
    )

    canonical_regions = tuple((u.start_ms, u.end_ms) for u in v2_load.utterances)
    consistency = check_consistency(v2_load.utterances, declared_regions=canonical_regions)
    overlap_probe = list(v2_load.utterances) + [
        Utterance("PROBE-OVERLAP", 500, 700, "overlap probe", confidence=1.0, revision=1)
    ]
    overlap_report = check_consistency(overlap_probe, declared_regions=canonical_regions)
    gap_report = check_consistency(
        [u for u in v2_load.utterances if u.utterance_id != "UTT-001"],
        declared_regions=((240, 1200),) + canonical_regions[1:],
    )
    rec.check("canonical_consistency_passes", consistency.ok, consistency.code, report=consistency.to_dict())
    rec.check("overlap_returns_e_overlap", (not overlap_report.ok) and overlap_report.code == Code.E_OVERLAP, overlap_report.code)
    rec.check("gap_returns_e_gap", (not gap_report.ok) and gap_report.code == Code.E_GAP, gap_report.code)

    cache_dir = report_dir / "content-cache"
    if cache_dir.exists():
        shutil.rmtree(cache_dir)
    cache = ContentAddressedCache(cache_dir)
    pcm_digest = paths.pcm_sha256
    v1_digest = digest_json([u.to_dict() for u in v1_load.utterances])
    v2_digest = digest_json([u.to_dict() for u in v2_load.utterances])

    frame_key = CacheKey.build(
        "audio-frame-features",
        pcm_hash=pcm_digest,
        format_version="pcm-s16le-16k-mono",
        config=frame_config.to_dict(),
    )
    _, frame_hit_1 = cache.get_or_compute(frame_key, lambda: features.to_dict(include_frames=True))
    _, frame_hit_2 = cache.get_or_compute(frame_key, lambda: features.to_dict(include_frames=True))

    vad_key = CacheKey.build(
        "vad-segments",
        pcm_hash=pcm_digest,
        config=features.vad_config,
        upstream_hashes={"frame_features": frame_key.key},
    )
    _, vad_hit_1 = cache.get_or_compute(
        vad_key,
        lambda: {"segments": list(features.speech_segments_ms), "threshold": features.energy_threshold},
        dependencies=[frame_key.key],
    )
    _, vad_hit_2 = cache.get_or_compute(
        vad_key,
        lambda: {"segments": list(features.speech_segments_ms), "threshold": features.energy_threshold},
        dependencies=[frame_key.key],
    )

    alignment_keys: dict[str, CacheKey] = {}
    first_hits = 0
    second_hits = 0
    for pass_index in (1, 2):
        for utterance in v2_load.utterances:
            alignment = align_interval(features, utterance.start_ms, utterance.end_ms, utterance.utterance_id)
            evidence = digest_json(
                [features.frames[index].to_dict() for index in alignment.frame_indices]
            )
            key = CacheKey.build(
                "utterance-alignment",
                pcm_hash=pcm_digest,
                transcript_digest=digest_json(utterance.to_dict()),
                format_version="v2",
                config={"frame": frame_config.to_dict(), "alignment_version": 1},
                upstream_hashes={"vad_frame_evidence": evidence, "vad_artifact": vad_key.key},
                interval=utterance.interval(),
            )
            alignment_keys[utterance.utterance_id] = key
            _, hit = cache.get_or_compute(
                key,
                lambda value=alignment: value.to_dict(),
                dependencies=[vad_key.key],
                interval=utterance.interval(),
            )
            if pass_index == 1:
                first_hits += int(hit)
            else:
                second_hits += int(hit)

    alt_features = extract_features(pcm_bytes, frame_config, energy_quantile=0.40)
    changed_intervals = changed_vad_intervals(features, alt_features, padding_ms=frame_config.hop_ms)
    affected_ids = [
        utterance.utterance_id
        for utterance in v2_load.utterances
        if any(max(utterance.start_ms, start) < min(utterance.end_ms, end) for start, end in changed_intervals)
    ]
    removed_keys = cache.invalidate_intervals(changed_intervals, namespace="utterance-alignment")

    third_hits = 0
    third_misses = 0
    for utterance in v2_load.utterances:
        alignment = align_interval(alt_features, utterance.start_ms, utterance.end_ms, utterance.utterance_id)
        evidence = digest_json([alt_features.frames[index].to_dict() for index in alignment.frame_indices])
        key = CacheKey.build(
            "utterance-alignment",
            pcm_hash=pcm_digest,
            transcript_digest=digest_json(utterance.to_dict()),
            format_version="v2",
            config={"frame": frame_config.to_dict(), "alignment_version": 1},
            upstream_hashes={"vad_frame_evidence": evidence, "vad_artifact": vad_key.key},
            interval=utterance.interval(),
        )
        _, hit = cache.get_or_compute(
            key,
            lambda value=alignment: value.to_dict(),
            dependencies=[vad_key.key],
            interval=utterance.interval(),
        )
        third_hits += int(hit)
        third_misses += int(not hit)

    transcript_v1_key = CacheKey.build(
        "transcript-v1",
        pcm_hash=pcm_digest,
        transcript_digest=v1_digest,
        format_version=v1_load.format_version,
        config={"reader": "pipe-header-or-field-set"},
    )
    _, transcript_v1_hit_1 = cache.get_or_compute(
        transcript_v1_key, lambda: [item.to_dict() for item in v1_load.utterances]
    )
    _, transcript_v1_hit_2 = cache.get_or_compute(
        transcript_v1_key, lambda: [item.to_dict() for item in v1_load.utterances]
    )
    transcript_v2_key = CacheKey.build(
        "transcript-v2",
        pcm_hash=pcm_digest,
        transcript_digest=v2_digest,
        format_version=v2_load.format_version,
        config={"reader": "shard-json+sqlite"},
    )
    _, transcript_v2_hit_1 = cache.get_or_compute(
        transcript_v2_key, lambda: [item.to_dict() for item in v2_load.utterances]
    )
    _, transcript_v2_hit_2 = cache.get_or_compute(
        transcript_v2_key, lambda: [item.to_dict() for item in v2_load.utterances]
    )

    mig_v12_key = CacheKey.build(
        "migration-v1-to-v2",
        transcript_digest=v1_digest,
        format_version="v1->v2",
        config=asdict(V1ToV2Policy()),
    )
    _, mig_v12_hit_1 = cache.get_or_compute(mig_v12_key, lambda: migrated_v2.to_dict())
    _, mig_v12_hit_2 = cache.get_or_compute(mig_v12_key, lambda: migrated_v2.to_dict())
    mig_v21_key = CacheKey.build(
        "migration-v2-to-v1",
        transcript_digest=v2_digest,
        format_version="v2->v1",
        config=asdict(V2ToV1Policy()),
    )
    _, mig_v21_hit_1 = cache.get_or_compute(mig_v21_key, lambda: migrated_v1.to_dict())
    _, mig_v21_hit_2 = cache.get_or_compute(mig_v21_key, lambda: migrated_v1.to_dict())
    adjudication_key = CacheKey.build(
        "adjudication-report",
        pcm_hash=pcm_digest,
        transcript_digest={"v1": v1_digest, "v2": v2_digest},
        format_version="v1+v2",
        config={"priority": ["confidence", "revision", "utterance_id"]},
        upstream_hashes={"migration_v1_v2": mig_v12_key.key, "migration_v2_v1": mig_v21_key.key},
    )
    _, adjudication_hit_1 = cache.get_or_compute(
        adjudication_key,
        lambda: adjudication.to_dict(),
        dependencies=[mig_v12_key.key, mig_v21_key.key],
    )
    _, adjudication_hit_2 = cache.get_or_compute(
        adjudication_key,
        lambda: adjudication.to_dict(),
        dependencies=[mig_v12_key.key, mig_v21_key.key],
    )
    replay = cache.replay_chain(adjudication_key.key)
    reloaded_cache = ContentAddressedCache(cache_dir)
    persisted_replay = reloaded_cache.replay_chain(adjudication_key.key)
    point_cache_keys = {utterance.utterance_id: alignment_keys[utterance.utterance_id].key for utterance in v2_load.utterances}
    point_chains = {
        utterance.utterance_id: (
            alignment_keys[utterance.utterance_id].key,
            vad_key.key,
            frame_key.key,
        )
        for utterance in v2_load.utterances
    }
    timeline = TimelineIndex(
        v2_load.utterances,
        declared_regions=canonical_regions,
        cache_keys=point_cache_keys,
        dependency_chains=point_chains,
    )
    point_hit = timeline.at(500)
    point_gap = timeline.at(1500)
    point_replay = point_hit.replay(cache) if point_hit.cache_key else {"ok": False}

    rec.check("content_cache_same_keys_hit", frame_hit_1 is False and frame_hit_2 is True and first_hits == 0 and second_hits == 5)
    rec.check(
        "transcript_migration_adjudication_artifacts_cached",
        all(
            hit_first is False and hit_second is True
            for hit_first, hit_second in [
                (transcript_v1_hit_1, transcript_v1_hit_2),
                (transcript_v2_hit_1, transcript_v2_hit_2),
                (mig_v12_hit_1, mig_v12_hit_2),
                (mig_v21_hit_1, mig_v21_hit_2),
                (adjudication_hit_1, adjudication_hit_2),
            ]
        ),
        transcript_hits=[transcript_v1_hit_2, transcript_v2_hit_2],
        migration_hits=[mig_v12_hit_2, mig_v21_hit_2],
        adjudication_hit=adjudication_hit_2,
    )
    rec.check(
        "config_change_targeted_interval_invalidation",
        affected_ids == ["V2-LEX-040"]
        and len(removed_keys) == 1
        and third_hits == 4
        and third_misses == 1
        and cache.has(frame_key.key)
        and cache.has(vad_key.key),
        changed_intervals=list(changed_intervals),
        affected_ids=affected_ids,
        removed_key_count=len(removed_keys),
        third_pass_hits=third_hits,
        third_pass_misses=third_misses,
        cache_stats=cache.stats(),
    )
    rec.check(
        "point_query_and_dependency_replay",
        point_hit.ok
        and point_hit.utterance is not None
        and point_hit.utterance.utterance_id == "UTT-001"
        and not point_gap.ok
        and point_gap.code == Code.E_GAP
        and point_replay["ok"]
        and persisted_replay["ok"],
        point_hit=point_hit.to_dict(),
        point_gap=point_gap.to_dict(),
        replay=point_replay,
        adjudication_replay_ok=replay["ok"],
        persisted_adjudication_replay_ok=persisted_replay["ok"],
    )

    snapshot_dir = report_dir / "snapshot-store"
    if snapshot_dir.exists():
        shutil.rmtree(snapshot_dir)
    snapshot_store = SnapshotStore(snapshot_dir)
    snapshot_v1 = build_snapshot(
        1,
        v2_load.utterances,
        config={"frame_ms": frame_config.frame_ms, "hop_ms": frame_config.hop_ms},
        declared_regions=canonical_regions,
        pcm_hash=pcm_digest,
        transcript_digests={"v1": v1_digest, "v2": v2_digest},
    )
    snapshot_store.initialize(snapshot_v1)
    stage_first = snapshot_store.try_stage()
    stage_second = snapshot_store.try_stage()
    snapshot_v2 = build_snapshot(
        2,
        v2_load.utterances,
        config={"frame_ms": frame_config.frame_ms, "hop_ms": frame_config.hop_ms, "commit": 2},
        declared_regions=canonical_regions,
        pcm_hash=pcm_digest,
        transcript_digests={"v1": v1_digest, "v2": v2_digest},
    )
    commit_ok = False
    commit_result_dict: dict[str, Any] = {}
    if stage_first.ok and stage_first.lease is not None:
        commit_result = stage_first.lease.commit(
            snapshot_v2,
            expected_version=snapshot_v1.version,
            expected_checksum=snapshot_v1.checksum,
        )
        commit_ok = commit_result.ok
        commit_result_dict = commit_result.to_dict()

    stale_attempt = snapshot_store.try_stage()
    stale_result_dict: dict[str, Any] = {}
    if stale_attempt.ok and stale_attempt.lease is not None:
        stale_candidate = build_snapshot(99, v2_load.utterances, config={"stale": True})
        stale_result = stale_attempt.lease.commit(
            stale_candidate,
            expected_version=1,
            expected_checksum=snapshot_v1.checksum,
        )
        stale_result_dict = stale_result.to_dict()

    checksum_attempt = snapshot_store.try_stage()
    checksum_result_dict: dict[str, Any] = {}
    if checksum_attempt.ok and checksum_attempt.lease is not None:
        checksum_candidate = build_snapshot(3, v2_load.utterances, config={"commit": 3})
        checksum_result = checksum_attempt.lease.commit(
            checksum_candidate,
            expected_version=2,
            expected_checksum="0" * 64,
        )
        checksum_result_dict = checksum_result.to_dict()

    recovery_attempt = snapshot_store.try_stage()
    recovery_result_dict: dict[str, Any] = {}
    recovered_ok = False
    current = snapshot_store.load()
    if recovery_attempt.ok and recovery_attempt.lease is not None and current is not None:
        recovery_candidate = build_snapshot(
            current.version + 1,
            current.utterances,
            config={**current.config, "recovered": True},
            declared_regions=current.declared_regions,
            pcm_hash=current.pcm_hash,
            transcript_digests=current.transcript_digests,
        )
        recovery_result = recovery_attempt.lease.commit(
            recovery_candidate,
            expected_version=current.version,
            expected_checksum=current.checksum,
        )
        recovered_ok = recovery_result.ok
        recovery_result_dict = recovery_result.to_dict()

    reloaded = snapshot_store.load()
    rec.check(
        "snapshot_version_checksum_and_e_stage_busy",
        stage_first.ok
        and not stage_second.ok
        and stage_second.code == Code.E_STAGE_BUSY
        and commit_ok
        and stale_result_dict.get("code") == Code.E_SNAPSHOT_VERSION_CONFLICT
        and checksum_result_dict.get("code") == Code.E_SNAPSHOT_CHECKSUM_MISMATCH
        and recovered_ok
        and reloaded is not None
        and reloaded.version == 3,
        busy_code=stage_second.code,
        commit=commit_result_dict,
        stale=stale_result_dict,
        checksum=checksum_result_dict,
        recovery=recovery_result_dict,
        final_version=reloaded.version if reloaded else None,
        final_checksum=reloaded.checksum if reloaded else None,
    )

    report: dict[str, Any] = {
        "component": "transcript-governance",
        "overall_ok": rec.ok,
        "environment": {
            "python": platform.python_version(),
            "numpy": np.__version__,
            "platform": platform.platform(),
        },
        "sample_paths": paths.to_dict(),
        "summary": {
            "total_checks": len(rec.checks),
            "passed": sum(1 for item in rec.checks if item["ok"]),
            "failed": sum(1 for item in rec.checks if not item["ok"]),
            "adjudication_conclusions": adjudication.conclusions,
        },
        "checks": rec.checks,
        "invalid_loads": invalid_results,
        "migration": {
            "v1_to_v2_fills": [item.to_dict() for item in migrated_v2.filled_defaults],
            "v2_to_v1_losses": [item.to_dict() for item in migrated_v1.loss_records],
            "boundary_pairs": [item.to_dict() for item in migrated_v2.boundary_pairs],
        },
        "refresh": {
            "plan": refresh_plan.to_dict(),
            "millisecond_consistent": ms_consistent,
            "recomputed_pairs": refresh_service.last_recomputed_pair_keys,
            "reused_pairs": refresh_service.last_reused_pair_keys,
        },
        "cache_replay": replay,
    }

    json_path = report_dir / "diagnostic-report.json"
    md_path = report_dir / "diagnostic-report.md"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    md_path.write_text(_render_markdown(report), encoding="utf-8")
    report["json_report"] = str(json_path)
    report["markdown_report"] = str(md_path)
    return report


def _render_markdown(report: dict[str, Any]) -> str:
    summary = report["summary"]
    lines = [
        "# 转写治理组件诊断报告",
        "",
        f"- 总体结果：{'通过' if report['overall_ok'] else '失败'}",
        f"- 检查项：{summary['passed']}/{summary['total_checks']} 通过，失败 {summary['failed']}",
        f"- Python：{report['environment']['python']}；NumPy：{report['environment']['numpy']}",
        f"- 裁决结论统计：覆盖 {summary['adjudication_conclusions'].get('covered', 0)}，"
        f"冲突 {summary['adjudication_conclusions'].get('conflict', 0)}，"
        f"补充 {summary['adjudication_conclusions'].get('supplement', 0)}",
        "",
        "## 检查项",
        "",
        "| 结果 | 检查 | 业务码 |",
        "| --- | --- | --- |",
    ]
    for check in report["checks"]:
        lines.append(f"| {'PASS' if check['ok'] else 'FAIL'} | `{check['name']}` | `{check.get('code', Code.OK)}` |")
    lines.extend(
        [
            "",
            "## 产物",
            "",
            "- JSON：`reports/diagnostic-report.json`",
            "- 样本目录：`samples/`",
            "- 内容缓存：`reports/content-cache/`",
            "- 快照存储：`reports/snapshot-store/`",
            "",
        ]
    )
    return "\n".join(lines) + "\n"
