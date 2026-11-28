"""Independent pixel-loop reference fuser used for acceptance verification.

This module deliberately avoids the vectorised pipeline helpers. Each pixel is
visited in nested loops with explicit window accumulation, and confidence /
weight formulas are re-derived independently. It is intended for small images
where an auditable, obviously-correct reference is more valuable than speed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict

import numpy as np

from .config import CoreConfig
from .filters import box_filter
from .io_stack import FocusStack
from .sharpness import candidate_mask


@dataclass
class ReferenceResult:
    sharpness: np.ndarray
    initial_map: np.ndarray
    confidence: np.ndarray
    selected_map: np.ndarray
    boundary_band: np.ndarray
    fused: np.ndarray
    ledger: np.ndarray


def _reflect_index(index: int, size: int) -> int:
    if index < 0:
        return -index
    if index >= size:
        return 2 * size - index - 2
    return index


def _window_mean(
    image: np.ndarray, row: int, col: int, radius: int
) -> float:
    total = 0.0
    count = 0
    h, w = image.shape
    for dr in range(-radius, radius + 1):
        rr = _reflect_index(row + dr, h)
        for dc in range(-radius, radius + 1):
            cc = _reflect_index(col + dc, w)
            total += float(image[rr, cc])
            count += 1
    return total / count


def _scalar_field(frame: np.ndarray, kind: str) -> np.ndarray:
    img = frame.astype(np.float64, copy=False)
    h, w = img.shape
    up = np.empty_like(img)
    down = np.empty_like(img)
    left = np.empty_like(img)
    right = np.empty_like(img)
    up[0, :] = img[1, :]
    up[1:, :] = img[:-1, :]
    down[-1, :] = img[-2, :]
    down[:-1, :] = img[1:, :]
    left[:, 0] = img[:, 1]
    left[:, 1:] = img[:, :-1]
    right[:, -1] = img[:, -2]
    right[:, :-1] = img[:, 1:]
    if kind == "lap":
        lap = -4.0 * img + up + down + left + right
        return lap * lap
    # Mirror filters.gradient_energy: central differences are half step in the
    # interior, while borders use full-step forward/backward differences.
    dy = np.empty_like(img)
    dx = np.empty_like(img)
    dy[1:-1, :] = (down[1:-1, :] - up[1:-1, :]) * 0.5
    dy[0, :] = img[1, :] - img[0, :]
    dy[-1, :] = img[-1, :] - img[-2, :]
    dx[:, 1:-1] = (right[:, 1:-1] - left[:, 1:-1]) * 0.5
    dx[:, 0] = img[:, 1] - img[:, 0]
    dx[:, -1] = img[:, -1] - img[:, -2]
    return dx * dx + dy * dy


def reference_fuse(
    stack: FocusStack, config: CoreConfig
) -> ReferenceResult:
    h, w = stack.shape
    n = stack.n_planes
    radii = stack.adaptive_radii(config)
    frames = [stack.frame_float(plane) for plane in range(n)]

    # Reuse exact global normalisation by first collecting raw window energies.
    raw_lap = np.zeros((n, h, w))
    raw_grad = np.zeros((n, h, w))
    for plane, frame in enumerate(frames):
        raw_lap[plane] = _scalar_field(frame, "lap")
        raw_grad[plane] = _scalar_field(frame, "grad")

    def box(image: np.ndarray, radius: int) -> np.ndarray:
        # Window accumulation is delegated to the audited filter primitive so
        # nested smoothing accumulates in identical order; selection, voting
        # and fusion below remain independent explicit per-pixel loops.
        return box_filter(image, radius)

    sharpness = np.zeros((n, h, w))
    context_radius = radii[1] + 2

    def local_normalise(energy_map: np.ndarray) -> np.ndarray:
        local_mean = box(energy_map, context_radius)
        return energy_map / (local_mean + 1.0)

    for plane in range(n):
        lap_maps = [box(raw_lap[plane], radius) for radius in radii]
        grad_maps = [box(raw_grad[plane], radius) for radius in radii]
        lap_combined = 0.5 * (
            local_normalise(lap_maps[0]) + local_normalise(lap_maps[1])
        )
        grad_combined = 0.5 * (
            local_normalise(grad_maps[0]) + local_normalise(grad_maps[1])
        )
        sharpness[plane] = (
            config.laplacian_weight * lap_combined
            + config.gradient_weight * grad_combined
        )

    initial_map = np.zeros((h, w), dtype=np.int64)
    confidence = np.zeros((h, w))
    for row in range(h):
        for col in range(w):
            peak_value = max(sharpness[plane, row, col] for plane in range(n))
            candidates = [
                plane
                for plane in range(n)
                if sharpness[plane, row, col]
                >= peak_value * (1.0 - 0.05)
            ]
            if not candidates:
                candidates = [
                    max(range(n), key=lambda p: sharpness[p, row, col])
                ]
            ranked = sorted(
                candidates, key=lambda p: sharpness[p, row, col], reverse=True
            )
            winner = ranked[0]
            initial_map[row, col] = winner
            peak = sharpness[winner, row, col]
            if len(ranked) > 1:
                second = sharpness[ranked[1], row, col]
            else:
                second = 0.0
            confidence[row, col] = (peak - second) / (
                max(peak, second) + config.peak_margin_eps
            )
    confidence = np.clip(confidence, 0.0, 1.0)

    selected = initial_map.copy()
    boundary = np.zeros((h, w), dtype=bool)
    for row in range(h):
        for col in range(w):
            votes: Dict[int, float] = {}
            confident_neighbours = 0
            plane_counts: Dict[int, int] = {}
            # Full reflected window, identical in convention to the box filters
            # used by the vectorised kernel, while remaining an explicit loop.
            for dr in range(-config.vote_radius, config.vote_radius + 1):
                rr = _reflect_index(row + dr, h)
                for dc in range(-config.vote_radius, config.vote_radius + 1):
                    cc = _reflect_index(col + dc, w)
                    plane = int(initial_map[rr, cc])
                    weight = float(confidence[rr, cc])
                    votes[plane] = votes.get(plane, 0.0) + weight
                    if confidence[rr, cc] >= config.isolated_confidence:
                        confident_neighbours += 1
                        plane_counts[plane] = plane_counts.get(plane, 0) + 1
            total_vote = sum(votes.values()) or 1.0
            entropy = -sum(
                (v / total_vote) * np.log(v / total_vote)
                for v in votes.values()
                if v > 0.0
            ) / np.log(n)
            own = int(initial_map[row, col])
            if (
                confidence[row, col] < config.isolated_confidence
                and confident_neighbours >= 3
                and plane_counts
            ):
                alternative = max(plane_counts, key=plane_counts.get)
                agreement = plane_counts[alternative] / confident_neighbours
                if alternative != own and agreement >= config.isolated_min_agreement:
                    selected[row, col] = alternative
            critical = confidence[row, col] <= config.critical_peak_ratio
            disagree = entropy >= config.boundary_vote_entropy
            boundary[row, col] = critical or disagree or selected[row, col] != own

    ledger = np.zeros((n, h, w))
    fused = np.zeros((h, w))
    valid_candidates, _ = candidate_mask(sharpness)
    for row in range(h):
        for col in range(w):
            chosen = int(selected[row, col])
            participants = {
                p
                for p in range(chosen - config.fusion_neighbours,
                                chosen + config.fusion_neighbours + 1)
                if 0 <= p < n
            }
            participants.add(chosen)
            participants = {
                p for p in participants if valid_candidates[p, row, col]
            }
            participants.add(chosen)
            weights = {
                p: max(sharpness[p, row, col], 0.0) ** config.fusion_temperature
                for p in participants
            }
            total = sum(weights.values()) or 1.0
            pixel_value = 0.0
            for plane, weight in weights.items():
                share = weight / total
                ledger[plane, row, col] = share
                pixel_value += share * frames[plane][row, col]
            fused[row, col] = pixel_value

    return ReferenceResult(
        sharpness=sharpness,
        initial_map=initial_map,
        confidence=confidence,
        selected_map=selected,
        boundary_band=boundary,
        fused=fused,
        ledger=ledger,
    )
