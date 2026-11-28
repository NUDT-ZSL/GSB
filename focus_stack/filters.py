"""Deterministic local image filters implemented with NumPy only.

Box filters use a padded cumulative sum. The same arithmetic is used by the
full pipeline, the incremental path and the reference fuser, which guarantees
bit identical results regardless of which rectangular region is processed.
"""

from __future__ import annotations

import numpy as np


def reflect_pad(array: np.ndarray, pad: int) -> np.ndarray:
    """Pad a 2D array with edge reflection (``numpy.pad`` reflect mode)."""
    if pad <= 0:
        return array
    return np.pad(array, pad, mode="reflect")


def box_sum(padded: np.ndarray, radius: int) -> np.ndarray:
    """Sum every ``(2r+1) x (2r+1)`` window of an already padded array.

    Parameters
    ----------
    padded:
        Array padded by at least ``radius`` on every side.
    radius:
        Half window size. The returned shape is
        ``padded.shape - 2*radius``.
    """
    if radius < 0:
        raise ValueError("box radius must be non-negative")
    if radius == 0:
        return padded.astype(np.float64, copy=True)
    integral = padded.astype(np.float64, copy=False).cumsum(axis=0).cumsum(axis=1)
    # Insert a zero row/column so window sums use simple index differences.
    integral = np.pad(integral, ((1, 0), (1, 0)), mode="constant", constant_values=0.0)
    size = 2 * radius + 1
    total = (
        integral[size:, size:]
        - integral[:-size, size:]
        - integral[size:, :-size]
        + integral[:-size, :-size]
    )
    return total


def box_filter(array: np.ndarray, radius: int) -> np.ndarray:
    """Reflect-padded box *mean* filter for a 2D array."""
    if radius < 0:
        raise ValueError("box radius must be non-negative")
    if radius == 0:
        return array.astype(np.float64, copy=True)
    padded = reflect_pad(array.astype(np.float64, copy=False), radius)
    window = box_sum(padded, radius)
    return window / float((2 * radius + 1) ** 2)


def box_filter_region(
    array: np.ndarray, radius: int, row0: int, row1: int, col0: int, col1: int
) -> np.ndarray:
    """Box mean restricted to a core rectangle.

    The crop is padded from the full ``array`` so border handling is identical
    to :func:`box_filter`; only the requested core pixels are returned.
    Bounds are clipped to the image and must describe a non-empty rectangle.
    """
    h, w = array.shape
    row0 = max(0, min(row0, h))
    row1 = max(0, min(row1, h))
    col0 = max(0, min(col0, w))
    col1 = max(0, min(col1, w))
    if row1 <= row0 or col1 <= col0:
        raise ValueError("empty filter region")
    if radius == 0:
        return array[row0:row1, col0:col1].astype(np.float64, copy=True)
    # Reuse the exact same integral-image arithmetic as the full-image filter
    # and crop afterwards. This guarantees bit identical values on every pixel,
    # which is required for the incremental/full recompute equivalence.
    return box_filter(array, radius)[row0:row1, col0:col1]


def laplacian_energy(image: np.ndarray) -> np.ndarray:
    """Four-neighbour discrete Laplacian, squared and returned as float64."""
    img = image.astype(np.float64, copy=False)
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
    laplacian = -4.0 * img + up + down + left + right
    return laplacian * laplacian


def gradient_energy(image: np.ndarray) -> np.ndarray:
    """Squared central-difference gradient magnitude (reflected borders)."""
    img = image.astype(np.float64, copy=False)
    h, w = img.shape
    dy = np.empty_like(img)
    dx = np.empty_like(img)
    dy[1:-1, :] = (img[2:, :] - img[:-2, :]) * 0.5
    dy[0, :] = img[1, :] - img[0, :]
    dy[-1, :] = img[-1, :] - img[-2, :]
    dx[:, 1:-1] = (img[:, 2:] - img[:, :-2]) * 0.5
    dx[:, 0] = img[:, 1] - img[:, 0]
    dx[:, -1] = img[:, -1] - img[:, -2]
    return dx * dx + dy * dy


def local_variance(image: np.ndarray, radius: int) -> np.ndarray:
    """Local variance using ``E[x^2] - E[x]^2`` with reflected borders."""
    img = image.astype(np.float64, copy=False)
    mean = box_filter(img, radius)
    mean_sq = box_filter(img * img, radius)
    variance = mean_sq - mean * mean
    np.maximum(variance, 0.0, out=variance)
    return variance
