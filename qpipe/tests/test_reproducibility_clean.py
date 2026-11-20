"""Focused determinism tests (replaces the exploratory version)."""

from __future__ import annotations

import numpy as np

from qpipe import QuantPipeline, RunContext, samplegen
from qpipe.deterministic import checksum_array


def test_two_full_runs_byte_identical(generated):
    first = QuantPipeline(
        generated["data_dir"],
        generated["work_dir"],
        context=RunContext(seed=generated["seed"]),
    ).run_all().to_dict()
    second = QuantPipeline(
        generated["data_dir"],
        generated["work_dir"] + "_b",
        context=RunContext(seed=generated["seed"]),
    ).run_all().to_dict()

    assert first["prediction_checksum"] == second["prediction_checksum"]
    assert first["activation_checksums"] == second["activation_checksums"]
    assert first["quant_errors"] == second["quant_errors"]
    np.testing.assert_array_equal(
        first["prediction_vector"], second["prediction_vector"]
    )


def test_chain_inputs_are_previous_outputs(generated, package):
    pipeline = QuantPipeline(
        generated["data_dir"],
        generated["work_dir"],
        context=RunContext(seed=generated["seed"]),
    )
    pipeline.run_all()
    for layer_index in range(1, package.layer_count):
        previous = pipeline.cache.read_array(
            "infer", layer_index - 1, "activation"
        )
        entry = pipeline.cache.read_entry("infer", layer_index)
        assert entry["payload"]["input_checksum"] == checksum_array(previous)


def test_recompute_then_rerun_is_stable(generated):
    from dataclasses import replace

    pipeline = QuantPipeline(
        generated["data_dir"],
        generated["work_dir"],
        context=RunContext(seed=generated["seed"]),
    )
    pipeline.run_all()
    package = samplegen.load_manifest(pipeline._manifest_path)

    changed = 1
    specs, weights = [], {}
    for spec in package.layers:
        if spec.layer_index == changed:
            spec = replace(spec, weight_revision=spec.weight_revision + 1)
            weights[changed] = samplegen.generate_weight(
                spec, seed=generated["seed"]
            )
        specs.append(spec)
    new_package = type(package)(
        model_tag=package.model_tag, layers=tuple(specs)
    )
    after_update = pipeline.apply_revision(
        new_package, changed_layer=changed, new_weights=weights
    ).to_dict()

    # Re-running the same snapshot must reproduce the same prediction checksum
    # regardless of recomputation order.
    rerun = pipeline.run_all(
        affected=set(new_package.indices())
    ).to_dict()
    assert (
        rerun["prediction_checksum"]
        == after_update["prediction_checksum"]
    )
