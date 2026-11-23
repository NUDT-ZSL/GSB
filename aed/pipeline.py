"""DAG pipeline runner, cache-key wiring and incremental invalidation."""

from __future__ import annotations

import json

import numpy as np

from .cache import ContentCache, audio_chunk_hashes, canonical_json, make_key
from .config import FEATURE_VERSION, PipelineConfig
from .detect import detect_candidates
from .errors import AEDError, Code
from .features import N_FEATURES, extract_features, frame_count
from .metrics import aggregate_metrics
from .postprocess import merge_candidates
from .synth import TRUTH_FRAME_MS

NODES = ("features", "candidates", "segments", "metrics")
UPSTREAM = {
    "features": (),
    "candidates": ("features",),
    "segments": ("candidates",),
    "metrics": ("segments",),
}


class Dialog:
    """In-memory view of one synthesized dialog and its truth."""

    def __init__(self, dialog_id, pcm, truth, duration_ms):
        self.dialog_id = dialog_id
        self.pcm = pcm
        self.pcm_bytes = pcm.tobytes()
        self.truth = truth
        self.duration_ms = duration_ms

    def with_truth(self, truth) -> "Dialog":
        return Dialog(self.dialog_id, self.pcm, truth, self.duration_ms)


class Pipeline:
    def __init__(self, cfg: PipelineConfig, cache: ContentCache):
        self.cfg = cfg.validate()
        self.cache = cache

    # ---- cache key construction -------------------------------------
    def key_components(self, node: str, dialog: Dialog,
                       keys: dict) -> dict:
        cfg = self.cfg
        if node == "features":
            return {
                "node": "features",
                "audio_chunks": audio_chunk_hashes(dialog.pcm_bytes),
                "sample_rate": cfg.sample_rate,
                "frame_len": cfg.frame_len,
                "hop": cfg.hop,
                "feature_version": FEATURE_VERSION,
            }
        if node == "candidates":
            return {
                "node": "candidates",
                "upstream": {"features": keys["features"]},
                "template_version": cfg.template_version,
                "score_threshold": cfg.score_threshold,
            }
        if node == "segments":
            return {
                "node": "segments",
                "upstream": {"candidates": keys["candidates"]},
                "min_event_ms": cfg.min_event_ms,
                "min_gap_ms": cfg.min_gap_ms,
                "hop": cfg.hop,
                "frame_len": cfg.frame_len,
            }
        if node == "metrics":
            return {
                "node": "metrics",
                "upstream": {"segments": keys["segments"]},
                "truth_hash": make_key({"truth": dialog.truth}),
            }
        raise ValueError(node)

    # ---- node execution ----------------------------------------------
    def _compute(self, node: str, dialog: Dialog, values: dict):
        if node == "features":
            feats = extract_features(dialog.pcm, self.cfg,
                                     dialog.dialog_id, dialog.duration_ms)
            return feats, feats.tobytes()
        if node == "candidates":
            cands = detect_candidates(values["features"], self.cfg)
            return cands, canonical_json(cands)
        if node == "segments":
            segs = merge_candidates(values["candidates"], self.cfg,
                                    dialog.duration_ms)
            return segs, canonical_json(segs)
        if node == "metrics":
            mets = aggregate_metrics(values["segments"], dialog.truth,
                                     dialog.duration_ms)
            return mets, canonical_json(mets)
        raise ValueError(node)

    @staticmethod
    def _decode(node: str, blob: bytes):
        if node == "features":
            arr = np.frombuffer(blob, dtype=np.float64)
            return arr.reshape(-1, N_FEATURES).copy()
        return json.loads(blob.decode())

    # ---- full / incremental run ---------------------------------------
    def run(self, dialog: Dialog) -> dict:
        keys, values, status = {}, {}, {}
        for node in NODES:
            components = self.key_components(node, dialog, keys)
            key = make_key(components)
            keys[node] = key
            blob = self.cache.get(key)
            if blob is not None:
                values[node] = self._decode(node, blob)
                status[node] = "hit"
            else:
                value, payload = self._compute(node, dialog, values)
                self.cache.put(node, components, payload)
                values[node] = value
                status[node] = "recomputed"
        report = {"dialog_id": dialog.dialog_id, "keys": keys,
                  "status": status, "values": values}
        self.self_check(report, dialog)
        return report

    def self_check(self, report: dict, dialog: Dialog) -> None:
        """Cross-node consistency self-check."""
        feats = report["values"]["features"]
        expected = frame_count(len(dialog.pcm), self.cfg)
        if feats.shape[0] != expected:
            raise AEDError(Code.E_FRAME_MISMATCH,
                           "feature frame count inconsistent across nodes",
                           dialog_id=dialog.dialog_id,
                           n_frames=feats.shape[0], expected=expected)
        mets = report["values"]["metrics"]
        if mets["n_frames"] != len(dialog.truth):
            raise AEDError(Code.E_FRAME_MISMATCH,
                           "metrics frame count inconsistent with truth",
                           dialog_id=dialog.dialog_id)

    # ---- invalidation planning ----------------------------------------
    def plan_invalidation(self, change: dict, dialog: Dialog) -> dict:
        """Compute affected nodes, frame intervals and event sets."""
        kind = change["kind"]
        dur = dialog.duration_ms
        if kind == "template_upgrade":
            return {
                "recompute": ["candidates", "segments", "metrics"],
                "reuse": ["features"],
                "intervals_ms": [[0, dur]],
                "affected_events": "all",
            }
        if kind == "truth_fix":
            idx = change["event_index"]
            events = _truth_event_spans(dialog.truth)
            interval = list(events[idx]) if idx < len(events) else [0, 0]
            return {"recompute": ["metrics"],
                    "reuse": ["features", "candidates", "segments"],
                    "intervals_ms": [interval],
                    "affected_events": [idx]}
        if kind == "hop_change":
            return {"recompute": list(NODES), "reuse": [],
                    "intervals_ms": [[0, dur]], "affected_events": "all"}
        if kind in ("min_duration_change", "min_gap_change"):
            intervals = self._candidate_intervals(dialog)
            return {"recompute": ["segments", "metrics"],
                    "reuse": ["features", "candidates"],
                    "intervals_ms": intervals or [[0, 0]],
                    "affected_events": "candidates"}
        raise ValueError(f"unknown change kind {kind!r}")

    def _candidate_intervals(self, dialog: Dialog) -> list:
        keys = {}
        for node in ("features", "candidates"):
            keys[node] = make_key(self.key_components(node, dialog, keys))
        blob = self.cache.get(keys["candidates"])
        if blob is None:
            return []
        cands = json.loads(blob.decode())
        hop_ms, frame_ms = self.cfg.hop_ms, self.cfg.frame_ms
        return [[int(c["start_frame"] * hop_ms),
                 int(c["end_frame"] * hop_ms + frame_ms)] for c in cands]

    def run_incremental(self, change: dict, dialog: Dialog) -> dict:
        """Recompute only the invalidated suffix; reuse upstream cache."""
        plan = self.plan_invalidation(change, dialog)
        if len(plan["recompute"]) > self.cfg.max_parallel_recompute:
            raise AEDError(Code.E_RESOURCE_LIMIT,
                           "parallel recompute node limit exceeded",
                           n_recompute=len(plan["recompute"]),
                           limit=self.cfg.max_parallel_recompute)
        report = self.run(dialog)
        recomputed = [n for n in NODES if report["status"][n] == "recomputed"]
        reused = [n for n in NODES if report["status"][n] == "hit"]
        return {"plan": plan, "report": report,
                "recompute_nodes": recomputed, "reused_nodes": reused,
                "affected_intervals_ms": plan["intervals_ms"],
                "affected_events": plan["affected_events"]}


def _truth_event_spans(truth) -> list:
    """Group contiguous same-type truth frames into (start_ms, end_ms)."""
    spans = []
    cur_type, start, prev_end = None, None, None
    for row in truth:
        if row["is_event"] != 1:
            continue
        s0 = row["frame_index"] * TRUTH_FRAME_MS
        s1 = s0 + TRUTH_FRAME_MS
        if cur_type == row["event_type"] and s0 <= (prev_end or 0):
            prev_end = s1
        else:
            if cur_type is not None:
                spans.append((start, prev_end))
            cur_type, start, prev_end = row["event_type"], s0, s1
    if cur_type is not None:
        spans.append((start, prev_end))
    return spans
