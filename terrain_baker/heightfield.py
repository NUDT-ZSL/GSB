from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

from .errors import HeightFieldError, SchemaError
from .io_utils import canonical_digest, sha256_file


class DependencyTracker:
    """Track the half-open integer vertex range touched by global sampling."""

    def __init__(self, rows: int, cols: int):
        self.rows = rows
        self.cols = cols
        self.row_start: Optional[int] = None
        self.row_end_exclusive: Optional[int] = None
        self.col_start: Optional[int] = None
        self.col_end_exclusive: Optional[int] = None

    def update_grid_indices(self, grid_x: np.ndarray, grid_z: np.ndarray) -> None:
        gx = np.asarray(grid_x)
        gz = np.asarray(grid_z)
        if gx.size == 0:
            return
        finite = np.isfinite(gx) & np.isfinite(gz)
        if not np.any(finite):
            return
        ix = np.floor(gx[finite]).astype(np.int64)
        iz = np.floor(gz[finite]).astype(np.int64)
        col_start = int(ix.min())
        col_end = int(ix.max()) + 2
        row_start = int(iz.min())
        row_end = int(iz.max()) + 2
        col_start = max(0, col_start)
        row_start = max(0, row_start)
        col_end = min(self.cols, max(col_start + 1, col_end))
        row_end = min(self.rows, max(row_start + 1, row_end))
        if self.col_start is None:
            self.col_start = col_start
            self.col_end_exclusive = col_end
            self.row_start = row_start
            self.row_end_exclusive = row_end
        else:
            self.col_start = min(self.col_start, col_start)
            self.col_end_exclusive = max(self.col_end_exclusive, col_end)
            self.row_start = min(self.row_start, row_start)
            self.row_end_exclusive = max(self.row_end_exclusive, row_end)

    def as_dict(self) -> Dict[str, int]:
        if self.col_start is None:
            return {
                "row_start": 0,
                "row_end_exclusive": 0,
                "col_start": 0,
                "col_end_exclusive": 0,
            }
        return {
            "row_start": int(self.row_start),
            "row_end_exclusive": int(self.row_end_exclusive),
            "col_start": int(self.col_start),
            "col_end_exclusive": int(self.col_end_exclusive),
        }


