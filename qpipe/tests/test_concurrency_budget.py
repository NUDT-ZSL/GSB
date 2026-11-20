"""Requirement 6: non-blocking stage lock and dual resource budgets."""

from __future__ import annotations

import pytest

from qpipe import E_RESOURCE_LIMIT, E_STAGE_BUSY, ResourceBudget, RunContext
from qpipe.storage import FileLock


def test_stage_lock_is_non_blocking(generated):
    from qpipe.storage import WorkDir

    work = WorkDir(generated["work_dir"])
    first = FileLock(work, "stage_infer")
    first.acquire()
    second = FileLock(work, "stage_infer")
    with pytest.raises(Exception) as exc:
        second.acquire()
    assert exc.value.code == E_STAGE_BUSY
    first.release()
    # After release the lock can be acquired immediately (no waiting).
    second.acquire()
    second.release()


def test_layer_count_hard_limit(generated):
    budget = ResourceBudget(max_layers_per_stage=3)
    pipeline = _pipeline(generated, budget)
    with pytest.raises(Exception) as exc:
        pipeline.run_all()
    assert exc.value.code == E_RESOURCE_LIMIT


def test_memory_hard_limit_triggers_resource_error(generated):
    # Each layer is 8x8 = 256 floats = 1024 bytes; force a tiny hard limit and
    # disable streaming so the hard guard fires.
    budget = ResourceBudget(
        max_resident_dequant_bytes=512,
        stream_dequant_bytes=1,
    )
    pipeline = _pipeline(generated, budget)
    with pytest.raises(Exception) as exc:
        pipeline.run_all()
    assert exc.value.code == E_RESOURCE_LIMIT


def test_streaming_degradation_does_not_change_results(generated):
    normal = _pipeline(
        generated, ResourceBudget()
    ).run_all().to_dict()

    streaming_budget = ResourceBudget(
        stream_dequant_bytes=1,
        max_resident_dequant_bytes=64 * 1024 * 1024,
    )
    streamed = _pipeline(
        generated, streaming_budget, work_suffix="_stream"
    ).run_all().to_dict()

    assert streamed["degraded_mode"] is True
    categories = {
        cat
        for stage in streamed["stage_results"]
        for cat in stage["degradation_categories"]
    }
    assert any(cat.startswith("streaming_") for cat in categories)
    # Streaming is only a scheduling/memory strategy: results stay identical.
    assert (
        streamed["prediction_checksum"] == normal["prediction_checksum"]
    )
    assert streamed["quant_errors"] == normal["quant_errors"]


def test_cache_entry_budget(generated):
    budget = ResourceBudget(max_cache_entries=1)
    pipeline = _pipeline(generated, budget)
    with pytest.raises(Exception) as exc:
        pipeline.run_all()
    assert exc.value.code == E_RESOURCE_LIMIT


def _pipeline(generated, budget, *, work_suffix=None):
    from qpipe import QuantPipeline

    work_dir = generated["work_dir"] + (work_suffix or "")
    return QuantPipeline(
        generated["data_dir"],
        work_dir,
        budget=budget,
        context=RunContext(seed=generated["seed"]),
    )
