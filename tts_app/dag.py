"""四阶段确定性合成计算图引擎与增量失效传播。

节点拓扑（由配置字典驱动连接）：
  normalize -> phonemes -> acoustic -> vocoder -> align
"""

import json
import os
from dataclasses import dataclass, field

from . import synth
from .cache import make_key
from .errors import E_RESOURCE_LIMIT, TtsError

NORMALIZER_VERSION = "norm_v1"
NODE_ORDER = ("normalize", "phonemes", "acoustic", "vocoder", "align")


@dataclass
class Limits:
    max_utterances_per_recompute: int = 1000
    max_cache_entries: int = 10000
    max_parallel_nodes: int = 4
    degrade: bool = False
    degraded: list = field(default_factory=list)

    def check_utterances(self, n):
        if n > self.max_utterances_per_recompute:
            if self.degrade:
                self.degraded.append("utterance_batch_split")
                return
            raise TtsError(E_RESOURCE_LIMIT,
                           "单次重算 utterance 数超限",
                           limit=self.max_utterances_per_recompute, actual=n)

    def clamp_parallel(self, requested):
        if requested > self.max_parallel_nodes:
            if self.degrade:
                self.degraded.append("parallel_clamped")
                return self.max_parallel_nodes
            raise TtsError(E_RESOURCE_LIMIT,
                           "并行重算节点数超限",
                           limit=self.max_parallel_nodes, actual=requested)
        return requested


class Engine:
    """确定性合成引擎：逐节点内容寻址缓存 + 谱系记录。"""

    def __init__(self, cache, speakers, config, limits=None, lineage_dir=None):
        self.cache = cache
        self.speakers = speakers
        self.config = config
        self.limits = limits or Limits()
        self.lineage_dir = lineage_dir
        if lineage_dir:
            os.makedirs(lineage_dir, exist_ok=True)

    def dependency_components(self, task):
        """当前任务参与缓存键的全部依赖组件（供谱系过期比对）。"""
        cfg = self.config
        spk = self.speakers[task["speaker_id"]]
        return {
            "text_hash": synth.sha256_bytes(task["text"].encode("utf-8")),
            "speaker_embed_version": spk["embed_version"],
            "speaker_params_hash": synth.sha256_bytes(
                json.dumps(spk, sort_keys=True).encode("utf-8")),
            "style": cfg["style"],
            "prosody": cfg["prosody"],
            "precision": cfg["precision"],
            "vocoder_version": cfg["vocoder"]["version"],
            "vocoder_params": cfg["vocoder"]["params"],
        }

    def _node_key(self, node, task, components, upstream_keys):
        comp = dict(components)
        comp["node"] = node
        comp["utterance_id"] = task["utterance_id"]
        comp["seed"] = task["seed"]
        comp["upstream"] = dict(upstream_keys)
        return make_key(comp), comp

    def run_utterance(self, task):
        cfg = self.config
        spk = self.speakers[task["speaker_id"]]
        deps = self.dependency_components(task)
        keys, reused, recomputed = {}, [], []

        def stage(node, components, compute):
            key, comp = self._node_key(node, task, components, keys)
            payload = self.cache.get(key)
            if payload is None:
                payload = compute()
                self.cache.put(key, comp, payload)
                recomputed.append(node)
            else:
                reused.append(node)
            keys[node] = key
            return payload

        # 键组件按节点作用域前向流动：上游键不含下游版本，
        # 保证增量修改仅使受影响后缀失效。
        normalized = stage(
            "normalize",
            {"text_hash": deps["text_hash"],
             "normalizer_version": NORMALIZER_VERSION},
            lambda: synth.normalize_text(task["text"]))

        def _phonemes():
            phones = synth.segment_phonemes(normalized)
            return {"phonemes": phones,
                    "digest": synth.sha256_bytes(
                        "|".join(phones).encode("utf-8"))}

        ph = stage("phonemes", {"text_hash": deps["text_hash"]}, _phonemes)

        acoustic_comp = {
            "phoneme_digest": ph["digest"],
            "speaker_embed_version": deps["speaker_embed_version"],
            "speaker_params_hash": deps["speaker_params_hash"],
            "style": deps["style"],
            "prosody": deps["prosody"],
            "precision": deps["precision"],
        }

        acoustic = stage(
            "acoustic", acoustic_comp,
            lambda: synth.acoustic_features(
                ph["phonemes"], spk, cfg["style"]["tag"],
                cfg["prosody"]["scale"],
                cfg["prosody"]["duration_scale"] or 1.0,
                cfg["prosody"]["duration_curve"],
                cfg["precision"], task["seed"], task["utterance_id"]))

        vocoder_comp = dict(acoustic_comp)
        vocoder_comp.update({
            "text_hash": deps["text_hash"],
            "vocoder_version": deps["vocoder_version"],
            "vocoder_params": deps["vocoder_params"],
        })

        pcm = stage(
            "vocoder", vocoder_comp,
            lambda: synth.vocoder_synthesize(
                acoustic, spk, cfg["vocoder"]["params"], cfg["precision"],
                task["seed"], task["utterance_id"], task["sample_rate"]))

        def _align():
            frames = synth.frame_features(pcm, task["speaker_id"], acoustic,
                                          task["sample_rate"])
            return {"frames": frames,
                    "prosody_check": synth.prosody_check(frames, acoustic)}

        aligned = stage("align", dict(vocoder_comp,
                                      frame_ms=synth.FRAME_MS), _align)

        result = {
            "utterance_id": task["utterance_id"],
            "pcm": pcm,
            "pcm_sha256": synth.sha256_bytes(pcm),
            "frames": aligned["frames"],
            "prosody_check": aligned["prosody_check"],
            "node_keys": dict(keys),
            "reused": reused,
            "recomputed": recomputed,
        }
        if self.lineage_dir:
            self._write_lineage(task, result)
        return result

    def run_all(self, tasks, parallel=1):
        """批量合成，输出重算/复用节点清单与受影响 utterance 集合。"""
        self.limits.check_utterances(len(tasks))
        self.limits.clamp_parallel(parallel)
        results = {}
        for task in tasks:
            results[task["utterance_id"]] = self.run_utterance(task)
        recomputed_nodes = [f"{u}:{n}" for u, r in results.items()
                            for n in r["recomputed"]]
        reused_nodes = [f"{u}:{n}" for u, r in results.items()
                        for n in r["reused"]]
        affected = sorted(u for u, r in results.items() if r["recomputed"])
        return {
            "results": results,
            "recomputed_nodes": recomputed_nodes,
            "reused_nodes": reused_nodes,
            "affected_utterances": affected,
            "degraded": list(self.limits.degraded) + self.cache.degraded,
        }

    def _lineage_path(self, utterance_id):
        return os.path.join(self.lineage_dir, f"{utterance_id}.lineage.json")

    def _write_lineage(self, task, result):
        record = {
            "utterance_id": task["utterance_id"],
            "config_version": self.config["source_version"],
            "node_path": list(NODE_ORDER),
            "node_keys": result["node_keys"],
            "key_components": {
                node: self.cache.describe(key)["components"]
                for node, key in result["node_keys"].items()},
            "pcm_sha256": result["pcm_sha256"],
        }
        with open(self._lineage_path(task["utterance_id"]), "w",
                  encoding="utf-8") as fh:
            json.dump(record, fh, ensure_ascii=False, indent=1, sort_keys=True)