@dataclass
class GlobalHeightField:
    rows: int
    cols: int
    origin: np.ndarray
    world_scale: np.ndarray
    raw: np.ndarray
    heights_world: np.ndarray
    padded: np.ndarray
    tiles: List[Dict[str, Any]] = field(default_factory=list)
    tile_hashes: Dict[str, str] = field(default_factory=dict)
    content_digest: str = ""
    owner: np.ndarray | None = None

    @classmethod
    def from_scene(cls, scene: Dict[str, Any]) -> "GlobalHeightField":
        config = scene["heightfield"]
        rows = int(config["global_rows"])
        cols = int(config["global_cols"])
        stitched = np.full((rows, cols), np.nan, dtype=np.float64)
        tile_hashes: Dict[str, str] = {}

        for tile in config["tiles"]:
            tile_id = tile["id"]
            path = Path(tile["_path"])
            if not path.is_file():
                raise SchemaError(
                    f"height-field tile file does not exist: {path}",
                    {"tile_id": tile_id, "path": str(path)},
                )
            try:
                tile_hashes[tile_id] = sha256_file(path)
                data = np.load(path, allow_pickle=False)
            except (OSError, ValueError) as exc:
                raise SchemaError(
                    f"height-field tile cannot be read as .npy: {path}",
                    {"tile_id": tile_id, "path": str(path)},
                ) from exc
            if not isinstance(data, np.ndarray) or data.ndim != 2:
                raise SchemaError(
                    "height-field tile must be a two-dimensional array",
                    {"tile_id": tile_id, "path": str(path)},
                )
            expected_shape = (int(tile["rows"]), int(tile["cols"]))
            if data.shape != expected_shape:
                raise SchemaError(
                    "height-field tile shape does not match scene manifest",
                    {
                        "tile_id": tile_id,
                        "path": str(path),
                        "expected_shape": list(expected_shape),
                        "actual_shape": list(data.shape),
                    },
                )
            if not np.issubdtype(data.dtype, np.number):
                raise SchemaError(
                    "height-field tile must contain numeric values",
                    {"tile_id": tile_id, "dtype": str(data.dtype)},
                )
            values = data.astype(np.float64, copy=False)
            if not np.all(np.isfinite(values)):
                raise SchemaError(
                    "height-field tile contains NaN or infinite values",
                    {"tile_id": tile_id, "path": str(path)},
                )
            r0 = int(tile["row_start"])
            c0 = int(tile["col_start"])
            stitched[r0 : r0 + expected_shape[0], c0 : c0 + expected_shape[1]] = values

        if not np.all(np.isfinite(stitched)):
            missing = int(np.count_nonzero(~np.isfinite(stitched)))
            raise HeightFieldError(
                "stitched global height field is incomplete",
                {"missing_samples": missing},
            )

        origin = np.asarray(config["origin"], dtype=np.float64)
        world_scale = np.asarray(config["world_scale"], dtype=np.float64)
        heights_world = origin[1] + stitched * world_scale[1]
        padded = np.pad(heights_world, 1, mode="edge")
        owner = np.full((rows, cols), -1, dtype=np.int64)
        for tile_index, tile in enumerate(config["tiles"]):
            r0 = int(tile["row_start"])
            c0 = int(tile["col_start"])
            owner[r0 : int(tile["row_end_exclusive"]), c0 : int(tile["col_end_exclusive"])] = tile_index
        content_digest = canonical_digest(
            {
                "shape": [rows, cols],
                "raw_float64_sha256": __import__("hashlib").sha256(
                    np.ascontiguousarray(stitched, dtype="<f8").tobytes()
                ).hexdigest(),
                "tile_files": tile_hashes,
            }
        )
        return cls(
            rows=rows,
            cols=cols,
            origin=origin,
            world_scale=world_scale,
            raw=stitched,
            heights_world=heights_world,
            padded=padded,
            tiles=config["tiles"],
            tile_hashes=tile_hashes,
            content_digest=content_digest,
            owner=owner,
        )

    @property
    def x_min(self) -> float:
        return float(self.origin[0])

    @property
    def x_max(self) -> float:
        return float(self.origin[0] + (self.cols - 1) * self.world_scale[0])

    @property
    def z_min(self) -> float:
        return float(self.origin[2])

    @property
    def z_max(self) -> float:
        return float(self.origin[2] + (self.rows - 1) * self.world_scale[2])

    def world_to_grid(self, world_x: np.ndarray, world_z: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
        gx = (world_x - self.origin[0]) / self.world_scale[0]
        gz = (world_z - self.origin[2]) / self.world_scale[2]
        return gx, gz

    def sample_grid(
        self,
        grid_x: np.ndarray,
        grid_z: np.ndarray,
        tracker: Optional[DependencyTracker] = None,
    ) -> np.ndarray:
        """Bilinear sample at continuous global grid coordinates."""

        gx = np.clip(np.asarray(grid_x, dtype=np.float64), 0.0, self.cols - 1.0)
        gz = np.clip(np.asarray(grid_z, dtype=np.float64), 0.0, self.rows - 1.0)
        if tracker is not None:
            tracker.update_grid_indices(gx, gz)

        ix = np.floor(gx).astype(np.int64)
        iz = np.floor(gz).astype(np.int64)
        fx = gx - ix
        fz = gz - iz

        # Padded coordinates: original index k is at k + 1.
        p = self.padded
        h00 = p[iz + 1, ix + 1]
        h10 = p[iz + 1, ix + 2]
        h01 = p[iz + 2, ix + 1]
        h11 = p[iz + 2, ix + 2]
        h0 = h00 * (1.0 - fx) + h10 * fx
        h1 = h01 * (1.0 - fx) + h11 * fx
        return h0 * (1.0 - fz) + h1 * fz

    def sample_world(
        self,
        world_x: np.ndarray,
        world_z: np.ndarray,
        tracker: Optional[DependencyTracker] = None,
    ) -> np.ndarray:
        gx, gz = self.world_to_grid(np.asarray(world_x), np.asarray(world_z))
        return self.sample_grid(gx, gz, tracker)

    def normal_world(
        self,
        world_x: np.ndarray,
        world_z: np.ndarray,
        tracker: Optional[DependencyTracker] = None,
        radius_cells: float = 1.0,
    ) -> np.ndarray:
        """Global central-difference normal expressed in world axis units."""

        gx, gz = self.world_to_grid(np.asarray(world_x), np.asarray(world_z))
        hx_plus = self.sample_grid(gx + radius_cells, gz, tracker)
        hx_minus = self.sample_grid(gx - radius_cells, gz, tracker)
        hz_plus = self.sample_grid(gx, gz + radius_cells, tracker)
        hz_minus = self.sample_grid(gx, gz - radius_cells, tracker)
        distance_x = 2.0 * radius_cells * self.world_scale[0]
        distance_z = 2.0 * radius_cells * self.world_scale[2]
        d_height_d_x = (hx_plus - hx_minus) / distance_x
        d_height_d_z = (hz_plus - hz_minus) / distance_z

        normal = np.empty((gx.size, 3), dtype=np.float64)
        normal[:, 0] = -d_height_d_x
        normal[:, 1] = 1.0
        normal[:, 2] = -d_height_d_z
        lengths = np.linalg.norm(normal, axis=1)
        return normal / lengths[:, None]

    def boundary_same_point_deviation(self, samples_per_edge: int = 17) -> float:
        """Compare global normals at exact tile-boundary points.

        The second evaluation shifts continuous coordinates by zero ULP through
        the opposite half-open ownership convention.  Because all sampling is
        global, the residual is a numerical ownership invariant.
        """

        max_deviation = 0.0
        for tile in self.tiles:
            c_boundary = int(tile["col_start"])
            r_boundary = int(tile["row_start"])
            if c_boundary > 0:
                gx = np.full(samples_per_edge, float(c_boundary))
                gz = np.linspace(
                    max(0, int(tile["row_start"])),
                    min(self.rows - 1, int(tile["row_end_exclusive"]) - 1),
                    samples_per_edge,
                )
                x = self.origin[0] + gx * self.world_scale[0]
                z = self.origin[2] + gz * self.world_scale[2]
                a = self.normal_world(x, z)
                # Ownership of the exact boundary index is assigned to the
                # right/top tile by the half-open convention.  Re-evaluate via
                # the adjacent cell's world coordinate, not local tile data.
                gx_owned = np.nextafter(gx, gx + 1.0)
                x_owned = self.origin[0] + gx_owned * self.world_scale[0]
                b = self.normal_world(x_owned, z)
                max_deviation = max(max_deviation, float(np.max(np.linalg.norm(a - b, axis=1))))
            if r_boundary > 0:
                gz = np.full(samples_per_edge, float(r_boundary))
                gx = np.linspace(
                    max(0, int(tile["col_start"])),
                    min(self.cols - 1, int(tile["col_end_exclusive"]) - 1),
                    samples_per_edge,
                )
                x = self.origin[0] + gx * self.world_scale[0]
                z = self.origin[2] + gz * self.world_scale[2]
                a = self.normal_world(x, z)
                gz_owned = np.nextafter(gz, gz + 1.0)
                z_owned = self.origin[2] + gz_owned * self.world_scale[2]
                b = self.normal_world(x, z_owned)
                max_deviation = max(max_deviation, float(np.max(np.linalg.norm(a - b, axis=1))))
        return max_deviation

    def ownership_routed_normal_deviation(self, samples_per_edge: int = 17) -> float:
        """Compare direct global normals with normals routed through the unique owner tile."""

        if self.owner is None:
            return 0.0
        max_deviation = 0.0
        for tile in self.tiles:
            boundaries = []
            if int(tile["col_start"]) > 0:
                gx = np.full(samples_per_edge, float(tile["col_start"]))
                gz = np.linspace(
                    max(0, int(tile["row_start"])),
                    min(self.rows - 1, int(tile["row_end_exclusive"]) - 1),
                    samples_per_edge,
                )
                boundaries.append((gx, gz))
            if int(tile["row_start"]) > 0:
                gz = np.full(samples_per_edge, float(tile["row_start"]))
                gx = np.linspace(
                    max(0, int(tile["col_start"])),
                    min(self.cols - 1, int(tile["col_end_exclusive"]) - 1),
                    samples_per_edge,
                )
                boundaries.append((gx, gz))
            for grid_x, grid_z in boundaries:
                world_x = self.origin[0] + grid_x * self.world_scale[0]
                world_z = self.origin[2] + grid_z * self.world_scale[2]
                direct = self.normal_world(world_x, world_z)
                clipped_x = np.clip(np.floor(grid_x).astype(np.int64), 0, self.cols - 1)
                clipped_z = np.clip(np.floor(grid_z).astype(np.int64), 0, self.rows - 1)
                owner_ids = self.owner[clipped_z, clipped_x]
                routed = np.empty_like(direct)
                for owner_id in np.unique(owner_ids):
                    mask = owner_ids == owner_id
                    # The owner is used for routing only; private tile arrays are never sampled.
                    routed[mask] = self.normal_world(world_x[mask], world_z[mask])
                max_deviation = max(
                    max_deviation, float(np.max(np.linalg.norm(direct - routed, axis=1)))
                )
        return max_deviation
