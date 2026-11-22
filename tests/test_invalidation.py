"""Invalidation propagation, deviation spectrum and consistency checks."""
from quant_conv_pipeline import nodes
from quant_conv_pipeline.errors import Code
from quant_conv_pipeline.sample_gen import read_jsonl
from quant_conv_pipeline.verify import verify_snapshot


def _updates(data, name):
    return read_jsonl(f"{data}/updates/{name}")


def test_full_run_all_recomputed(ran):
    inv = ran["run_result"]["invalidation"]
    assert inv["kind"] == "full_run"
    assert len(inv["affected_jobs"]) == 12
    assert len(inv["recompute"]) == 12 * 4
    assert inv["reuse"] == []
    assert ran["run_result"]["tolerance_failures"] == []


def test_weight_update_invalidates_weight_quant_and_downstream(ran):
    pipe = ran["pipe"]
    res = pipe.commit(_updates(ran["data"], "weight_params_v2.jsonl"),
                      "weight")
    inv = res["invalidation"]
    assert inv["affected_nodes"] == [nodes.WEIGHT_QUANT, nodes.BLOCKED_MAC,
                                     nodes.REDUCE_DEQUANT]
    assert inv["affected_jobs"] == ["job-002", "job-005", "job-007"]
    recomputed = {(e["node"], e["job_id"]) for e in inv["recompute"]}
    reused = {(e["node"], e["job_id"]) for e in inv["reuse"]}
    expected = {(n, j) for n in inv["affected_nodes"]
                for j in inv["affected_jobs"]}
    assert recomputed == expected
    assert reused == {(nodes.QUANT_INPUT_PREP, j)
                      for j in inv["affected_jobs"]}
    assert recomputed.isdisjoint(reused)
    assert all(j in inv["affected_jobs"] for _, j in recomputed | reused)


def test_calib_update_invalidates_only_reduce_dequant(ran):
    pipe = ran["pipe"]
    res = pipe.commit(_updates(ran["data"], "calib_stats_v2.jsonl"), "calib")
    inv = res["invalidation"]
    assert inv["affected_nodes"] == [nodes.REDUCE_DEQUANT]
    assert inv["affected_jobs"] == ["job-003", "job-007", "job-009"]
    recomputed = {(e["node"], e["job_id"]) for e in inv["recompute"]}
    reused = {(e["node"], e["job_id"]) for e in inv["reuse"]}
    assert recomputed == {(nodes.REDUCE_DEQUANT, j)
                          for j in inv["affected_jobs"]}
    assert reused == {(n, j) for j in inv["affected_jobs"]
                      for n in (nodes.QUANT_INPUT_PREP, nodes.WEIGHT_QUANT,
                                nodes.BLOCKED_MAC)}
    assert recomputed.isdisjoint(reused)


def test_outputs_actually_change_after_weight_update(ran):
    pipe = ran["pipe"]
    before = pipe.load_snapshot("snap-0001")["lineage"]["job-002"]
    pipe.commit(_updates(ran["data"], "weight_params_v2.jsonl"), "weight")
    after = pipe.load_snapshot("snap-0002")["lineage"]["job-002"]
    assert before["node_keys"][nodes.QUANT_INPUT_PREP] == \
        after["node_keys"][nodes.QUANT_INPUT_PREP]
    for node in (nodes.WEIGHT_QUANT, nodes.BLOCKED_MAC,
                 nodes.REDUCE_DEQUANT):
        assert before["node_keys"][node] != after["node_keys"][node]
    assert before["output_sha256"] != after["output_sha256"]


def test_deviation_spectrum_structure(ran):
    spectra = ran["run_result"]["spectra"]
    assert set(spectra.keys()) == set(ran["run_result"]["lineage"].keys())
    for jid, spec in spectra.items():
        assert spec["reference"] == "float64_dequantized"
        assert spec["dominant_error_source"] in (
            "quant_input", "fixed_point_mac", "accumulator_reduce",
            "dequant_output")
        for level, m in spec["levels"].items():
            for metric in ("max_abs", "mean_abs", "max_rel", "p50", "p90",
                           "p99", "amplification"):
                assert metric in m, (jid, level, metric)
        # blocked int32 reduce is exact: no blocking-induced deviation
        assert spec["levels"]["accumulator_reduce"]["max_abs"] == 0.0


def test_tolerance_exceeded_reports_first_position(ran):
    pipe = ran["pipe"]
    pipe.cfg.tolerances.dequant_output = 1e-9
    res = pipe.commit(_updates(ran["data"], "calib_stats_v2.jsonl"), "calib")
    assert res["tolerance_failures"], "expected E_TOLERANCE_EXCEEDED"
    failure = res["tolerance_failures"][0]
    assert failure["code"] == Code.E_TOLERANCE_EXCEEDED
    assert failure["job_id"] == "job-003"
    assert failure["details"]["level"] == "dequant_output"
    assert len(failure["details"]["position"]) == 2
    assert abs(failure["details"]["deviation"]) > 1e-9


def test_verify_snapshot_consistent(ran):
    pipe = ran["pipe"]
    pipe.commit(_updates(ran["data"], "weight_params_v2.jsonl"), "weight")
    report = verify_snapshot(pipe, "snap-0002")
    assert report["consistent"]
    assert report["entries_replayed_ok"] == 3 * 4
    assert report["recompute_count"] + report["reuse_count"] >= \
        report["affected_count"]
