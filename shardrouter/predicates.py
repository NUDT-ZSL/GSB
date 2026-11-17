"""Deterministic parser and normalizer for the supported logical predicate DSL."""

from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass
from typing import Any, Iterable

from .errors import RouterError, sql_error
from .models import Bound, ColumnConstraint, PredicateInfo, TableConfig, canonical_value_key


@dataclass(frozen=True)
class _Token:
    kind: str
    value: str
    pos: int


@dataclass(frozen=True)
class _Term:
    column: str
    operator: str
    values: tuple[Any, ...]
    pos: int


_NUMBER = re.compile(
    r"[-+]?(?:(?:\d+\.\d*|\.\d+|\d+)(?:[eE][-+]?\d+)?)"
)
_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_KEYWORDS = {"and", "between", "false", "in", "not", "null", "or", "true", "where"}


def _lex(sql: str) -> list[_Token]:
    tokens: list[_Token] = []
    index = 0
    length = len(sql)
    while index < length:
        char = sql[index]
        if char.isspace():
            index += 1
            continue
        if char in "(),":
            tokens.append(_Token(char, char, index))
            index += 1
            continue
        if char == "'":
            start = index
            index += 1
            parts: list[str] = []
            closed = False
            while index < length:
                if sql[index] == "'":
                    if index + 1 < length and sql[index + 1] == "'":
                        parts.append("'")
                        index += 2
                        continue
                    index += 1
                    closed = True
                    break
                parts.append(sql[index])
                index += 1
            if not closed:
                raise sql_error("unterminated string literal", sql, start)
            tokens.append(_Token("STRING", "".join(parts), start))
            continue
        two = sql[index : index + 2]
        if two in ("<=", ">=", "!=", "<>"):
            normalized = "!=" if two == "<>" else two
            tokens.append(_Token("OP", normalized, index))
            index += 2
            continue
        if char in "=<>":
            if char == "=" and index + 1 < length and sql[index + 1] == "=":
                tokens.append(_Token("OP", "=", index))
                index += 2
            else:
                tokens.append(_Token("OP", char, index))
                index += 1
            continue
        number_match = _NUMBER.match(sql, index)
        if number_match is not None:
            raw = number_match.group(0)
            if "." in raw or "e" in raw.lower():
                value: Any = float(raw)
            else:
                value = int(raw, 10)
            tokens.append(_Token("NUMBER", repr(value) if isinstance(value, float) else str(value), index))
            index = number_match.end()
            continue
        ident_match = _IDENT.match(sql, index)
        if ident_match is not None:
            raw = ident_match.group(0)
            if raw.casefold() in _KEYWORDS:
                tokens.append(_Token("KEYWORD", raw.upper(), index))
            else:
                tokens.append(_Token("IDENT", raw, index))
            index = ident_match.end()
            continue
        raise sql_error(f"unexpected character {char!r}", sql, index)
    tokens.append(_Token("EOF", "", length))
    return tokens


