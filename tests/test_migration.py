from __future__ import annotations

import pytest

from ctr_cvr_joint.migration import migrate_spec, replay_migration, replay_to_text
from ctr_cvr_joint.specs import load_spec_bytes, normalize_spec, serialize_spec


def test_every_upgrade_and_downgrade_replays(fixtures):
    sources = {
        "v1_to_v2": (fixtures["spec_v1"], 2),
        "v1_to_v3": (fixtures["spec_v1"], 3),
        "v2_to_v3": (fixtures["spec_v2"], 3),
        "v2_to_v1": (fixtures["spec_v2"], 1),
        "v3_to_v2": (fixtures["spec_v3"], 2),
        "v3_to_v1": (fixtures["spec_v3"], 1),
    }
    for name, (source, target) in sources.items():
        result = migrate_spec(source, target)
        assert result.replay_ok, name
        replayed = replay_migration(source.spec, result.records, target)
        assert replayed.to_dict() == result.target_spec.to_dict(), name
        text, checksum = replay_to_text(source.spec, result.records, target)
        assert text == result.target_text
        assert checksum == result.target_checksum


def test_upgrade_fills_records_with_sources(fixtures):
    result = migrate_spec(fixtures["spec_v1"], 2)
    assert result.fills
    paths = {record.path for record in result.fills}
    assert "numeric.ctr_base.missing_strategy" in paths
    assert "numeric.ctr_base.default" in paths
    assert "statistics.ctr.semantics_name" in paths
    assert all(record.source for record in result.fills)
    default_record = next(
        record
        for record in result.fills
        if record.path == "numeric.ctr_base.default"
    )
    assert default_record.target_expressible is False


def test_downgrade_records_are_explicit_drops(fixtures):
    to_v2 = migrate_spec(fixtures["spec_v3"], 2)
    to_v1 = migrate_spec(fixtures["spec_v3"], 1)
    assert to_v2.drops
    assert to_v1.drops
    paths_v1 = {record.path for record in to_v1.drops}
    assert "numeric.ctr_base.missing_strategy" in paths_v1
    assert "numeric.ctr_base.default" in paths_v1
    assert "statistics.ctr.semantics_version" in paths_v1
    assert all(record.op == "drop" for record in to_v1.drops)


def test_migrated_text_loads_to_same_feature_view(fixtures):
    for source_key in ("spec_v1", "spec_v2", "spec_v3"):
        source = fixtures[source_key]
        for target in (1, 2, 3):
            result = migrate_spec(source, target)
            reparsed = load_spec_bytes(result.target_text.encode("utf-8"))
            expected = normalize_spec(result.target_spec).to_dict()
            actual = normalize_spec(reparsed.spec).to_dict()
            assert actual["numeric"] == expected["numeric"]
            assert actual["categorical"] == expected["categorical"]
            assert actual["cross"] == expected["cross"]
            assert actual["statistics"] == expected["statistics"]


def test_serialize_roundtrip_preserves_target_version(fixtures):
    for version, spec in (
        (1, fixtures["spec_v1"]),
        (2, fixtures["spec_v2"]),
        (3, fixtures["spec_v3"]),
    ):
        text = serialize_spec(spec.spec, version)
        assert load_spec_bytes(text.encode("utf-8")).version == version

