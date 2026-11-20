import threading
import time

import pytest

from conftest import tweak_resource
from xretrieval.canon import canonical_bytes
from xretrieval.engine import Engine
from xretrieval.errors import XError
from xretrieval.store import BuildLock


def test_build_busy_not_blocking(built_v1, paths):
    lock = BuildLock(built_v1).acquire("test")
    try:
        with pytest.raises(XError) as excinfo:
            Engine(built_v1).build(paths["v2"], paths["docs"])
        assert excinfo.value.code == "E_BUILD_BUSY"
    finally:
        lock.release()


def test_queries_consistent_during_build(built_v1, paths):
    pre = canonical_bytes(Engine(built_v1).run_query_file(paths["queries"]))
    during = {}

    def slow_build():
        def pause(lang, count):
            time.sleep(0.3)
        Engine(built_v1).build(paths["v2"], paths["docs"], crash_hook=pause)

    thread = threading.Thread(target=slow_build)
    thread.start()
    time.sleep(0.05)
    during["bytes"] = canonical_bytes(
        Engine(built_v1).run_query_file(paths["queries"], workers=4))
    thread.join()
    assert during["bytes"] == pre
    post = Engine(built_v1).run_query_file(paths["queries"])
    assert all(r["bridge"]["resource_version"] == 2 for r in post)


def test_postings_limit(built_v1, paths, tmp_path):
    tiny = tweak_resource(paths["v1"], tmp_path / "r.json",
                          max_postings_per_partition=10)
    with pytest.raises(XError) as excinfo:
        Engine(tmp_path / "s2").build(tiny, paths["docs"])
    assert excinfo.value.code == "E_RESOURCE_LIMIT"
    assert excinfo.value.context["partition"] == "de"


def test_scoring_budget_degrade_and_error(built_v1, paths, tmp_path):
    tiny = tweak_resource(paths["v1"], tmp_path / "r.json",
                          max_scoring_entries=5)
    state = tmp_path / "s"
    Engine(state).build(tiny, paths["docs"])
    results = Engine(state).run_query_file(paths["queries"])
    assert any(any(d["category"] == "SCORING_CANDIDATES_DROPPED"
                   for d in r["degraded"]) for r in results)
    tiny_err = tweak_resource(paths["v1"], tmp_path / "r2.json",
                              max_scoring_entries=5, resource_policy="error")
    state2 = tmp_path / "s2"
    Engine(state2).build(tiny_err, paths["docs"])
    with pytest.raises(XError) as excinfo:
        Engine(state2).run_query_file(paths["queries"])
    assert excinfo.value.code == "E_RESOURCE_LIMIT"
