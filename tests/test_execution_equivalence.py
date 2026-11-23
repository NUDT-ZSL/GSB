from __future__ import annotations

import numpy as np
import pytest

from transformer_fusion import execute


def test_fused_kernel_matches_float64_reference(dataset, stores):
    data_dir, manifest = dataset
    report = execute(
        manifest,
        data_dir=data_dir,
        cache_dir=stores["cache_dir"],
        snapshot_dir=stores["snapshot_dir"],
    )
    assert report.ok, report.to_dict()["diagnostics"]
    assert len(report.cases) == 9
    assert report.selfcheck["ok"]

    fused_cases = [case for case in report.cases if case.plan and case.plan.path == "fused"]
    assert fused_cases
    for case in report.cases:
        assert case.status == "ok"
        assert case.equivalence["ok"]
        assert case.equivalence["reference_dtype"] == "float64"
        assert case.reference_output is not None
        assert case.reference_output.dtype == np.float64
        assert case.reference_output.shape == (
            case.parsed.task.output_shape if case.parsed else case.reference_output.shape
        )
        if case.pair and case.pair.feasible:
            assert case.fused_output is not None
            assert case.separate_output is not None
            assert np.array_equal(case.fused_output, case.separate_output)
            assert np.array_equal(
                np.asarray(case.fused_output).view(np.uint8),
                np.asarray(case.separate_output).view(np.uint8),
            )
            assert case.intermediate_digest == case.separate_intermediate_digest
            assert case.equivalence["fused_vs_separate_byte_identical"] is True
            assert case.extra["fused_kernel_metadata"]["materialized_intermediate_bytes"] == 0
            assert case.block_boundaries
            assert case.block_boundaries[0][0] == 0
            assert case.block_boundaries[-1][1] == case.parsed.task.numel
            for left, right in zip(case.block_boundaries, case.block_boundaries[1:]):
                assert left[1] == right[0]


def test_plans_record_explicit_policy_and_fallback_reasons(dataset, stores):
    data_dir, manifest = dataset
    report = execute(
        manifest,
        data_dir=data_dir,
        cache_dir=stores["cache_dir"],
        snapshot_dir=stores["snapshot_dir"],
    )
    cases = report.case_map
    assert cases["case_gelu_perm"].plan.path == "fused"
    assert "traversal" in " ".join(cases["case_gelu_perm"].plan.priority)
    assert cases["case_memory_first"].plan.path == "separate"
    assert cases["case_memory_first"].plan.fallback_category == "policy"
    assert cases["case_memory_first"].plan.upstream_code == "E_POLICY_MEMORY"
    assert cases["case_downstream_layernorm"].plan.path == "separate"
    assert cases["case_downstream_layernorm"].plan.upstream_code == "E_POLICY_LOCALITY"
    assert cases["case_reduction_between"].pair.feasible is False
    assert cases["case_reduction_between"].pair.reason_code == "E_REDUCTION_DEPENDENCY"
    assert cases["case_reduction_candidate"].pair.reason_code == "E_REDUCTION_DEPENDENCY"
    assert cases["case_memory_first"].plan.metrics["fused_single_block_memory_higher"] is True


def test_tolerance_exceeded_reports_locations_and_source(dataset, stores):
    data_dir, manifest = dataset
    report = execute(
        manifest,
        data_dir=data_dir,
        cache_dir=stores["cache_dir"],
        snapshot_dir=stores["snapshot_dir"],
        abs_tol=1e-300,
        rel_tol=1e-300,
        case_ids=["case_downstream_layernorm"],
        run_selfcheck=False,
    )
    assert not report.ok
    case = report.cases[0]
    diag = next(d for d in case.diagnostics if d.code == "E_TOLERANCE_EXCEEDED")
    assert diag.stage == "adjudication"
    assert diag.details["violation_count"] > 0
    assert diag.details["locations"]
    assert diag.details["locations"][0]["likely_source"] == "activation_precision_cast"
    assert diag.details["deviation_spectrum"]["bins"]
    assert case.equivalence["max_abs_deviation"] > 0.0
