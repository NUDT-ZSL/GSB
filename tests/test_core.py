import copy
import json

import pytest

from scheduler_sim import fixedpoint
from scheduler_sim.errors import AffinityError, ManifestError, ReplayError
from scheduler_sim.filters import filter_feasible
from scheduler_sim.manifest import load_nodes, load_pods
from scheduler_sim.scoring import DEFAULT_WEIGHTS, schedule_queue, score_pod
from scheduler_sim.session import Session, load_weights

SAMPLES = __import__("pathlib").Path(__file__).resolve().parent.parent / "samples"


def make_session(weights=None):
    s = Session(weights)
    s.load_manifest_files(SAMPLES / "nodes.json", SAMPLES / "pods.json")
    return s


# -- requirement 1: manifest validation -----------------------------------
def test_valid_samples_load():
    nv, nodes = load_nodes(SAMPLES / "nodes.json")
    pv, pods = load_pods(SAMPLES / "pods.json")
    assert nv == "nodes-v1" and pv == "pods-v1"
    assert len(nodes) == 7 and len(pods) == 5
    assert [n.name for n in nodes] == sorted(n.name for n in nodes)


@pytest.mark.parametrize("mutate,field_part", [
    (lambda d: d["nodes"].append(copy.deepcopy(d["nodes"][0])), "name"),
    (lambda d: d["nodes"][0]["taints"].append(
        {"key": "k", "value": "v", "effect": "Bogus"}), "effect"),
    (lambda d: d["nodes"][0]["allocatable"].update(cpu_millis=0),
     "cpu_millis"),
    (lambda d: d["nodes"][0]["labels"].update({"bad key": "x"}), "labels"),
])
def test_manifest_rejected_wholesale(tmp_path, mutate, field_part):
    doc = json.loads((SAMPLES / "nodes.json").read_text())
    mutate(doc)
    path = tmp_path / "nodes.json"
    path.write_text(json.dumps(doc))
    with pytest.raises(ManifestError) as excinfo:
        load_nodes(path)
    assert excinfo.value.code == "E_MANIFEST"
    assert excinfo.value.field and field_part in excinfo.value.field
    assert excinfo.value.entry


def test_pod_request_must_be_positive_int(tmp_path):
    doc = json.loads((SAMPLES / "pods.json").read_text())
    doc["pods"][0]["request"]["cpu_millis"] = -1
    path = tmp_path / "pods.json"
    path.write_text(json.dumps(doc))
    with pytest.raises(ManifestError) as excinfo:
        load_pods(path)
    assert "cpu_millis" in excinfo.value.field


def test_affinity_keyref_cycle():
    with pytest.raises(AffinityError) as excinfo:
        load_pods(SAMPLES / "broken" / "pods_affinity_cycle.json")
    assert excinfo.value.code == "E_AFFINITY"
    assert "rule-a" in str(excinfo.value)


# -- requirement 2: filter stage -------------------------------------------
def test_filter_deterministic_and_reasoned():
    _, nodes = load_nodes(SAMPLES / "nodes.json")
    _, pods = load_pods(SAMPLES / "pods.json")
    pod = pods[0]
    feasible1, rejected1 = filter_feasible(pod, nodes)
    feasible2, rejected2 = filter_feasible(pod, list(reversed(nodes)))
    assert [n.name for n in feasible1] == [n.name for n in feasible2]
    assert rejected1 == rejected2  # identical rejection sequence
    stages = {r["node"]: {x["stage"] for x in r["reasons"]}
              for r in rejected1}
    assert "TaintToleration" in stages["node-beta"]
    assert "Resources" in stages["node-epsilon"]
    assert "NodeAffinity" in stages["node-omega1"]


def test_filter_integer_resource_boundary():
    _, nodes = load_nodes(SAMPLES / "nodes.json")
    _, pods = load_pods(SAMPLES / "pods.json")
    pod = copy.deepcopy(pods[0])
    epsilon = next(n for n in nodes if n.name == "node-epsilon")
    from scheduler_sim.manifest import Toleration
    pod.request["cpu_millis"] = 100  # exactly the free amount
    pod.request["memory_mib"] = 256  # exactly the free amount
    pod.tolerations.append(Toleration(
        key="maintenance", operator="Equal", value="true",
        effect="NoExecute"))
    assert filter_feasible(pod, [epsilon])[0]  # exact fit is feasible
    pod.request["cpu_millis"] = 101
    assert not filter_feasible(pod, [epsilon])[0]


# -- requirement 3/4: fixed-point scoring and error bounds ------------------
def test_weighted_sum_is_pure_integer():
    contribs = [{"plugin": "a", "raw": 100, "weight": 3},
                {"plugin": "b", "raw": 50, "weight": 2},
                {"plugin": "c", "raw": 94, "weight": 1},
                {"plugin": "d", "raw": 29, "weight": 1}]
    total, bound, details = fixedpoint.weighted_sum(contribs)
    assert isinstance(total, int) and isinstance(bound, int)
    assert total == 4285 + 1428 + 1342 + 414  # floor(raw*w*100/7) each
    assert bound == 4  # one grid unit per available plugin
    assert [d["contribution_units"] for d in details] == [4285, 1428, 1342, 414]


