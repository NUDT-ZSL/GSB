from __future__ import annotations

from dataclasses import dataclass

from .model import Point, SmoothingParams, Terrain


@dataclass(frozen=True)
class SmoothedPath:
    anchors: tuple[Point, ...]
    expanded: tuple[Point, ...]
    segments: tuple[tuple[Point, Point], ...]
    applied: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "anchors": [list(point) for point in self.anchors],
            "segments": [
                {"start": list(start), "end": list(end)}
                for start, end in self.segments
            ],
            "applied": self.applied,
        }


def _line_points(start: Point, end: Point) -> tuple[Point, ...]:
    row0, col0 = start
    row1, col1 = end
    dr = abs(row1 - row0)
    dc = abs(col1 - col0)
    sr = 1 if row0 < row1 else -1
    sc = 1 if col0 < col1 else -1
    error = dc - dr
    row, col = row0, col0
    result: list[Point] = [(row, col)]
    while (row, col) != (row1, col1):
        twice = 2 * error
        if twice > -dr:
            error -= dr
            col += sc
        if twice < dc:
            error += dc
            row += sr
        result.append((row, col))
    return tuple(result)


def _can_step(terrain: Terrain, source: Point, target: Point, diagonal_rule: str) -> bool:
    if not terrain.passable(target):
        return False
    dr = target[0] - source[0]
    dc = target[1] - source[1]
    if max(abs(dr), abs(dc)) != 1:
        return False
    if dr != 0 and dc != 0:
        if diagonal_rule == "never":
            return False
        if diagonal_rule == "no_corner_cutting":
            if not terrain.passable((source[0] + dr, source[1])):
                return False
            if not terrain.passable((source[0], source[1] + dc)):
                return False
    return True


def validate_path(
    terrain: Terrain,
    path: tuple[Point, ...],
    diagonal_rule: str,
) -> bool:
    if not path or not terrain.passable(path[0]):
        return False
    return all(
        _can_step(terrain, source, target, diagonal_rule)
        for source, target in zip(path, path[1:])
    )


def _segment_is_valid(
    terrain: Terrain,
    start: Point,
    end: Point,
    diagonal_rule: str,
) -> tuple[bool, tuple[Point, ...]]:
    cells = _line_points(start, end)
    if not all(terrain.passable(cell) for cell in cells):
        return False, ()
    for source, target in zip(cells, cells[1:]):
        if not _can_step(terrain, source, target, diagonal_rule):
            return False, ()
    return True, cells


def _simplify_collinear(path: tuple[Point, ...]) -> tuple[Point, ...]:
    if len(path) <= 2:
        return path
    anchors: list[Point] = [path[0]]
    for index in range(1, len(path) - 1):
        direction = (
            path[index + 1][0] - path[index][0],
            path[index + 1][1] - path[index][1],
        )
        incoming = (
            path[index][0] - path[index - 1][0],
            path[index][1] - path[index - 1][1],
        )
        if incoming == direction:
            continue
        anchors.append(path[index])
    anchors.append(path[-1])
    return tuple(anchors)


def smooth_path(
    terrain: Terrain,
    path: tuple[Point, ...],
    params: SmoothingParams,
    diagonal_rule: str,
) -> SmoothedPath:
    if not path:
        return SmoothedPath((), (), (), False)
    if not params.enabled or params.algorithm == "none" or len(path) <= 2:
        return SmoothedPath(
            path,
            path,
            tuple(zip(path, path[1:])),
            False,
        )
    if params.algorithm == "collinear":
        anchors = _simplify_collinear(path)
        return SmoothedPath(
            anchors,
            anchors,
            tuple(zip(anchors, anchors[1:])),
            len(anchors) != len(path),
        )

    anchors: list[Point] = [path[0]]
    expanded: list[Point] = [path[0]]
    segments: list[tuple[Point, Point]] = []
    index = 0
    while index < len(path) - 1:
        furthest = index + 1
        upper_bound = len(path) - 1
        if params.max_lookahead > 0:
            upper_bound = min(upper_bound, index + params.max_lookahead)
        for candidate_index in range(upper_bound, index, -1):
            valid, cells = _segment_is_valid(
                terrain,
                path[index],
                path[candidate_index],
                diagonal_rule,
            )
            if valid:
                furthest = candidate_index
                break
        valid, cells = _segment_is_valid(
            terrain,
            path[index],
            path[furthest],
            diagonal_rule,
        )
        if not valid:
            cells = (path[index], path[index + 1])
            furthest = index + 1
        segments.append((path[index], path[furthest]))
        expanded.extend(cells[1:])
        if furthest != len(path) - 1:
            anchors.append(path[furthest])
        index = furthest
    anchors.append(path[-1])
    return SmoothedPath(
        anchors=tuple(anchors),
        expanded=tuple(expanded),
        segments=tuple(segments),
        applied=len(anchors) != len(path),
    )
