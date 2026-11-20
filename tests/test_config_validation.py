"""Requirement 7: configuration and input validation with explicit fields."""

from __future__ import annotations

import pytest

from seraph_core.config.config import EngineConfig
from seraph_core.errors import EngineError


@pytest.mark.parametrize("field,value", [
    ("sample_rate", 8000),
    ("frame_ms", 0.0),
    ("hop_ms", 25.0),
    ("precision", "bfloat16"),
    ("fixed_scale", 0),
    ("fixed_scale", 2 ** 40),
    ("regularization", 5.0),
    ("regularization", -0.1),
    ("convergence_tolerance", 0.0),
    ("convergence_tolerance", -1.0),
    ("epochs", 0),
    ("learning_rate", 0.0),
    ("lr_scale", -1.0),
    ("feature_scale", 0.0),
    ("chunk_size", -2),
    ("min_silence_frames", 0),
])
def test_invalid_fields_raise_config_error(field, value):
    config = EngineConfig()
    setattr(config, field, value)
    with pytest.raises(EngineError) as exc:
        config.validate()
    assert exc.value.code == "E_CONFIG_INVALID"
    assert exc.value.field == field


def test_unknown_field_rejected():
    with pytest.raises(EngineError) as exc:
        EngineConfig.from_dict({"precision": "float64", "mystery": 1})
    assert exc.value.code == "E_CONFIG_INVALID"
    assert exc.value.field == "mystery"


def test_unknown_precision_message():
    with pytest.raises(EngineError) as exc:
        EngineConfig.from_dict({"precision": "int4"})
    assert exc.value.field == "precision"


def test_frame_lengths_are_exact_samples():
    config = EngineConfig(sample_rate=16000, frame_ms=20.0, hop_ms=10.0)
    assert config.frame_length == 320
    assert config.hop_length == 160


def test_non_aligned_frame_ms_rejected():
    with pytest.raises(EngineError) as exc:
        EngineConfig(frame_ms=20.01).validate()
    assert exc.value.field == "frame_ms"


def test_canonical_json_is_stable():
    config_a = EngineConfig()
    config_b = EngineConfig()
    assert config_a.canonical_json() == config_b.canonical_json()


def test_resource_limit_field_validation():
    config = EngineConfig()
    config.resource_limits.max_dialogs = 0
    with pytest.raises(EngineError) as exc:
        config.validate()
    assert exc.value.field == "resource_limits.max_dialogs"
