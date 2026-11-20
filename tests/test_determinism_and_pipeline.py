import numpy as np

from transfer_scheduler.config import GateConfig, PipelineConfig
from transfer_scheduler.data import generate_task_arrays, generate_task_catalog
from transfer_scheduler.determinism import derive_seed, fingerprint_arrays
from transfer_scheduler.pipeline import run_serial, run_task


def test_seed_derivation_is_stable_and_order_sensitive():
    assert derive_seed("a", 1) == derive_seed("a", 1)
    assert derive_seed("a", 1) != derive_seed(1, "a")
    assert 0 <= derive_seed("x") < 2**64


def test_task_arrays_byte_identical_across_calls(tasks):
    task = tasks[0]
    first = generate_task_arrays(task)
    second = generate_task_arrays(task)
    assert np.array_equal(first["features"], second["features"])
    assert np.array_equal(first["weights"], second["weights"])
    assert fingerprint_arrays([first["features"], first["weights"]]) == fingerprint_arrays(
        [second["features"], second["weights"]]
    )


def test_pipeline_byte_identical_on_repeat(tasks, config):
    result_a = run_task(
        tasks[0], config.pipeline, config.transfer_gate, config.score_gate
    )
    result_b = run_task(
        tasks[0], config.pipeline, config.transfer_gate, config.score_gate
    )
    assert result_a.fingerprints == result_b.fingerprints
    assert tuple(stage.fingerprint for stage in result_a.stages) == tuple(
        stage.fingerprint for stage in result_b.stages
    )
    assert result_a.score == result_b.score


def test_serial_baseline_sorted_and_deterministic(tasks, config):
    first = run_serial(
        tasks, config.pipeline, config.transfer_gate, config.score_gate
    )
    shuffled = list(reversed(tasks))
    second = run_serial(
        shuffled, config.pipeline, config.transfer_gate, config.score_gate
    )
    assert list(first) == sorted(first)
    assert first.keys() == second.keys()
    for task_id in first:
        assert first[task_id].fingerprints == second[task_id].fingerprints


def test_invalid_adapter_kind_raises_config_error(tasks, config):
    from dataclasses import replace

    from transfer_scheduler.errors import EConfigInvalid

    bad = replace(tasks[0], adapter_kind="not_exist")
    try:
        run_task(bad, config.pipeline, config.transfer_gate, config.score_gate)
    except EConfigInvalid as exc:
        assert exc.context["field"] == "adapter_kind"
    else:  # pragma: no cover
        raise AssertionError("应当抛出 EConfigInvalid")
