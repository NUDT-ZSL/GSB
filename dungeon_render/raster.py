"""Whole-image coordinate rasterization.

A tile is rendered as a rectangular crop of the whole output image.  Every
pixel is mapped back to a level cell with
``cell = floor(global_pixel / cell_pixels)``; wall directions, outlines and
attenuation therefore never depend on the tile origin, tile order or worker
count, which eliminates one-pixel seam artifacts by construction.
"""
from __future__ import annotations

import numpy as np

from .lighting import walkable_light_factors
from .models import Level, RenderTask, TileRect

FLOOR_GRAY = 180
CORRIDOR_GRAY = 150
WALL_GRAY = 60
VOID_GRAY = 0
WALL_EDGE_GRAY = 25
ROOM_EDGE_GRAY = 90
DOOR_FRAME_GRAY = 20


class Renderer:
    def __init__(self, level: Level, task: RenderTask):
        self.level = level
        self.task = task
        walkable = level.walkable()
        self.walkable_mask = np.zeros((level.height, level.width), dtype=bool)
        self.door_mask = np.zeros_like(self.walkable_mask)
        self.room_id = np.full((level.height, level.width), -2, dtype=np.int32)
        for col, row in walkable:
            self.walkable_mask[row, col] = True
        for col, row in level.doors:
            self.door_mask[row, col] = True
        for (col, row), rid in level.rooms.items():
            self.room_id[row, col] = rid
        self.light_target = walkable_light_factors(level)
        self.thickness = level.wall_thickness

    def render_region(self, rect: TileRect) -> tuple[np.ndarray, np.ndarray]:
        """Render one tile; identical to a crop of the whole-image render."""
        level = self.level
        cell_pixels = self.task.cell_pixels
        local_x = np.arange(rect.x, rect.x + rect.width, dtype=np.int64)
        local_y = np.arange(rect.y, rect.y + rect.height, dtype=np.int64)
        global_x, global_y = np.meshgrid(local_x, local_y)
        cell_col = np.floor_divide(global_x, cell_pixels)
        cell_row = np.floor_divide(global_y, cell_pixels)
        in_grid = (
            (cell_col >= 0)
            & (cell_col < level.width)
            & (cell_row >= 0)
            & (cell_row < level.height)
        )
        safe_col = np.clip(cell_col, 0, level.width - 1)
        safe_row = np.clip(cell_row, 0, level.height - 1)

        walkable = np.zeros_like(in_grid)
        is_door = np.zeros_like(in_grid)
        room_id = np.full(in_grid.shape, -2, dtype=np.int32)
        walkable[in_grid] = self.walkable_mask[safe_row[in_grid], safe_col[in_grid]]
        is_door[in_grid] = self.door_mask[safe_row[in_grid], safe_col[in_grid]]
        room_id[in_grid] = self.room_id[safe_row[in_grid], safe_col[in_grid]]

        wall_edge, room_edge, door_frame = self._outline_masks(
            rect, global_x, global_y, cell_col, cell_row, in_grid,
            walkable, is_door, room_id,
        )
        outline = wall_edge | room_edge | door_frame

        gray = np.full(in_grid.shape, VOID_GRAY, dtype=np.float64)
        in_room = walkable & (room_id >= 0)
        in_corridor = walkable & (room_id == -1)
        light = np.zeros(in_grid.shape, dtype=np.float64)
        valid_light = in_grid & walkable
        light[valid_light] = self.light_target[
            safe_row[valid_light], safe_col[valid_light]
        ]
        base = np.where(in_room, FLOOR_GRAY, CORRIDOR_GRAY)
        gray[walkable] = 0.7 * base[walkable] + 0.3 * light[walkable]
        gray[in_grid & ~walkable] = WALL_GRAY

        gray[wall_edge] = WALL_EDGE_GRAY
        gray[room_edge] = ROOM_EDGE_GRAY
        gray[door_frame] = DOOR_FRAME_GRAY
        gray[~in_grid] = VOID_GRAY

        quantized = np.clip(np.floor(gray + 0.5), 0, 255).astype(np.uint8)
        outline_image = (outline.astype(np.uint8)) * 255
        return quantized, outline_image

    def _outline_masks(
        self, rect, global_x, global_y, cell_col, cell_row, in_grid,
        walkable, is_door, room_id,
    ):
        level = self.level
        thickness = self.thickness
        wall_edge = np.zeros(in_grid.shape, dtype=bool)
        room_edge = np.zeros_like(wall_edge)
        door_frame = np.zeros_like(wall_edge)

        directions = (
            (1, 0),
            (-1, 0),
            (0, 1),
            (0, -1),
        )
        for d_col, d_row in directions:
            neighbor_col = cell_col + d_col
            neighbor_row = cell_row + d_row
            neighbor_inside = (
                (neighbor_col >= 0)
                & (neighbor_col < level.width)
                & (neighbor_row >= 0)
                & (neighbor_row < level.height)
            )
            clip_col = np.clip(neighbor_col, 0, level.width - 1)
            clip_row = np.clip(neighbor_row, 0, level.height - 1)
            neighbor_walkable = np.zeros_like(in_grid)
            neighbor_walkable[neighbor_inside] = self.walkable_mask[
                clip_row[neighbor_inside], clip_col[neighbor_inside]
            ]
            neighbor_door = np.zeros_like(in_grid)
            neighbor_door[neighbor_inside] = self.door_mask[
                clip_row[neighbor_inside], clip_col[neighbor_inside]
            ]
            neighbor_room = np.full(in_grid.shape, -2, dtype=np.int32)
            neighbor_room[neighbor_inside] = self.room_id[
                clip_row[neighbor_inside], clip_col[neighbor_inside]
            ]

            if d_col == 1:
                distance = (cell_col + 1) * self.task.cell_pixels - global_x
            elif d_col == -1:
                distance = global_x - cell_col * self.task.cell_pixels + 1
            elif d_row == 1:
                distance = (cell_row + 1) * self.task.cell_pixels - global_y
            else:
                distance = global_y - cell_row * self.task.cell_pixels + 1

            near = distance <= thickness
            # Wall-facing edge stroke; a door cell opens the wall on both
            # adjoining cells, so neither side keeps the dark stroke.
            wall_edge |= (
                in_grid
                & walkable
                & ~neighbor_walkable
                & neighbor_inside
                & near
                & ~is_door
                & ~neighbor_door
            )
            # Room boundary between two differently labelled walkable cells.
            room_edge |= (
                in_grid
                & walkable
                & neighbor_walkable
                & (room_id >= 0)
                & (neighbor_room >= 0)
                & (room_id != neighbor_room)
                & near
                & ~is_door
                & ~neighbor_door
            )

        door_frame |= self._door_frame_mask(global_x, global_y, is_door)
        return wall_edge, room_edge, door_frame

    def _door_frame_mask(self, global_x, global_y, is_door) -> np.ndarray:
        cell_pixels = self.task.cell_pixels
        cell_col = np.floor_divide(global_x, cell_pixels)
        cell_row = np.floor_divide(global_y, cell_pixels)
        left = cell_col * cell_pixels
        top = cell_row * cell_pixels
        right = left + cell_pixels
        bottom = top + cell_pixels
        half = self.thickness // 2
        on_vertical = (
            (np.abs(global_x - left) <= half) | (np.abs(global_x - (right - 1)) <= half)
        )
        on_horizontal = (
            (np.abs(global_y - top) <= half)
            | (np.abs(global_y - (bottom - 1)) <= half)
        )
        return is_door & (on_vertical | on_horizontal)

    def render_whole(self) -> tuple[np.ndarray, np.ndarray]:
        """Reference path: render the complete image as a single tile."""
        rect = TileRect(
            col=0,
            row=0,
            x=0,
            y=0,
            width=self.task.output_width,
            height=self.task.output_height,
        )
        return self.render_region(rect)
