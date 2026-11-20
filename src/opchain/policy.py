"""Explicit policy adjudication between latency, precision and memory."""

from .errors import ErrorCode, OpChainError


def dtype_itemsize(dtype_name):
    return 8 if dtype_name == "float64" else 4


def estimate_latency_us(element_count, op_count, transpose_count, coeffs):
    """Deterministic estimate from element/op counts (observable counterpart)."""
    return (
        element_count * coeffs["element_ns"] / 1000.0
        + op_count * coeffs["op_us"]
        + transpose_count * coeffs["transpose_us"]
    )


def build_estimate(spec, block_size, coeffs):
    element_count = 1
    for dim in spec.input_shape:
        element_count *= dim
    op_count = len(spec.op_chain)
    transpose_count = sum(1 for op in spec.op_chain if op["op"] == "transpose")
    block_count = sum(
        _stage_element_count(spec, stage) // block_size
        + (1 if _stage_element_count(spec, stage) % block_size else 0)
        for stage in range(op_count)
    )
    estimated_us = estimate_latency_us(
        element_count, op_count, transpose_count, coeffs
    )
    return {
        "element_count": element_count,
        "op_count": op_count,
        "transpose_count": transpose_count,
        "block_size": int(block_size),
        "estimated_block_count": int(block_count),
        "estimated_latency_us": float(estimated_us),
        "estimated_latency_ms": float(estimated_us / 1000.0),
        "coefficients": dict(coeffs),
    }


def _stage_element_count(spec, stage):
    total = 1
    for dim in spec.input_shape:
        total *= dim
    return total


def _resident_bytes(spec, block_size):
    # Two block buffers (input + output) plus one full staged tensor, in bytes.
    itemsize = dtype_itemsize(spec.dtype)
    total = _stage_element_count(spec, 0)
    return (total + 2 * block_size) * itemsize


def adjudicate_block_size(spec, config):
    """Select a block size by explicit priority rules and resource ceilings."""
    requested = spec.block_size
    candidates = sorted({requested, *config.block_candidates}, reverse=True)
    feasible = []
    degraded = []
    for candidate in candidates:
        resident = _resident_bytes(spec, candidate)
        if resident > config.max_resident_bytes:
            continue
        feasible.append(candidate)

    if not feasible:
        raise OpChainError(
            ErrorCode.RESOURCE_LIMIT,
            "no block candidate fits the resident-memory ceiling",
            {
                "requested": requested,
                "max_resident_bytes": config.max_resident_bytes,
                "allow_degrade": config.allow_degrade,
            },
        )

    if requested in feasible:
        chosen = requested
    else:
        if not config.allow_degrade:
            raise OpChainError(
                ErrorCode.RESOURCE_LIMIT,
                "requested block size exceeds the resident-memory ceiling and "
                "degradation is disabled",
                {"requested": requested, "max_resident_bytes": config.max_resident_bytes},
            )
        chosen = _score(feasible, spec, config)
        degraded.append(
            {
                "category": "memory_forced_block_downgrade",
                "requested": requested,
                "chosen": chosen,
                "reason": "requested block exceeds resident-memory ceiling; "
                "priority rules selected the best feasible fallback",
            }
        )

    rationale = _rationale(spec, config, feasible if feasible else [chosen], chosen)
    return {
        "requested_block_size": requested,
        "selected_block_size": int(chosen),
        "priority": list(config.priority),
        "resident_bytes": _resident_bytes(spec, chosen),
        "max_resident_bytes": config.max_resident_bytes,
        "rationale": rationale,
        "degradations": degraded,
    }


def _score(candidates, spec, config):
    """Lexicographic score driven by the configured explicit priority order."""
    ranked = []
    total_elements = _stage_element_count(spec, 0)
    for candidate in candidates:
        metrics = {
            # Latency: fewer blocks => lower first-case latency.
            "latency": -(-total_elements // candidate),
            # Memory: smaller resident footprint is better.
            "memory": _resident_bytes(spec, candidate),
            # Precision: larger blocks keep more contiguous statistics locally.
            "precision": -candidate,
        }
        ranked.append((tuple(metrics[item] for item in config.priority), candidate))
    ranked.sort(key=lambda item: (item[0], -item[1]))
    return ranked[0][1]


def _rationale(spec, config, feasible, chosen):
    return [
        f"priority order: {' > '.join(config.priority)}",
        f"feasible block sizes under {config.max_resident_bytes} bytes: {sorted(feasible)}",
        f"selected block size {chosen} with resident estimate "
        f"{_resident_bytes(spec, chosen)} bytes",
        "larger blocks reduce boundary overhead but increase residency and "
        "transpose layout irregularity; smaller blocks reduce first-case "
        "latency but raise reduction/verification overhead",
    ]
