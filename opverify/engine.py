from __future__ import annotations

from pathlib import Path
from typing import Any

from .cache import build_cache_key
from .chunked import run_chunked
from .contracts import normalize_task, parameter_digest
from .errors import OpVerifyError, error_response
from .hashing import array_content_hash
from .planner import build_plan
from .reference import reference_task
from .samples import load_arrays
from .verify import compare_outputs, declared_tolerance


def evaluate_job(
    record: dict[str, Any],
    *,
    base_dir: str | Path | None = None,
    prior_revision: int | None = None,
    upstream_artifacts: list[dict[str, str]] | None = None,
    temp_dir: str | Path | None = None,
) -> dict[str, Any]:
    task = normalize_task(record, prior_revision=prior_revision)
    arrays = load_arrays(task, base_dir)
    input_hash = array_content_hash(arrays["input"])
    weight_hash = array_content_hash(arrays["weight"]) if "weight" in arrays else None

    reference = reference_task(task, arrays)
    plan = build_plan(task)
    chunk_output = run_chunked(task, arrays, plan=plan, temp_dir=temp_dir)
    comparison = compare_outputs(reference, chunk_output, **declared_tolerance(task))

    param_hash = parameter_digest(task)
    cache_key, cache_payload = build_cache_key(
        task,
        input_hash=input_hash,
        parameter_hash=param_hash,
        upstream_artifacts=upstream_artifacts,
        weight_hash=weight_hash,
    )
    reference_digest = array_content_hash(reference)
    output_digest = array_content_hash(chunk_output)

    return {
        "ok": True,
        "task": task,
        "arrays": arrays,
        "reference": reference,
        "output": chunk_output,
        "plan": plan,
        "comparison": comparison,
        "input_hash": input_hash,
        "weight_hash": weight_hash,
        "parameter_digest": param_hash,
        "reference_digest": reference_digest,
        "output_digest": output_digest,
        "cache_key": cache_key,
        "cache_payload": cache_payload,
    }


def run_unified(
    record: dict[str, Any],
    *,
    base_dir: str | Path | None = None,
    prior_revision: int | None = None,
    upstream_artifacts: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    try:
        result = evaluate_job(
            record,
            base_dir=base_dir,
            prior_revision=prior_revision,
            upstream_artifacts=upstream_artifacts,
        )
    except OpVerifyError as exc:
        return exc.to_dict()
    except FileNotFoundError as exc:
        return error_response(
            "E_ARTIFACT_MISSING",
            str(exc),
            field="$.input_file",
        )
    except ValueError as exc:
        return error_response(
            "E_SHAPE_INVALID",
            f"invalid matrix or operator value: {exc}",
            field="$.input_shape",
        )

    task = result["task"]
    return {
        "ok": True,
        "job_id": task["job_id"],
        "code": "OK",
        "revision": task["revision"],
        "op_kind": task["op_kind"],
        "output_shape": task["output_shape"],
        "output_digest": result["output_digest"],
        "reference_digest": result["reference_digest"],
        "input_hash": result["input_hash"],
        "parameter_digest": result["parameter_digest"],
        "cache_key": result["cache_key"],
        "comparison": result["comparison"],
        "plan": {
            "selected_block_size": result["plan"]["selected_block_size"],
            "selection_reason": result["plan"]["selection_reason"],
            "tile_count": len(result["plan"]["tiles"]),
            "plan_digest": result["plan"]["plan_digest"],
        },
    }
