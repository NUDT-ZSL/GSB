import json

import numpy as np
import pytest

from pano_workbench.errors import E_LATENCY_BUDGET, PanoError
from pano_workbench.query import QueryEngine, estimate_cost_us
from pano_workbench.session import Session


@pytest.fixture()
def session(cfg):
    s = Session(cfg)
    s.run_pipeline()
    return s


def test_index_is_byte_reproducible(session):
    a = json.loads(session.index.canonical_bytes())
    session._commit_snapshot()          # rebuild from the same state
    b = json.loads(session.index.canonical_bytes())
    b["snapshot_version"] = a["snapshot_version"]   # version bumps by design
    assert a == b


def test_index_entries_cover_all_scored_instances(session):
    mask = session.mask
    scored = ~mask.ignore_mask
    ids = {int(v) for v in np.unique(mask.instance_map[scored]) if v != 0}
    assert {e.instance_id for e in session.index.entries} == ids
    for e in session.index.entries:
        region = mask.instance_map == e.instance_id
        assert e.pixel_count == int((region & scored).sum())
        minx, miny, maxx, maxy = e.bbox
        ys, xs = np.where(region)
        assert (minx, miny, maxx, maxy) == (
            int(xs.min()), int(ys.min()), int(xs.max()), int(ys.max()))


def test_query_pixel_matches_mask(session):
    mask = session.mask
    for y in range(0, mask.height, 7):
        for x in range(0, mask.width, 5):
            r = session.query_pixel(x, y)
            assert r.category_id == int(mask.class_map[y, x])
            assert r.instance_id == int(mask.instance_map[y, x])
            assert abs(r.confidence - float(mask.conf_map[y, x])) < 1e-6
            assert r.latency_estimate_us == pytest.approx(
                estimate_cost_us(session.index.num_entries,
                                 session.index.mask_size))
            assert r.latency_observed_us >= 0.0


def test_query_instance_returns_stats(session):
    entry = session.index.entries[0]
    r = session.query_instance(entry.instance_id)
    assert r.category_id == entry.category_id
    assert r.confidence == pytest.approx(entry.conf_mean)
    assert r.metric_contribution["status"] == entry.metric_status
    assert r.entries_checked >= 1


def test_latency_budget_exceeded_reports_stages(cfg):
    s = Session(cfg)
    s.run_pipeline()
    ticks = iter(range(0, 10 ** 9, 1))
    clock = lambda: next(ticks) * 0.010      # each read advances 10 ms
    engine = QueryEngine(s.index, cfg, clock=clock)
    with pytest.raises(PanoError) as exc:
        engine.query_pixel(0, 0)
    assert exc.value.code == E_LATENCY_BUDGET
    assert exc.value.details["stages_completed"] == ["locate"]
    assert "entries_checked" in exc.value.details


def test_latency_budget_exceeded_on_instance_scan(cfg):
    s = Session(cfg)
    s.run_pipeline()
    ticks = iter(range(0, 10 ** 9, 1))
    engine = QueryEngine(s.index, cfg, clock=lambda: next(ticks) * 0.010)
    with pytest.raises(PanoError) as exc:
        engine.query_instance(s.index.entries[-1].instance_id)
    assert exc.value.code == E_LATENCY_BUDGET
    assert exc.value.details["stages_completed"] == ["locate"]
    assert exc.value.details["entries_checked"] >= 1
