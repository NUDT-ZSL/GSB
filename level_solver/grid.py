"""Immutable grid states and room geometry helpers."""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Iterable, Iterator, Sequence

from .rng import DeterministicRNG

BLOCKED = -1
EMPTY = 0
ROOM = 1
CORRIDOR = 2
FLOOR_VALUES = frozenset({ROOM, CORRIDOR})

Cell = tuple[int, int]


@dataclass(frozen=True)
class Room:
    x: int
    y: int
    w: int
    h: int

    @property
    def area(self) -> int:
        return self.w * self.h

    @property
    def right(self) -> int:
        return self.x + self.w

    @property
    def bottom(self) -> int:
        return self.y + self.h

    def cells(self) -> Iterator[Cell]:
        for y in range(self.y, self.bottom):
            for x in range(self.x, self.right):
                yield x, y

    def contains(self, x: int, y: int) -> bool:
        return self.x <= x < self.right and self.y <= y < self.bottom

    def canonical(self) -> dict[str, int]:
        return {"x": self.x, "y": self.y, "w": self.w, "h": self.h}


@dataclass(frozen=True)
class LevelState:
    grid: tuple[tuple[int, ...], ...]
    rooms: tuple[Room, ...]

    @classmethod
    def initial(
        cls, width: int, height: int, blocked: frozenset[tuple[int, int]]
    ) -> "LevelState":
        rows = []
        for y in range(height):
            rows.append(
                tuple(BLOCKED if (x, y) in blocked else EMPTY for x in range(width))
            )
        return cls(grid=tuple(rows), rooms=tuple())

    @classmethod
    def from_lists(
        cls, grid: Sequence[Sequence[int]], rooms: Sequence[Room]
    ) -> "LevelState":
        return cls(
            grid=tuple(tuple(int(value) for value in row) for row in grid),
            rooms=tuple(rooms),
        )

    @property
    def width(self) -> int:
        return len(self.grid[0]) if self.grid else 0

    @property
    def height(self) -> int:
        return len(self.grid)

    def mutable_grid(self) -> list[list[int]]:
        return [list(row) for row in self.grid]

    def with_grid_rooms(
        self, grid: Sequence[Sequence[int]], rooms: Sequence[Room]
    ) -> "LevelState":
        return LevelState.from_lists(grid, rooms)

    def room_cells(self) -> set[Cell]:
        result: set[Cell] = set()
        for room in self.rooms:
            result.update(room.cells())
        return result


def rect_gap(first: Room, second: Room) -> int:
    """Return empty-cell distance between two rectangles.

    Adjacent rectangles have gap 0.  A one-cell empty corridor between them has
    gap 1.  A negative value means the rectangles overlap.
    """

    gap_x = max(second.x - first.right, first.x - second.right)
    gap_y = max(second.y - first.bottom, first.y - second.bottom)
    if gap_x < 0 and gap_y < 0:
        return -1
    if gap_x >= 0 and gap_y >= 0:
        return max(gap_x, gap_y)
    return gap_x if gap_x >= 0 else gap_y


def all_room_pairs(rooms: Sequence[Room]) -> Iterator[tuple[int, int, int]]:
    for first in range(len(rooms)):
        for second in range(first + 1, len(rooms)):
            yield first, second, rect_gap(rooms[first], rooms[second])


def grid_from_state(state: LevelState) -> list[list[int]]:
    return state.mutable_grid()


def floor_cells(grid: Sequence[Sequence[int]]) -> Iterator[Cell]:
    for y, row in enumerate(grid):
        for x, value in enumerate(row):
            if value in FLOOR_VALUES:
                yield x, y


def room_grid_errors(state: LevelState) -> list[str]:
    errors: list[str] = []
    height = len(state.grid)
    width = len(state.grid[0]) if height else 0
    occupied: set[Cell] = set()
    covered_rooms: set[Cell] = set()
    for index, room in enumerate(state.rooms):
        if room.w <= 0 or room.h <= 0:
            errors.append(f"room {index} has a non-positive size")
            continue
        if (
            room.x < 0
            or room.y < 0
            or room.right > width
            or room.bottom > height
        ):
            errors.append(f"room {index} is outside the grid")
            continue
        cells = set(room.cells())
        overlap = occupied & cells
        if overlap:
            errors.append(f"room {index} overlaps another room")
        occupied.update(cells)
        for x, y in room.cells():
            covered_rooms.add((x, y))
            if state.grid[y][x] != ROOM:
                errors.append(
                    f"room {index} cell ({x},{y}) is not represented as ROOM"
                )
                break
    actual_rooms: set[Cell] = set()
    for cell in floor_cells(state.grid):
        if state.grid[cell[1]][cell[0]] == ROOM:
            actual_rooms.add(cell)
    if actual_rooms != covered_rooms:
        errors.append("ROOM cells and rectangle room metadata are inconsistent")
    return errors


