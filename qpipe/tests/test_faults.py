"""Requirement 7: parameter-driven fault injection and attribution."""

from __future__ import annotations

import numpy as np
import pytest

from qpipe import (
    E_SHAPE_MISMATCH,
    QuantPipeline,
    ResourceBudget,
    RunContext,
    samplegen,
)


def _run_with_faults(generated, faults, *, suffix=""):
    context = RunContext(seed=generated["seed"], faults=faults)
    pipeline = QuantPipeline(
        generated["data_dir"],
        generated["work_dir"] + suffix,
        budget=ResourceBudget(),
        context=context,
    )
    return pipeline, pipeline.run_all()


def test_quantize_fault_recorded_and_degraded(generated):
    faults = [
        {
            "stage": "quantize",
            "layer_index": 1,
            "kind": "unknown_quant_dtype",
            "code": "E_PACKAGE_FORMAT",
            "message": "injected unknown quant dtype",
        }
    ]
    pipeline, summary = _run_with_faults(generated, faults, suffix="_fq")
    payload = summary.to_dict()
    records = payload["fault_records"]
    assert any(
        r["stage"] == "quantize"
        and r["layer_index"] == 1
        and r["code"] == "E_PACKAGE_FORMAT"
        for r in records
    )
    degraded = [
        d for d in payload["degradations"] if d["layer_index"] == 1
    ]
    assert degraded and degraded[0]["trigger_code"] == "E_PACKAGE_FORMAT"
    assert degraded[0]["category"] == "degraded_quantize"


def test_calibration_shape_fault_attributed(generated):
    faults = [
        {
            "stage": "calibrate",
            "layer_index": 0,
            "kind": "split_mismatch",
            "code": E_SHAPE_MISMATCH,
            "message": "injected calibration split mismatch",
        }
    ]
    _, summary = _run_with_faults(generated, faults, suffix="_fc")
    payload = summary.to_dict()
    assert any(
        r["stage"] == "calibrate" and r["code"] == E_SHAPE_MISMATCH
        for r in payload["fault_records"]
    )
    assert any(
        d["category"] == "degraded_calibrate"
        for d in payload["degradations"]
    )


def test_infer_dimension_fault_attributed(generated):
    faults = [
        {
            "stage": "infer",
            "layer_index": 2,
            "kind": "input_dim_mismatch",
            "code": E_SHAPE_MISMATCH,
            "message": "injected inference input dimension mismatch",
        }
    ]
    _, summary = _run_with_faults(generated, faults, suffix="_fi")
    payload = summary.to_dict()
    assert any(
        r["stage"] == "infer"
        and r["layer_index"] == 2
        and r["code"] == E_SHAPE_MISMATCH
        for r in payload["fault_records"]
    )


def test_validate_fault_attributed(generated):
    faults = [
        {
            "stage": "validate",
            "layer_index": 0,
            "kind": "truncated_weight",
            "code": "E_PACKAGE_FORMAT",
            "message": "injected truncated weight read",
        }
    ]
    _, summary = _run_with_faults(generated, faults, suffix="_fv")
    payload = summary.to_dict()
    assert any(
        r["stage"] == "validate" and r["layer_index"] == 0
        for r in payload["fault_records"]
    )


def test_faults_are_input_driven_no_source_change(generated):
    # The same pipeline with an empty fault list must produce no records.
    _, clean = _run_with_faults(generated, [], suffix="_clean")
    assert clean.to_dict()["fault_records"] == []