class _Parser:
    def __init__(self, sql: str, column_names: Iterable[str]) -> None:
        self.sql = sql
        self.tokens = _lex(sql)
        self.index = 0
        self.columns = {name.casefold(): name for name in column_names}

    @property
    def current(self) -> _Token:
        return self.tokens[self.index]

    def advance(self) -> _Token:
        token = self.current
        self.index += 1
        return token

    def expect(self, kind: str, message: str) -> _Token:
        if self.current.kind != kind:
            raise sql_error(message, self.sql, self.current.pos)
        return self.advance()

    def parse(self) -> tuple[_Term, ...]:
        if self.current.kind == "KEYWORD" and self.current.value == "WHERE":
            self.advance()
        if self.current.kind == "EOF":
            raise sql_error("predicate is empty", self.sql, 0)
        terms = self.parse_and()
        if self.current.kind == "KEYWORD" and self.current.value == "OR":
            raise sql_error("OR is not supported; use AND only", self.sql, self.current.pos)
        if self.current.kind != "EOF":
            raise sql_error("unexpected token after predicate", self.sql, self.current.pos)
        return terms

    def parse_and(self) -> tuple[_Term, ...]:
        terms = list(self.parse_primary())
        while self.current.kind == "KEYWORD" and self.current.value == "AND":
            self.advance()
            terms.extend(self.parse_primary())
        return tuple(terms)

    def parse_primary(self) -> tuple[_Term, ...]:
        if self.current.kind == "(":
            self.advance()
            terms = self.parse_and()
            self.expect(")", "expected ')' to close predicate group")
            return terms
        return (self.parse_term(),)

    def parse_term(self) -> _Term:
        token = self.current
        if token.kind != "IDENT":
            raise sql_error("expected a column name", self.sql, token.pos)
        self.advance()
        canonical_column = self.columns.get(token.value.casefold())
        if canonical_column is None:
            raise sql_error(f"unknown column {token.value!r}", self.sql, token.pos)
        column = canonical_column.casefold()

        if self.current.kind == "KEYWORD" and self.current.value == "IN":
            self.advance()
            self.expect("(", "expected '(' after IN")
            values: list[Any] = []
            if self.current.kind == ")":
                raise sql_error("IN list must not be empty", self.sql, self.current.pos)
            while True:
                values.append(self.parse_value())
                if self.current.kind == ",":
                    self.advance()
                    continue
                break
            self.expect(")", "expected ')' after IN list")
            return _Term(column, "IN", tuple(values), token.pos)

        if self.current.kind == "KEYWORD" and self.current.value == "BETWEEN":
            self.advance()
            lower = self.parse_value()
            if not (self.current.kind == "KEYWORD" and self.current.value == "AND"):
                raise sql_error("BETWEEN requires AND", self.sql, self.current.pos)
            self.advance()
            upper = self.parse_value()
            return _Term(column, "BETWEEN", (lower, upper), token.pos)

        if self.current.kind == "KEYWORD" and self.current.value == "NOT":
            raise sql_error("NOT is not supported", self.sql, self.current.pos)
        if self.current.kind != "OP":
            raise sql_error("expected a comparison operator", self.sql, self.current.pos)
        operator = self.advance().value
        value = self.parse_value()
        return _Term(column, operator, (value,), token.pos)

    def parse_value(self) -> Any:
        token = self.current
        if token.kind == "NUMBER":
            self.advance()
            value = float(token.value) if any(ch in token.value.lower() for ch in ".e") else int(token.value)
            if isinstance(value, float) and not math.isfinite(value):
                raise sql_error("numeric literal must be finite", self.sql, token.pos)
            return value
        if token.kind == "STRING":
            self.advance()
            return token.value
        if token.kind == "KEYWORD":
            if token.value == "NULL":
                self.advance()
                return None
            if token.value == "TRUE":
                self.advance()
                return True
            if token.value == "FALSE":
                self.advance()
                return False
        raise sql_error("expected a literal value", self.sql, token.pos)


def _render_number(value: int | float) -> str:
    if isinstance(value, float):
        return repr(value)
    return str(value)


def render_value(value: Any) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (int, float)):
        return _render_number(value)
    if isinstance(value, str):
        return "'" + value.replace("'", "''") + "'"
    raise TypeError(f"unsupported literal type: {type(value).__name__}")


def render_term(term: _Term) -> str:
    column = term.column
    if term.operator == "IN":
        unique: dict[tuple[str, str], Any] = {}
        for value in term.values:
            unique[canonical_value_key(value)] = value
        ordered = sorted(unique.values(), key=canonical_value_key)
        if len(ordered) == 1:
            return f"{column} = {render_value(ordered[0])}"
        return f"{column} IN ({', '.join(render_value(value) for value in ordered)})"
    if term.operator == "BETWEEN":
        return f"{column} BETWEEN {render_value(term.values[0])} AND {render_value(term.values[1])}"
    operator = "<>" if term.operator == "!=" else term.operator
    return f"{column} {operator} {render_value(term.values[0])}"


def _compare_values(left: Any, right: Any) -> int | None:
    if isinstance(left, bool) or isinstance(right, bool):
        if isinstance(left, bool) and isinstance(right, bool):
            return (left > right) - (left < right)
        return None
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return (left > right) - (left < right)
    if isinstance(left, str) and isinstance(right, str):
        return (left > right) - (left < right)
    if left is None and right is None:
        return 0
    return None


def _tighten_lower(current: Bound | None, candidate: Bound) -> Bound | None:
    if current is None:
        return candidate
    comparison = _compare_values(candidate.value, current.value)
    if comparison is None:
        return current
    if comparison > 0:
        return candidate
    if comparison < 0:
        return current
    return Bound(candidate.value, current.inclusive and candidate.inclusive)


def _tighten_upper(current: Bound | None, candidate: Bound) -> Bound | None:
    if current is None:
        return candidate
    comparison = _compare_values(candidate.value, current.value)
    if comparison is None:
        return current
    if comparison < 0:
        return candidate
    if comparison > 0:
        return current
    return Bound(candidate.value, current.inclusive and candidate.inclusive)