def iter_neighbors(
    x: int, y: int, width: int, height: int, order: Sequence[Cell]
) -> Iterator[Cell]:
    for dx, dy in order:
        nx, ny = x + dx, y + dy
        if 0 <= nx < width and 0 <= ny < height:
            yield nx, ny


def shuffled_neighbor_order(rng: DeterministicRNG) -> list[Cell]:
    order: list[Cell] = [(1, 0), (0, 1), (-1, 0), (0, -1)]
    rng.shuffle(order)
    return order


def find_path(
    grid: Sequence[Sequence[int]],
    sources: Iterable[Cell],
    targets: Iterable[Cell],
    *,
    neighbor_order: Sequence[Cell] | None = None,
    passable: Callable[[int], bool] | None = None,
) -> list[Cell] | None:
    """Find a deterministic shortest path through non-blocked cells."""

    height = len(grid)
    width = len(grid[0]) if height else 0
    source_set = set(sources)
    target_set = set(targets)
    if not source_set or not target_set:
        return None
    passable = passable or (lambda value: value != BLOCKED)
    order = list(neighbor_order or ((1, 0), (0, 1), (-1, 0), (0, -1)))

    queue: deque[Cell] = deque(sorted(source_set))
    parent: dict[Cell, Cell | None] = {cell: None for cell in source_set}
    found: Cell | None = None
    for cell in sorted(source_set):
        if cell in target_set:
            found = cell
            break
    while queue and found is None:
        current = queue.popleft()
        for neighbor in iter_neighbors(
            current[0], current[1], width, height, order
        ):
            if neighbor in parent:
                continue
            x, y = neighbor
            if not passable(grid[y][x]):
                continue
            parent[neighbor] = current
            if neighbor in target_set:
                found = neighbor
                break
            queue.append(neighbor)
    if found is None:
        return None
    path: list[Cell] = []
    cursor: Cell | None = found
    while cursor is not None:
        path.append(cursor)
        cursor = parent[cursor]
    path.reverse()
    return path


def floor_components(
    grid: Sequence[Sequence[int]],
    *,
    values: frozenset[int] = FLOOR_VALUES,
    order: Sequence[Cell] = ((1, 0), (0, 1), (-1, 0), (0, -1)),
) -> list[list[Cell]]:
    height = len(grid)
    width = len(grid[0]) if height else 0
    visited: set[Cell] = set()
    components: list[list[Cell]] = []
    for y in range(height):
        for x in range(width):
            if grid[y][x] not in values or (x, y) in visited:
                continue
            queue: deque[Cell] = deque([(x, y)])
            visited.add((x, y))
            component: list[Cell] = []
            while queue:
                current = queue.popleft()
                component.append(current)
                for neighbor in iter_neighbors(
                    current[0], current[1], width, height, order
                ):
                    nx, ny = neighbor
                    if neighbor in visited or grid[ny][nx] not in values:
                        continue
                    visited.add(neighbor)
                    queue.append(neighbor)
            component.sort(key=lambda cell: (cell[1], cell[0]))
            components.append(component)
    components.sort(key=lambda component: component[0])
    return components


def floor_is_connected(grid: Sequence[Sequence[int]]) -> bool:
    return len(floor_components(grid)) <= 1


def cells_reachable(
    grid: Sequence[Sequence[int]], start: Cell, end: Cell
) -> bool:
    height = len(grid)
    width = len(grid[0]) if height else 0
    sx, sy = start
    ex, ey = end
    if not (0 <= sx < width and 0 <= sy < height):
        return False
    if not (0 <= ex < width and 0 <= ey < height):
        return False
    if grid[sy][sx] not in FLOOR_VALUES or grid[ey][ex] not in FLOOR_VALUES:
        return False
    if start == end:
        return True
    queue: deque[Cell] = deque([start])
    visited = {start}
    while queue:
        current = queue.popleft()
        for neighbor in iter_neighbors(
            current[0], current[1], width, height, ((1, 0), (0, 1), (-1, 0), (0, -1))
        ):
            if neighbor in visited:
                continue
            nx, ny = neighbor
            if grid[ny][nx] not in FLOOR_VALUES:
                continue
            if neighbor == end:
                return True
            visited.add(neighbor)
            queue.append(neighbor)
    return False


def clear_rectangle(
    grid: list[list[int]], room: Room, *, value: int = EMPTY
) -> None:
    for x, y in room.cells():
        grid[y][x] = value


def add_rectangle(grid: list[list[int]], room: Room) -> None:
    for x, y in room.cells():
        grid[y][x] = ROOM


def carve_path(grid: list[list[int]], path: Sequence[Cell]) -> None:
    for x, y in path:
        if grid[y][x] == EMPTY:
            grid[y][x] = CORRIDOR


def state_digest_material(state: LevelState) -> dict[str, object]:
    return {
        "grid": [list(row) for row in state.grid],
        "rooms": [room.canonical() for room in state.rooms],
    }


def clone_grid(grid: Sequence[Sequence[int]]) -> list[list[int]]:
    return [list(row) for row in grid]
