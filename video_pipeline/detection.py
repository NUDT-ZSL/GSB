"""Sliding-window color/edge detector with deterministic NMS."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

import cv2
import numpy as np

from .config import DetectionConfig, WindowSpec


EPSILON = 1e-9


@dataclass(frozen=True)
class Candidate:
    class_name: str
    bbox: tuple[int, int, int, int]
    score: float
    stable_key: str
    window_id: str
    window_index: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "class_name": self.class_name,
            "bbox": list(self.bbox),
            "score": self.score,
            "stable_key": self.stable_key,
            "window_id": self.window_id,
            "window_index": self.window_index,
        }


@dataclass(frozen=True)
class WindowFeatures:
    color_ratio: float
    color_mean_similarity: float
    edge_density: float
    score: float
    passes_feature_thresholds: bool


def iou(box_a: Iterable[int | float], box_b: Iterable[int | float]) -> float:
    ax1, ay1, ax2, ay2 = (float(value) for value in box_a)
    bx1, by1, bx2, by2 = (float(value) for value in box_b)
    intersection_width = max(0.0, min(ax2, bx2) - max(ax1, bx1))
    intersection_height = max(0.0, min(ay2, by2) - max(ay1, by1))
    intersection = intersection_width * intersection_height
    if intersection <= 0.0:
        return 0.0
    area_a = max(0.0, ax2 - ax1) * max(0.0, ay2 - ay1)
    area_b = max(0.0, bx2 - bx1) * max(0.0, by2 - by1)
    union = area_a + area_b - intersection
    if union <= 0.0:
        return 0.0
    return intersection / union


def greater_or_equal(value: float, threshold: float) -> bool:
    return value >= threshold - EPSILON


class SlidingWindowDetector:
    def __init__(self, config: DetectionConfig):
        self.config = config
        self._weight_sum = config.color_weight + config.edge_weight
        self._windows_by_class: dict[str, list[WindowSpec]] = {}
        for window in config.windows:
            self._windows_by_class.setdefault(window.class_name, []).append(window)

    def detect_frame(
        self,
        image: np.ndarray,
        route: str,
        frame_index: int,
    ) -> list[dict[str, Any]]:
        if image.ndim != 3 or image.shape[2] != 3:
            raise ValueError(f"expected BGR image, got shape {image.shape}")
        height, width = image.shape[:2]
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        gradient_x = cv2.Sobel(gray, cv2.CV_32F, 1, 0, ksize=3)
        gradient_y = cv2.Sobel(gray, cv2.CV_32F, 0, 1, ksize=3)
        edge_magnitude = cv2.magnitude(gradient_x, gradient_y)
        edge_integral = cv2.integral(
            (edge_magnitude > self.config.edge_pixel_threshold).astype(np.uint8)
        )
        image_integral = cv2.integral(image)
        color_integrals: dict[str, np.ndarray] = {}
        for class_name, target_color in self.config.target_colors.items():
            target = np.asarray(target_color, dtype=np.int16)
            difference = np.abs(image.astype(np.int16) - target)
            color_mask = (np.max(difference, axis=2) <= self.config.color_tolerance).astype(np.uint8)
            color_integrals[class_name] = cv2.integral(color_mask)

        candidates: list[Candidate] = []
        for window in self.config.windows:
            x_step = max(1, window.step)
            y_step = max(1, window.step)
            for y1 in range(0, max(1, height - window.height + 1), y_step):
                y2 = min(height, y1 + window.height)
                if y2 <= y1:
                    continue
                for x1 in range(0, max(1, width - window.width + 1), x_step):
                    x2 = min(width, x1 + window.width)
                    if x2 <= x1:
                        continue
                    features = self._features_from_integrals(
                        x1,
                        y1,
                        x2,
                        y2,
                        color_integrals[window.class_name],
                        edge_integral,
                        image_integral,
                        window.class_name,
                    )
                    if not features.passes_feature_thresholds:
                        continue
                    if not greater_or_equal(features.score, self.config.detection_threshold):
                        continue
                    stable_key = f"{route}|{frame_index:010d}|{window.global_index:06d}"
                    candidates.append(
                        Candidate(
                            class_name=window.class_name,
                            bbox=(int(x1), int(y1), int(x2), int(y2)),
                            score=round(features.score, 12),
                            stable_key=stable_key,
                            window_id=window.window_id,
                            window_index=window.global_index,
                        )
                    )

        kept: list[Candidate] = []
        for class_name in sorted(self._windows_by_class):
            class_candidates = [candidate for candidate in candidates if candidate.class_name == class_name]
            kept.extend(self._nms(class_candidates))
        kept.sort(key=lambda candidate: (-candidate.score, candidate.class_name, candidate.stable_key))
        return [candidate.to_dict() for candidate in kept]

    def _features_from_integrals(
        self,
        x1: int,
        y1: int,
        x2: int,
        y2: int,
        color_integral: np.ndarray,
        edge_integral: np.ndarray,
        image_integral: np.ndarray,
        class_name: str,
    ) -> WindowFeatures:
        area = (x2 - x1) * (y2 - y1)
        if area <= 0:
            return WindowFeatures(0.0, 0.0, 0.0, 0.0, False)
        color_pixels = int(
            color_integral[y2, x2]
            - color_integral[y1, x2]
            - color_integral[y2, x1]
            + color_integral[y1, x1]
        )
        edge_pixels = int(
            edge_integral[y2, x2]
            - edge_integral[y1, x2]
            - edge_integral[y2, x1]
            + edge_integral[y1, x1]
        )
        color_ratio = float(color_pixels) / float(area)
        edge_density = float(edge_pixels) / float(area)
        sum_b = float(
            image_integral[y2, x2, 0]
            - image_integral[y1, x2, 0]
            - image_integral[y2, x1, 0]
            + image_integral[y1, x1, 0]
        )
        sum_g = float(
            image_integral[y2, x2, 1]
            - image_integral[y1, x2, 1]
            - image_integral[y2, x1, 1]
            + image_integral[y1, x1, 1]
        )
        sum_r = float(
            image_integral[y2, x2, 2]
            - image_integral[y1, x2, 2]
            - image_integral[y2, x1, 2]
            + image_integral[y1, x1, 2]
        )
        target_b, target_g, target_r = self.config.target_colors[class_name]
        if self.config.color_tolerance == 0:
            color_mean_similarity = (
                1.0
                if sum_b / area == target_b
                and sum_g / area == target_g
                and sum_r / area == target_r
                else 0.0
            )
        else:
            tolerance = float(self.config.color_tolerance)
            similarity_b = max(0.0, min(1.0, 1.0 - abs(sum_b / area - target_b) / tolerance))
            similarity_g = max(0.0, min(1.0, 1.0 - abs(sum_g / area - target_g) / tolerance))
            similarity_r = max(0.0, min(1.0, 1.0 - abs(sum_r / area - target_r) / tolerance))
            color_mean_similarity = (similarity_b + similarity_g + similarity_r) / 3.0
        weighted_score = (
            self.config.color_weight * color_mean_similarity
            + self.config.edge_weight * edge_density
        ) / self._weight_sum
        score = min(1.0, max(0.0, weighted_score))
        passes = greater_or_equal(
            color_ratio, self.config.color_ratio_threshold
        ) and greater_or_equal(edge_density, self.config.edge_density_threshold)
        return WindowFeatures(color_ratio, color_mean_similarity, edge_density, score, passes)

    def features_for_roi(
        self,
        roi: np.ndarray,
        edge_roi: np.ndarray,
        class_name: str,
    ) -> WindowFeatures:
        if roi.size == 0 or edge_roi.size == 0:
            return WindowFeatures(0.0, 0.0, 0.0, 0.0, False)
        target = np.asarray(self.config.target_colors[class_name], dtype=np.int16)
        pixel_difference = np.abs(roi.astype(np.int16) - target)
        color_mask = np.max(pixel_difference, axis=2) <= self.config.color_tolerance
        color_ratio = float(np.count_nonzero(color_mask)) / float(roi.shape[0] * roi.shape[1])
        edge_mask = edge_roi > self.config.edge_pixel_threshold
        edge_density = float(np.count_nonzero(edge_mask)) / float(edge_roi.shape[0] * edge_roi.shape[1])
        mean_bgr = np.mean(roi.reshape(-1, 3), axis=0, dtype=np.float64)
        if self.config.color_tolerance == 0:
            color_mean_similarity = 1.0 if np.allclose(mean_bgr, target) else 0.0
        else:
            channel_similarity = np.clip(
                1.0 - np.abs(mean_bgr - target.astype(np.float64)) / float(self.config.color_tolerance),
                0.0,
                1.0,
            )
            color_mean_similarity = float(np.mean(channel_similarity))
        weighted_score = (
            self.config.color_weight * color_mean_similarity
            + self.config.edge_weight * edge_density
        ) / self._weight_sum
        score = min(1.0, max(0.0, weighted_score))
        passes = greater_or_equal(
            color_ratio, self.config.color_ratio_threshold
        ) and greater_or_equal(edge_density, self.config.edge_density_threshold)
        return WindowFeatures(color_ratio, color_mean_similarity, edge_density, score, passes)

    def _nms(self, candidates: list[Candidate]) -> list[Candidate]:
        ordered = sorted(candidates, key=lambda candidate: (-candidate.score, candidate.stable_key))
        kept: list[Candidate] = []
        for candidate in ordered:
            suppressed = False
            for previous in kept:
                if iou(candidate.bbox, previous.bbox) > self.config.nms_iou_threshold + EPSILON:
                    suppressed = True
                    break
            if not suppressed:
                kept.append(candidate)
        return kept
