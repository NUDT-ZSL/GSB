"""Deterministic constraint candidate generation and validation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Sequence

from .grid import (
    BLOCKED,
    CORRIDOR,
    EMPTY,
    ROOM,
    Cell,
    LevelState,
    Room,
    carve_path,
    cells_reachable,
    clear_rectangle,
    find_path,
    floor_components,
    floor_is_connected,
    rect_gap,
    room_grid_errors,
    shuffled_neighbor_order,
    add_rectangle,
)
from .model import Constraint, LevelSpec
from .rng import DeterministicRNG, derive_seed

DEFAULT_ROOM_AREA = 12
DEFAULT_ROOM_SIDE = 4


class CandidateRejected(Exception):
    def __init__(self, message: str, *, conflict_ids: Sequence[str] = ()) -> None:
        super().__init__(message)
        self.message = message
        self.conflict_ids = tuple(conflict_ids)


@dataclass(frozen=True)
class ConstraintViolation(Exception):
    constraint_id: str
    message: str

    def as_dict(self) -> dict[str, str]:
        return {"constraint_id": self.constraint_id, "message": self.message}


def _prior_room_caps(spec: LevelSpec, index: int) -> tuple[int, int]:
    max_area = DEFAULT_ROOM_AREA
    spacing = 0
    for constraint in spec.constraints[:index]:
        if constraint.type == "max_area":
            max_area = min(max_area, int(constraint.get("max_area")))
        elif constraint.type == "room_spacing":
            spacing = max(spacing, int(constraint.get("min_spacing")))
    return max_area, spacing


def _room_fits(
    grid: Sequence[Sequence[int]],
    rooms: Sequence[Room],
    candidate: Room,
    spacing: int,
) -> bool:
    height = len(grid)
    width = len(grid[0]) if height else 0
    if (
        candidate.w <= 0
        or candidate.h <= 0
        or candidate.x < 0
        or candidate.y < 0
        or candidate.right > width
        or candidate.bottom > height
    ):
        return False
    for x, y in candidate.cells():
        if grid[y][x] != EMPTY:
            return False
    for room in rooms:
        if rect_gap(room, candidate) < spacing:
            return False
    return True


def _candidate_positions(
    width: int,
    height: int,
    room_width: int,
    room_height: int,
    rng: DeterministicRNG,
) -> list[Cell]:
    max_x = width - room_width
    max_y = height - room_height
    if max_x < 0 or max_y < 0:
        return []
    count = (max_x + 1) * (max_y + 1)
    # Small and medium grids can be fully enumerated.  For very large grids,
    # sample a bounded deterministic subset and then append a scanning tail.
    positions: list[Cell] = []
    if count <= 20_000:
        positions = [(x, y) for y in range(max_y + 1) for x in range(max_x + 1)]
        rng.shuffle(positions)
        return positions
    seen: set[Cell] = set()
    for _ in range(5_000):
        cell = (rng.randbelow(max_x + 1), rng.randbelow(max_y + 1))
        if cell not in seen:
            seen.add(cell)
            positions.append(cell)
    start = rng.randbelow(count)
    for offset in range(min(count, 20_000)):
        linear = (start + offset) % count
        cell = (linear % (max_x + 1), linear // (max_x + 1))
        if cell not in seen:
            seen.add(cell)
            positions.append(cell)
    return positions


def _place_room(
    grid: list[list[int]],
    rooms: list[Room],
    width: int,
    height: int,
    room_width: int,
    room_height: int,
    spacing: int,
    rng: DeterministicRNG,
) -> bool:
    for x, y in _candidate_positions(width, height, room_width, room_height, rng):
        candidate = Room(x, y, room_width, room_height)
        if _room_fits(grid, rooms, candidate, spacing):
            add_rectangle(grid, candidate)
            rooms.append(candidate)
            return True
    return False


def _random_room_shape(
    max_area: int, rng: DeterministicRNG
) -> tuple[int, int]:
    side = max(1, min(DEFAULT_ROOM_SIDE, max_area))
    width = 1 + rng.randbelow(side)
    height_limit = max(1, min(DEFAULT_ROOM_SIDE, max_area // width))
    height = 1 + rng.randbelow(height_limit)
    return width, height


def _apply_min_rooms(
    spec: LevelSpec, constraint: Constraint, state: LevelState, rng: DeterministicRNG
) -> LevelState:
    target = int(constraint.get("count"))
    if len(state.rooms) >= target:
        return state
    max_area, spacing = _prior_room_caps(spec, constraint.index)

    def build(compact: bool) -> LevelState:
        grid = state.mutable_grid()
        rooms = list(state.rooms)
        while len(rooms) < target:
            placed = False
            if compact:
                placed = _place_room(
                    grid,
                    rooms,
                    spec.width,
                    spec.height,
                    1,
                    1,
                    spacing,
                    rng,
                )
            else:
                for _shape_try in range(20):
                    room_width, room_height = _random_room_shape(max_area, rng)
                    if room_width * room_height > max_area:
                        continue
                    if _place_room(
                        grid,
                        rooms,
                        spec.width,
                        spec.height,
                        room_width,
                        room_height,
                        spacing,
                        rng,
                    ):
                        placed = True
                        break
                if not placed:
                    fallback_shapes: list[tuple[int, int]] = []
                    for room_width in range(1, min(spec.width, max_area) + 1):
                        for room_height in range(
                            1, min(spec.height, max_area // room_width) + 1
                        ):
                            if room_width * room_height <= max_area:
                                fallback_shapes.append((room_width, room_height))
                    fallback_shapes.sort(
                        key=lambda shape: (-shape[0] * shape[1], shape)
                    )
                    rng.shuffle(fallback_shapes)
                    for room_width, room_height in fallback_shapes:
                        if _place_room(
                            grid,
                            rooms,
                            spec.width,
                            spec.height,
                            room_width,
                            room_height,
                            spacing,
                            rng,
                        ):
                            placed = True
                            break
            if not placed:
                raise CandidateRejected(
                    "unable to place the requested number of rooms",
                    conflict_ids=(constraint.id,),
                )
        return LevelState.from_lists(grid, rooms)

    try:
        return build(compact=False)
    except CandidateRejected:
        # Dense specifications often need the smallest legal footprint even
        # though random rooms fit individually.
        return build(compact=True)


def _split_room(
    room: Room,
    max_area: int,
    spacing: int,
    rng: DeterministicRNG,
    depth: int = 0,
) -> list[Room] | None:
    if room.area <= max_area:
        return [room]
    if depth > 64:
        return None

    orientations = ["horizontal", "vertical"]
    if rng.randbelow(2):
        orientations.reverse()
    for orientation in orientations:
        if orientation == "horizontal":
            maximum_cut = room.h - spacing - 1
            if maximum_cut < 1:
                continue
            middle = max(1, room.h // 2)
            cuts = list(range(1, maximum_cut + 1))
            cuts.sort(key=lambda cut: (abs(cut - middle), cut))
            if len(cuts) > 12:
                offset = rng.randbelow(max(1, len(cuts) - 12))
                cuts = cuts[offset : offset + 12]
            for cut in cuts:
                first = Room(room.x, room.y, room.w, cut)
                second = Room(
                    room.x,
                    room.y + cut + spacing,
                    room.w,
                    room.h - cut - spacing,
                )
                left = _split_room(first, max_area, spacing, rng, depth + 1)
                right = _split_room(second, max_area, spacing, rng, depth + 1)
                if left is not None and right is not None:
                    return left + right
        else:
            maximum_cut = room.w - spacing - 1
            if maximum_cut < 1:
                continue
            middle = max(1, room.w // 2)
            cuts = list(range(1, maximum_cut + 1))
            cuts.sort(key=lambda cut: (abs(cut - middle), cut))
            if len(cuts) > 12:
                offset = rng.randbelow(max(1, len(cuts) - 12))
                cuts = cuts[offset : offset + 12]
            for cut in cuts:
                first = Room(room.x, room.y, cut, room.h)
                second = Room(
                    room.x + cut + spacing,
                    room.y,
                    room.w - cut - spacing,
                    room.h,
                )
                left = _split_room(first, max_area, spacing, rng, depth + 1)
                right = _split_room(second, max_area, spacing, rng, depth + 1)
                if left is not None and right is not None:
                    return left + right
    return None


def _sparse_retile(room: Room, spacing: int) -> list[Room]:
    """Fallback tiling used when straight splits cannot honor prior spacing.

    Single-cell rooms placed at ``spacing + 1`` intervals remain inside the
    original rectangle and always satisfy area and spacing bounds.  The
    bounding box may contain fewer floor cells afterwards, which is permitted:
    max-area and spacing constraints do not require preserving every cell.
    """

    step = spacing + 1
    rooms: list[Room] = []
    for y in range(room.y, room.bottom, step):
        for x in range(room.x, room.right, step):
            rooms.append(Room(x, y, 1, 1))
    return rooms


def _apply_max_area(
    spec: LevelSpec, constraint: Constraint, state: LevelState, rng: DeterministicRNG
) -> LevelState:
    max_area = int(constraint.get("max_area"))
    if all(room.area <= max_area for room in state.rooms):
        return state
    _, prior_spacing = _prior_room_caps(spec, constraint.index)
    grid = state.mutable_grid()
    result_rooms: list[Room] = []
    for room in state.rooms:
        if room.area <= max_area:
            result_rooms.append(room)
            continue
        pieces = _split_room(room, max_area, prior_spacing, rng)
        if pieces is None:
            pieces = _sparse_retile(room, prior_spacing)
            if not pieces:
                raise CandidateRejected(
                    "unable to split a room under the area limit and prior spacing",
                    conflict_ids=(constraint.id,),
                )
        clear_rectangle(grid, room)
        for piece in pieces:
            if not _room_fits(grid, result_rooms, piece, prior_spacing):
                raise CandidateRejected(
                    "split room placement violates prior constraints",
                    conflict_ids=(constraint.id,),
                )
            add_rectangle(grid, piece)
            result_rooms.append(piece)
    return LevelState.from_lists(grid, result_rooms)


def _spacing_violations(
    rooms: Sequence[Room], spacing: int
) -> list[tuple[int, int, int]]:
    violations: list[tuple[int, int, int]] = []
    for first in range(len(rooms)):
        for second in range(first + 1, len(rooms)):
            gap = rect_gap(rooms[first], rooms[second])
            if gap < spacing:
                violations.append((first, second, gap))
    return violations


def _find_relocation(
    grid: list[list[int]],
    kept_rooms: Sequence[Room],
    room: Room,
    spacing: int,
    rng: DeterministicRNG,
) -> Room | None:
    for x, y in _candidate_positions(
        len(grid[0]) if grid else 0,
        len(grid),
        room.w,
        room.h,
        rng,
    ):
        candidate = Room(x, y, room.w, room.h)
        if _room_fits(grid, kept_rooms, candidate, spacing):
            return candidate
    return None


def _apply_room_spacing(
    spec: LevelSpec, constraint: Constraint, state: LevelState, rng: DeterministicRNG
) -> LevelState:
    spacing = int(constraint.get("min_spacing"))
    if not _spacing_violations(state.rooms, spacing):
        return state
    grid = state.mutable_grid()
    rooms = list(state.rooms)
    max_repairs = max(8, len(rooms) * 5)
    for _repair in range(max_repairs):
        violations = _spacing_violations(rooms, spacing)
        if not violations:
            return LevelState.from_lists(grid, rooms)
        first, second, _gap = violations[rng.randbelow(len(violations))]
        choices = [second, first]
        if rng.randbelow(2):
            choices.reverse()
        repaired = False
        for room_index in choices:
            old_room = rooms[room_index]
            clear_rectangle(grid, old_room)
            kept_rooms = [
                room for index, room in enumerate(rooms) if index != room_index
            ]
            new_room = _find_relocation(
                grid, kept_rooms, old_room, spacing, rng
            )
            if new_room is not None:
                add_rectangle(grid, new_room)
                rooms[room_index] = new_room
                repaired = True
                break
            # Restore the old room before trying the other side of the pair.
            add_rectangle(grid, old_room)
        if not repaired:
            raise CandidateRejected(
                "no legal room relocation satisfies the spacing bound",
                conflict_ids=(constraint.id,),
            )
    if _spacing_violations(rooms, spacing):
        raise CandidateRejected(
            "spacing repair did not converge",
            conflict_ids=(constraint.id,),
        )
    return LevelState.from_lists(grid, rooms)


def _apply_connectivity(
    spec: LevelSpec, constraint: Constraint, state: LevelState, rng: DeterministicRNG
) -> LevelState:
    grid = state.mutable_grid()
    for _iteration in range(max(2, spec.width * spec.height + 4)):
        components = floor_components(grid)
        if len(components) <= 1:
            return LevelState.from_lists(grid, state.rooms)
        first_index = rng.randbelow(len(components))
        second_index = rng.randbelow(len(components) - 1)
        if second_index >= first_index:
            second_index += 1
        path = find_path(
            grid,
            components[first_index],
            components[second_index],
            neighbor_order=shuffled_neighbor_order(rng),
            passable=lambda value: value != BLOCKED,
        )
        if path is None:
            raise CandidateRejected(
                "blocked cells separate floor components",
                conflict_ids=(constraint.id,),
            )
        carve_path(grid, path)
    raise CandidateRejected(
        "connectivity repair did not converge",
        conflict_ids=(constraint.id,),
    )


def _apply_start_end(
    spec: LevelSpec, constraint: Constraint, state: LevelState, rng: DeterministicRNG
) -> LevelState:
    sx, sy = constraint.get("start")
    ex, ey = constraint.get("end")
    grid = state.mutable_grid()
    for x, y in ((sx, sy), (ex, ey)):
        if grid[y][x] == EMPTY:
            grid[y][x] = CORRIDOR
        elif grid[y][x] == BLOCKED:
            raise CandidateRejected(
                "start or end is blocked", conflict_ids=(constraint.id,)
            )
    if cells_reachable(grid, (sx, sy), (ex, ey)):
        return LevelState.from_lists(grid, state.rooms)
    path = find_path(
        grid,
        [(sx, sy)],
        [(ex, ey)],
        neighbor_order=shuffled_neighbor_order(rng),
        passable=lambda value: value != BLOCKED,
    )
    if path is None:
        raise CandidateRejected(
            "start and end are separated by blocked cells",
            conflict_ids=(constraint.id,),
        )
    carve_path(grid, path)
    return LevelState.from_lists(grid, state.rooms)


def apply_constraint(
    spec: LevelSpec,
    constraint: Constraint,
    state: LevelState,
    attempt: int,
) -> LevelState:
    """Generate one deterministic candidate state for a constraint attempt."""

    if constraint.index >= len(spec.constraints) or (
        spec.constraints[constraint.index].id != constraint.id
    ):
        raise ValueError("constraint does not belong to this specification")
    rng = DeterministicRNG(
        derive_seed(
            spec.seed,
            spec.spec_digest,
            constraint.index,
            constraint.id,
            attempt,
            f"apply:{constraint.type}",
        )
    )
    if constraint.type == "min_rooms":
        return _apply_min_rooms(spec, constraint, state, rng)
    if constraint.type == "max_area":
        return _apply_max_area(spec, constraint, state, rng)
    if constraint.type == "room_spacing":
        return _apply_room_spacing(spec, constraint, state, rng)
    if constraint.type == "connectivity":
        return _apply_connectivity(spec, constraint, state, rng)
    if constraint.type == "start_end_reachable":
        return _apply_start_end(spec, constraint, state, rng)
    raise CandidateRejected(
        f"unsupported constraint type: {constraint.type}",
        conflict_ids=(constraint.id,),
    )


def check_constraint(
    spec: LevelSpec, constraint: Constraint, state: LevelState
) -> None:
    if constraint.type == "min_rooms":
        count = int(constraint.get("count"))
        if len(state.rooms) < count:
            raise ConstraintViolation(
                constraint.id,
                f"only {len(state.rooms)} rooms exist; {count} required",
            )
        return
    if constraint.type == "max_area":
        limit = int(constraint.get("max_area"))
        oversized = [room for room in state.rooms if room.area > limit]
        if oversized:
            raise ConstraintViolation(
                constraint.id,
                f"{len(oversized)} room(s) exceed max_area {limit}",
            )
        return
    if constraint.type == "room_spacing":
        spacing = int(constraint.get("min_spacing"))
        violations = _spacing_violations(state.rooms, spacing)
        if violations:
            first, second, gap = violations[0]
            raise ConstraintViolation(
                constraint.id,
                f"rooms {first} and {second} have gap {gap}, below {spacing}",
            )
        return
    if constraint.type == "connectivity":
        if not floor_is_connected(state.grid):
            raise ConstraintViolation(
                constraint.id, "floor cells form multiple disconnected components"
            )
        return
    if constraint.type == "start_end_reachable":
        start = tuple(constraint.get("start"))
        end = tuple(constraint.get("end"))
        if not cells_reachable(state.grid, start, end):
            raise ConstraintViolation(
                constraint.id,
                f"start {list(start)} cannot reach end {list(end)}",
            )
        return
    raise ConstraintViolation(
        constraint.id, f"unsupported constraint type: {constraint.type}"
    )


def check_state_integrity(spec: LevelSpec, state: LevelState) -> None:
    if state.width != spec.width or state.height != spec.height:
        raise CandidateRejected(
            "candidate grid dimensions do not match the specification"
        )
    errors = room_grid_errors(state)
    if errors:
        raise CandidateRejected(errors[0])
    for x, y in spec.blocked:
        if state.grid[y][x] != BLOCKED:
            raise CandidateRejected(
                f"candidate overwrote blocked cell ({x},{y})"
            )


def estimate_state_bytes(spec: LevelSpec) -> int:
    # Python ints/rows have overhead; 16 bytes/cell plus a fixed allowance is a
    # conservative estimate used only for the memory-budget admission check.
    return spec.width * spec.height * 16 + 16_384


def iter_constraints_through(spec: LevelSpec, index: int) -> Iterable[Constraint]:
    return spec.constraints[: index + 1]
