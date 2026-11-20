"""Pipeline orchestration: caching, directed invalidation, recalibration."""

from __future__ import annotations

import tempfile
from concurrent.futures import ProcessPoolExecutor

import numpy as np

from . import errors
from .cache import ArtifactCache
from .codec import dumps, loads, sha256_bytes, hash_obj
from .config import VadConfig, validate_config
from .keys import NODE_ORDER, build_components, keys_from_components
from .lineage import build_lineage
from .nodes import (
    compute_decision,
    compute_features,
    compute_noise,
    compute_segments,
)

_DOWNSTREAM = {
    "features": ("noise", "decision", "segments"),
    "noise": ("decision", "segments"),
    "decision": ("segments",),
    "segments": (),
}

_FIELD_TO_NODE = {
    "sample_rate": "features",
    "frame_ms": "features",
    "hop_ms": "features",
    "percentile": "noise",
    "percentile_window": "noise",
    "param_version": "noise",
    "mult_low": "decision",
    "mult_high": "decision",
    "min_speech_ms": "segments",
    "min_silence_ms": "segments",
}


def affected_nodes(old_cfg: VadConfig, new_cfg: VadConfig):
    """Nodes invalidated by a config change: changed node + downstream suffix."""
    hit = set()
    for field, node in _FIELD_TO_NODE.items():
        if getattr(old_cfg, field) != getattr(new_cfg, field):
            hit.add(node)
            hit.update(_DOWNSTREAM[node])
    return [n for n in NODE_ORDER if n in hit]


def _frame_intervals(diff_idx):
    intervals = []
    for i in diff_idx:
        i = int(i)
        if intervals and i == intervals[-1][1]:
            intervals[-1][1] = i + 1
        else:
            intervals.append([i, i + 1])
    return intervals


