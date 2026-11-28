"""Focus stack loading, metadata validation and per-frame statistics."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence

import numpy as np

from .filters import laplacian_energy


class MetadataError(ValueError):
    """Raised when stack metadata is inconsistent.

    The message always identifies the stack id, frame index and offending
    field so that rejected inputs can be located in production manifests.
    """


@dataclass
class FrameSummary:
    frame_index: int
    focus_distance: float
    magnification: float
    dof_near: float
    dof_far: float
    mean: float
    variance: float
    laplacian_energy: float

    def to_dict(self) -> Dict[str, Any]:
        return {
            "frame_index": self.frame_index,
            "focus_distance": self.focus_distance,
            "magnification": self.magnification,
            "dof_near": self.dof_near,
            "dof_far": self.dof_far,
            "mean": self.mean,
            "variance": self.variance,
            "laplacian_energy": self.laplacian_energy,
        }


class FocusStack:
    """A single focus stack: integer frames plus calibrated metadata."""

    def __init__(
        self,
        stack_id: str,
        frames: Sequence[np.ndarray],
        focus_distances: Sequence[float],
        magnifications: Sequence[float],
        dof_ranges: Sequence[Sequence[float]],
        *,
        ideal_plane: Optional[np.ndarray] = None,
        sharpness_reference: Optional[np.ndarray] = None,
    ) -> None:
        self.stack_id = str(stack_id)
        self.frames: List[np.ndarray] = [np.asarray(f) for f in frames]
        self.focus_distances = np.asarray(focus_distances, dtype=np.float64)
        self.magnifications = np.asarray(magnifications, dtype=np.float64)
        self.dof_ranges = np.asarray(dof_ranges, dtype=np.float64)
        self.ideal_plane = None if ideal_plane is None else np.asarray(ideal_plane)
        self.sharpness_reference = (
            None if sharpness_reference is None else np.asarray(sharpness_reference)
        )
        self._validate()
        self._summaries = self._compute_summaries()

    # ------------------------------------------------------------------ basic
    @property
    def n_planes(self) -> int:
        return len(self.frames)

    @property
    def shape(self):
        return self.frames[0].shape

    @property
    def dtype(self):
        return self.frames[0].dtype

    @property
    def summaries(self) -> List[FrameSummary]:
        return self._summaries

    def frame_float(self, index: int) -> np.ndarray:
        return self.frames[index].astype(np.float64, copy=False)

    def adaptive_radii(self, config) -> tuple[int, int]:
        """Window radii adapted to image size and depth step density."""
        min_dim = min(self.shape)
        n = self.n_planes
        depth_span = float(self.focus_distances[-1] - self.focus_distances[0])
        mean_step = depth_span / max(n - 1, 1)
        # Dense focus sampling -> focus bands are narrow -> smaller windows.
        step_ratio = mean_step / max(depth_span, 1e-12)
        density = float(np.clip(step_ratio * (n - 1), 0.0, 1.0))
        base = max(
            config.min_window_radius,
            int(round(config.base_window_fraction * min_dim * (0.75 + 0.5 * density))),
        )
        large = max(
            base + 1,
            int(round(config.large_window_fraction * min_dim * (0.75 + 0.5 * density))),
        )
        base = min(base, config.max_window_radius)
        large = min(max(large, base + 1), config.max_window_radius)
        return base, large

    # -------------------------------------------------------------- validation
    def _validate(self) -> None:
        sid = self.stack_id
        n = len(self.frames)
        if n < 2:
            raise MetadataError(f"[{sid}] need at least 2 focal planes, got {n}")
        for name, seq in (
            ("focus_distances", self.focus_distances),
            ("magnifications", self.magnifications),
            ("dof_ranges", self.dof_ranges),
        ):
            if len(seq) != n:
                raise MetadataError(
                    f"[{sid}] length mismatch: {name} has {len(seq)} entries but "
                    f"there are {n} frames"
                )
        first_shape = self.frames[0].shape
        if self.frames[0].ndim != 2:
            raise MetadataError(f"[{sid}] frames must be 2D integer images")
        for idx, frame in enumerate(self.frames):
            if frame.shape != first_shape:
                raise MetadataError(
                    f"[{sid}] frame {idx} shape {frame.shape} != reference shape "
                    f"{first_shape}"
                )
            if not np.issubdtype(frame.dtype, np.integer):
                raise MetadataError(
                    f"[{sid}] frame {idx} dtype {frame.dtype} is not integer"
                )
        # Strictly increasing, unique focus distances.
        for idx in range(1, n):
            if self.focus_distances[idx] <= self.focus_distances[idx - 1]:
                raise MetadataError(
                    f"[{sid}] focus_distances must be strictly increasing; "
                    f"index {idx - 1}->{idx} breaks ordering"
                )
        # DOF range shape, ordering and coverage of each own focus distance.
        if self.dof_ranges.shape != (n, 2):
            raise MetadataError(
                f"[{sid}] dof_ranges must have shape ({n}, 2), got "
                f"{self.dof_ranges.shape}"
            )
        for idx in range(n):
            near, far = self.dof_ranges[idx]
            focus = self.focus_distances[idx]
            if not (near <= focus <= far):
                raise MetadataError(
                    f"[{sid}] frame {idx}: DOF range [{near}, {far}] does not "
                    f"cover its focus distance {focus}"
                )
        self._validate_dof_union()
        self._validate_magnification()
        if self.ideal_plane is not None:
            if self.ideal_plane.shape != first_shape:
                raise MetadataError(
                    f"[{sid}] ideal_plane shape {self.ideal_plane.shape} != "
                    f"{first_shape}"
                )
            if self.ideal_plane.min() < 0 or self.ideal_plane.max() >= n:
                raise MetadataError(f"[{sid}] ideal_plane index out of range")
        if self.sharpness_reference is not None:
            if self.sharpness_reference.shape != (n,) + first_shape:
                raise MetadataError(
                    f"[{sid}] sharpness_reference shape "
                    f"{self.sharpness_reference.shape} != {(n,) + first_shape}"
                )

    def _validate_dof_union(self) -> None:
        """Union of DOF intervals must cover every focus distance."""
        sid = self.stack_id
        order = np.argsort(self.dof_ranges[:, 0])
        intervals = self.dof_ranges[order]
        cur_near, cur_far = intervals[0]
        for near, far in intervals[1:]:
            if near > cur_far:
                raise MetadataError(
                    f"[{sid}] DOF coverage gap between {cur_far} and {near}; "
                    f"not all focus distances are covered by a depth range"
                )
            cur_far = max(cur_far, far)
        focus_min = float(self.focus_distances.min())
        focus_max = float(self.focus_distances.max())
        tol = 1e-9 * max(1.0, abs(cur_near), abs(cur_far))
        if cur_near > focus_min + tol or cur_far < focus_max - tol:
            raise MetadataError(
                f"[{sid}] union DOF [{cur_near}, {cur_far}] does not cover focus "
                f"distances [{focus_min}, {focus_max}]"
            )

    def _validate_magnification(self) -> None:
        sid = self.stack_id
        mag = self.magnifications
        tol = 1e-9
        increasing = np.all(mag[1:] >= mag[:-1] - tol)
        decreasing = np.all(mag[1:] <= mag[:-1] + tol)
        if not (increasing or decreasing):
            # Locate the first violation for a precise rejection reason.
            for idx in range(1, len(mag)):
                up = mag[idx] > mag[idx - 1] + tol
                down = mag[idx] < mag[idx - 1] - tol
                if up and not increasing:
                    raise MetadataError(
                        f"[{sid}] magnification not monotonic: increase at index "
                        f"{idx} after decreasing values"
                    )
                if down and not decreasing:
                    raise MetadataError(
                        f"[{sid}] magnification not monotonic: decrease at index "
                        f"{idx} after increasing values"
                    )

    def _compute_summaries(self) -> List[FrameSummary]:
        summaries: List[FrameSummary] = []
        for idx, frame in enumerate(self.frames):
            img = frame.astype(np.float64, copy=False)
            near, far = self.dof_ranges[idx]
            summaries.append(
                FrameSummary(
                    frame_index=idx,
                    focus_distance=float(self.focus_distances[idx]),
                    magnification=float(self.magnifications[idx]),
                    dof_near=float(near),
                    dof_far=float(far),
                    mean=float(img.mean()),
                    variance=float(img.var()),
                    laplacian_energy=float(laplacian_energy(img).mean()),
                )
            )
        return summaries

    # ------------------------------------------------------------- serialising
    def metadata_signature(self) -> Dict[str, Any]:
        """Content used by persistence to reject incompatible checkpoints."""
        return {
            "stack_id": self.stack_id,
            "shape": list(self.shape),
            "n_planes": self.n_planes,
            "focus_distances": self.focus_distances.tolist(),
            "magnifications": self.magnifications.tolist(),
            "dof_ranges": self.dof_ranges.tolist(),
            "frame_dtype": str(self.dtype),
        }

    def replace_frame(self, index: int, new_frame: np.ndarray) -> "FocusStack":
        """Return a copy with one frame replaced (metadata is unchanged)."""
        if not (0 <= index < self.n_planes):
            raise IndexError(f"frame index {index} out of range 0..{self.n_planes - 1}")
        frames = list(self.frames)
        frames[index] = np.asarray(new_frame)
        return FocusStack(
            self.stack_id,
            frames,
            self.focus_distances,
            self.magnifications,
            self.dof_ranges,
            ideal_plane=self.ideal_plane,
            sharpness_reference=self.sharpness_reference,
        )

    def add_frame(
        self,
        new_frame: np.ndarray,
        focus_distance: float,
        magnification: float,
        dof_range: Sequence[float],
        *,
        sharpness_reference: Optional[np.ndarray] = None,
    ) -> "FocusStack":
        """Return a new stack with an additional focal plane inserted in order."""
        new_frame = np.asarray(new_frame)
        insert_at = int(np.searchsorted(self.focus_distances, focus_distance))
        frames = list(self.frames)
        distances = self.focus_distances.tolist()
        mags = self.magnifications.tolist()
        dofs = self.dof_ranges.tolist()
        frames.insert(insert_at, new_frame)
        distances.insert(insert_at, float(focus_distance))
        mags.insert(insert_at, float(magnification))
        dofs.insert(insert_at, [float(dof_range[0]), float(dof_range[1])])
        ref = None
        if sharpness_reference is not None:
            ref = np.insert(
                self.sharpness_reference
                if self.sharpness_reference is not None
                else np.zeros((0,) + self.shape),
                insert_at,
                np.asarray(sharpness_reference),
                axis=0,
            )
        return FocusStack(
            self.stack_id,
            frames,
            distances,
            mags,
            dofs,
            ideal_plane=self.ideal_plane,
            sharpness_reference=ref,
        )
