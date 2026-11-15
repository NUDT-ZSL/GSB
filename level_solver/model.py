"""JSON level specification parsing and canonicalization."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

from .errors import SolverError, input_error

ALLOWED_CONSTRAINT_TYPES = frozenset(
    {
        "min_rooms",
        "max_area",
        "room_spacing",
        "connectivity",
        "start_end_reachable",
    }
)

_TYPE_ALIASES = {
    "min_room_count": "min_rooms",
    "minimum_rooms": "min_rooms",
    "max_room_area": "max_area",
    "area_limit": "max_area",
    "spacing": "room_spacing",
    "min_spacing": "room_spacing",
    "connected": "connectivity",
    "reachability": "start_end_reachable",
}

_MISSING = object()


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def digest_value(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Constraint:
    index: int
    id: str
    type: str
    params: Mapping[str, Any]

    def get(self, key: str, default: Any = _MISSING) -> Any:
        if key in self.params:
            return self.params[key]
        if default is _MISSING:
            raise KeyError(key)
        return default

    def canonical(self) -> dict[str, Any]:
        return {"id": self.id, "type": self.type, "params": dict(self.params)}


@dataclass(frozen=True)
class SolverConfig:
    rollback_limit: int
    attempt_limit: int
    concurrency: int
    memory_budget_kb: int
    snapshot_dir: str | None
    output: str | None


@dataclass(frozen=True)
class LevelSpec:
    width: int
    height: int
    blocked: frozenset[tuple[int, int]]
    seed: int
    constraints: tuple[Constraint, ...]
    config: SolverConfig

    @property
    def available_count(self) -> int:
        return self.width * self.height - len(self.blocked)

    @property
    def spec_digest(self) -> str:
        return digest_value(self.canonical_summary())

    @property
    def run_id(self) -> str:
        return hashlib.sha256(
            f"{self.spec_digest}\0{self.seed}".encode("utf-8")
        ).hexdigest()

    def canonical_summary(self) -> dict[str, Any]:
        return {
            "format": "level-constraint-spec-v1",
            "width": self.width,
            "height": self.height,
            "blocked": [list(coord) for coord in sorted(self.blocked)],
            "seed": self.seed,
            "constraints": [constraint.canonical() for constraint in self.constraints],
            "rollback_limit": self.config.rollback_limit,
            "attempt_limit": self.config.attempt_limit,
        }

    def constraint_ids(self) -> list[str]:
        return [constraint.id for constraint in self.constraints]


def _lookup(mapping: Mapping[str, Any], *paths: str, default: Any = _MISSING) -> Any:
    for path in paths:
        current: Any = mapping
        found = True
        for part in path.split("."):
            if not isinstance(current, Mapping) or part not in current:
                found = False
                break
            current = current[part]
        if found:
            return current
    if default is not _MISSING:
        return default
    raise KeyError(paths[0])


def _int_value(
    value: Any,
    *,
    code: str,
    field: str,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise input_error(code, f"{field} must be an integer", field=field)
    if minimum is not None and value < minimum:
        raise input_error(
            code,
            f"{field} must be >= {minimum}",
            field=field,
            minimum=minimum,
            actual=value,
        )
    if maximum is not None and value > maximum:
        raise input_error(
            code,
            f"{field} must be <= {maximum}",
            field=field,
            maximum=maximum,
            actual=value,
        )
    return value


def _finite_number(
    value: Any, *, code: str, field: str, minimum: float | None = None
) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise input_error(code, f"{field} must be a finite number", field=field)
    number = float(value)
    if not math.isfinite(number):
        raise input_error(code, f"{field} must be finite", field=field)
    if minimum is not None and number < minimum:
        raise input_error(
            code,
            f"{field} must be >= {minimum}",
            field=field,
            minimum=minimum,
            actual=value,
        )
    return number


def _coordinate(value: Any, *, field: str) -> tuple[int, int]:
    if isinstance(value, Mapping):
        if "x" not in value or "y" not in value:
            raise input_error(
                "E_BLOCKED", f"{field} must contain integer x and y", field=field
            )
        x, y = value["x"], value["y"]
    elif isinstance(value, (list, tuple)) and len(value) == 2:
        x, y = value
    else:
        raise input_error(
            "E_BLOCKED",
            f"{field} must be [x, y] or {{'x': x, 'y': y}}",
            field=field,
        )
    if isinstance(x, bool) or not isinstance(x, int):
        raise input_error("E_BLOCKED", f"{field}.x must be an integer", field=field)
    if isinstance(y, bool) or not isinstance(y, int):
        raise input_error("E_BLOCKED", f"{field}.y must be an integer", field=field)
    return x, y


def _normalize_type(raw_type: Any, index: int) -> str:
    if not isinstance(raw_type, str) or not raw_type.strip():
        raise input_error(
            "E_CONSTRAINT_TYPE",
            "constraint type must be a non-empty string",
            constraint_index=index,
        )
    normalized = raw_type.strip().lower().replace("-", "_").replace(" ", "_")
    normalized = _TYPE_ALIASES.get(normalized, normalized)
    if normalized not in ALLOWED_CONSTRAINT_TYPES:
        raise input_error(
            "E_CONSTRAINT_TYPE",
            f"unsupported constraint type: {raw_type}",
            constraint_index=index,
            allowed_types=sorted(ALLOWED_CONSTRAINT_TYPES),
        )
    return normalized


def _normalize_constraint(raw: Any, index: int) -> Constraint:
    if not isinstance(raw, Mapping):
        raise input_error(
            "E_CONSTRAINT_PARAM",
            "each constraint must be a JSON object",
            constraint_index=index,
        )
    constraint_type = _normalize_type(raw.get("type"), index)
    raw_id = raw.get("id", raw.get("name", f"constraint_{index}"))
    if not isinstance(raw_id, str) or not raw_id.strip():
        raise input_error(
            "E_CONSTRAINT_PARAM",
            "constraint id must be a non-empty string",
            constraint_index=index,
        )
    constraint_id = raw_id.strip()

    params: dict[str, Any] = {}
    if constraint_type == "min_rooms":
        value = _lookup(
            raw,
            "count",
            "min_rooms",
            "rooms",
            default=_lookup(raw, "params.count", "params.min_rooms", default=None),
        )
        if value is None:
            raise input_error(
                "E_CONSTRAINT_PARAM",
                "min_rooms requires count",
                constraint_index=index,
                constraint_id=constraint_id,
            )
        params["count"] = _int_value(
            value, code="E_CONSTRAINT_PARAM", field="min_rooms.count", minimum=0
        )
    elif constraint_type == "max_area":
        value = _lookup(
            raw,
            "max_area",
            "area",
            "limit",
            default=_lookup(raw, "params.max_area", "params.area", default=None),
        )
        if value is None:
            raise input_error(
                "E_CONSTRAINT_PARAM",
                "max_area requires max_area",
                constraint_index=index,
                constraint_id=constraint_id,
            )
        params["max_area"] = _int_value(
            value, code="E_CONSTRAINT_PARAM", field="max_area.max_area", minimum=1
        )
    elif constraint_type == "room_spacing":
        value = _lookup(
            raw,
            "min_spacing",
            "spacing",
            "distance",
            default=_lookup(
                raw, "params.min_spacing", "params.spacing", default=None
            ),
        )
        if value is None:
            raise input_error(
                "E_CONSTRAINT_PARAM",
                "room_spacing requires min_spacing",
                constraint_index=index,
                constraint_id=constraint_id,
            )
        params["min_spacing"] = _int_value(
            value,
            code="E_CONSTRAINT_PARAM",
            field="room_spacing.min_spacing",
            minimum=0,
        )
    elif constraint_type == "connectivity":
        # No parameters are required.
        pass
    elif constraint_type == "start_end_reachable":
        start = _lookup(
            raw,
            "start",
            "from",
            default=_lookup(raw, "params.start", "params.from", default=None),
        )
        end = _lookup(
            raw,
            "end",
            "to",
            default=_lookup(raw, "params.end", "params.to", default=None),
        )
        if start is None or end is None:
            raise input_error(
                "E_CONSTRAINT_PARAM",
                "start_end_reachable requires start and end",
                constraint_index=index,
                constraint_id=constraint_id,
            )
        params["start"] = list(_coordinate(start, field="start_end_reachable.start"))
        params["end"] = list(_coordinate(end, field="start_end_reachable.end"))
    return Constraint(index=index, id=constraint_id, type=constraint_type, params=params)


def _normalize_blocked(value: Any) -> frozenset[tuple[int, int]]:
    if value is None:
        return frozenset()
    if not isinstance(value, Iterable) or isinstance(value, (str, bytes, Mapping)):
        raise input_error("E_BLOCKED", "blocked must be an array of coordinates")
    result: set[tuple[int, int]] = set()
    for index, item in enumerate(value):
        result.add(_coordinate(item, field=f"blocked[{index}]"))
    return frozenset(result)


def _normalize_config(root: Mapping[str, Any]) -> SolverConfig:
    rollback_raw = _lookup(
        root, "rollback_limit", "config.rollback_limit", default=3
    )
    attempt_raw = _lookup(root, "attempt_limit", "config.attempt_limit", default=12)
    concurrency_raw = _lookup(
        root, "concurrency", "config.concurrency", default=1
    )
    memory_raw = _lookup(
        root,
        "memory_budget_kb",
        "memory_budget",
        "config.memory_budget_kb",
        "config.memory_budget",
        default=65536,
    )
    snapshot_raw = _lookup(
        root, "snapshot_dir", "config.snapshot_dir", default=None
    )
    output_raw = _lookup(root, "output", "output_file", "config.output", default=None)

    rollback_limit = _int_value(
        rollback_raw,
        code="E_CONFIG",
        field="rollback_limit",
        minimum=0,
        maximum=100000,
    )
    attempt_limit = _int_value(
        attempt_raw,
        code="E_CONFIG",
        field="attempt_limit",
        minimum=1,
        maximum=1000,
    )
    concurrency = _int_value(
        concurrency_raw,
        code="E_CONFIG",
        field="concurrency",
        minimum=1,
        maximum=128,
    )
    memory_budget_kb = int(
        _finite_number(
            memory_raw, code="E_CONFIG", field="memory_budget_kb", minimum=1
        )
    )
    if snapshot_raw is not None and (
        not isinstance(snapshot_raw, str) or not snapshot_raw.strip()
    ):
        raise input_error(
            "E_CONFIG", "snapshot_dir must be a non-empty path string"
        )
    if output_raw is not None and (
        not isinstance(output_raw, str) or not output_raw.strip()
    ):
        raise input_error("E_CONFIG", "output must be a non-empty path string")
    return SolverConfig(
        rollback_limit=rollback_limit,
        attempt_limit=attempt_limit,
        concurrency=concurrency,
        memory_budget_kb=memory_budget_kb,
        snapshot_dir=snapshot_raw.strip() if isinstance(snapshot_raw, str) else None,
        output=output_raw.strip() if isinstance(output_raw, str) else None,
    )


def parse_spec(payload: Any) -> LevelSpec:
    """Validate and normalize an already-decoded JSON object.

    No filesystem writes happen in this function.
    """

    if not isinstance(payload, Mapping):
        raise input_error("E_SPEC", "top-level JSON value must be an object")

    width_raw = _lookup(payload, "width", "grid.width", default=None)
    height_raw = _lookup(payload, "height", "grid.height", default=None)
    if width_raw is None:
        raise input_error("E_WIDTH", "width is required")
    if height_raw is None:
        raise input_error("E_HEIGHT", "height is required")
    width = _int_value(
        width_raw, code="E_WIDTH", field="width", minimum=1, maximum=1_000_000
    )
    height = _int_value(
        height_raw, code="E_HEIGHT", field="height", minimum=1, maximum=1_000_000
    )
    if width * height > 100_000_000:
        raise input_error(
            "E_CONFIG",
            "grid is too large for the local engine",
            width=width,
            height=height,
            maximum_cells=100_000_000,
        )

    blocked_raw = _lookup(
        payload, "blocked", "unavailable", "grid.blocked", default=[]
    )
    blocked = _normalize_blocked(blocked_raw)
    for x, y in blocked:
        if x < 0 or x >= width or y < 0 or y >= height:
            raise input_error(
                "E_BLOCKED",
                "blocked coordinate is outside the grid",
                coordinate=[x, y],
                width=width,
                height=height,
            )

    if "constraints" not in payload:
        raise input_error("E_CONSTRAINT_PARAM", "constraints is required")
    raw_constraints = payload.get("constraints")
    if not isinstance(raw_constraints, list):
        raise input_error("E_CONSTRAINT_PARAM", "constraints must be an array")
    constraints = tuple(
        _normalize_constraint(raw, index)
        for index, raw in enumerate(raw_constraints)
    )
    seen_ids: set[str] = set()
    for constraint in constraints:
        if constraint.id in seen_ids:
            raise input_error(
                "E_CONSTRAINT_PARAM",
                "constraint ids must be unique",
                constraint_id=constraint.id,
            )
        seen_ids.add(constraint.id)

    seed_raw = payload.get("seed")
    if seed_raw is None:
        raise input_error("E_SEED", "seed is required")
    seed = _int_value(seed_raw, code="E_SEED", field="seed")

    available_count = width * height - len(blocked)
    for constraint in constraints:
        if constraint.type == "min_rooms":
            count = int(constraint.get("count"))
            if count > available_count:
                raise input_error(
                    "E_CONSTRAINT_PARAM",
                    "min_rooms count exceeds the number of available cells",
                    constraint_id=constraint.id,
                    count=count,
                    available_count=available_count,
                )
        elif constraint.type == "start_end_reachable":
            for label in ("start", "end"):
                x, y = constraint.get(label)
                if not (0 <= x < width and 0 <= y < height):
                    raise input_error(
                        "E_CONSTRAINT_PARAM",
                        f"{label} coordinate is outside the grid",
                        constraint_id=constraint.id,
                        coordinate=[x, y],
                    )
                if (x, y) in blocked:
                    raise input_error(
                        "E_CONSTRAINT_PARAM",
                        f"{label} coordinate is blocked",
                        constraint_id=constraint.id,
                        coordinate=[x, y],
                    )

    config = _normalize_config(payload)
    return LevelSpec(
        width=width,
        height=height,
        blocked=blocked,
        seed=seed,
        constraints=constraints,
        config=config,
    )


def load_spec(path: str | Path) -> LevelSpec:
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except UnicodeError as exc:
        raise SolverError("E_JSON", f"specification is not valid UTF-8: {exc}") from exc
    except OSError as exc:
        raise SolverError("E_IO", f"cannot read specification: {exc}") from exc
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SolverError(
            "E_JSON",
            "specification is not valid JSON",
            {"line": exc.lineno, "column": exc.colno},
        ) from exc
    return parse_spec(payload)