class Pipeline:
    def __init__(self, cache: ArtifactCache, cfg: VadConfig):
        validate_config(cfg)
        self.cache = cache
        self.cfg = cfg

    def _compute_node(self, node, clip, artifacts):
        cfg = self.cfg
        if node == "features":
            art = compute_features(clip["pcm"], cfg.sample_rate, cfg.frame_ms, cfg.hop_ms)
            if art["n_frames"] != len(clip["truth"]):
                raise errors.VadError(
                    errors.E_FRAME_COUNT,
                    "feature frame count does not match the truth file",
                    clip_id=clip["clip_id"],
                    expected=len(clip["truth"]),
                    actual=art["n_frames"],
                )
            return art
        if node == "noise":
            energy = artifacts["features"]["matrix"][:, 0]
            inactive = [f["frame_index"] for f in clip["truth"] if not f["is_speech"]]
            return compute_noise(energy, inactive, cfg.percentile, cfg.percentile_window)
        if node == "decision":
            energy = artifacts["features"]["matrix"][:, 0]
            return compute_decision(
                energy, artifacts["noise"]["noise"], cfg.mult_low, cfg.mult_high
            )
        if node == "segments":
            return compute_segments(
                artifacts["decision"]["decisions"],
                cfg.frame_ms,
                cfg.hop_ms,
                clip["duration_ms"],
                cfg.min_speech_ms,
                cfg.min_silence_ms,
            )
        raise AssertionError(node)

    def run_clip(self, clip):
        cfg = self.cfg
        pcm = np.ascontiguousarray(clip["pcm"])
        hop = cfg.sample_rate * cfg.hop_ms // 1000
        n_frames_est = (len(pcm) + hop - 1) // hop
        if n_frames_est > cfg.max_frames_in_memory:
            raise errors.VadError(
                errors.E_RESOURCE_LIMIT,
                "frame feature residency limit exceeded",
                clip_id=clip["clip_id"],
                frames=n_frames_est,
                limit=cfg.max_frames_in_memory,
            )
        pcm_hash = sha256_bytes(pcm.tobytes())
        truth_hash = hash_obj(clip["truth"])
        components = build_components(pcm_hash, truth_hash, cfg, clip["duration_ms"])
        keys = keys_from_components(components)

        artifacts = {}
        reused, recomputed = [], []
        conflicts = 0
        for node in NODE_ORDER:
            raw = self.cache.get(keys[node])
            if raw is not None:
                artifacts[node] = loads(raw)
                reused.append(node)
                continue
            art = self._compute_node(node, clip, artifacts)
            try:
                self.cache.commit(keys[node], dumps(art))
            except errors.VadError as exc:
                if exc.code != errors.E_CACHE_BUSY:
                    raise
                conflicts += 1
                raw = self.cache.get(keys[node])
                if raw is None:
                    raise
                art = loads(raw)
            artifacts[node] = art
            recomputed.append(node)

        lineage = build_lineage(clip["clip_id"], keys, components, cfg.param_version)
        return {
            "clip_id": clip["clip_id"],
            "duration_ms": clip["duration_ms"],
            "n_frames": artifacts["features"]["n_frames"],
            "noise": artifacts["noise"]["noise"],
            "thresholds": {
                "low": artifacts["decision"]["low_threshold"],
                "high": artifacts["decision"]["high_threshold"],
            },
            "decisions": [int(b) for b in artifacts["decision"]["decisions"]],
            "segments": artifacts["segments"]["segments"],
            "lineage": lineage,
            "reused": reused,
            "recomputed": recomputed,
            "cache_conflicts": conflicts,
            "degraded": self.cache.degraded,
            "_artifacts": artifacts,
        }

    def recalibrate(self, clip, new_cfg: VadConfig, old_result):
        """Directed invalidation: recompute only the affected downstream suffix."""
        validate_config(new_cfg)
        affected = affected_nodes(self.cfg, new_cfg)
        new_result = Pipeline(self.cache, new_cfg).run_clip(clip)

        # full-recompute equivalence: fresh cache, same new config
        with tempfile.TemporaryDirectory() as tmp:
            fresh = Pipeline(
                ArtifactCache(tmp, max_entries=new_cfg.max_cache_entries), new_cfg
            ).run_clip(clip)
        byte_identical = all(
            dumps(new_result["_artifacts"][n]) == dumps(fresh["_artifacts"][n])
            for n in NODE_ORDER
        )

        old_dec = np.asarray(old_result["decisions"], dtype=np.int8)
        new_dec = np.asarray(new_result["decisions"], dtype=np.int8)
        diff = np.nonzero(old_dec != new_dec)[0]
        old_segs = {tuple(s) for s in old_result["segments"]}
        new_segs = {tuple(s) for s in new_result["segments"]}

        reused_set = set(new_result["reused"])
        recomputed_set = set(new_result["recomputed"])
        selfcheck = {
            "no_overlap": not (reused_set & recomputed_set),
            "covers_affected": recomputed_set == set(affected)
            and reused_set | recomputed_set == set(NODE_ORDER),
        }
        return {
            "clip_id": clip["clip_id"],
            "affected_nodes": affected,
            "reused": new_result["reused"],
            "recomputed": new_result["recomputed"],
            "affected_frames": _frame_intervals(diff),
            "segments_added": sorted(list(s) for s in new_segs - old_segs),
            "segments_removed": sorted(list(s) for s in old_segs - new_segs),
            "byte_identical_to_full_recompute": byte_identical,
            "selfcheck": selfcheck,
            "result": new_result,
        }

    def run_manifest(self, clips, jobs=1):
        seen = set()
        for clip in clips:
            if clip["clip_id"] in seen:
                raise errors.VadError(
                    errors.E_DUPLICATE_CLIP_ID,
                    "duplicate clip_id in manifest",
                    clip_id=clip["clip_id"],
                )
            seen.add(clip["clip_id"])
        hop = self.cfg.sample_rate * self.cfg.hop_ms // 1000
        total_frames = sum((len(c["pcm"]) + hop - 1) // hop for c in clips)
        if total_frames > self.cfg.max_frames_in_memory:
            raise errors.VadError(
                errors.E_RESOURCE_LIMIT,
                "total frame feature residency limit exceeded",
                frames=total_frames,
                limit=self.cfg.max_frames_in_memory,
            )
        degraded = False
        workers = max(1, min(int(jobs), self.cfg.max_parallel_nodes))
        if int(jobs) > self.cfg.max_parallel_nodes:
            degraded = True  # clamped to the configured parallelism limit
        if workers <= 1 or len(clips) <= 1:
            results = [self.run_clip(c) for c in clips]
        else:
            payloads = [(str(self.cache.root), self.cfg, c) for c in clips]
            with ProcessPoolExecutor(max_workers=workers) as pool:
                results = list(pool.map(_run_clip_worker, payloads))
        return {"results": results, "degraded": degraded or self.cache.degraded}


def _run_clip_worker(payload):
    cache_root, cfg, clip = payload
    cache = ArtifactCache(cache_root, max_entries=cfg.max_cache_entries)
    return Pipeline(cache, cfg).run_clip(clip)
