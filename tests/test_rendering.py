from __future__ import annotations

import numpy as np
import pytest

from panoweave.cache import ContentAddressedCache
from panoweave.errors import BusinessError, ErrorCode
from panoweave.formats import parse_text, serialize_scene
from panoweave.injections import InjectionSpec
from panoweave.migration import migrate_scene
from panoweave.pipeline import render_text, validate_fusion
from panoweave.pngutil import encode_png
from panoweave.samples import parameter_text


def _render(sample, version, tmp_path, injections=()):
    cache = ContentAddressedCache(tmp_path / f"cache_{version}", 64)
    return render_text(
        parameter_text(sample["root"], version),
        sample["images"],
        config=sample["config"],
        cache=cache,
        injections=injections,
    )


def test_three_versions_render_byte_identical_panorama_and_mask(sample, tmp_path):
    results = {
        version: _render(sample, version, tmp_path)
        for version in ("v1", "v2", "v3")
    }
    panorama_bytes = {
        version: encode_png(result.panorama_rgb)
        for version, result in results.items()
    }
    assert len(set(panorama_bytes.values())) == 1
    assert len({result.panorama_hash for result in results.values()}) == 1
    assert len({result.mask_hash for result in results.values()}) == 1
    assert len({result.weight_hash for result in results.values()}) == 1
    assert all(
        result.view_ids == (0, 1, 2) for result in results.values()
    )
    assert results["v1"].valid_mask.sum() > 0
    assert results["v1"].valid_mask.sum() < results["v1"].valid_mask.size


def test_fixed_seed_and_cleared_cache_reproduce_exact_bytes(sample, tmp_path):
    cache_dir = tmp_path / "cache"
    cache = ContentAddressedCache(cache_dir, 64)
    first = render_text(
        parameter_text(sample["root"], "v1"),
        sample["images"],
        config=sample["config"],
        cache=cache,
    )
    first_png = encode_png(first.panorama_rgb)
    cache.clear()
    second = render_text(
        parameter_text(sample["root"], "v1"),
        sample["images"],
        config=sample["config"],
        cache=cache,
    )
    assert encode_png(second.panorama_rgb) == first_png
    assert np.array_equal(first.valid_mask, second.valid_mask)
    assert first.panorama_hash == second.panorama_hash
    assert first.mask_hash == second.mask_hash
    assert first.geometry_hash == second.geometry_hash


def test_serialized_migrations_render_identically(sample, tmp_path):
    reference = _render(sample, "v3", tmp_path / "reference")
    for target in ("v1", "v2", "v3"):
        migration = migrate_scene(
            sample["parsed"]["v3"], target, sample["config"].parse_defaults
        )
        migrated_text = serialize_scene(migration.scene, target)
        reparsed = parse_text(migrated_text, sample["config"].parse_defaults)
        assert reparsed.scene.semantic_hash == reference.scene_hash
        rendered = render_text(
            migrated_text,
            sample["images"],
            config=sample["config"],
            cache=ContentAddressedCache(tmp_path / f"migrated_{target}", 16),
        )
        assert rendered.panorama_hash == reference.panorama_hash
        assert rendered.mask_hash == reference.mask_hash


def test_geometry_injection_skips_view_and_traces_degradation(sample, tmp_path):
    result = _render(
        sample,
        "v3",
        tmp_path,
        injections=[
            InjectionSpec("geometry", "singular_homography", view_id=0)
        ],
    )
    assert result.skipped_views == (0,)
    report = result.ledger.report()
    assert report["stage_records"][0]["code"] == ErrorCode.HOMOGRAPHY_SINGULAR
    assert report["degradations"][0]["category"] == "skip_view"
    assert report["degradations"][0]["upstream_code"] == ErrorCode.HOMOGRAPHY_SINGULAR
    assert report["degradation_chain_traceable"] is True
    assert report["all_attributed"] is True
    assert result.valid_mask.any()


def test_gain_and_feather_injections_fallback(sample, tmp_path):
    gain = _render(
        sample,
        "v3",
        tmp_path,
        injections=[InjectionSpec("gain", "zero_gain", view_id=1)],
    )
    gain_report = gain.ledger.report()
    assert gain_report["stage_records"][0]["code"] == ErrorCode.GAIN_INVALID
    assert gain_report["degradations"][0]["category"] == "unit_gain_fallback"
    feather = _render(
        sample,
        "v3",
        tmp_path / "feather",
        injections=[InjectionSpec("blend", "negative_feather")],
    )
    feather_report = feather.ledger.report()
    assert feather_report["stage_records"][0]["code"] == ErrorCode.FEATHER_INVALID
    assert feather_report["degradations"][0]["category"] == "radius_zero_fallback"
    assert feather.valid_mask.any()


def test_fusion_validation_returns_required_codes_and_positions():
    shape = (4, 5)
    valid = np.ones(shape, dtype=bool)
    zero_rgb = np.zeros((*shape, 3), dtype=np.float64)

    weight_sum = np.ones(shape, dtype=np.float64)
    weight_sum[1, 2] = 0.0
    with pytest.raises(BusinessError) as gap:
        validate_fusion(weight_sum, valid, zero_rgb, 1e-9)
    assert gap.value.code == ErrorCode.OVERLAP_GAP
    assert gap.value.details["positions"][0] == [1, 2]

    invalid_expected = valid.copy()
    invalid_expected[0, 0] = False
    filled = zero_rgb.copy()
    filled[0, 0] = (0.2, 0.3, 0.4)
    with pytest.raises(BusinessError) as invalid:
        validate_fusion(
            np.ones(shape), invalid_expected, filled, 1e-9
        )
    assert invalid.value.code == ErrorCode.INVALID_FILLED
    assert invalid.value.details["positions"][0] == [0, 0]

    half = np.full(shape, 0.5, dtype=np.float64)
    with pytest.raises(BusinessError) as weights:
        validate_fusion(half, valid, zero_rgb, 1e-9)
    assert weights.value.code == ErrorCode.WEIGHT_SUM
    assert weights.value.details["positions"]

