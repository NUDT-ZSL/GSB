from xretrieval.engine import Engine
from xretrieval.store import StateStore


def test_initial_build_builds_all_partitions(state, paths):
    report = Engine(state).build(paths["v1"], paths["docs"])
    assert sorted(report["rebuilt"]) == ["de", "en", "fr", "zh"]
    assert report["reused"] == []
    current = StateStore(state).load_current()
    assert current["bridge"]["resource_version"] == 1
    assert sorted(current["partitions"]) == ["de", "en", "fr", "zh"]


def test_incremental_rebuild_only_affected(built_v1, paths):
    before = StateStore(built_v1).load_current()
    report = Engine(built_v1).build(paths["v2"], paths["docs"])
    assert report["rebuilt"] == ["de"]
    assert sorted(report["reused"]) == ["en", "fr", "zh"]
    after = StateStore(built_v1).load_current()
    assert after["partitions"]["de"]["partition_version"] == 2
    for lang in ("en", "fr", "zh"):
        assert after["partitions"][lang] == before["partitions"][lang]


def test_v3_affects_en_and_fr(built_v1, paths):
    Engine(built_v1).build(paths["v2"], paths["docs"])
    report = Engine(built_v1).build(paths["v3"], paths["docs"])
    assert sorted(report["rebuilt"]) == ["en", "fr"]
    assert sorted(report["reused"]) == ["de", "zh"]


def test_weights_only_change_rebuilds_nothing(built_v1, paths, tmp_path):
    from conftest import tweak_resource
    tweaked = tweak_resource(paths["v1"], tmp_path / "r.json",
                             max_expansion_candidates=100)
    report = Engine(built_v1).build(tweaked, paths["docs"])
    assert report["rebuilt"] == []
    current = StateStore(built_v1).load_current()
    assert current["bridge"]["snapshot_version"] == 2


def test_unchanged_resource_is_noop(built_v1, paths):
    report = Engine(built_v1).build(paths["v1"], paths["docs"])
    assert report["rebuilt"] == []
    current = StateStore(built_v1).load_current()
    assert current["bridge"]["snapshot_version"] == 1
    for ref in current["partitions"].values():
        assert ref["partition_version"] == 1
