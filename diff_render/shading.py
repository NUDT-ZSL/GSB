"""Simplified microfacet shading with hand-written analytic backpropagation.

The model is a Lambertian diffuse term plus a GGX/Smith specular term under
two directional lights.  Every derivative below is derived analytically with
the chain rule and implemented directly on NumPy arrays; no numerical
differentiation, autodiff framework or third-party renderer is involved.

All math is float64 and uses only element-wise NumPy ufuncs plus fixed-order
array sums, so results are independent of thread scheduling.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any, Dict, Tuple

import numpy as np


@dataclass
class ForwardContext:
    """Intermediate arrays reused by the analytic backward pass."""

    color: np.ndarray  # (S, 3) per-sample RGB radiance
    per_light: list
    diffuse_factor: np.ndarray
    ambient: float


def _safe_clip(value: np.ndarray) -> np.ndarray:
    return np.maximum(value, 0.0)


def forward_samples(params: np.ndarray, geometry: Dict[str, Any]) -> ForwardContext:
    """Evaluate per-sample radiance for parameters ``[albedo, roughness]``."""
    albedo = float(params[0])
    roughness = float(params[1])
    alpha = roughness * roughness
    alpha_sq = alpha * alpha
    normals = geometry["normals"]
    view = geometry["view"]
    f0 = geometry["f0"]
    ambient = geometry["ambient"]

    diffuse_factor = albedo / math.pi
    color = np.full(
        (normals.shape[0], 3), ambient, dtype=np.float64
    )  # constant ambient term
    per_light = []

    for light_direction, light_color in geometry["lights"]:
        half_vector = normals + view[None, :] + light_direction[None, :]
        half_norm = np.sqrt(np.sum(half_vector * half_vector, axis=-1, keepdims=True))
        half_vector = half_vector / half_norm

        no_l = _safe_clip(np.sum(normals * light_direction[None, :], axis=-1))
        no_v = _safe_clip(np.sum(normals * view[None, :], axis=-1))
        no_h = _safe_clip(np.sum(normals * half_vector, axis=-1))
        v_dot_h = _safe_clip(np.sum(view[None, :] * half_vector, axis=-1))

        # GGX / Trowbridge-Reitz normal distribution function.
        denom = (no_h * no_h) * (alpha_sq - 1.0) + 1.0
        distribution = alpha_sq / (math.pi * denom * denom)

        # Smith geometry with the UE4 Schlick-GGX approximation.
        k = alpha * 0.5
        g1_v = no_v / (no_v * (1.0 - k) + k)
        g1_l = no_l / (no_l * (1.0 - k) + k)
        geometry_term = g1_v * g1_l

        # Fresnel-Schlick.
        fresnel = f0 + (1.0 - f0) * np.power(1.0 - v_dot_h, 5)

        specular = (distribution * geometry_term * fresnel) / (
            4.0 * np.maximum(no_l, 1.0e-12) * np.maximum(no_v, 1.0e-12)
        )
        diffuse = diffuse_factor * no_l
        radiance = (diffuse[:, None] + specular[:, None]) * light_color[None, :]
        color = color + radiance

        per_light.append(
            {
                "light_color": light_color,
                "no_l": no_l,
                "no_v": no_v,
                "no_h": no_h,
                "v_dot_h": v_dot_h,
                "denom": denom,
                "distribution": distribution,
                "geometry_term": geometry_term,
                "fresnel": fresnel,
                "specular": specular,
                "alpha": alpha,
                "k": k,
                "g1_v": g1_v,
                "g1_l": g1_l,
            }
        )

    return ForwardContext(color=color, per_light=per_light,
                          diffuse_factor=diffuse_factor, ambient=ambient)


def _backward_light(cache: Dict[str, Any], d_bsdf: np.ndarray) -> Tuple[float, float]:
    """Return ``(d/d albedo, d/d roughness)`` contributions for one light.

    The forward specular term is
    ``specular = D * G * F / (4 * NoL * NoV)``.  All factors that depend on
    ``alpha = roughness^2`` (including the Smith denominator) are differentiated
    analytically here.  The Fresnel factor carries no roughness dependence.
    """
    alpha = cache["alpha"]
    k = cache["k"]
    no_l = cache["no_l"]
    no_v = cache["no_v"]
    no_h = cache["no_h"]
    denom = cache["denom"]
    distribution = cache["distribution"]
    geometry_term = cache["geometry_term"]
    g1_v = cache["g1_v"]
    g1_l = cache["g1_l"]

    safe_no_l = np.maximum(no_l, 1.0e-12)
    safe_no_v = np.maximum(no_v, 1.0e-12)
    denominator = 4.0 * safe_no_l * safe_no_v

    # d(specular)/d(...) where specular = (D G F) / denominator
    d_dgf = d_bsdf / denominator
    d_distribution = d_dgf * geometry_term * cache["fresnel"]
    d_geometry_term = d_dgf * distribution * cache["fresnel"]

    # Chain through the 1/(4 NoL NoV) denominator: NoL/NoV depend on alpha only
    # through the Smith G1 functions below (the raw clamped cosines themselves
    # are geometry-only), so the explicit denominator's cosine factors have no
    # direct alpha derivative and this term contributes zero parameter gradient.

    # --- GGX distribution D = alpha^2 / (pi * denom^2) ---------------------
    # dD/d(denom) = -2 D / denom; denom = 1 + no_h^2 (alpha^2 - 1)
    d_denom = -2.0 * d_distribution * distribution / denom
    d_alpha_d = float(
        np.sum(d_denom * (2.0 * alpha * no_h * no_h))
        + np.sum(2.0 * d_distribution * distribution / alpha)
    )

    # --- Smith geometry G = G1(NoV) * G1(NoL); G1(x) = x / (x(1-k)+k) ------
    d_g1_v = d_geometry_term * g1_l
    d_g1_l = d_geometry_term * g1_v
    denom_g_v = no_v * (1.0 - k) + k
    denom_g_l = no_l * (1.0 - k) + k
    # dG1/dk = -x(1-x) / denom_g^2 = -G1 (1-x) / denom_g
    d_k = (
        np.sum(d_g1_v * (-g1_v * (1.0 - no_v) / denom_g_v))
        + np.sum(d_g1_l * (-g1_l * (1.0 - no_l) / denom_g_l))
    )
    d_alpha_g = float(d_k * 0.5)  # k = alpha / 2

    d_alpha = d_alpha_d + d_alpha_g
    # alpha = roughness^2  =>  d/d(roughness) = 2 roughness d/d(alpha)
    d_roughness = 2.0 * np.sqrt(alpha) * d_alpha
    return 0.0, float(d_roughness)


def backward_samples(
    cache: ForwardContext,
    d_color: np.ndarray,
    geometry: Dict[str, Any],
) -> np.ndarray:
    """Backpropagate per-sample color gradients to ``[albedo, roughness]``."""
    d_albedo = 0.0
    d_roughness = 0.0

    # Ambient term is parameter-independent.
    for light_cache in cache.per_light:
        light_color = light_cache["light_color"]
        no_l = light_cache["no_l"]

        # radiance = (diffuse + specular) * light_color
        d_bsdf = np.sum(d_color * light_color[None, :], axis=-1)

        # diffuse = (albedo / pi) * no_l
        d_albedo += float(np.sum(d_bsdf * no_l / math.pi))

        light_cache = dict(light_cache)
        light_cache["f0"] = geometry["f0"]
        light_d_albedo, light_d_roughness = _backward_light(light_cache, d_bsdf)
        d_albedo += light_d_albedo
        d_roughness += light_d_roughness

    return np.array([d_albedo, d_roughness], dtype=np.float64)


def aggregate_pixels(
    color: np.ndarray, pixel_index: np.ndarray, pixel_count: int
) -> np.ndarray:
    """Average subpixel colors per pixel in canonical flattened pixel order."""
    image = np.zeros((pixel_count, 3), dtype=np.float64)
    # Fixed-order accumulation (pixel index is globally sorted); np.add.at is
    # order-independent and never relies on reduction ordering.
    np.add.at(image, pixel_index, color)
    samples_per_pixel = color.shape[0] // pixel_count
    return image / samples_per_pixel


def render_image(params: np.ndarray, geometry: Dict[str, Any]) -> np.ndarray:
    """Full deterministic forward pass returning the flattened RGB image."""
    cache = forward_samples(params, geometry)
    return aggregate_pixels(cache.color, geometry["pixel_index"], geometry["pixel_count"])


def loss_and_grad(
    params: np.ndarray, geometry: Dict[str, Any], target: np.ndarray
) -> Tuple[float, np.ndarray, np.ndarray]:
    """Return ``(loss, analytic_gradient, image)``.

    Loss is the per-pixel squared color difference summed over all pixels and
    channels: ``L = sum_pixels sum_channels (render - target)^2``.
    """
    cache = forward_samples(params, geometry)
    image = aggregate_pixels(cache.color, geometry["pixel_index"], geometry["pixel_count"])
    residual = image - target
    loss = float(np.sum(residual * residual))

    samples_per_pixel = geometry["samples"]
    # dL/d(image) = 2 residual; pixel averaging propagates 1/S to each sample.
    d_image = 2.0 * residual
    d_color = d_image[geometry["pixel_index"]] / samples_per_pixel
    gradient = backward_samples(cache, d_color, geometry)
    return loss, gradient, image


def finite_difference_grad(
    params: np.ndarray,
    geometry: Dict[str, Any],
    target: np.ndarray,
    step: float,
) -> np.ndarray:
    """Central finite-difference reference gradient (used only for checking)."""
    reference = np.empty_like(params)
    for index in range(params.shape[0]):
        plus = params.copy()
        minus = params.copy()
        plus[index] += step
        minus[index] -= step
        loss_plus = float(
            np.sum((render_image(plus, geometry) - target) ** 2)
        )
        loss_minus = float(
            np.sum((render_image(minus, geometry) - target) ** 2)
        )
        reference[index] = (loss_plus - loss_minus) / (2.0 * step)
    return reference
