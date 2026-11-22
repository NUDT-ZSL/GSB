"""合成产物追溯与过期结果拒绝（E_LINEAGE_BROKEN）。"""

import json
import os

from . import synth
from .errors import E_LINEAGE_BROKEN, TtsError

# 参与过期判定的依赖项
DEP_FIELDS = ("speaker_embed_version", "speaker_params_hash", "style",
              "prosody", "precision", "vocoder_version", "vocoder_params")


def _broken(message, utterance_id, **details):
    return TtsError(E_LINEAGE_BROKEN, message,
                    utterance_id=utterance_id, **details)


def load_lineage(lineage_dir, utterance_id):
    path = os.path.join(lineage_dir, f"{utterance_id}.lineage.json")
    if not os.path.exists(path):
        raise _broken("追溯链缺失", utterance_id, reason="lineage_missing")
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def changed_dependencies(record, engine, task):
    """对比谱系记录的键组成与当前依赖，返回变化的依赖项列表。"""
    base = engine.dependency_components(task)
    changed = []
    comps = record["key_components"].get("vocoder") or {}
    for field_name in DEP_FIELDS:
        old = comps.get(field_name)
        new = base.get(field_name)
        if old != new:
            changed.append({"dependency": field_name, "old": old, "new": new})
    return changed


def verify_lineage(lineage_dir, utterance_id, engine, task, cache):
    """校验谱系：依赖版本一致且可按记录重放出相同 PCM。"""
    record = load_lineage(lineage_dir, utterance_id)
    changed = changed_dependencies(record, engine, task)
    if changed:
        raise _broken("缓存键依赖版本已变化，产物仍来自旧键",
                      utterance_id, changed_dependencies=changed)
    vocoder_key = record["node_keys"].get("vocoder")
    pcm = cache.get(vocoder_key)
    if pcm is None:
        raise _broken("按谱系缓存键重放失败：产物缺失",
                      utterance_id, reason="replay_missing",
                      vocoder_key=vocoder_key)
    if synth.sha256_bytes(pcm) != record["pcm_sha256"]:
        raise _broken("重放 PCM 与谱系记录不符",
                      utterance_id, reason="replay_mismatch")
    return {"utterance_id": utterance_id, "pcm_sha256": record["pcm_sha256"],
            "config_version": record["config_version"],
            "node_path": record["node_path"], "ok": True}
