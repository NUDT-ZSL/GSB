import dataclasses

import pytest

from pano_workbench.errors import E_INCONSISTENT, PanoError
from pano_workbench.selfcheck import (
    check_index_vs_mask,
    check_metrics_recompute,
    check_snapshot_version,
)
from pano_workbench.session import Session


@pytest.fixture()
def session(cfg):
    s = Session(cfg)
    s.run_pipeline()
    return s


def test_self_check_passes_on_healthy_session(session):
    result = session.self_check()
    assert set(result) == {"index_vs_mask", "metrics_recompute",
                           "snapshot_version"}


def test_index_vs_mask_detects_tampered_entry(session):
    entry = session.index.entries[0]
    bad = dataclasses.replace(entry, pixel_count=entry.pixel_count + 1)
    entries = list(session.index.entries)
    entries[0] = bad
    session.index.entries = tuple(entries)
    with pytest.raises(PanoError) as exc:
        check_index_vs_mask(session.index, session.mask)
    assert exc.value.code == E_INCONSISTENT
    assert exc.value.details["category"] == "index_vs_mask"


def test_metrics_recompute_detects_tampered_aggregate(session, cfg):
    session.report.miou += 0.25
    with pytest.raises(PanoError) as exc:
        check_metrics_recompute(session.report, cfg)
    assert exc.value.code == E_INCONSISTENT
    assert exc.value.details["category"] == "metrics_recompute"


def test_snapshot_version_skew_detected(session):
    session.index.snapshot_version = session.manager.current().version + 7
    with pytest.raises(PanoError) as exc:
        check_snapshot_version(session.index, session.manager.current())
    assert exc.value.code == E_INCONSISTENT
    assert exc.value.details["category"] == "snapshot_version"
