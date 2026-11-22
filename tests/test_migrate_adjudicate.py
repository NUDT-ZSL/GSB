import pytest

from tts_app.adjudicate import adjudicate, cross_dialect_pcm_check
from tts_app.config import load_config
from tts_app.errors import E_SEMANTIC_DRIFT, TtsError
from tts_app.migrate import downgrade_to_v1, migrate, upgrade

V1 = "V1|spk_alpha|neutral|1.0|fp32|griffin_v1"
V2 = ("V2|speaker=spk_alpha|style=neutral|prosody_scale=1.0|precision=fp32"
      "|vocoder=griffin_v1|duration_scale=1.0")
V3 = ('{"version":"v3","speaker":"spk_alpha","style":{"tag":"neutral"},'
      '"prosody":{"scale":1.0,"duration_scale":1.0},"precision":"fp32",'
      '"vocoder":{"version":"griffin_v1","params":{"n_fft":512,"hop":128}}}')


def test_upgrade_fills_defaults_with_provenance():
    cfg = load_config("V1|spk_beta||1.0||griffin_v1")
    out, rec = upgrade(cfg, "v3")
    fields = {f["field"] for f in rec["filled_defaults"]}
    assert {"style.tag", "precision", "prosody.duration_scale"} <= fields
    assert all(f["source"].startswith("default:") for f in rec["filled_defaults"])
    assert rec["source_version"] == "v1" and rec["target_version"] == "v3"
    assert out["style"]["tag"] == "neutral"


def test_downgrade_drop_and_flatten():
    cfg = load_config(
        '{"version":"v3","speaker":"s","style":{"tag":"x","inherit":"base"},'
        '"prosody":{"scale":1.0,"duration_curve":[1.0,2.0]},'
        '"precision":"fp32","vocoder":{"version":"g"}}')
    out, rec = downgrade_to_v1(cfg, rules={
        "style.inherit": "drop", "prosody.duration_curve": "flatten"})
    rules = {d["field"]: d["rule"] for d in rec["dropped"]}
    assert rules == {"style.inherit": "drop",
                     "prosody.duration_curve": "flatten"}
    flat = [d for d in rec["dropped"] if d["rule"] == "flatten"][0]
    assert flat["flattened_to"] == 1.5
    assert out["prosody"]["duration_scale"] == 1.5
    assert rec["source_version"] == "v3" and rec["target_version"] == "v1"


def test_downgrade_without_rule_is_drift():
    cfg = load_config(
        '{"version":"v3","speaker":"s","style":{"tag":"x","inherit":"base"},'
        '"prosody":{"scale":1.0},"precision":"fp32","vocoder":{"version":"g"}}')
    with pytest.raises(TtsError) as ei:
        downgrade_to_v1(cfg)
    assert ei.value.code == E_SEMANTIC_DRIFT
    assert "style.inherit" in ei.value.details["unrecoverable"]


def test_migrate_same_version_noop():
    cfg = load_config(V2)
    out, rec = migrate(cfg, "v2")
    assert rec["filled_defaults"] == [] and rec["dropped"] == []
    assert out == cfg


def test_adjudicate_equivalent_dialects():
    cfg = adjudicate({"v1": V1, "v2": V2, "v3": V3})
    assert cfg["speaker"] == "spk_alpha"


def test_adjudicate_drift():
    drift_v3 = V3.replace('"duration_scale":1.0',
                          '"duration_scale":1.0,"duration_curve":[1.0,2.0]')
    with pytest.raises(TtsError) as ei:
        adjudicate({"v1": V1, "v2": V2, "v3": drift_v3})
    assert ei.value.code == E_SEMANTIC_DRIFT
    assert "prosody.duration_curve" in ei.value.details["unrecoverable"]


def test_cross_dialect_pcm_identical(speakers, tasks):
    _, res = cross_dialect_pcm_check(
        {"v1": V1, "v2": V2, "v3": V3}, tasks[0], speakers)
    assert res["pcm"] and res["prosody_check"]["ok"]
