import pytest

from tts_app.config import load_config
from tts_app.errors import (E_FORMAT_CORRUPT, E_VERSION_UNSUPPORTED,
                            TtsError)


def test_v1_ok():
    cfg = load_config("V1|spk_a|neutral|1.0|fp32|griffin_v1")
    assert cfg["source_version"] == "v1"
    assert cfg["speaker"] == "spk_a"
    assert cfg["prosody"]["scale"] == 1.0


def test_v2_ok():
    cfg = load_config(
        "V2|speaker=spk_a|style=calm|prosody_scale=1.1|precision=fp16"
        "|vocoder=griffin_v1|duration_scale=0.9")
    assert cfg["source_version"] == "v2"
    assert cfg["prosody"]["duration_scale"] == 0.9
    assert cfg["precision"] == "fp16"


def test_v3_ok():
    cfg = load_config(
        '{"version":"v3","speaker":"s","style":{"tag":"neutral"},'
        '"prosody":{"scale":1.0},"precision":"fp32",'
        '"vocoder":{"version":"griffin_v1"}}')
    assert cfg["source_version"] == "v3"
    assert cfg["vocoder"]["params"] == {"n_fft": 512, "hop": 128}


def test_unknown_version():
    with pytest.raises(TtsError) as ei:
        load_config("V9|whatever")
    assert ei.value.code == E_VERSION_UNSUPPORTED


def test_v1_truncated_header():
    with pytest.raises(TtsError) as ei:
        load_config("V1spk_a|neutral|1.0|fp32|griffin_v1")
    assert ei.value.code in (E_FORMAT_CORRUPT, E_VERSION_UNSUPPORTED)


def test_v1_field_count_mismatch():
    with pytest.raises(TtsError) as ei:
        load_config("V1|spk_a|neutral|1.0")
    assert ei.value.code == E_FORMAT_CORRUPT
    assert ei.value.details["line"] == 1
    assert ei.value.details["field"] == "field_count"


def test_v2_missing_required():
    with pytest.raises(TtsError) as ei:
        load_config("V2|speaker=spk_a|prosody_scale=1.0|precision=fp32"
                    "|vocoder=griffin_v1")
    assert ei.value.code == E_FORMAT_CORRUPT
    assert ei.value.details["field"] == "style"


def test_v2_bad_number():
    with pytest.raises(TtsError) as ei:
        load_config("V2|speaker=spk_a|style=calm|prosody_scale=abc"
                    "|precision=fp32|vocoder=griffin_v1")
    assert ei.value.code == E_FORMAT_CORRUPT
    assert ei.value.details["field"] == "prosody_scale"


def test_v3_missing_field():
    with pytest.raises(TtsError) as ei:
        load_config('{"version":"v3","speaker":"s","style":{"tag":"x"},'
                    '"precision":"fp32","vocoder":{"version":"g"}}')
    assert ei.value.code == E_FORMAT_CORRUPT
    assert ei.value.details["field"] == "prosody"
