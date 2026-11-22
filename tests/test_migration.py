from __future__ import annotations

import pytest

from ner_governance.errors import (
    E_SEMANTIC_DRIFT,
    GovernanceError,
)
from ner_governance.migration import migrate_type_set, render_type_set
from ner_governance.models import AliasDefinition, TypeDefinition, TypeSet
from ner_governance.typesets import load_type_set, read_type_set_text


def test_all_sample_migration_pairs_replay(sample_paths, tmp_path):
    config = load_type_set  # keep signature-local imports explicit below
    from ner_governance.config import load_config

    cfg = load_config(sample_paths["config"])
    views = {
        name: load_type_set(sample_paths[name]) for name in ("v1", "v2", "v3")
    }
    assert config is load_type_set
    for source_name in views:
        for target_name in ("v1", "v2", "v3"):
            if source_name == target_name:
                continue
            first = migrate_type_set(views[source_name], target_name, cfg)
            second = migrate_type_set(views[source_name], target_name, cfg)
            assert first.replay_ok
            assert first.lossless
            assert first.text == second.text
            assert first.fingerprint_after_replay == first.target_fingerprint
            assert first.fingerprint_after_replay == (
                views[source_name].semantic_fingerprint()
            )


def test_v1_upgrade_records_default_completions(sample_paths):
    from ner_governance.config import load_config

    cfg = load_config(sample_paths["config"])
    v1 = load_type_set(sample_paths["v1"])
    to_v2 = migrate_type_set(v1, "v2", cfg)
    to_v3 = migrate_type_set(v1, "v3", cfg)
    assert len(to_v2.completions) == 9
    assert len(to_v3.completions) == 14
    assert all(item["source_line"] >= 2 for item in to_v2.completions)
    assert {item["source"] for item in to_v2.completions} == {
        "v1_implicit_default"
    }
    metadata_completions = [
        item for item in to_v3.completions if item["item"].endswith(".metadata")
    ]
    assert len(metadata_completions) == 5
    assert all(item["value"] == {} for item in metadata_completions)


def test_v3_downgrade_flattens_nested_alias_rows(sample_paths):
    from ner_governance.config import load_config

    cfg = load_config(sample_paths["config"])
    v3 = load_type_set(sample_paths["v3"])
    result = migrate_type_set(v3, "v1", cfg)
    assert result.lossless
    assert result.replay_ok
    assert len(result.drops) == 5
    assert {item["action"] for item in result.drops} == {
        "representation_flattened"
    }
    assert all(item["source_line"] is not None for item in result.drops)
    assert result.text == sample_paths["v1"].read_text(encoding="utf-8")


def _lossy_v3() -> TypeSet:
    return TypeSet(
        format_version="v3",
        label_set_version="ls-test",
        types=[
            TypeDefinition(
                name="PERSON",
                bio_prefix_required=True,
                enabled=True,
                aliases=[
                    AliasDefinition(
                        "PER",
                        "PERSON",
                        enabled=False,
                        metadata={"locale": "zh"},
                        source_line=5,
                    ),
                    AliasDefinition(
                        "PERSON_NAME",
                        "PERSON",
                        enabled=True,
                        metadata={"nested": {"scope": "news"}},
                        source_line=6,
                    ),
                ],
                source_line=3,
            )
        ],
    )


@pytest.mark.parametrize(
    ("nested", "disabled", "remaining_aliases", "lossless"),
    [
        ("discard", "discard", 0, False),
        ("flatten", "flatten", 2, False),
    ],
)
def test_lossy_downgrade_details(
    nested,
    disabled,
    remaining_aliases,
    lossless,
):
    from tests.conftest import make_config

    cfg = make_config(
        downgrade_policy={
            "nested_aliases": nested,
            "disabled_aliases": disabled,
            "bio_prefix_required": "flatten",
        }
    )
    source = _lossy_v3()
    result = migrate_type_set(source, "v1", cfg)
    assert result.replay_ok
    assert result.lossless is lossless
    assert len(result.drops) >= 2
    assert all(item["source_line"] in (5, 6) for item in result.drops)
    assert sum(1 for item in result.drops if item["lossy"]) >= 2
    rendered = read_type_set_text(result.text)
    assert sum(len(item.aliases) for item in rendered.types) == remaining_aliases
    if nested == "discard":
        assert any(
            item["action"] == "discarded_alias" for item in result.drops
        )


def test_v1_bio_false_reject_is_explicit():
    from tests.conftest import make_config

    source = TypeSet(
        format_version="v3",
        label_set_version="ls-test",
        types=[
            TypeDefinition(
                name="PERSON",
                bio_prefix_required=False,
                enabled=True,
                source_line=2,
            )
        ],
    )
    cfg = make_config(
        downgrade_policy={
            "nested_aliases": "flatten",
            "disabled_aliases": "discard",
            "bio_prefix_required": "reject",
        }
    )
    with pytest.raises(GovernanceError) as raised:
        migrate_type_set(source, "v1", cfg)
    assert raised.value.code == E_SEMANTIC_DRIFT
    assert raised.value.details["field"] == (
        "types[PERSON].bio_prefix_required"
    )


def test_render_v1_rejects_unhandled_metadata(sample_paths):
    source = _lossy_v3()
    with pytest.raises(GovernanceError):
        render_type_set(source, "v1")