def _bound_satisfied(value: Any, lower: Bound | None, upper: Bound | None) -> bool:
    if lower is not None:
        comparison = _compare_values(value, lower.value)
        if comparison is None:
            return False
        if comparison < 0 or (comparison == 0 and not lower.inclusive):
            return False
    if upper is not None:
        comparison = _compare_values(value, upper.value)
        if comparison is None:
            return False
        if comparison > 0 or (comparison == 0 and not upper.inclusive):
            return False
    return True


def _constraints_from_terms(terms: Iterable[_Term]) -> dict[str, ColumnConstraint]:
    point_sets: dict[str, list[tuple[Any, ...]]] = {}
    lower_bounds: dict[str, Bound] = {}
    upper_bounds: dict[str, Bound] = {}
    positions: dict[str, int] = {}

    for term in terms:
        positions.setdefault(term.column, term.pos)
        if term.operator == "=":
            point_sets.setdefault(term.column, []).append((term.values[0],))
        elif term.operator == "IN":
            point_sets.setdefault(term.column, []).append(tuple(term.values))
        elif term.operator == "BETWEEN":
            lower_bounds[term.column] = _tighten_lower(
                lower_bounds.get(term.column), Bound(term.values[0], True)
            )  # type: ignore[assignment]
            upper_bounds[term.column] = _tighten_upper(
                upper_bounds.get(term.column), Bound(term.values[1], True)
            )  # type: ignore[assignment]
        elif term.operator == ">":
            lower_bounds[term.column] = _tighten_lower(
                lower_bounds.get(term.column), Bound(term.values[0], False)
            )  # type: ignore[assignment]
        elif term.operator == ">=":
            lower_bounds[term.column] = _tighten_lower(
                lower_bounds.get(term.column), Bound(term.values[0], True)
            )  # type: ignore[assignment]
        elif term.operator == "<":
            upper_bounds[term.column] = _tighten_upper(
                upper_bounds.get(term.column), Bound(term.values[0], False)
            )  # type: ignore[assignment]
        elif term.operator == "<=":
            upper_bounds[term.column] = _tighten_upper(
                upper_bounds.get(term.column), Bound(term.values[0], True)
            )  # type: ignore[assignment]

    constraints: dict[str, ColumnConstraint] = {}
    columns = sorted(set(point_sets) | set(lower_bounds) | set(upper_bounds))
    for column in columns:
        points: tuple[Any, ...] = ()
        if column in point_sets:
            current: dict[tuple[str, str], Any] | None = None
            for values in point_sets[column]:
                candidate = {canonical_value_key(value): value for value in values}
                if current is None:
                    current = candidate
                else:
                    current = {key: current[key] for key in current.keys() & candidate.keys()}
            lower = lower_bounds.get(column)
            upper = upper_bounds.get(column)
            points = tuple(
                sorted(
                    (
                        value
                        for value in (current or {}).values()
                        if _bound_satisfied(value, lower, upper)
                    ),
                    key=canonical_value_key,
                )
            )
        constraints[column] = ColumnConstraint(
            points=points,
            lower=lower_bounds.get(column),
            upper=upper_bounds.get(column),
        )
    return constraints


def normalize_predicate(table: TableConfig, sql: str) -> PredicateInfo:
    """Parse, validate, and canonicalize a predicate for one logical table."""

    if not isinstance(sql, str) or not sql.strip():
        raise sql_error("predicate is empty", sql if isinstance(sql, str) else "", 0)
    parser = _Parser(sql, table.column_names)
    terms = parser.parse()

    rendered: dict[str, tuple[str, _Term]] = {}
    for term in terms:
        text = render_term(term)
        rendered[text] = (text, term)
    ordered_terms = tuple(value[1] for _, value in sorted(rendered.items(), key=lambda item: item[0]))
    normalized = " AND ".join(render_term(term) for term in ordered_terms)
    routing_id = hashlib.sha256(f"{table.name}\x1f{normalized}".encode("utf-8")).hexdigest()

    constraints = _constraints_from_terms(ordered_terms)
    shard_keys_normalized = tuple(key.casefold() for key in table.shard_keys)
    missing = tuple(key for key in shard_keys_normalized if key not in constraints)
    positions: dict[str, int] = {}
    for term in ordered_terms:
        positions.setdefault(term.column, term.pos)
    referenced = tuple(sorted(constraints))

    return PredicateInfo(
        table_name=table.name,
        original=sql,
        normalized=normalized,
        routing_id=routing_id,
        referenced_columns=referenced,
        constraints=constraints,
        has_complete_shard_key=all(
            key.casefold() in constraints for key in table.shard_keys
        ),
        missing_shard_keys=missing,
        column_positions=positions,
    )


def predicate_error_offset(error: RouterError) -> int | None:
    if error.position is None:
        return None
    return int(error.position.get("offset", 0))
