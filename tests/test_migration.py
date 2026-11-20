from __future__ import annotations

from rope_attention.config import parse_config
from rope_attention.data import lossy_v3_config, sample_configs
from rope_attention.errors import E_SEMANTIC_DRIFT
from rope_attention.migration import migrate


def test_promotion_fills_defaults_with_explicit_sources() -> None:
    result = migrate(parse_config(sample_configs()["v1"]), "v3")
    assert result.business_code == "OK"
    assert result.chain == ("v1->v2", "v2->v3")
    filled = {item["field"] for item in result.fill_details}
    assert filled == {"variant", "frequency_adjustment", "per_head_scale"}
    assert result.target_config is not None
    assert result.target_config.source_version == "v3"
    assert result.target_config.effective_variant == "rope"
    assert result.field_provenance["block_size"] == "retained:source"


def test_lossless_demotion_still_reports_removed_schema_fields() -> None:
    result = migrate(parse_config(sample_configs()["v3"]), "v2")
    assert result.business_code == "OK"
    assert not result.semantic_drift
    fields = {item["field"] for item in result.discard_details}
    assert fields == {"frequency_adjustment", "per_head_scale"}
    assert all(item["recoverable"] for item in result.discard_details)
    assert result.target_config is not None
    assert result.target_config.source_version == "v2"


def test_lossy_demotion_returns_semantic_drift_and_loss_details() -> None:
    result = migrate(parse_config(lossy_v3_config()), "v2")
    assert result.business_code == E_SEMANTIC_DRIFT
    assert result.semantic_drift
    assert set(result.unrecoverable_fields) == {
        "variant",
        "frequency_adjustment",
        "per_head_scale",
    }
    rules = {item["rule"] for item in result.discard_details}
    assert "nonuniform_head_scale_flattened_to_mean_and_residuals_dropped" in rules
    assert "v2_cannot_express_position_blockwise_frequency_adjustment" in rules


def test_non_rope_v2_to_v1_is_explicit_semantic_drift() -> None:
    source = parse_config(
        "v2\nbase_theta=10000.0|pos_scale=1.0|block_size=6|variant=ntk\n"
    )
    result = migrate(source, "v1")
    assert result.business_code == E_SEMANTIC_DRIFT
    assert result.unrecoverable_fields == ("variant",)
    assert result.target_config is not None
    assert result.target_config.source_version == "v1"


def test_strict_mode_does_not_emit_an_unrecoverable_target() -> None:
    result = migrate(
        parse_config(lossy_v3_config()),
        "v2",
        allow_semantic_drift=False,
    )
    assert not result.ok
    assert result.target_text is None
    assert result.target_config is None
    assert result.business_code == E_SEMANTIC_DRIFT


def test_demoted_config_is_parseable_again() -> None:
    for source_version, target_version in (
        ("v1", "v2"),
        ("v2", "v3"),
        ("v3", "v2"),
        ("v2", "v1"),
        ("v1", "v3"),
        ("v3", "v1"),
    ):
        result = migrate(
            parse_config(sample_configs()[source_version]),
            target_version,
        )
        assert result.target_text is not None
        reparsed = parse_config(result.target_text)
        assert reparsed.source_version == target_version

