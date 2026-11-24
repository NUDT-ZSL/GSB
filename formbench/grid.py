from __future__ import annotations

from .schema import Grid


def cells_for_bbox(
    bbox: tuple[int, int, int, int],
    grid: Grid,
    image_width: int,
    image_height: int,
) -> list[list[int]]:
    """Return grid cells intersected by a whole-coordinate rectangle."""
    x, y, width, height = bbox
    cell_width = image_width // grid.cols
    cell_height = image_height // grid.rows
    if image_width % grid.cols or image_height % grid.rows:
        raise ValueError("grid must divide image before cell lookup")

    x1 = max(x, x + width - 1)
    y1 = max(y, y + height - 1)
    col0 = min(grid.cols - 1, max(0, x // cell_width))
    col1 = min(grid.cols - 1, max(0, x1 // cell_width))
    row0 = min(grid.rows - 1, max(0, y // cell_height))
    row1 = min(grid.rows - 1, max(0, y1 // cell_height))
    return [[row, col] for row in range(row0, row1 + 1) for col in range(col0, col1 + 1)]