def test_error_bound_covers_truncation():
    # exact value: sum(raw*w*G/W); computed loses < 1 unit per plugin
    contribs = [{"plugin": "x", "raw": 33, "weight": 1},
                {"plugin": "y", "raw": 77, "weight": 2}]
    total, bound, _ = fixedpoint.weighted_sum(contribs)
    exact_milli = (33 * 1 * 100 + 77 * 2 * 100) / 3
    assert 0 <= exact_milli - total < bound


def test_tie_marked_and_broken_by_name():
    s = make_session()
    result = s.run()
    tie = next(r for r in result["reports"] if r["pod"] == "pod-tie")
    assert tie["tie_group"] == ["node-omega2"]
    assert tie["winner"] == "node-omega1"
    assert "node-name" in tie["tie_break_reason"]


def test_indistinguishable_margin():
    # diff strictly below combined bound -> indistinguishable
    assert fixedpoint.indistinguishable(5000, 2, 5003, 2)
    assert not fixedpoint.indistinguishable(5000, 2, 5004, 2)


# -- requirement 5: unavailable plugins -------------------------------------
def test_unavailable_plugin_excluded_from_normalization():
    s = make_session()
    result = s.run()
    web1 = next(r for r in result["reports"] if r["pod"] == "pod-web-1")
    delta = next(x for x in web1["scores"] if x["node"] == "node-delta")
    img = next(p for p in delta["plugins"] if p["plugin"] == "image_locality")
    assert img["status"] == "unavailable"
    assert img["contribution_units"] is None
    # normalization used only the 3 available plugins: W = 3+2+1 = 6
    assert delta["score_units"] == 0 + 3333 + 1616
    assert delta["error_bound_units"] == 3


def test_restricted_node_reports_unavailable_not_zero():
    s = make_session()
    result = s.run()
    gpu = next(r for r in result["reports"] if r["pod"] == "pod-gpu")
    alpha = next(x for x in gpu["scores"] if x["node"] == "node-alpha")
    rb = next(p for p in alpha["plugins"] if p["plugin"] == "resource_balance")
    assert rb["status"] == "unavailable"
    assert "restricted" in rb["reason"]


def test_stats_missing_events_logged():
    s = make_session()
    s.run()
    assert any(e["code"] == "E_STATS" and e["node"] == "node-delta"
               for e in s.events)


# -- requirement 6: deterministic propagation path ---------------------------
def test_repeated_runs_bit_identical():
    r1 = make_session().run()
    r2 = make_session().run()
    assert json.dumps(r1, sort_keys=True) == json.dumps(r2, sort_keys=True)


# -- requirement 7: snapshot / replay ----------------------------------------
def test_snapshot_roundtrip_bit_identical(tmp_path):
    s1 = make_session()
    s1.run()
    snap = tmp_path / "snap.json"
    s1.save_snapshot(snap)
    s2 = make_session(load_weights(SAMPLES / "weights_alt.yaml"))
    s2.run()
    actions = s2.restore(snap)
    assert any("wholesale" in a for a in actions)
    assert s2.weights == s1.weights
    assert json.dumps(s2.result, sort_keys=True) == \
        json.dumps(s1.result, sort_keys=True)


def test_snapshot_tampering_triggers_replay_error(tmp_path):
    s1 = make_session()
    s1.run()
    snap = tmp_path / "snap.json"
    s1.save_snapshot(snap)
    doc = json.loads(snap.read_text())
    doc["result"]["bindings"]["pod-web-1"] = "node-delta"  # tamper
    snap.write_text(json.dumps(doc))
    s2 = make_session()
    with pytest.raises(ReplayError) as excinfo:
        s2.restore(snap)
    assert excinfo.value.code == "E_REPLAY"


def test_snapshot_version_mismatch_replaces_not_merges(tmp_path):
    s1 = make_session()
    s1.run()
    snap = tmp_path / "snap.json"
    s1.save_snapshot(snap)
    doc = json.loads(snap.read_text())
    # recorded version no longer matches the current session
    doc["nodes_version"] = "deadbeefdeadbeef"
    snap.write_text(json.dumps(doc))
    s2 = make_session(load_weights(SAMPLES / "weights_alt.yaml"))
    s2.run()
    actions = s2.restore(snap)
    assert any("wholesale" in a for a in actions)
    # state is replaced wholesale, not merged: alt weights are gone
    assert s2.weights == s1.weights
    assert json.dumps(s2.result, sort_keys=True) == \
        json.dumps(s1.result, sort_keys=True)


# -- requirement 8: weights re-run -------------------------------------------
def test_weight_change_reschedules_deterministically():
    s1 = make_session()
    r1 = s1.run()
    s2 = make_session(load_weights(SAMPLES / "weights_alt.yaml"))
    r2 = s2.run()
    assert r1["bindings"] != r2["bindings"]  # weights matter
    s3 = make_session(load_weights(SAMPLES / "weights_alt.yaml"))
    assert json.dumps(r2, sort_keys=True) == json.dumps(s3.run(),
                                                      sort_keys=True)
