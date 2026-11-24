from __future__ import annotations

from alert_engine.engine import InputPaths
from alert_engine.rules import RuleCatalog


def _catalog(sample_dir):
    parsed = InputPaths.from_directory(sample_dir).load()
    return parsed, RuleCatalog(
        v1_versions=parsed.v1_versions,
        v2_versions=parsed.v2_versions,
        patch_batches=parsed.patch_batches,
        observation_clock_ms=parsed.observation_clock_ms,
    )


def test_cross_generation_conflict_identifies_field_and_generations(sample_dir):
    _, catalog = _catalog(sample_dir)
    cred = [
        item for item in catalog.conflicts if item["rule_key"] == "R-CRED"
    ]
    fields = {item["field"] for item in cred}
    assert fields == {"dedup_window_ms", "enabled"}
    for item in cred:
        assert item["left"]["generation"] == 1
        assert item["right"]["generation"] == 2
        assert item["left"]["source"] == "v1"
        assert item["right"]["source"] == "v2"


def test_tombstone_covers_earlier_non_tombstone(sample_dir):
    parsed, catalog = _catalog(sample_dir)

    before = catalog.active_version(
        "R-TOMB", parsed.observation_clock_ms + 20_000
    )
    after = catalog.active_version(
        "R-TOMB", parsed.observation_clock_ms + 31_000
    )
    assert before is not None and before.tombstone is False
    assert after is not None and after.tombstone is True
    assert after.generation == 3


def test_authority_prefers_latest_effective_then_generation(sample_dir):
    parsed, catalog = _catalog(sample_dir)
    patch_version = catalog.active_version(
        "R-PATCH", parsed.observation_clock_ms + 30_000
    )
    assert patch_version is not None
    assert patch_version.source == "patch"
    assert patch_version.generation == 4
    assert patch_version.dedup_window_ms == 1_000


def test_version_overlap_is_ambiguous_not_arbitrarily_bound(sample_dir):
    parsed, catalog = _catalog(sample_dir)
    resolution = catalog.resolve(
        "R-OVERLAP", parsed.observation_clock_ms + 40_500
    )
    assert resolution.status == "ambiguous"
    assert resolution.reason_code == "E_VERSION_AMBIGUOUS"
    assert {item.source for item in resolution.candidates} == {"v1", "v2"}
    assert {item.generation for item in resolution.candidates} == {1, 2}

    resolved = catalog.resolve(
        "R-OVERLAP", parsed.observation_clock_ms + 42_000
    )
    assert resolved.status == "resolved"
    assert resolved.version is not None
    assert resolved.version.source == "v2"
    assert resolved.version.generation == 2


def test_dependency_closure_follows_parent_rule_keys(sample_dir):
    _, catalog = _catalog(sample_dir)
    closure = catalog.dependency_closure({"R-PARENT"})
    assert "R-PARENT" in closure
    assert "R-CHILD" in closure
