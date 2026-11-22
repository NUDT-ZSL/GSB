from __future__ import annotations

from dataclasses import replace

import pytest

from panoweave.errors import BusinessError, ErrorCode
from panoweave.formats import parse_text, serialize_v3
from panoweave.injections import apply_text_injection, InjectionSpec
from panoweave.migration import (
    assert_representable_equality,
    assert_semantic_equivalence,
    check_roundtrip,
    migrate_scene,
    v1_inexpressible_fields,
)
from panoweave.model import DistortionStage, ExposureCurve
from panoweave.samples import parameter_text


def test_three_versions_resolve_to_same_internal_representation(sample):
    parsed = sample["parsed"]
    hashes = {version: item.scene.semantic_hash for version, item in parsed.items()}
    assert len(set(hashes.values())) == 1
    result = assert_semantic_equivalence(
        list(parsed.values()), sample["config"].parse_defaults
    )
    assert result["equivalent"] is True
    assert parsed["v1"].completions
    assert parsed["v2"].completions
    assert parsed["v3"].completions == ()


def test_unknown_version_is_rejected(sample):
    text = parameter_text(sample["root"], "v1").replace("PSM-V1", "PSM-V9")
    with pytest.raises(BusinessError) as caught:
        parse_text(text, sample["config"].parse_defaults)
    assert caught.value.code == ErrorCode.VERSION_UNSUPPORTED
    assert caught.value.field_path == "version"


def test_v1_wrong_extrinsic_dimension_reports_field_offset(sample):
    text = parameter_text(sample["root"], "v1")
    injected = apply_text_injection(
        text, InjectionSpec("parse", "extrinsic_dimension", 0)
    )
    with pytest.raises(BusinessError) as caught:
        parse_text(injected, sample["config"].parse_defaults)
    error = caught.value
    assert error.code == ErrorCode.FORMAT_CORRUPT
    assert error.field_path == "views[0].extrinsic"
    assert error.char_offset is not None and error.char_offset > 0
    assert error.details["expected"] == 12
    assert error.details["actual"] == 11


def test_v1_field_count_error_reports_column_and_offset(sample):
    lines = parameter_text(sample["root"], "v1").splitlines()
    lines[1] = "|".join(lines[1].split("|")[:-1])
    with pytest.raises(BusinessError) as caught:
        parse_text("\n".join(lines) + "\n", sample["config"].parse_defaults)
    error = caught.value
    assert error.code == ErrorCode.FORMAT_CORRUPT
    assert "views[0]" in (error.field_path or "")
    assert error.char_offset is not None
    assert error.details["expected"] == 7
    assert error.details["actual"] == 6


def test_v2_missing_required_field_is_rejected(sample):
    text = parameter_text(sample["root"], "v2").replace(
        "feather_radius=8\n", ""
    )
    with pytest.raises(BusinessError) as caught:
        parse_text(text, sample["config"].parse_defaults)
    error = caught.value
    assert error.code == ErrorCode.FORMAT_CORRUPT
    assert error.field_path == "feather_radius"
    assert error.char_offset is not None


def test_v3_unparseable_number_is_rejected_with_offset(sample):
    text = parameter_text(sample["root"], "v3")
    injected = apply_text_injection(
        text, InjectionSpec("parse", "unparseable_number", 0)
    )
    with pytest.raises(BusinessError) as caught:
        parse_text(injected, sample["config"].parse_defaults)
    error = caught.value
    assert error.code == ErrorCode.FORMAT_CORRUPT
    assert "focal" in (error.field_path or "")
    assert error.char_offset is not None


def test_upgrade_fills_and_records_sources(sample):
    v1_to_v2 = migrate_scene(
        sample["parsed"]["v1"], "v2", sample["config"].parse_defaults
    )
    paths = {item.field_path for item in v1_to_v2.items}
    assert "views[0].homography" in paths
    assert "views[0].overlap_weight" in paths
    assert "views[0].exposure_mode" in paths
    assert "feather_radius" in paths
    assert all(item.source for item in v1_to_v2.items)
    v1_to_v3 = migrate_scene(
        sample["parsed"]["v1"], "v3", sample["config"].parse_defaults
    )
    v3_paths = {item.field_path for item in v1_to_v3.items}
    assert "views[0].distortion_chain" in v3_paths
    assert "views[0].exposure_curve" in v3_paths


def test_downgrade_flattens_chain_and_drops_unrepresentable_curve(sample):
    base = sample["parsed"]["v3"].scene
    first = base.views[0]
    chain = first.distortion_chain + (
        DistortionStage("scale", (("sx", 1.03), ("sy", 1.03))),
    )
    custom_curve = ExposureCurve(
        "linear_gain", first.exposure_gain, gamma=2.2
    )
    views = list(base.views)
    views[0] = replace(
        first,
        distortion_chain=chain,
        exposure_curve=custom_curve,
    )
    custom_scene = replace(base, feather_radius=13.5, views=tuple(views))
    custom = parse_text(
        serialize_v3(custom_scene), sample["config"].parse_defaults
    )
    result = migrate_scene(custom, "v2", sample["config"].parse_defaults)
    assert len(result.scene.views[0].distortion_chain) == 1
    assert result.scene.views[0].distortion_chain[0].model == "radial"
    assert "views[0].distortion_chain[1]" in result.dropped_fields
    assert "views[0].exposure_curve.gamma" in result.dropped_fields
    lowered = migrate_scene(custom, "v1", sample["config"].parse_defaults)
    assert "feather_radius" in lowered.dropped_fields
    assert lowered.scene.feather_radius == sample["config"].parse_defaults.feather_radius


def test_semantic_drift_lists_v1_unrecoverable_fields(sample):
    base = sample["parsed"]["v3"].scene
    first = base.views[0]
    views = list(base.views)
    views[0] = replace(
        first,
        distortion_chain=first.distortion_chain
        + (DistortionStage("scale", (("sx", 1.01), ("sy", 1.01))),),
    )
    custom_scene = replace(base, feather_radius=12.0, views=tuple(views))
    custom = parse_text(
        serialize_v3(custom_scene), sample["config"].parse_defaults
    )
    drift_fields = v1_inexpressible_fields(
        custom, sample["config"].parse_defaults
    )
    assert any(item["path"] == "$.feather_radius" for item in drift_fields)
    with pytest.raises(BusinessError) as caught:
        assert_semantic_equivalence(
            [sample["parsed"]["v1"], custom],
            sample["config"].parse_defaults,
        )
    error = caught.value
    assert error.code == ErrorCode.SEMANTIC_DRIFT
    assert "$.feather_radius" in error.details["unrecoverable_fields"]


def test_roundtrip_reports_pass_and_mismatch(sample):
    for source_version in ("v1", "v2", "v3"):
        for via_version in ("v1", "v2", "v3"):
            report = check_roundtrip(
                sample["parsed"][source_version],
                via_version,
                sample["config"].parse_defaults,
            )
            assert report["representable_equal"] is True
    altered_views = list(sample["parsed"]["v2"].scene.views)
    altered_views[0] = replace(
        altered_views[0], focal=altered_views[0].focal + 0.5
    )
    altered = replace(
        sample["parsed"]["v2"].scene, views=tuple(altered_views)
    )
    with pytest.raises(BusinessError) as caught:
        assert_representable_equality(
            sample["parsed"]["v2"].scene,
            altered,
            "v1",
            source_version="v2",
        )
    assert caught.value.code == ErrorCode.ROUNDTRIP_MISMATCH
    assert caught.value.field_path == "$.views[0].focal"

