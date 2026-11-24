"""Bounded execution and in-flight buffer accounting."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Dict

from .errors import E_RESOURCE_LIMIT, fail


@dataclass
class ResourceUsage:
    peak_inflight_pixels: int = 0
    peak_spectrum_bytes: int = 0
    peak_active_workers: int = 0

    def to_dict(self) -> Dict[str, int]:
        return {
            "peak_inflight_pixels": self.peak_inflight_pixels,
            "peak_spectrum_bytes": self.peak_spectrum_bytes,
            "peak_active_workers": self.peak_active_workers,
        }


class ResourceGate:
    """Weighted memory gate used around tile processing."""

    def __init__(
        self,
        max_workers: int = 2,
        max_inflight_pixels: int = 2_000_000,
        max_spectrum_bytes: int = 64 * 1024 * 1024,
    ) -> None:
        self.max_workers = max(1, int(max_workers))
        self.max_inflight_pixels = max(1, int(max_inflight_pixels))
        self.max_spectrum_bytes = max(4096, int(max_spectrum_bytes))
        self._condition = threading.Condition()
        self._inflight_pixels = 0
        self._spectrum_bytes = 0
        self._active_workers = 0
        self.usage = ResourceUsage()

    def acquire(self, pixel_slots: int, spectrum_bytes: int) -> None:
        pixel_slots = int(pixel_slots)
        spectrum_bytes = int(spectrum_bytes)
        if pixel_slots > self.max_inflight_pixels:
            raise fail(
                E_RESOURCE_LIMIT,
                "single tile pixel requirement exceeds in-flight budget",
                required_pixels=pixel_slots,
                limit_pixels=self.max_inflight_pixels,
            )
        if spectrum_bytes > self.max_spectrum_bytes:
            raise fail(
                E_RESOURCE_LIMIT,
                "single tile spectrum requirement exceeds buffer budget",
                required_bytes=spectrum_bytes,
                limit_bytes=self.max_spectrum_bytes,
            )
        with self._condition:
            while (
                self._inflight_pixels + pixel_slots > self.max_inflight_pixels
                or self._spectrum_bytes + spectrum_bytes > self.max_spectrum_bytes
                or self._active_workers >= self.max_workers
            ):
                self._condition.wait()
            self._inflight_pixels += pixel_slots
            self._spectrum_bytes += spectrum_bytes
            self._active_workers += 1
            self.usage.peak_inflight_pixels = max(
                self.usage.peak_inflight_pixels, self._inflight_pixels
            )
            self.usage.peak_spectrum_bytes = max(
                self.usage.peak_spectrum_bytes, self._spectrum_bytes
            )
            self.usage.peak_active_workers = max(
                self.usage.peak_active_workers, self._active_workers
            )

    def release(self, pixel_slots: int, spectrum_bytes: int) -> None:
        with self._condition:
            self._inflight_pixels -= int(pixel_slots)
            self._spectrum_bytes -= int(spectrum_bytes)
            self._active_workers -= 1
            if self._inflight_pixels < 0 or self._spectrum_bytes < 0 or self._active_workers < 0:
                raise RuntimeError("resource gate accounting became negative")
            self._condition.notify_all()

    def estimate(self, width: int, height: int, overlap_h: int, overlap_w: int) -> tuple[int, int]:
        tile_pixels = int(width) * int(height)
        overlap_pixels = max(1, int(overlap_h) * int(overlap_w))
        # source, corrected, aligned, weight and contribution plus work arrays.
        pixel_slots = tile_pixels * 6 + overlap_pixels * 4
        fft_h = 1
        fft_w = 1
        while fft_h < 2 * max(1, overlap_h) - 1:
            fft_h <<= 1
        while fft_w < 2 * max(1, overlap_w) - 1:
            fft_w <<= 1
        # Two complex64-equivalent plans and several real response arrays.
        spectrum_bytes = fft_h * fft_w * (16 * 3 + 8 * 3)
        return pixel_slots, spectrum_bytes
