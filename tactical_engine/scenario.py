from __future__ import annotations

import json
import math
import copy
from decimal import Decimal
from pathlib import Path
from typing import Any

from .errors import EngineError
from .hashing import canonical_decimal, sha256_canonical
from .model import (
    Candidate,
    Point,
    PrecisionSpec,
    ReplayEvent,
    Scenario,
    SearchParams,
    SmoothingParams,
    UnitSpec,
)
from .precision import build_terrain, quantize_fixed, tolerance_to_fixed

SUPPORTED_SCHEMA_VERSION = 1
SEARCH_METRICS = {"manhattan", "octile", "chebyshev"}
DIAGONAL_RULES = {"never", "allow", "no_corner_cutting"}
SMOOTHING_ALGORITHMS = {"none", "collinear", "line_of_sight"}
BUDGET_POLICIES = {"reachable", "unreachable"}
REPLAY_EVENT_KINDS = {"move", "arrive", "budget_exhausted", "wait", "plan"}


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON constant is not allowed: {value}")


def _schema_error(message: str, path: str = "$") -> EngineError:
    return EngineError("E_SCHEMA", message, {"path": path})


def _object(value: Any, path: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise _schema_error("expected an object", path)
    return value


def _array(value: Any, path: str) -> list[Any]:
    if not isinstance(value, list):
        raise _schema_error("expected an array", path)
    return value


def _string(value: Any, path: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value):
        raise _schema_error("expected a non-empty string", path)
    return value


def _integer(value: Any, path: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise _schema_error("expected an integer", path)
    return value


def _decimal(value: Any, path: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise _schema_error("expected a finite JSON number", path)
    if isinstance(value, int):
        result = Decimal(value)
    elif isinstance(value, float):
        result = Decimal(str(value))
    else:
        result = value
    if not result.is_finite():
        raise _schema_error("expected a finite JSON number", path)
    return result


def _require_float_range(
    value: Decimal,
    code: str,
    path: str,
    *,
    allow_zero: bool,
) -> None:
    try:
        as_float = float(value)
    except (OverflowError, ValueError) as exc:
        raise EngineError(
            code,
            "number is outside the supported finite float64 range",
            {"path": path},
        ) from exc
    valid = math.isfinite(as_float) and (as_float >= 0 if allow_zero else as_float > 0)
    if not valid:
        raise EngineError(
            code,
            "number must be finite, positive, and representable as float64",
            {"path": path, "value": canonical_decimal(value)},
        )


def _point(value: Any, path: str, width: int, height: int, code: str) -> Point:
    coords = _array(value, path)
    if len(coords) != 2:
        raise EngineError(code, "point must contain exactly [row, col]", {"path": path})
    row = _integer(coords[0], f"{path}[0]")
    col = _integer(coords[1], f"{path}[1]")
    if not (0 <= row < height and 0 <= col < width):
        raise EngineError(
            code,
            "point is outside terrain bounds",
            {"path": path, "point": [row, col], "width": width, "height": height},
        )
    return row, col


def _required(obj: dict[str, Any], key: str, path: str) -> Any:
    if key not in obj:
        raise _schema_error(f"missing required field '{key}'", f"{path}.{key}")
    return obj[key]


def _parse_terrain_payload(raw: Any) -> tuple[int, int, tuple[tuple[Decimal, ...], ...], frozenset[Point]]:
    terrain = _object(raw, "$.terrain")
    width_value = _required(terrain, "width", "$.terrain")
    height_value = _required(terrain, "height", "$.terrain")
    path = "$.terrain.width"
    try:
        width = _integer(width_value, path)
        height = _integer(height_value, "$.terrain.height")
    except EngineError:
        raise
    if width <= 0 or height <= 0:
        raise EngineError(
            "E_GRID_SIZE",
            "terrain width and height must be positive integers",
            {"width": width, "height": height},
        )

    rows_raw = _array(_required(terrain, "costs", "$.terrain"), "$.terrain.costs")
    if len(rows_raw) != height:
        raise EngineError(
            "E_GRID_SHAPE",
            "terrain costs must contain exactly one row per grid row",
            {"expected_rows": height, "actual_rows": len(rows_raw)},
        )
    rows: list[tuple[Decimal, ...]] = []
    for row_index, row_raw in enumerate(rows_raw):
        row_values = _array(row_raw, f"$.terrain.costs[{row_index}]")
        if len(row_values) != width:
            raise EngineError(
                "E_GRID_SHAPE",
                "terrain cost row width does not match grid width",
                {"row": row_index, "expected": width, "actual": len(row_values)},
            )
        parsed_row: list[Decimal] = []
        for col_index, value in enumerate(row_values):
            cost = _decimal(value, f"$.terrain.costs[{row_index}][{col_index}]")
            if cost <= 0:
                raise EngineError(
                    "E_TERRAIN_COST",
                    "terrain cost coefficients must be finite positive numbers",
                    {"row": row_index, "col": col_index, "value": canonical_decimal(cost)},
                )
            _require_float_range(
                cost,
                "E_TERRAIN_COST",
                f"$.terrain.costs[{row_index}][{col_index}]",
                allow_zero=False,
            )
            parsed_row.append(cost)
        rows.append(tuple(parsed_row))

    blocked_raw = _array(_required(terrain, "blocked", "$.terrain"), "$.terrain.blocked")
    blocked: set[Point] = set()
    for index, value in enumerate(blocked_raw):
        blocked.add(_point(value, f"$.terrain.blocked[{index}]", width, height, "E_BLOCKED_COORD"))
    return width, height, tuple(rows), frozenset(blocked)


def _parse_point_field(
    obj: dict[str, Any],
    key: str,
    path: str,
    width: int,
    height: int,
    code: str,
) -> Point:
    return _point(_required(obj, key, path), f"{path}.{key}", width, height, code)


def _parse_units(
    raw: Any,
    width: int,
    height: int,
    blocked: frozenset[Point],
    scale: Decimal,
) -> tuple[UnitSpec, ...]:
    units_raw = _array(raw, "$.units")
    if not units_raw:
        raise _schema_error("at least one unit is required", "$.units")
    units: list[UnitSpec] = []
    ids: set[str] = set()
    for index, unit_raw in enumerate(units_raw):
        path = f"$.units[{index}]"
        unit = _object(unit_raw, path)
        unit_id = _string(_required(unit, "id", path), f"{path}.id")
        if unit_id in ids:
            raise EngineError("E_UNIT_ID", "unit ids must be unique", {"unit_id": unit_id})
        ids.add(unit_id)
        name = _string(unit.get("name", unit_id), f"{path}.name")
        start = _parse_point_field(unit, "start", path, width, height, "E_POINT_COORD")
        goal = _parse_point_field(unit, "goal", path, width, height, "E_POINT_COORD")
        if start == goal:
            raise EngineError(
                "E_POINT_OVERLAP",
                "unit start and goal must not overlap",
                {"unit_id": unit_id, "point": list(start)},
            )
        if start in blocked or goal in blocked:
            raise EngineError(
                "E_POINT_BLOCKED",
                "unit start and goal must be passable",
                {"unit_id": unit_id, "start": list(start), "goal": list(goal)},
            )
        budget = _decimal(_required(unit, "budget", path), f"{path}.budget")
        if budget < 0:
            raise EngineError(
                "E_UNIT_BUDGET",
                "unit movement budget must be finite and non-negative",
                {"unit_id": unit_id, "budget": canonical_decimal(budget)},
            )
        _require_float_range(budget, "E_UNIT_BUDGET", f"{path}.budget", allow_zero=True)

        candidates_raw = unit.get("candidates", [])
        candidates_values = _array(candidates_raw, f"{path}.candidates")
        candidates: list[Candidate] = []
        candidate_ids: set[str] = set()
        for candidate_index, candidate_raw in enumerate(candidates_values):
            candidate_path = f"{path}.candidates[{candidate_index}]"
            candidate_obj = _object(candidate_raw, candidate_path)
            candidate_id = _string(
                _required(candidate_obj, "id", candidate_path),
                f"{candidate_path}.id",
            )
            if candidate_id in candidate_ids:
                raise EngineError(
                    "E_CANDIDATE_ID",
                    "candidate target ids must be unique within a unit",
                    {"unit_id": unit_id, "candidate_id": candidate_id},
                )
            candidate_ids.add(candidate_id)
            cell = _parse_point_field(
                candidate_obj,
                "cell",
                candidate_path,
                width,
                height,
                "E_POINT_COORD",
            )
            if cell in blocked:
                raise EngineError(
                    "E_POINT_BLOCKED",
                    "candidate target must be passable",
                    {"unit_id": unit_id, "candidate_id": candidate_id, "cell": list(cell)},
                )
            candidates.append(Candidate(candidate_id, cell))
        if not candidates:
            candidates = [Candidate("goal", goal)]

        units.append(
            UnitSpec(
                id=unit_id,
                name=name,
                start=start,
                goal=goal,
                budget=budget,
                budget_fixed=quantize_fixed(budget, scale),
                candidates=tuple(candidates),
            )
        )
    return tuple(units)


def _parse_replay(
    raw: Any,
    units: tuple[UnitSpec, ...],
    blocked: frozenset[Point],
    width: int,
    height: int,
) -> tuple[ReplayEvent, ...]:
    unit_map = {unit.id: unit for unit in units}
    events_raw = _array(raw, "$.replay")
    events: list[ReplayEvent] = []
    for index, event_raw in enumerate(events_raw):
        path = f"$.replay[{index}]"
        event_obj = _object(event_raw, path)
        kind = _string(_required(event_obj, "event", path), f"{path}.event")
        if kind not in REPLAY_EVENT_KINDS:
            raise EngineError(
                "E_REPLAY_EVENT",
                "unknown replay event type",
                {"index": index, "event": kind, "allowed": sorted(REPLAY_EVENT_KINDS)},
            )
        unit_id = _string(_required(event_obj, "unit_id", path), f"{path}.unit_id")
        if unit_id not in unit_map:
            raise EngineError(
                "E_REPLAY_TARGET",
                "replay event references an unknown unit",
                {"index": index, "unit_id": unit_id},
            )
        data: dict[str, Any] = {}
        if kind in {"move", "plan"}:
            has_to = "to" in event_obj
            has_candidate = "candidate" in event_obj
            if has_to == has_candidate:
                raise EngineError(
                    "E_REPLAY_TARGET",
                    "move/plan events require exactly one of 'to' or 'candidate'",
                    {"index": index},
                )
            if has_to:
                cell = _point(event_obj["to"], f"{path}.to", width, height, "E_POINT_COORD")
                if cell in blocked:
                    raise EngineError(
                        "E_POINT_BLOCKED",
                        "replay target must be passable",
                        {"index": index, "target": list(cell)},
                    )
                data["to"] = cell
            else:
                candidate_id = _string(event_obj["candidate"], f"{path}.candidate")
                candidate_map = {candidate.id: candidate for candidate in unit_map[unit_id].candidates}
                if candidate_id not in candidate_map:
                    raise EngineError(
                        "E_REPLAY_TARGET",
                        "replay event references an unknown candidate",
                        {"index": index, "unit_id": unit_id, "candidate": candidate_id},
                    )
                data["candidate"] = candidate_id
                data["to"] = candidate_map[candidate_id].cell
        elif kind == "arrive":
            if "at" in event_obj:
                at = _point(event_obj["at"], f"{path}.at", width, height, "E_POINT_COORD")
                if at in blocked:
                    raise EngineError(
                        "E_POINT_BLOCKED",
                        "arrival cell must be passable",
                        {"index": index, "at": list(at)},
                    )
                data["at"] = at
        events.append(ReplayEvent(index=index, kind=kind, unit_id=unit_id, data=data))
    return tuple(events)


def parse_scenario(raw: Any) -> Scenario:
    root = _object(raw, "$")
    schema_version = _integer(
        _required(root, "schema_version", "$"),
        "$.schema_version",
    )
    if schema_version != SUPPORTED_SCHEMA_VERSION:
        raise EngineError(
            "E_SCHEMA_VERSION",
            "unsupported scenario schema version",
            {"supported": SUPPORTED_SCHEMA_VERSION, "actual": schema_version},
        )
    scenario_id = _string(_required(root, "scenario_id", "$"), "$.scenario_id")
    width, height, costs, blocked = _parse_terrain_payload(
        _required(root, "terrain", "$")
    )

    search_obj = _object(_required(root, "search", "$"), "$.search")
    weight = _decimal(
        _required(search_obj, "heuristic_weight", "$.search"),
        "$.search.heuristic_weight",
    )
    if weight < 0:
        raise EngineError(
            "E_HEURISTIC_WEIGHT",
            "heuristic weight must be finite and non-negative",
            {"value": canonical_decimal(weight)},
        )
    _require_float_range(
        weight,
        "E_HEURISTIC_WEIGHT",
        "$.search.heuristic_weight",
        allow_zero=True,
    )
    metric = _string(
        _required(search_obj, "heuristic_metric", "$.search"),
        "$.search.heuristic_metric",
    )
    if metric not in SEARCH_METRICS:
        raise EngineError(
            "E_HEURISTIC_METRIC",
            "unsupported heuristic metric",
            {"value": metric, "allowed": sorted(SEARCH_METRICS)},
        )
    diagonal_rule = _string(
        _required(search_obj, "diagonal_rule", "$.search"),
        "$.search.diagonal_rule",
    )
    if diagonal_rule not in DIAGONAL_RULES:
        raise EngineError(
            "E_DIAGONAL_RULE",
            "unsupported diagonal movement rule",
            {"value": diagonal_rule, "allowed": sorted(DIAGONAL_RULES)},
        )
    diagonal_factor = _decimal(
        search_obj.get("diagonal_cost_factor", Decimal("1.4142135623730951")),
        "$.search.diagonal_cost_factor",
    )
    if diagonal_factor <= 0:
        raise EngineError(
            "E_DIAGONAL_FACTOR",
            "diagonal cost factor must be finite and positive",
            {"value": canonical_decimal(diagonal_factor)},
        )
    _require_float_range(
        diagonal_factor,
        "E_DIAGONAL_FACTOR",
        "$.search.diagonal_cost_factor",
        allow_zero=False,
    )

    precision_obj = _object(_required(root, "precision", "$"), "$.precision")
    scale = _decimal(
        _required(precision_obj, "fixed_scale", "$.precision"),
        "$.precision.fixed_scale",
    )
    if scale <= 0:
        raise EngineError(
            "E_SCALE",
            "fixed scale must be finite and positive",
            {"value": canonical_decimal(scale)},
        )
    _require_float_range(
        scale,
        "E_SCALE",
        "$.precision.fixed_scale",
        allow_zero=False,
    )
    tolerance = _decimal(
        _required(precision_obj, "tolerance", "$.precision"),
        "$.precision.tolerance",
    )
    if tolerance <= 0:
        raise EngineError(
            "E_TOLERANCE",
            "tolerance must be finite and positive",
            {"value": canonical_decimal(tolerance)},
        )
    _require_float_range(
        tolerance,
        "E_TOLERANCE",
        "$.precision.tolerance",
        allow_zero=False,
    )
    budget_policy = _string(
        precision_obj.get("budget_policy", "reachable"),
        "$.precision.budget_policy",
    )
    if budget_policy not in BUDGET_POLICIES:
        raise EngineError(
            "E_BUDGET_POLICY",
            "unsupported budget boundary policy",
            {"value": budget_policy, "allowed": sorted(BUDGET_POLICIES)},
        )
    tolerance_fixed = tolerance_to_fixed(tolerance, scale)

    smoothing_obj = _object(
        root.get("smoothing", {"enabled": True, "algorithm": "line_of_sight"}),
        "$.smoothing",
    )
    enabled_raw = smoothing_obj.get("enabled", True)
    if not isinstance(enabled_raw, bool):
        raise EngineError(
            "E_SMOOTHING",
            "smoothing.enabled must be boolean",
            {"path": "$.smoothing.enabled"},
        )
    algorithm = _string(
        smoothing_obj.get("algorithm", "line_of_sight"),
        "$.smoothing.algorithm",
    )
    if algorithm not in SMOOTHING_ALGORITHMS:
        raise EngineError(
            "E_SMOOTHING",
            "unsupported smoothing algorithm",
            {"value": algorithm, "allowed": sorted(SMOOTHING_ALGORITHMS)},
        )
    max_lookahead = _integer(
        smoothing_obj.get("max_lookahead", 0),
        "$.smoothing.max_lookahead",
    )
    if max_lookahead < 0:
        raise EngineError(
            "E_SMOOTHING",
            "smoothing.max_lookahead must be a non-negative integer",
            {"value": max_lookahead},
        )

    resources_obj = _object(_required(root, "resources", "$"), "$.resources")
    concurrency = _integer(
        _required(resources_obj, "concurrency", "$.resources"),
        "$.resources.concurrency",
    )
    if concurrency <= 0:
        raise EngineError(
            "E_CONCURRENCY",
            "concurrency must be a positive integer",
            {"value": concurrency},
        )
    memory_budget = _integer(
        _required(resources_obj, "memory_budget_bytes", "$.resources"),
        "$.resources.memory_budget_bytes",
    )
    if memory_budget <= 0:
        raise EngineError(
            "E_MEMORY_BUDGET",
            "memory budget must be a positive integer number of bytes",
            {"value": memory_budget},
        )

    units = _parse_units(
        _required(root, "units", "$"),
        width,
        height,
        blocked,
        scale,
    )
    replay = _parse_replay(
        _required(root, "replay", "$"),
        units,
        blocked,
        width,
        height,
    )
    terrain = build_terrain(width, height, costs, blocked, scale, diagonal_factor)
    fingerprint_payload = {
        "format": "tactical-scene-v1",
        "scenario_id": scenario_id,
        "terrain": {
            "width": width,
            "height": height,
            "costs": [[canonical_decimal(cost) for cost in row] for row in costs],
            "blocked": sorted([list(point) for point in blocked]),
        },
        "search": {
            "heuristic_weight": weight,
            "heuristic_metric": metric,
            "diagonal_rule": diagonal_rule,
            "diagonal_cost_factor": diagonal_factor,
        },
        "precision": {
            "fixed_scale": scale,
            "tolerance": tolerance,
            "budget_policy": budget_policy,
        },
        "smoothing": {
            "enabled": enabled_raw,
            "algorithm": algorithm,
            "max_lookahead": max_lookahead,
        },
        "resources": {
            "concurrency": concurrency,
            "memory_budget_bytes": memory_budget,
        },
        "units": [
            {
                "id": unit.id,
                "name": unit.name,
                "start": unit.start,
                "goal": unit.goal,
                "budget": unit.budget,
                "candidates": [
                    {"id": candidate.id, "cell": candidate.cell}
                    for candidate in unit.candidates
                ],
            }
            for unit in units
        ],
        "replay": [
            {
                "index": event.index,
                "kind": event.kind,
                "unit_id": event.unit_id,
                "data": event.data,
            }
            for event in replay
        ],
    }
    return Scenario(
        schema_version=schema_version,
        scenario_id=scenario_id,
        terrain=terrain,
        search=SearchParams(weight, metric, diagonal_rule, diagonal_factor),
        precision=PrecisionSpec(scale, tolerance, tolerance_fixed, budget_policy),
        smoothing=SmoothingParams(enabled_raw, algorithm, max_lookahead),
        concurrency=concurrency,
        memory_budget_bytes=memory_budget,
        units=units,
        replay=replay,
        fingerprint=sha256_canonical(fingerprint_payload),
    )


def load_scenario(path: str | Path) -> Scenario:
    scenario_path = Path(path)
    raw = _read_json_document(scenario_path)
    return parse_scenario(raw)


def _read_json_document(path: Path) -> Any:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise EngineError(
            "E_IO",
            "unable to read JSON input file",
            {"path": str(path), "reason": str(exc)},
        ) from exc
    try:
        return json.loads(
            text,
            parse_float=Decimal,
            parse_int=int,
            parse_constant=_reject_constant,
        )
    except (json.JSONDecodeError, ValueError) as exc:
        raise EngineError(
            "E_JSON_INVALID",
            "input file is not valid finite JSON",
            {"path": str(path), "reason": str(exc)},
        ) from exc


def load_scenario_parts(
    terrain_path: str | Path,
    units_path: str | Path,
    replay_path: str | Path,
    options_path: str | Path,
) -> Scenario:
    terrain_raw = _read_json_document(Path(terrain_path))
    units_raw = _read_json_document(Path(units_path))
    replay_raw = _read_json_document(Path(replay_path))
    options_raw = _read_json_document(Path(options_path))

    if not isinstance(options_raw, dict):
        raise _schema_error("options root must be an object", "$options")
    merged = copy.deepcopy(options_raw)
    merged["terrain"] = (
        terrain_raw["terrain"]
        if isinstance(terrain_raw, dict) and "terrain" in terrain_raw
        else terrain_raw
    )
    if isinstance(units_raw, dict) and "units" in units_raw:
        merged["units"] = units_raw["units"]
    elif isinstance(units_raw, list):
        merged["units"] = units_raw
    else:
        raise _schema_error("units input must be an array or an object with 'units'", "$units")
    if isinstance(replay_raw, dict) and "replay" in replay_raw:
        merged["replay"] = replay_raw["replay"]
    elif isinstance(replay_raw, list):
        merged["replay"] = replay_raw
    else:
        raise _schema_error("replay input must be an array or an object with 'replay'", "$replay")
    return parse_scenario(merged)
