import copy
import dataclasses

import numpy as np
import pytest

from pano_workbench.errors import E_INCONSISTENT, PanoError
from pano_workbench.session import Session


@pytest.fixture()
def session(cfg):
    s = Session(cfg)
    s.run_pipeline()
    return s


def test_query_matches_batch_everywhere(session):
    summary = session.verify_consistency()
    assert summary["consistent"] is True
    assert summary["pixels_checked"] > 0
    assert summary["instances_checked"] == session.index.num_entries


def test_pixel_divergence_reports_position_and_both_values(session):
    bad_mask = copy.deepcopy(session.mask)
    scored = ~bad_mask.ignore_mask
    y, x = (int(v) for v in np.argwhere(scored)[0])
    old = int(bad_mask.class_map[y, x])
    bad_mask.class_map[y, x] = (old % session.cfg.num_categories) + 1
    session.index.mask = bad_mask                 # query path now diverges
    with pytest.raises(PanoError) as exc:
        session.verify_consistency()
    assert exc.value.code == E_INCONSISTENT
    assert exc.value.details["position"] == [x, y] or \
        tuple(exc.value.details["position"]) == (x, y)
    assert exc.value.details["query_path"] != exc.value.details["batch_path"]


def test_instance_divergence_reports_both_values(session):
    entry = session.index.entries[0]
    bad = dataclasses.replace(
        entry, metric_status="FP" if entry.metric_status == "TP" else "TP")
    entries = list(session.index.entries)
    entries[0] = bad
    session.index.entries = tuple(entries)
    session.index._by_id[bad.instance_id] = bad
    with pytest.raises(PanoError) as exc:
        session.verify_consistency()
    assert exc.value.code == E_INCONSISTENT
    assert exc.value.details["position"] == entry.instance_id
    assert exc.value.details["query_path"] != exc.value.details["batch_path"]
