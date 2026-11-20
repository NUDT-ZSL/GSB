"""Requirement 1: validation errors and deterministic quantisation."""

from __future__ import annotations

import json
import os

import numpy as np
import pytest

from qpipe import (
    E_PACKAGE_FORMAT,
    E_SCALE_INVALID,
    E_SHAPE_MISMATCH,
    E_ZP_OUT_OF_RANGE,
    PipelineError,
    samplegen,
)
from qpipe.quantize import quantize_layer


def test_quantize_deterministic_and_roundtrip(package):
    spec = package.layers[0]
    weight = samplegen.generate_weight(spec)
    r1 = quantize_layer(spec, weight)
    r2 = quantize_layer(spec, weight)
    assert r1.quant_checksum == r2.quant_checksum
    assert r1.dequant_checksum == r2.dequant_checksum
    np.testing.assert_array_equal(r1.quant_values, r2.quant_values)
    np.testing.assert_array_equal(r1.dequant, r2.dequant)
    assert r1.max_abs_error <= spec.scale_hint + 1e-7


def test_shape_mismatch(package):
    spec = package.layers[0]
    bad = np.zeros(
        (spec.in_features * 2, spec.out_features), dtype=np.float32
    )
    with pytest.raises(PipelineError) as exc:
        quantize_layer(spec, bad)
    assert exc.value.code == E_SHAPE_MISMATCH
    assert exc.value.layer_id == spec.layer_id
    assert exc.value.model_tag == spec.model_tag


def test_scale_invalid(package):
    spec = package.layers[0]
    weight = samplegen.generate_weight(spec)
    from dataclasses import replace

    bad_spec = replace(spec, scale_hint=0.0)
    with pytest.raises(PipelineError) as exc:
        quantize_layer(bad_spec, weight)
    assert exc.value.code == E_SCALE_INVALID


def test_zero_point_out_of_range(package):
    spec = package.layers[0]
    from dataclasses import replace

    bad_spec = replace(spec, zero_point=9999)
    weight = samplegen.generate_weight(spec)
    with pytest.raises(PipelineError) as exc:
        quantize_layer(bad_spec, weight)
    assert exc.value.code == E_ZP_OUT_OF_RANGE


def test_manifest_missing_field(generated):
    manifest = os.path.join(generated["data_dir"], "manifest.jsonl")
    with open(manifest, "r", encoding="utf-8") as handle:
        rows = [json.loads(line) for line in handle if line.strip()]
    del rows[0]["scale_hint"]
    broken = os.path.join(generated["data_dir"], "broken.jsonl")
    with open(broken, "w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row) + "\n")
    with pytest.raises(PipelineError) as exc:
        samplegen.load_manifest(broken)
    assert exc.value.code == E_PACKAGE_FORMAT
    assert "scale_hint" in exc.value.detail["missing"]


def test_unknown_quant_dtype(generated):
    manifest = os.path.join(generated["data_dir"], "manifest.jsonl")
    with open(manifest, "r", encoding="utf-8") as handle:
        rows = [json.loads(line) for line in handle if line.strip()]
    rows[0]["quant_dtype"] = "fp4"
    broken = os.path.join(generated["data_dir"], "dtype.jsonl")
    with open(broken, "w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row) + "\n")
    with pytest.raises(PipelineError) as exc:
        samplegen.load_manifest(broken)
    assert exc.value.code == E_PACKAGE_FORMAT
