from __future__ import annotations

from dataclasses import dataclass
from typing import Iterator, List, Sequence, Tuple


ChunkId = Tuple[int, int, int]


@dataclass(frozen=True, order=True)
class Box:
    """Half-open 3D box in global voxel coordinates."""

    z0: int
    z1: int
    y0: int
    y1: int
    x0: int
    x1: int

    @classmethod
    def from_shape(cls, shape: Sequence[int]) -> "Box":
        return cls(0, int(shape[0]), 0, int(shape[1]), 0, int(shape[2]))

    @property
    def shape(self) -> Tuple[int, int, int]:
        return (self.z1 - self.z0, self.y1 - self.y0, self.x1 - self.x0)

    @property
    def numel(self) -> int:
        dz, dy, dx = self.shape
        return dz * dy * dx

    def expand(self, radius: int, limit: "Box | None" = None) -> "Box":
        if radius < 0:
            raise ValueError("radius must be non-negative")
        box = Box(
            self.z0 - radius,
            self.z1 + radius,
            self.y0 - radius,
            self.y1 + radius,
            self.x0 - radius,
            self.x1 + radius,
        )
        if limit is not None:
            box = box.intersection(limit)
        return box

    def intersection(self, other: "Box") -> "Box":
        box = Box(
            max(self.z0, other.z0),
            min(self.z1, other.z1),
            max(self.y0, other.y0),
            min(self.y1, other.y1),
            max(self.x0, other.x0),
            min(self.x1, other.x1),
        )
        if box.z0 >= box.z1 or box.y0 >= box.y1 or box.x0 >= box.x1:
            raise ValueError(f"boxes do not overlap: {self} and {other}")
        return box

    def intersects(self, other: "Box") -> bool:
        return (
            self.z0 < other.z1
            and self.z1 > other.z0
            and self.y0 < other.y1
            and self.y1 > other.y0
            and self.x0 < other.x1
            and self.x1 > other.x0
        )

    def to_tuple(self) -> Tuple[int, int, int, int, int, int]:
        return (self.z0, self.z1, self.y0, self.y1, self.x0, self.x1)


@dataclass(frozen=True)
class Chunk:
    index: ChunkId
    core: Box

    @property
    def id(self) -> str:
        return format_chunk_id(self.index)


def format_chunk_id(index: ChunkId) -> str:
    return f"{int(index[0]):03d}_{int(index[1]):03d}_{int(index[2]):03d}"


def parse_chunk_id(value: str) -> ChunkId:
    parts = str(value).strip().split("_")
    if len(parts) != 3:
        raise ValueError(f"invalid chunk id: {value!r}")
    return tuple(int(part) for part in parts)  # type: ignore[return-value]


def chunk_grid(shape: Sequence[int], chunk_size: Sequence[int]) -> List[Chunk]:
    if len(shape) != 3 or len(chunk_size) != 3:
        raise ValueError("shape and chunk_size must be 3D")
    chunks: List[Chunk] = []
    nz = (int(shape[0]) + int(chunk_size[0]) - 1) // int(chunk_size[0])
    ny = (int(shape[1]) + int(chunk_size[1]) - 1) // int(chunk_size[1])
    nx = (int(shape[2]) + int(chunk_size[2]) - 1) // int(chunk_size[2])
    for iz in range(nz):
        z0 = iz * int(chunk_size[0])
        for iy in range(ny):
            y0 = iy * int(chunk_size[1])
            for ix in range(nx):
                x0 = ix * int(chunk_size[2])
                core = Box(
                    z0,
                    min(shape[0], z0 + int(chunk_size[0])),
                    y0,
                    min(shape[1], y0 + int(chunk_size[1])),
                    x0,
                    min(shape[2], x0 + int(chunk_size[2])),
                )
                chunks.append(Chunk((iz, iy, ix), core))
    return chunks


def iter_grid_indices(shape: Sequence[int], chunk_size: Sequence[int]) -> Iterator[ChunkId]:
    nz = (int(shape[0]) + int(chunk_size[0]) - 1) // int(chunk_size[0])
    ny = (int(shape[1]) + int(chunk_size[1]) - 1) // int(chunk_size[1])
    nx = (int(shape[2]) + int(chunk_size[2]) - 1) // int(chunk_size[2])
    for iz in range(nz):
        for iy in range(ny):
            for ix in range(nx):
                yield iz, iy, ix


def local_slice(global_box: Box, source_box: Box) -> Tuple[slice, slice, slice]:
    overlap = global_box.intersection(source_box)
    return (
        slice(overlap.z0 - source_box.z0, overlap.z1 - source_box.z0),
        slice(overlap.y0 - source_box.y0, overlap.y1 - source_box.y0),
        slice(overlap.x0 - source_box.x0, overlap.x1 - source_box.x0),
    )


def placement_slices(core: Box, padded_box: Box) -> Tuple[slice, slice, slice]:
    """Slices locating ``core`` inside ``padded_box``."""
    if not padded_box.intersects(core):
        raise ValueError("core is not inside padded box")
    intersection = padded_box.intersection(core)
    return (
        slice(intersection.z0 - padded_box.z0, intersection.z1 - padded_box.z0),
        slice(intersection.y0 - padded_box.y0, intersection.y1 - padded_box.y0),
        slice(intersection.x0 - padded_box.x0, intersection.x1 - padded_box.x0),
    )
