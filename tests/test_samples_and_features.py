from __future__ import annotations

import numpy as np
import pytest

from dialog_emotion.canonical import canonical_bytes, content_hash
from dialog_emotion.config import PipelineConfig, ResourceLimits
from dialog_emotion.errors import BusinessError
from dialog_emotion.features import feature_product
from dialog_emotion.samples import generate_bundle, load_bundle, write_bundle
from dialog_emotion.synthesis import synthesize_dialog


def test_synthesis_is_byte_deterministic() -> None:
    left = generate_bundle(
        duration_ms_by_dialog={"dlg-001": 900},
        dialog_seeds={"dlg-001": 77},
        speaker_ids=["spk_a", "spk_b"],
        frame_variants=((20.0, 10.0),),
    )
    right = generate_bundle(
        duration_ms_by_dialog={"dlg-001": 900},
        dialog_seeds={"dlg-001": 77},
        speaker_ids=["spk_a", "spk_b"],
        frame_variants=((20.0, 10.0),),
    )
    assert left.dialogs["dlg-001"].pcm.tobytes() == right.dialogs["dlg-001"].pcm.tobytes()
    assert content_hash(
        left.dialogs["dlg-001"].truth_variants
    ) == content_hash(right.dialogs["dlg-001"].truth_variants)


def test_write_and_load_preserve_samples(bundle, tmp_path) -> None:
    summary = write_bundle(bundle, tmp_path / "samples")
    loaded = load_bundle(tmp_path / "samples")
    assert summary["dialog_count"] == 2
    for dialog_id, dialog in bundle.dialogs.items():
        assert (
            loaded.dialogs[dialog_id].pcm.tobytes()
            == dialog.pcm.tobytes()
        )
        assert loaded.dialogs[dialog_id].truth_variants == dialog.truth_variants


def test_feature_frame_count_matches_truth_and_is_deterministic(bundle) -> None:
    config = bundle.base_config
    dialog = bundle.dialogs["dlg-001"]
    truth_rows = dialog.truth_variants[config.frame_variant_key()]
    first = feature_product("dlg-001", dialog.pcm, config, len(truth_rows))
    second = feature_product("dlg-001", dialog.pcm, config, len(truth_rows))
    assert first["frame_count"] == len(truth_rows) == 179
    assert first["frame_samples"] == 320
    assert first["hop_samples"] == 160
    assert canonical_bytes(first) == canonical_bytes(second)
    assert first["feature_matrix_hash"] == second["feature_matrix_hash"]


def test_feature_frame_mismatch_has_actual_count(bundle) -> None:
    config = bundle.base_config
    dialog = bundle.dialogs["dlg-001"]
    truth_rows = dialog.truth_variants[config.frame_variant_key()]
    with pytest.raises(BusinessError) as captured:
        feature_product(
            "dlg-001", dialog.pcm, config, len(truth_rows) + 1
        )
    assert captured.value.code == "E_FRAME_MISMATCH"
    assert captured.value.details["dialog_id"] == "dlg-001"
    assert captured.value.details["actual_frame_count"] == len(truth_rows)


def test_feature_resident_memory_limit(bundle) -> None:
    dialog = bundle.dialogs["dlg-001"]
    config = PipelineConfig(
        limits=ResourceLimits(max_resident_feature_bytes=8)
    )
    truth_rows = dialog.truth_variants[config.frame_variant_key()]
    with pytest.raises(BusinessError) as captured:
        feature_product("dlg-001", dialog.pcm, config, len(truth_rows))
    assert captured.value.code == "E_RESOURCE_LIMIT"


def test_pcm_is_16k_mono_int16(bundle) -> None:
    dialog = bundle.dialogs["dlg-001"]
    assert dialog.sample_rate == 16_000
    assert dialog.pcm.dtype == np.dtype("<i2")
    assert len(dialog.pcm) == dialog.duration_ms * 16
