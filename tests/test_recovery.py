import pytest

from xretrieval.engine import CrashSimulated, Engine


def _crash_after(n):
    def hook(lang, count):
        if count >= n:
            raise CrashSimulated(lang)
    return hook


def _results_bytes(engine, queries, out):
    engine.run_query_file(queries, str(out))
    return out.read_bytes()


def test_recovery_byte_identical_to_fresh(tmp_path, paths):
    crashed = tmp_path / "crashed"
    Engine(crashed).build(paths["v1"], paths["docs"])
    Engine(crashed).build(paths["v2"], paths["docs"])
    with pytest.raises(CrashSimulated):
        Engine(crashed).build(paths["v3"], paths["docs"],
                              crash_hook=_crash_after(1))
    report = Engine(crashed).build(paths["v3"], paths["docs"])
    assert report["rebuilt"] == ["fr"]
    assert sorted(report["reused"]) == ["de", "en", "zh"]

    fresh = tmp_path / "fresh"
    for version in ("v1", "v2", "v3"):
        Engine(fresh).build(paths[version], paths["docs"])

    a = _results_bytes(Engine(crashed), paths["queries"],
                       tmp_path / "a.jsonl")
    b = _results_bytes(Engine(fresh), paths["queries"], tmp_path / "b.jsonl")
    assert a == b


def test_recovery_with_resource_changed_during_interruption(tmp_path, paths):
    state = tmp_path / "state"
    Engine(state).build(paths["v1"], paths["docs"])
    with pytest.raises(CrashSimulated):
        Engine(state).build(paths["v2"], paths["docs"],
                            crash_hook=_crash_after(1))
    # v2 build died after committing de; operator swaps in v3 and reruns
    report = Engine(state).build(paths["v3"], paths["docs"])
    assert sorted(report["rebuilt"]) == ["en", "fr"]
    assert sorted(report["reused"]) == ["de", "zh"]

    fresh = tmp_path / "fresh"
    for version in ("v1", "v2", "v3"):
        Engine(fresh).build(paths[version], paths["docs"])
    got = Engine(state).run_query_file(paths["queries"])
    want = Engine(fresh).run_query_file(paths["queries"])
    strip = lambda rs: [(r["query_id"],
                         [(h["doc_id"], h["score"]) for h in r["results"]])
                        for r in rs]
    assert strip(got) == strip(want)


def test_uncommitted_partition_rebuilt_from_scratch(tmp_path, paths):
    state = tmp_path / "state"
    with pytest.raises(CrashSimulated):
        Engine(state).build(paths["v1"], paths["docs"],
                            crash_hook=_crash_after(2))
    report = Engine(state).build(paths["v1"], paths["docs"])
    assert sorted(report["rebuilt"]) == ["fr", "zh"]
    assert sorted(report["reused"]) == ["de", "en"]
