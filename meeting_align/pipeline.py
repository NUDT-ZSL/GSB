"""Pipeline orchestration: seams -> calibration -> mapping -> confidence.

Also implements targeted invalidation (per-segment dependency records) and
versioned snapshot management.  Every stage is content-addressed through
the ArtifactCache, so unchanged inputs reuse persisted snapshots and a
partial change recomputes only the affected suffix while staying
byte-identical to a from-scratch run.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from .align import resolve_segment, validate_global_segments
from .align import map_segment as _map_span
from .cache import ArtifactCache
from .calibration import calibrate_all
from .confidence import propagate_word_intervals
from .config import AlignConfig
from .features import check_frame_memory
from .lineage import attach_lineage
from .seams import detect_seam_candidates, seam_set_version
from .snapshots import SnapshotStore
from .util import hash_obj, hash_pcm, round_ms


@dataclass
class Batch:
    batch_id: str
    pcm: np.ndarray
    declared_sample_rate: int
    segments: list = field(default_factory=list)

    def to_dict(self):
        return {
            "batch_id": self.batch_id,
            "declared_sample_rate": self.declared_sample_rate,
            "num_samples": int(self.pcm.size),
            "segments": self.segments,
        }


def _seams_key_components(ns, pcm_hash, config, hop):
    return {
        "ns": ns,
        "pcm_hash": pcm_hash,
        "frame_len": config.frame_len,
        "frame_hop": hop,
        "gap_energy_threshold": config.gap_energy_threshold,
        "seam_energy_threshold": config.seam_energy_threshold,
        "seam_zcr_threshold": config.seam_zcr_threshold,
        "max_seam_candidates": config.max_seam_candidates,
        "sample_rate": config.sample_rate,
    }


class Pipeline:
    def __init__(self, workdir, config: AlignConfig):
        self.workdir = Path(workdir)
        self.workdir.mkdir(parents=True, exist_ok=True)
        self.config = config
        self.cache = ArtifactCache(
            self.workdir / "cache", max_entries=config.resource.max_cache_entries
        )
        self.snapshots = SnapshotStore(self.workdir / "snapshots")

    def _detect_seams(self, pcm, hop, ns):
        pcm_hash = hash_pcm(pcm)
        components = _seams_key_components(ns, pcm_hash, self.config, hop)
        key = self.cache.make_key(components)
        cached = self.cache.get(key)
        if cached is not None:
            return cached, key
        result = detect_seam_candidates(pcm, self.config, hop=hop)
        self.cache.put(components, result)
        return result, key

    def run(self, batches: dict, order: list, previous=None):
        """Run the full pipeline. `previous` is an optional earlier result
        used to compute the affected/reused segment sets."""
        config = self.config
        degradations = []

        # 1. PCM digests snapshot (whole replacement on any change)
        pcm_hashes = {b: hash_pcm(batches[b].pcm) for b in order}
        declared = {b: batches[b].declared_sample_rate for b in order}
        digest_payload = {
            "order": list(order),
            "pcm_hashes": pcm_hashes,
            "declared_sample_rates": declared,
            "num_samples": {b: int(batches[b].pcm.size) for b in order},
        }
        digest_snap, _ = self.snapshots.upsert("batch_pcm_digests", digest_payload)

        # 2. alignment config snapshot
        config_snap, _ = self.snapshots.upsert("align_config", config.to_dict())

        # 3. frame memory resource check (may degrade the hop)
        total_samples = sum(int(batches[b].pcm.size) for b in order)
        hop, degradation = check_frame_memory(
            total_samples, config.frame_len, config.frame_hop, config.resource
        )
        if degradation:
            degradations.append(degradation)

        # 4. per-batch + global seam detection (content addressed)
        batch_seams, batch_seam_keys = {}, {}
        for b in order:
            batch_seams[b], batch_seam_keys[b] = self._detect_seams(
                batches[b].pcm, hop, f"seams:{b}"
            )
        global_pcm = np.concatenate([batches[b].pcm for b in order])
        global_seams, global_seam_key = self._detect_seams(
            global_pcm, hop, "seams:global"
        )
        for r in list(batch_seams.values()) + [global_seams]:
            if r["truncated"]:
                degradations.append(
                    {
                        "category": "seam_candidates_capped",
                        "max_seam_candidates": config.max_seam_candidates,
                    }
                )

        # 5. seam set snapshot
        seam_payload = {
            "batch_seams": {b: batch_seams[b] for b in order},
            "global_seams": global_seams,
            "order": list(order),
        }
        seam_snap, _ = self.snapshots.upsert("seam_set", seam_payload)
        seam_hash = seam_set_version(global_seams)["hash"]

        # 6. cross-batch calibration (content addressed)
        batch_starts, pos = {}, 0
        for b in order:
            batch_starts[b] = pos
            pos += int(batches[b].pcm.size)
        calib_components = {
            "ns": "calibration",
            "batch_seam_hashes": {
                b: seam_set_version(batch_seams[b])["hash"] for b in order
            },
            "global_seam_hash": seam_hash,
            "declared_sample_rates": declared,
            "order": list(order),
            "batch_starts": batch_starts,
            "sample_rate": config.sample_rate,
            "match_tolerance_ms": config.match_tolerance_ms,
            "rate_tolerance": config.rate_tolerance,
        }
        calib_key = self.cache.make_key(calib_components)
        calib_result = self.cache.get(calib_key)
        if calib_result is None:
            calib_result = calibrate_all(
                order, batch_seams, global_seams, batch_starts, declared, config
            )
            self.cache.put(calib_components, calib_result)
        calibrations = calib_result["calibrations"]
        calib_hash = calib_result["version"]
        config_hash_pre = hash_obj(config.to_dict())
        calib_b_hashes = {b: hash_obj(calibrations[b]) for b in order}
        prev_calib = self.snapshots.load("calibration")
        prev_versions = (prev_calib or {}).get("payload", {}).get(
            "batch_versions", {}
        )
        prev_hashes = (prev_calib or {}).get("payload", {}).get(
            "batch_hashes", {}
        )
        batch_versions = {}
        for b in order:
            if prev_hashes.get(b) == calib_b_hashes[b]:
                batch_versions[b] = prev_versions.get(b, 1)
            else:
                batch_versions[b] = prev_versions.get(b, 0) + 1
        calib_snap, _ = self.snapshots.upsert(
            "calibration",
            {
                "calibrations": calibrations,
                "hash": calib_hash,
                "batch_versions": batch_versions,
                "batch_hashes": calib_b_hashes,
            },
        )

        # 7. segment mapping on the global axis (per-segment content keys)
        sr = config.sample_rate
        seams_global_ms = [
            round_ms(c["sample_index"] / sr * 1000.0)
            for c in global_seams["candidates"]
            if c["kind"] == "splice"
        ]
        gaps_global_ms = [
            [round_ms(g[0] / sr * 1000.0), round_ms(g[1] / sr * 1000.0)]
            for g in global_seams["gaps"]
        ]
        config_hash = config_hash_pre
        mapped_segments, map_keys, dep_records = [], {}, {}
        crossed_by_id = {}
        for b in order:
            calib_b_hash = calib_b_hashes[b]
            for segment in batches[b].segments:
                # dependency scope: the seams/gaps intersecting the initial
                # mapped span.  Remote seam changes must not invalidate this
                # segment (requirement: targeted invalidation).
                span0, span1 = _map_span(segment, calibrations[b])
                crossed = [s for s in seams_global_ms if span0 <= s <= span1]
                near_gaps = [
                    g for g in gaps_global_ms if span0 <= g[1] and span1 >= g[0]
                ]
                crossed_hash = hash_obj({"seams": crossed, "gaps": near_gaps})
                crossed_by_id[segment["id"]] = crossed_hash
                components = {
                    "ns": "segment_mapping",
                    "segment_hash": hash_obj(segment),
                    "calibration_hash": calib_b_hash,
                    "crossed_seams_hash": crossed_hash,
                    "strategy": config.conflict_strategy,
                    "config_hash": config_hash,
                    "frame_hop": hop,
                }
                key = self.cache.make_key(components)
                mapped = self.cache.get(key)
                if mapped is None:
                    mapped = resolve_segment(
                        segment, calibrations[b], seams_global_ms,
                        gaps_global_ms, config,
                    )
                    self.cache.put(components, mapped)
                map_keys[segment["id"]] = key
                mapped_segments.append(mapped)
                dep_records[segment["id"]] = {
                    "batch_pcm_hash": pcm_hashes[b],
                    "calibration_hash": calib_b_hash,
                    "crossed_seams_hash": crossed_hash,
                    "config_hash": config_hash,
                    "frame_hop": hop,
                }

        # 8. targeted invalidation: diff dependency records
        recompute, reuse = [], []
        previous_deps = (previous or {}).get("dependency_records", {})
        for seg_id, record in dep_records.items():
            if previous_deps.get(seg_id) == record:
                reuse.append(seg_id)
            else:
                recompute.append(seg_id)

        # 9. ordering / overlap / range validation
        total_duration_ms = round_ms(total_samples / sr * 1000.0)
        ordered = validate_global_segments(mapped_segments, total_duration_ms)

        # 10. confidence interval propagation (content addressed)
        confidence = []
        for seg in ordered:
            components = {
                "ns": "confidence",
                "map_key": map_keys[seg["id"]],
                "calibration_residual_ms": calibrations[seg["batch_id"]][
                    "residual_rms_ms"
                ],
                "confidence_decay_per_ms": config.confidence_decay_per_ms,
                "frame_hop": hop,
            }
            key = self.cache.make_key(components)
            payload = self.cache.get(key)
            if payload is None:
                payload = propagate_word_intervals(
                    seg, calibrations[seg["batch_id"]], config, hop
                )
                self.cache.put(components, payload)
            confidence.append(payload)

        # 11. lineage on every global segment
        for seg in ordered:
            attach_lineage(
                seg,
                map_cache_key=map_keys[seg["id"]],
                calibration_version=batch_versions[seg["batch_id"]],
                calibration_hash=calib_b_hashes[seg["batch_id"]],
                seam_set_version=seam_snap["version"],
                seam_set_hash=seam_hash,
                crossed_seams_hash=crossed_by_id[seg["id"]],
                frame_hop=hop,
                config_version=config_snap["version"],
                pcm_hash=pcm_hashes[seg["batch_id"]],
            )

        return {
            "business_code": "OK",
            "degradations": degradations,
            "applied_hop": hop,
            "versions": {
                "align_config": config_snap["version"],
                "seam_set": seam_snap["version"],
                "batch_pcm_digests": digest_snap["version"],
                "calibration": calib_snap["version"],
            },
            "hashes": {
                "pcm": pcm_hashes,
                "seam_set": seam_hash,
                "calibration": calib_hash,
                "config": config_hash,
            },
            "total_duration_ms": total_duration_ms,
            "seams": {
                "global": global_seams,
                "batches": batch_seams,
                "cache_keys": {**batch_seam_keys, "global": global_seam_key},
            },
            "calibration": calibrations,
            "segments": ordered,
            "confidence": confidence,
            "invalidation": {"recompute": recompute, "reuse": reuse},
            "dependency_records": dep_records,
            "cache_keys": {"calibration": calib_key, "segments": map_keys},
            "lineage_state": {
                "calibration_versions": batch_versions,
                "calibration_hashes": calib_b_hashes,
                "seam_set_version": seam_snap["version"],
                "seam_set_hash": seam_hash,
                "frame_hop": hop,
                "config_version": config_snap["version"],
                "pcm_hashes": pcm_hashes,
                "crossed_seams": crossed_by_id,
            },
        }
