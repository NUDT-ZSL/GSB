import copy

import pytest

from tts_app.cache import ContentCache
from tts_app.dag import NODE_ORDER, Engine, Limits
from tts_app.errors import E_RESOURCE_LIMIT, TtsError


def test_determinism(engine, tasks, tmp_path, speakers, cfg_v2):
    r1 = engine.run_utterance(tasks[0])
    other = Engine(ContentCache(str(tmp_path / "c2")), speakers, cfg_v2)
    r2 = other.run_utterance(tasks[0])
    assert r1["pcm"] == r2["pcm"]
    assert r1["node_keys"] == r2["node_keys"]
    assert list(r1["node_keys"]) == list(NODE_ORDER)


def test_cache_key_components_and_reverse_lookup(engine, tasks):
    res = engine.run_utterance(tasks[0])
    meta = engine.cache.describe(res["node_keys"]["vocoder"])
    comp = meta["components"]
    for field in ("text_hash", "phoneme_digest", "speaker_embed_version",
                  "style", "prosody", "precision", "vocoder_version",
                  "upstream"):
        assert field in comp, field
    assert "acoustic" in comp["upstream"]


def test_cache_reuse_same_key(engine, tasks):
    engine.run_all(tasks)
    run = engine.run_all(tasks)
    assert run["recomputed_nodes"] == []
    assert len(run["reused_nodes"]) == len(tasks) * len(NODE_ORDER)


def test_incremental_speaker_param(engine, tasks, speakers, tmp_path, cfg_v2):
    engine.run_all(tasks)
    spk2 = copy.deepcopy(speakers)
    spk2["spk_alpha"]["f0_base"] += 10.0
    inc = Engine(engine.cache, spk2, cfg_v2)
    run = inc.run_all(tasks)
    alpha_utts = {t["utterance_id"] for t in tasks
                  if t["speaker_id"] == "spk_alpha"}
    assert set(run["affected_utterances"]) == alpha_utts
    # 受影响 utterance 仅重算 acoustic 及后缀，normalize/phonemes 复用
    for utt in alpha_utts:
        recomputed = set(run["results"][utt]["recomputed"])
        assert recomputed == {"acoustic", "vocoder", "align"}
        assert set(run["results"][utt]["reused"]) == {"normalize", "phonemes"}
    # 与从零全量合成逐字节一致
    full = Engine(ContentCache(str(tmp_path / "full")), spk2, cfg_v2)
    full_run = full.run_all(tasks)
    for utt in run["results"]:
        assert run["results"][utt]["pcm"] == full_run["results"][utt]["pcm"]


def test_incremental_style_change(engine, tasks, cfg_v2, tmp_path, speakers):
    engine.run_all(tasks)
    cfg2 = copy.deepcopy(cfg_v2)
    cfg2["style"] = {"tag": "excited", "inherit": None}
    run = Engine(engine.cache, speakers, cfg2).run_all(tasks)
    assert len(run["affected_utterances"]) == len(tasks)
    full = Engine(ContentCache(str(tmp_path / "f2")), speakers, cfg2)
    full_run = full.run_all(tasks)
    for utt in run["results"]:
        assert run["results"][utt]["pcm"] == full_run["results"][utt]["pcm"]


def test_incremental_vocoder_bump(engine, tasks, cfg_v2):
    engine.run_all(tasks)
    cfg2 = copy.deepcopy(cfg_v2)
    cfg2["vocoder"] = dict(cfg2["vocoder"], version="griffin_v2",
                           params={"n_fft": 1024, "hop": 128})
    run = Engine(engine.cache, speakers=engine.speakers,
                 config=cfg2).run_all(tasks)
    for res in run["results"].values():
        assert set(res["recomputed"]) >= {"vocoder", "align"}
        assert "normalize" in res["reused"]


def test_resource_limit_utterances(tasks, speakers, cfg_v2, tmp_path):
    limits = Limits(max_utterances_per_recompute=1)
    engine = Engine(ContentCache(str(tmp_path / "c")), speakers, cfg_v2,
                    limits=limits)
    with pytest.raises(TtsError) as ei:
        engine.run_all(tasks)
    assert ei.value.code == E_RESOURCE_LIMIT


def test_resource_limit_degrade(tasks, speakers, cfg_v2, tmp_path):
    limits = Limits(max_utterances_per_recompute=1, max_parallel_nodes=1,
                    degrade=True)
    engine = Engine(ContentCache(str(tmp_path / "c")), speakers, cfg_v2,
                    limits=limits)
    run = engine.run_all(tasks, parallel=8)
    assert "utterance_batch_split" in run["degraded"]
    assert "parallel_clamped" in run["degraded"]


def test_cache_entry_limit(tasks, speakers, cfg_v2, tmp_path):
    cache = ContentCache(str(tmp_path / "c"), max_entries=1)
    engine = Engine(cache, speakers, cfg_v2)
    with pytest.raises(TtsError) as ei:
        engine.run_all(tasks[:1])
    assert ei.value.code == E_RESOURCE_LIMIT


def test_cache_entry_limit_evict(tasks, speakers, cfg_v2, tmp_path):
    cache = ContentCache(str(tmp_path / "c"), max_entries=3, on_limit="evict")
    engine = Engine(cache, speakers, cfg_v2)
    engine.run_all(tasks[:1])
    assert cache.stats()["entries"] <= 3
    assert "cache_evict" in cache.stats()["degraded"]
