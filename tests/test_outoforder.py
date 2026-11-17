"""Out-of-order / interleaved anchor updates: final conclusions always
match the final effective anchor version, late revocations overwrite
stale conclusions, and the overwrite shows up in the counters."""

import os

from certtrust.engine import Engine
from certtrust.anchors import parse_snapshot_files
from certtrust.parsing import parse_cert_files, parse_chain_files
from certtrust.validation import PARTIAL, TRUSTED

from tests.conftest import read_json


def _import(engine, ooo, event):
    certs, _ = parse_cert_files([os.path.join(ooo, "certs.json")])
    claims, _ = parse_chain_files(
        [os.path.join(ooo, f) for f in event.get("chains", [])], certs)
    snaps, _ = parse_snapshot_files(
        [os.path.join(ooo, f) for f in event.get("snapshots", [])])
    engine.import_certs(certs)
    engine.import_claims(claims)
    engine.import_snapshots(snaps)
    return engine.recompute()


def _run_events(workdir, events, ooo):
    engine = Engine(workdir)
    with engine.lock():
        engine.load()
        conclusions = []
        for event in events:
            conclusions = _import(engine, ooo, event)
        engine.save()
    return engine, conclusions


def test_interleaved_import_converges_to_final_anchor(samples, workdir):
    ooo = samples["dirs"]["out_of_order"]
    events = read_json(os.path.join(ooo, "events.json"))
    engine, conclusions = _run_events(workdir, events, ooo)
    assert engine.state["applied"]["snapshot_version"] == 3
    for concl in conclusions:
        assert concl.anchor_version == 3
        assert concl.anchor_key == engine.state["applied"]["stable_key"]


def test_late_revocation_overwrites_stale_conclusion(samples, workdir):
    ooo = samples["dirs"]["out_of_order"]
    events = read_json(os.path.join(ooo, "events.json"))
    engine = Engine(workdir)
    with engine.lock():
        first = _import(engine, ooo, events[0])
        by_id = {c.claim_id: c for c in first}
        assert by_id["claim-leaf-rev"].verdict == TRUSTED
        for event in events[1:]:
            _import(engine, ooo, event)
        engine.save()
    final = engine.conclusions()["claim-leaf-rev"]
    assert final.verdict == PARTIAL
    counters = engine.state["counters"]
    assert counters["overwritten"] >= 1
    assert counters["consistent"] >= 1


def test_arrival_order_does_not_change_final_state(samples, tmp_path):
    ooo = samples["dirs"]["out_of_order"]
    events = read_json(os.path.join(ooo, "events.json"))
    forward, _ = _run_events(str(tmp_path / "a"), events, ooo)
    backward, _ = _run_events(str(tmp_path / "b"), list(reversed(events)), ooo)
    assert forward.state["applied"] == backward.state["applied"]
    assert forward.state["conclusions"] == backward.state["conclusions"]


def test_older_snapshot_arriving_late_is_ignored(samples, workdir):
    ooo = samples["dirs"]["out_of_order"]
    events = read_json(os.path.join(ooo, "events.json"))
    engine = Engine(workdir)
    with engine.lock():
        _import(engine, ooo, events[0])
        conclusions = _import(engine, ooo, events[1])
    assert all(c.anchor_version == 2 for c in conclusions)
    assert engine.state["applied"]["snapshot_version"] == 2
