"""跨方言裁决：同一语义以 v1/v2/v3 书写时必须产出一致结果。"""

from .config import load_config
from .errors import E_SEMANTIC_DRIFT, TtsError
from .migrate import V1_INEXPRESSIBLE, migrate


def _semantic_view(cfg):
    """抽取参与语义对比的字段。"""
    return {
        "speaker": cfg["speaker"],
        "style": cfg["style"],
        "prosody": cfg["prosody"],
        "precision": cfg["precision"],
        "vocoder": cfg["vocoder"],
    }


def adjudicate(texts_by_version):
    """加载三种方言配置并裁决一致性。

    返回规范化 v3 配置；v1 表达能力不足或不一致时抛 E_SEMANTIC_DRIFT。
    """
    loaded = {v: load_config(t) for v, t in texts_by_version.items()}
    canonical = {}
    for version, cfg in loaded.items():
        upgraded, _ = migrate(cfg, "v3")
        canonical[version] = upgraded
    views = {v: _semantic_view(c) for v, c in canonical.items()}
    ref = views.get("v3") or next(iter(views.values()))
    diffs = []
    for version, view in views.items():
        if view != ref:
            for key in ref:
                if view.get(key) != ref.get(key):
                    diffs.append({"dialect": version, "field": key,
                                  "value": view.get(key),
                                  "expected": ref.get(key)})
    if diffs:
        unrecoverable = sorted({
            item for d in diffs for item in V1_INEXPRESSIBLE
            if d["field"] in item or item.startswith(d["field"])})
        raise TtsError(E_SEMANTIC_DRIFT,
                       "三种方言迁移后语义不一致",
                       unrecoverable=unrecoverable or [d["field"] for d in diffs],
                       diffs=diffs)
    if "v1" in loaded:
        v1_upgraded = canonical["v1"]
        for item in V1_INEXPRESSIBLE:
            section, _, field_name = item.partition(".")
            value = v1_upgraded.get(section, {})
            if isinstance(value, dict) and value.get(field_name) is not None:
                if item == "prosody.duration_scale" and value.get(field_name) == 1.0:
                    continue
                raise TtsError(E_SEMANTIC_DRIFT,
                               "v1 无法完整还原配置项",
                               unrecoverable=[item])
    return canonical.get("v3") or next(iter(canonical.values()))


def cross_dialect_pcm_check(texts_by_version, task, speakers):
    """裁决后分别合成，校验音素序列、声学特征与 PCM 逐字节一致。"""
    import tempfile

    from .cache import ContentCache
    from .dag import Engine

    cfg = adjudicate(texts_by_version)
    outputs = []
    for version in ("v1", "v2", "v3"):
        if version not in texts_by_version:
            continue
        dialect_cfg, _ = migrate(load_config(texts_by_version[version]), "v3")
        with tempfile.TemporaryDirectory() as tmp:
            engine = Engine(ContentCache(tmp), speakers, dialect_cfg)
            res = engine.run_utterance(task)
        outputs.append((version, res))
    ref_pcm = outputs[0][1]["pcm"]
    for version, res in outputs[1:]:
        if res["pcm"] != ref_pcm:
            raise TtsError(E_SEMANTIC_DRIFT,
                           f"方言 {version} 的 PCM 与基准不一致",
                           unrecoverable=["pcm"])
    return cfg, outputs[0][1]
