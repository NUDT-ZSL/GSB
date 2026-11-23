"""业务要求1: 解析校验与错误码; 非法输入不写入持久化状态。"""

import copy

import pytest

from alertcorr import errors as E
from alertcorr.engine import Engine
from alertcorr.validation import validate_events_list, validate_topology_dict


def _expect(code, fn, *args):
    with pytest.raises(E.BusinessError) as exc_info:
        fn(*args)
    assert exc_info.value.code == code
    assert exc_info.value.context
    return exc_info.value


def test_duplicate_service(topo_dict):
    bad = copy.deepcopy(topo_dict)
    bad["services"].append(copy.deepcopy(bad["services"][0]))
    _expect(E.E1001_DUPLICATE_SERVICE, validate_topology_dict, bad)


def test_duplicate_instance(topo_dict):
    bad = copy.deepcopy(topo_dict)
    bad["services"][1]["instances"].append({"id": "db-1", "weight": 1.0})
    _expect(E.E1002_DUPLICATE_INSTANCE, validate_topology_dict, bad)


def test_unknown_upstream(topo_dict):
    bad = copy.deepcopy(topo_dict)
    bad["services"][0]["upstream"] = ["ghost"]
    err = _expect(E.E1003_UNKNOWN_UPSTREAM, validate_topology_dict, bad)
    assert err.context["upstream"] == "ghost"


def test_self_dependency(topo_dict):
    bad = copy.deepcopy(topo_dict)
    bad["services"][0]["upstream"] = ["db"]
    _expect(E.E1004_SELF_DEPENDENCY, validate_topology_dict, bad)


def test_dependency_cycle(topo_dict):
    bad = copy.deepcopy(topo_dict)
    for svc in bad["services"]:
        if svc["name"] == "db":
            svc["upstream"] = ["api"]
    err = _expect(E.E1005_DEPENDENCY_CYCLE, validate_topology_dict, bad)
    assert "api" in err.context["cycle"]


def test_seq_not_increasing(topo_dict, events_list):
    topo = validate_topology_dict(topo_dict)
    bad = copy.deepcopy(events_list)
    bad[5]["seq"] = bad[4]["seq"]
    _expect(E.E2001_SEQ_NOT_INCREASING, validate_events_list, bad, topo)


def test_negative_clock(topo_dict, events_list):
    topo = validate_topology_dict(topo_dict)
    bad = copy.deepcopy(events_list)
    bad[0]["logical_ts_ms"] = -1
    _expect(E.E2002_NEGATIVE_CLOCK, validate_events_list, bad, topo)


def test_invalid_severity(topo_dict, events_list):
    topo = validate_topology_dict(topo_dict)
    bad = copy.deepcopy(events_list)
    bad[0]["severity"] = "fatal"
    _expect(E.E2003_INVALID_SEVERITY, validate_events_list, bad, topo)


def test_invalid_flag(topo_dict, events_list):
    topo = validate_topology_dict(topo_dict)
    bad = copy.deepcopy(events_list)
    bad[0]["flag"] = "muted"
    _expect(E.E2004_INVALID_FLAG, validate_events_list, bad, topo)


def test_non_finite_metric(topo_dict, events_list):
    topo = validate_topology_dict(topo_dict)
    bad = copy.deepcopy(events_list)
    bad[0]["metric"] = float("nan")
    _expect(E.E2005_NON_FINITE_METRIC, validate_events_list, bad, topo)


def test_unknown_instance(topo_dict, events_list):
    topo = validate_topology_dict(topo_dict)
    bad = copy.deepcopy(events_list)
    bad[0]["instance_id"] = "ghost-1"
    _expect(E.E2006_UNKNOWN_INSTANCE, validate_events_list, bad, topo)


def test_invalid_input_writes_nothing(tmp_path, topo_dict, events_list):
    eng = Engine(str(tmp_path / "x.db"))
    bad = copy.deepcopy(topo_dict)
    bad["services"][0]["upstream"] = ["ghost"]
    with pytest.raises(E.BusinessError):
        eng.load_topology(bad)
    assert eng.store.get_meta("topology_version") is None

    eng.load_topology(topo_dict)
    bad_events = copy.deepcopy(events_list)
    bad_events[3]["severity"] = "fatal"
    with pytest.raises(E.BusinessError):
        eng.ingest(bad_events)
    assert eng.store.load_events() == []
    eng.close()


def test_topology_version_rollback(engine, topo_dict):
    engine.load_topology(topo_dict)
    older = copy.deepcopy(topo_dict)
    older["version"] = 1
    with pytest.raises(E.BusinessError) as exc_info:
        engine.load_topology(older)
    assert exc_info.value.code == E.E3003_TOPOLOGY_VERSION_ROLLBACK
