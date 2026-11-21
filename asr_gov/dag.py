"""有向无环计算图：分帧特征 -> 边界初判 -> 跨版本规范化 -> 一致性校验 -> 快照汇聚。

缓存键只依赖节点真实输入（切片内容哈希、该切片实际用到的词表项指纹、
配置版本、特征摘要），因此未受新词表涉及且时间轴未变的切片键不变、
直接命中缓存，不被判为受影响。追溯记录可重放同一时间轴与校验结论；
依赖版本已变而结果仍来自旧键时返回 E_LINEAGE_STALE 并指出依赖项。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .adaptive import ScaleObservation, decide
from .config import RecognitionConfig, Vocab, canonical_json, sha256_hex
from .errors import LineageStale
from .framing import frame_pcm, detect_boundaries
from .migrate import migrate
from .validate import validate_slices

NODES = ["framing", "boundary", "normalize", "validate", "snapshot"]


@dataclass
class VersionVector:
    config_version: int
    vocab_version: str
    annotation_revision: int

    def to_dict(self):
        return {
            "config_version": self.config_version,
            "vocab_version": self.vocab_version,
            "annotation_revision": self.annotation_revision,
        }


@dataclass
class LineageRecord:
    utterance_id: str
    node_path: list
    cache_keys: dict
    input_versions: dict

    def to_dict(self):
        return {
            "utterance_id": self.utterance_id,
            "node_path": list(self.node_path),
            "cache_keys": dict(self.cache_keys),
            "input_versions": dict(self.input_versions),
        }


@dataclass
class SliceResult:
    utterance_id: str
    start_ms: int
    end_ms: int
    text: str
    validation: str
    lineage: LineageRecord

    def to_dict(self):
        return {
            "utterance_id": self.utterance_id,
            "start_ms": self.start_ms,
            "end_ms": self.end_ms,
            "text": self.text,
            "validation": self.validation,
            "lineage": self.lineage.to_dict(),
        }


@dataclass
class RunResult:
    slices: list
    snapshot: dict
    decision: object
    validation_report: object
    candidates: list
    cache_hits: list = field(default_factory=list)
    cache_misses: list = field(default_factory=list)

    @property
    def affected_slices(self):
        return sorted({k.split(":", 2)[1] for k in self.cache_misses
                       if k.startswith(("normalize:", "validate:"))})

    def timeline(self):
        return [(s.utterance_id, s.start_ms, s.end_ms) for s in self.slices]

    def to_dict(self):
        return {
            "slices": [s.to_dict() for s in self.slices],
            "snapshot": self.snapshot,
            "decision": self.decision.to_dict(),
            "validation": self.validation_report.to_dict(),
            "candidates": [c.to_dict() for c in self.candidates],
            "cache_hits": list(self.cache_hits),
            "cache_misses": list(self.cache_misses),
            "affected_slices": self.affected_slices,
        }


def _key(node, scope, payload) -> str:
    return f"{node}:{scope}:{sha256_hex(canonical_json(payload).encode())[:16]}"


class Pipeline:
    def __init__(self):
        self.cache = {}
        self.artifacts = {}

    def _lookup(self, key, compute):
        if key in self.cache:
            return self.cache[key], True
        value = compute()
        self.cache[key] = value
        return value, False

    def run(self, clip, slice_set, config: RecognitionConfig, vocab: Vocab,
            annotation_revision: int, requested_chunks: int = 1) -> RunResult:
        versions = VersionVector(config.version, vocab.version, annotation_revision)
        hits, misses = [], []

        framing_key = _key("framing", clip.clip_id, {
            "clip_digest": clip.digest(), "params": config.framing_params(),
        })
        features, hit = self._lookup(framing_key, lambda: frame_pcm(
            clip.samples, clip.sample_rate, config.frame_length_ms, config.frame_shift_ms))
        (hits if hit else misses).append(framing_key)

        boundary_key = _key("boundary", clip.clip_id, {
            "framing_key": framing_key, "params": config.boundary_params(),
        })
        candidates, hit = self._lookup(boundary_key, lambda: detect_boundaries(
            features, config.energy_threshold_high_mult, config.energy_threshold_low_mult,
            config.min_slice_ms, config.max_slices))
        (hits if hit else misses).append(boundary_key)

        normalized = migrate(slice_set, 3).slice_set
        observation = ScaleObservation(
            num_slices=len(normalized.slices),
            num_frames=features.n_frames,
            num_speakers=len({s.speaker for s in normalized.slices if s.speaker}),
            memory_headroom_bytes=max(
                0, config.max_resident_feature_bytes - features.resident_bytes()),
        )
        decision = decide(observation, config)

        slice_keys = {}
        normalize_keys = {}
        for s in normalized.slices:
            vocab_fp = vocab.fingerprint_for(s.token_strings())
            nkey = _key("normalize", s.utterance_id, {
                "slice": s.fingerprint(),
                "vocab_fingerprint": vocab_fp,
            })
            normalize_keys[s.utterance_id] = nkey
            artifact, hit = self._lookup(nkey, lambda s=s: {
                "utterance_id": s.utterance_id,
                "start_us": s.start_us, "end_us": s.end_us,
                "text": s.text, "tokens": s.token_strings(),
            })
            slice_keys[s.utterance_id] = artifact
            (hits if hit else misses).append(nkey)

        report = validate_slices(
            normalized.slices, features, clip.n_samples,
            config.energy_threshold_low_mult, config,
            requested_chunks=requested_chunks, clip_id=clip.clip_id)

        validate_keys = {}
        for s in normalized.slices:
            vkey = _key("validate", s.utterance_id, {
                "normalize_key": normalize_keys[s.utterance_id],
                "features_digest": features.digest(),
                "decision": decision.digest(),
            })
            validate_keys[s.utterance_id] = vkey
            _, hit = self._lookup(vkey, lambda s=s: report.verdicts[s.utterance_id])
            (hits if hit else misses).append(vkey)

        snapshot_key = _key("snapshot", clip.clip_id, {
            "validate_keys": [validate_keys[u] for u in sorted(validate_keys)],
            "config_checksum": config.checksum(),
            "vocab": vocab.to_dict(),
        })
        stats = {
            "num_slices": len(normalized.slices),
            "num_frames": features.n_frames,
            "num_candidates": len(candidates),
            "decision_path": decision.path,
        }
        _, hit = self._lookup(snapshot_key, lambda: {
            "config": config.to_dict(), "vocab_version": vocab.version,
            "annotation_revision": annotation_revision, "stats": stats,
        })
        (hits if hit else misses).append(snapshot_key)

        results = []
        for s in normalized.slices:
            lineage = LineageRecord(
                utterance_id=s.utterance_id,
                node_path=list(NODES),
                cache_keys={
                    "framing": framing_key,
                    "boundary": boundary_key,
                    "normalize": normalize_keys[s.utterance_id],
                    "validate": validate_keys[s.utterance_id],
                    "snapshot": snapshot_key,
                },
                input_versions=versions.to_dict(),
            )
            results.append(SliceResult(
                utterance_id=s.utterance_id,
                start_ms=s.start_ms, end_ms=s.end_ms, text=s.text,
                validation=report.verdicts[s.utterance_id], lineage=lineage,
            ))
        return RunResult(
            slices=results,
            snapshot={"key": snapshot_key, "stats": stats,
                      "versions": versions.to_dict()},
            decision=decision, validation_report=report, candidates=candidates,
            cache_hits=hits, cache_misses=misses,
        )

    def verify_lineage(self, record: LineageRecord, expected_keys: dict):
        """比对追溯记录与当前应有缓存键；任一依赖已变而仍持旧键即拒绝。"""
        stale = []
        for node, expected in expected_keys.items():
            actual = record.cache_keys.get(node)
            if actual != expected:
                stale.append({"node": node, "recorded": actual, "expected": expected})
        if stale:
            raise LineageStale(
                f"切片 {record.utterance_id} 的 {len(stale)} 项依赖已失效",
                details={"utterance_id": record.utterance_id, "stale_dependencies": stale},
            )
        return True

    def replay(self, record: LineageRecord):
        """用追溯记录中的缓存键重放：所有键必须仍在缓存中，结论可重现。"""
        missing = [node for node, key in record.cache_keys.items()
                   if key not in self.cache]
        if missing:
            raise LineageStale(
                f"切片 {record.utterance_id} 重放失败，缓存键缺失",
                details={"utterance_id": record.utterance_id, "missing_nodes": missing},
            )
        return {node: self.cache[key] for node, key in record.cache_keys.items()}
