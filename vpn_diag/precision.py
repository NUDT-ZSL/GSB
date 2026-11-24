"""Fixed-point and float64 reference latency/cost estimates."""
from __future__ import annotations

from typing import Any

from .fixedpoint import FIXED_SCALE, q_to_float
from .model import LinkProfile, PeerConfig


def _floor_mul(left_q: int, right_q: int, scale: int) -> tuple[int, int]:
    product = left_q * right_q
    value = product // scale
    remainder = product - value * scale
    return value, 1 if remainder else 0


def estimate_fragment_cost(
    *,
    fragment_length: int,
    crypto_overhead: int,
    peer: PeerConfig,
    profile: LinkProfile,
    scale: int = FIXED_SCALE,
) -> dict[str, Any]:
    wire_bytes = fragment_length + crypto_overhead

    rtt_q = profile.rtt_q
    rtt_f = profile.rtt_f
    rtt_error = 1 if abs(rtt_f - q_to_float(rtt_q, scale)) > 0.0 else 0

    loss_q, loss_error = _floor_mul(rtt_q, profile.loss_q, scale)
    loss_f = rtt_f * profile.loss_f

    queue_numerator = wire_bytes * 8 * scale * scale
    queue_q = queue_numerator // profile.bandwidth_q
    queue_remainder = queue_numerator - queue_q * profile.bandwidth_q
    queue_error = 1 if queue_remainder else 0
    queue_f = wire_bytes * 8.0 / profile.bandwidth_f

    jitter_q, jitter_error = _floor_mul(rtt_q, profile.jitter_q, scale)
    jitter_f = rtt_f * profile.jitter_f

    subtotal_q = rtt_q + loss_q + queue_q + jitter_q
    subtotal_f = rtt_f + loss_f + queue_f + jitter_f
    subtotal_error = rtt_error + loss_error + queue_error + jitter_error

    if peer.link_weight_q == scale:
        weighted_q = subtotal_q
        weight_error = 0
    else:
        weighted_q, weight_error = _floor_mul(subtotal_q, peer.link_weight_q, scale)
    weighted_f = subtotal_f * peer.link_weight_f
    error_bound_q = subtotal_error + weight_error

    components = {
        "rtt": {
            "fixed_q": rtt_q,
            "fixed_ms": q_to_float(rtt_q, scale),
            "reference_ms": rtt_f,
            "error_bound_q": rtt_error,
            "error_bound_ms": q_to_float(rtt_error, scale),
        },
        "loss_penalty": {
            "fixed_q": loss_q,
            "fixed_ms": q_to_float(loss_q, scale),
            "reference_ms": loss_f,
            "error_bound_q": loss_error,
            "error_bound_ms": q_to_float(loss_error, scale),
        },
        "bandwidth_queue": {
            "fixed_q": queue_q,
            "fixed_ms": q_to_float(queue_q, scale),
            "reference_ms": queue_f,
            "error_bound_q": queue_error,
            "error_bound_ms": q_to_float(queue_error, scale),
        },
        "jitter": {
            "fixed_q": jitter_q,
            "fixed_ms": q_to_float(jitter_q, scale),
            "reference_ms": jitter_f,
            "error_bound_q": jitter_error,
            "error_bound_ms": q_to_float(jitter_error, scale),
        },
        "link_weight": {
            "fixed_q": weighted_q,
            "fixed_ms": q_to_float(weighted_q, scale),
            "reference_ms": weighted_f,
            "error_bound_q": weight_error,
            "error_bound_ms": q_to_float(weight_error, scale),
        },
    }

    return {
        "wire_bytes": wire_bytes,
        "estimate_q": weighted_q,
        "estimate_ms": q_to_float(weighted_q, scale),
        "reference_ms": weighted_f,
        "residual_ms": weighted_f - q_to_float(weighted_q, scale),
        "error_bound_q": error_bound_q,
        "error_bound_ms": q_to_float(error_bound_q, scale),
        "components": components,
        "rto_q": max(0, weighted_q),
        "rto_reference_ms": weighted_f,
    }


def rto_pair(estimate: dict[str, Any], clock_step_q: int, clock_step_f: float,
             scale: int = FIXED_SCALE) -> tuple[int, float]:
    rto_q = max(clock_step_q, int(estimate["estimate_q"]))
    rto_f = max(clock_step_f, float(estimate["reference_ms"]))
    return rto_q, rto_f


def margin_pair(limit_q: int, used_q: int, cost_q: int,
                limit_f: float, used_f: float, cost_f: float,
                scale: int = FIXED_SCALE) -> dict[str, Any]:
    fixed_margin_q = limit_q - used_q - cost_q
    reference_margin_f = limit_f - used_f - cost_f
    fixed_sendable = fixed_margin_q >= 0
    reference_sendable = reference_margin_f >= 0.0
    return {
        "fixed_margin_q": fixed_margin_q,
        "fixed_margin_ms": q_to_float(fixed_margin_q, scale),
        "reference_margin_ms": reference_margin_f,
        "fixed_sendable": fixed_sendable,
        "reference_sendable": reference_sendable,
        "divergent": fixed_sendable != reference_sendable,
    }
