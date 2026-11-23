"""业务要求2: 基于注入逻辑时钟的降噪聚合, 逐位可复现。"""

from alertcorr.denoise import compute_clusters
from alertcorr.model import Window, canonical_json
from alertcorr.validation import validate_events_list, validate_topology_dict

WINDOW_MS = 60_000


def _clusters(topo_dict, events_list, windows=()):
    topo = validate_topology_dict(topo_dict)
    events = validate_events_list(events_list, topo)
    return compute_clusters(events, topo, list(windows), WINDOW_MS)


def test_cluster_window_boundary(topo_dict, events_list):
    clusters = _clusters(topo_dict, events_list)
    first = clusters["db~cpu_high~0000"]
    assert first["events"] == [1, 2, 3]
    second = clusters["db~cpu_high~0001"]
    assert second["events"] == [12, 13]
    assert second["start_ts"] - first["start_ts"] > WINDOW_MS


def test_flag_suppression(topo_dict, events_list):
    clusters = _clusters(topo_dict, events_list)
    web = clusters["web~http_5xx~0000"]
    assert web["suppressed"] == [9]
    assert web["suppressed_count"] == 1
    assert web["representative"] == 8
    batch = clusters["batch~job_fail~0000"]
    assert batch["status"] == "suppressed"
    assert batch["representative"] is None


def test_window_full_coverage_suppression(topo_dict, events_list):
    win = Window(window_id="w1", kind="silence", service="db",
                 instance_id="", start_ms=900, end_ms=2600)
    clusters = _clusters(topo_dict, events_list, windows=[win])
    assert clusters["db~cpu_high~0000"]["status"] == "suppressed"
    partial = Window(window_id="w2", kind="silence", service="db",
                     instance_id="", start_ms=2000, end_ms=2600)
    clusters2 = _clusters(topo_dict, events_list, windows=[partial])
    assert clusters2["db~cpu_high~0000"]["suppressed"] == [3]
    assert clusters2["db~cpu_high~0000"]["status"] == "active"


def test_representative_by_severity_weight_clock(topo_dict, events_list):
    clusters = _clusters(topo_dict, events_list)
    first = clusters["db~cpu_high~0000"]
    assert first["representative"] == 1
    second = clusters["db~cpu_high~0001"]
    assert second["representative"] == 12


def test_determinism_bitwise(topo_dict, events_list):
    a = canonical_json(_clusters(topo_dict, events_list))
    for _ in range(3):
        b = canonical_json(_clusters(topo_dict, events_list))
        assert a == b
