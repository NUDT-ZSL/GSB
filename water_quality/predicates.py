"""Predicate parsing, SQL compilation and explicit Kleene three-valued evaluation."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

from .db import canonical_json


class PredicateError(ValueError):
    pass


TRUTH_UNKNOWN = None


FIELD_MAP: dict[str, dict[str, str]] = {
    "reading": {
        "reading_id": "r.reading_id",
        "point_id": "r.point_id",
        "sampled_at": "r.sampled_at",
        "residual_chlorine": "r.residual_chlorine",
        "chlorine": "r.residual_chlorine",
        "turbidity": "r.turbidity",
        "ph": "r.ph",
        "water_temp": "r.water_temp",
    },
    "point": {
        "point_id": "p.point_id",
        "pipe_id": "p.pipe_id",
        "location": "p.location",
        "metric_types": "p.metric_types",
        "partition_id": "p.partition_id",
    },
    "pipe": {
        "row_id": "pi.row_id",
        "pipe_id": "pi.pipe_id",
        "upstream_node": "pi.upstream_node",
        "downstream_node": "pi.downstream_node",
        "region": "pi.region",
        "diameter_mm": "pi.diameter_mm",
        "material": "pi.material",
        "partition_id": "pi.partition_id",
    },
}


SOURCE_ALIASES = {
    "reading": "r",
    "point": "p",
    "pipe": "pi",
}


OPERATORS = {
    "<",
    "<=",
    ">",
    ">=",
    "=",
    "==",
    "!=",
    "<>",
    "IN",
    "NOT IN",
    "IS NULL",
    "IS NOT NULL",
}


def normalize_operator(operator: str) -> str:
    op = str(operator).strip().upper()
    op = {"==": "=", "<>": "!="}.get(op, op)
    if op not in OPERATORS:
        raise PredicateError(f"unsupported predicate operator: {operator!r}")
    return op


def normalize_field(field_name: str) -> str:
    return field_name.strip().lower()


def sql_literal(value: Any) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, float) and not math.isfinite(value):
        raise PredicateError("threshold values must be finite")
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, str):
        return "'" + value.replace("'", "''") + "'"
    raise PredicateError(f"unsupported SQL literal type: {type(value).__name__}")


def threshold_text(value: Any) -> str:
    if isinstance(value, (list, tuple)):
        return "(" + ", ".join(threshold_text(item) for item in value) + ")"
    return sql_literal(value)


@dataclass(frozen=True)
class Predicate:
    predicate_id: str
    source: str
    field: str
    operator: str
    value: Any = None

    @property
    def column(self) -> str:
        try:
            return FIELD_MAP[self.source][self.field]
        except KeyError as exc:
            raise PredicateError(
                f"unsupported {self.source} predicate field: {self.field!r}"
            ) from exc

    @property
    def display_value(self) -> str:
        if self.operator in {"IS NULL", "IS NOT NULL"}:
            return ""
        return threshold_text(self.value)

    def sql(self) -> str:
        column = self.column
        if self.operator == "IS NULL":
            return f"({column} IS NULL)"
        if self.operator == "IS NOT NULL":
            return f"({column} IS NOT NULL)"
        if self.operator == "IN":
            values = self._in_values()
            placeholders = ", ".join(sql_literal(item) for item in values)
            if not values:
                return "(0 = 1)"
            return f"({column} IS NOT NULL AND {column} IN ({placeholders}))"
        if self.operator == "NOT IN":
            values = self._in_values()
            placeholders = ", ".join(sql_literal(item) for item in values)
            if not values:
                return "(1 = 1)"
            return f"({column} IS NOT NULL AND {column} NOT IN ({placeholders}))"
        # Explicit IS NOT NULL makes the "UNKNOWN is not TRUE" policy visible in
        # every generated SQL expression, rather than relying on an implicit
        # WHERE-clause coercion.
        return f"({column} IS NOT NULL AND {column} {self.operator} {sql_literal(self.value)})"

    def text(self) -> str:
        if self.operator in {"IS NULL", "IS NOT NULL"}:
            return f"({self.column} {self.operator})"
        return f"({self.column} {self.operator} {self.display_value})".replace(
            f"{self.operator} ({self.display_value})", f"{self.operator} {self.display_value}"
        )

    def parameters(self) -> tuple[Any, ...]:
        """The generated SQL embeds validated literals, so no bound params remain."""

        return ()

    def _in_values(self) -> tuple[Any, ...]:
        if not isinstance(self.value, (list, tuple)):
            raise PredicateError(f"{self.operator} requires a list value")
        if any(item is None for item in self.value):
            raise PredicateError(f"{self.operator} does not accept NULL list members")
        return tuple(self.value)

    def evaluate(self, row: Mapping[str, Any]) -> tuple[bool | None, bool]:
        """Return (truth value, null_caused_unknown)."""

        value = row.get(self.field)
        if self.operator == "IS NULL":
            return value is None, False
        if self.operator == "IS NOT NULL":
            return value is not None, False
        if value is None:
            return TRUTH_UNKNOWN, True
        if self.operator == "IN":
            return value in self._in_values(), False
        if self.operator == "NOT IN":
            return value not in self._in_values(), False
        if self.operator == "=":
            return value == self.value, False
        if self.operator == "!=":
            return value != self.value, False
        if self.operator == "<":
            return value < self.value, False
        if self.operator == "<=":
            return value <= self.value, False
        if self.operator == ">":
            return value > self.value, False
        if self.operator == ">=":
            return value >= self.value, False
        raise PredicateError(f"cannot evaluate operator {self.operator}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "predicate_id": self.predicate_id,
            "source": self.source,
            "field": self.field,
            "operator": self.operator,
            "value": self.value,
            "sql": self.sql(),
        }


@dataclass
class PredicateGroup:
    predicates: tuple[Predicate, ...] = ()

    def sql(self) -> str:
        if not self.predicates:
            return "(1 = 1)"
        return "(" + " AND ".join(predicate.sql() for predicate in self.predicates) + ")"

    def text(self) -> str:
        if not self.predicates:
            return "(1 = 1)"
        return "(" + " AND ".join(predicate.text() for predicate in self.predicates) + ")"

    def evaluate(self, row: Mapping[str, Any]) -> tuple[bool | None, list[Predicate]]:
        results: list[bool | None] = []
        null_unknowns: list[Predicate] = []
        for predicate in self.predicates:
            result, null_caused = predicate.evaluate(row)
            results.append(result)
            if null_caused and result is TRUTH_UNKNOWN:
                null_unknowns.append(predicate)
        return kleene_and(results), null_unknowns


@dataclass
class PredicateSet:
    """Disjunction of conjunction groups (DNF)."""

    groups: tuple[PredicateGroup, ...] = (PredicateGroup(),)

    def sql(self) -> str:
        if not self.groups:
            return "(0 = 1)"
        return "(" + " OR ".join(group.sql() for group in self.groups) + ")"

    def text(self) -> str:
        if not self.groups:
            return "(0 = 1)"
        return "(" + " OR ".join(group.text() for group in self.groups) + ")"

    def evaluate(
        self, row: Mapping[str, Any]
    ) -> tuple[bool | None, list[tuple[Predicate, PredicateGroup]]]:
        group_results: list[bool | None] = []
        null_unknowns: list[tuple[Predicate, PredicateGroup]] = []
        for group in self.groups:
            result, group_unknowns = group.evaluate(row)
            group_results.append(result)
            for predicate in group_unknowns:
                null_unknowns.append((predicate, group))
        return kleene_or(group_results), null_unknowns

    @property
    def predicates(self) -> tuple[Predicate, ...]:
        return tuple(predicate for group in self.groups for predicate in group.predicates)


def kleene_and(values: Iterable[bool | None]) -> bool | None:
    saw_unknown = False
    for value in values:
        if value is False:
            return False
        if value is None:
            saw_unknown = True
    return None if saw_unknown else True


def kleene_or(values: Iterable[bool | None]) -> bool | None:
    saw_unknown = False
    for value in values:
        if value is True:
            return True
        if value is None:
            saw_unknown = True
    return None if saw_unknown else False


def truth_label(value: bool | None) -> str:
    if value is True:
        return "TRUE"
    if value is False:
        return "FALSE"
    return "UNKNOWN"


def _parse_predicate(
    item: Mapping[str, Any],
    *,
    source: str,
    fallback_id: str,
) -> Predicate:
    if not isinstance(item, Mapping):
        raise PredicateError(f"predicate must be an object, got {type(item).__name__}")
    field_name = normalize_field(str(item.get("field", "")))
    if field_name not in FIELD_MAP[source]:
        raise PredicateError(f"unsupported {source} predicate field: {field_name!r}")
    operator = normalize_operator(str(item.get("operator", item.get("op", ""))))
    value = item.get("value")
    if operator not in {"IS NULL", "IS NOT NULL"} and "value" not in item:
        raise PredicateError(f"predicate {fallback_id} is missing value")
    predicate_id = str(item.get("predicate_id", fallback_id))
    return Predicate(
        predicate_id=predicate_id,
        source=source,
        field=field_name,
        operator=operator,
        value=value,
    )


def parse_predicate_list(
    items: Sequence[Mapping[str, Any]] | None,
    *,
    source: str,
    query_id: str,
) -> tuple[Predicate, ...]:
    result: list[Predicate] = []
    for index, item in enumerate(items or (), start=1):
        result.append(
            _parse_predicate(
                item,
                source=source,
                fallback_id=f"{query_id}.{source.upper()}.{index:02d}",
            )
        )
    return tuple(result)


def parse_reading_groups(
    raw: Mapping[str, Any],
    *,
    query_id: str,
) -> PredicateSet:
    groups_raw = raw.get("reading_predicate_groups")
    if groups_raw is None and "reading_predicates" in raw:
        groups_raw = [raw.get("reading_predicates") or []]
    if groups_raw is None:
        return PredicateSet((PredicateGroup(),))
    if not isinstance(groups_raw, list):
        raise PredicateError("reading_predicate_groups must be a list of lists")
    groups: list[PredicateGroup] = []
    for group_index, group in enumerate(groups_raw, start=1):
        if group is None:
            group = []
        if not isinstance(group, list):
            raise PredicateError("each reading predicate group must be a list")
        predicates = parse_predicate_list(
            group,
            source="reading",
            query_id=f"{query_id}.G{group_index:02d}",
        )
        groups.append(PredicateGroup(predicates))
    if not groups:
        groups = [PredicateGroup()]
    return PredicateSet(tuple(groups))


@dataclass(frozen=True)
class QuerySpec:
    query_id: str
    description: str
    join_type: str
    pipe_predicates: tuple[Predicate, ...] = ()
    point_predicates: tuple[Predicate, ...] = ()
    reading_predicates: PredicateSet = field(default_factory=PredicateSet)
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "QuerySpec":
        query_id = str(data.get("query_id", "")).strip()
        if not query_id:
            raise PredicateError("query_id is required")
        join_type = str(data.get("join_type", "inner")).strip().lower()
        if join_type not in {"inner", "left", "right", "full"}:
            raise PredicateError(f"unsupported join_type: {join_type}")
        return cls(
            query_id=query_id,
            description=str(data.get("description", "")),
            join_type=join_type,
            pipe_predicates=parse_predicate_list(
                data.get("pipe_predicates"), source="pipe", query_id=query_id
            ),
            point_predicates=parse_predicate_list(
                data.get("point_predicates"), source="point", query_id=query_id
            ),
            reading_predicates=parse_reading_groups(data, query_id=query_id),
            raw=dict(data),
        )

    @classmethod
    def from_json(cls, text: str) -> "QuerySpec":
        return cls.from_dict(json.loads(text))

    def to_dict(self) -> dict[str, Any]:
        return {
            "query_id": self.query_id,
            "description": self.description,
            "join_type": self.join_type,
            "pipe_predicates": [
                predicate.to_dict() for predicate in self.pipe_predicates
            ],
            "point_predicates": [
                predicate.to_dict() for predicate in self.point_predicates
            ],
            "reading_predicate_sql": self.reading_predicates.sql(),
            "reading_predicate_text": self.reading_predicates.text(),
        }


def compile_side_predicate(predicates: Sequence[Predicate]) -> PredicateSet:
    return PredicateSet((PredicateGroup(tuple(predicates)),))


def predicate_signature(query: QuerySpec) -> str:
    payload = {
        "join_type": query.join_type,
        "pipe": [predicate.to_dict() for predicate in query.pipe_predicates],
        "point": [predicate.to_dict() for predicate in query.point_predicates],
        "reading": {
            "sql": query.reading_predicates.sql(),
            "groups": [
                [predicate.to_dict() for predicate in group.predicates]
                for group in query.reading_predicates.groups
            ],
        },
    }
    return canonical_json(payload)

