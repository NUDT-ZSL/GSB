"""Query parsing, canonical identifiers and execution-plan compilation.

Two input shapes are accepted:

1. Structured JSON objects (``kind: "aggregate" | "window"``).
2. A constrained SQL dialect (single SELECT, no subqueries) which is
   normalized into the same internal representation.

The same semantic query always produces the same canonical text and the same
SHA-256 structural digest regardless of whitespace, key order or identifier
case.  Unsupported operators or subqueries fail with E_EXPR.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

from .errors import BusinessError, E_EXPR
from .schema import NUMERIC_TYPES, Table

AGG_FUNCS = ("COUNT", "SUM", "MIN", "MAX", "MEAN")
WINDOW_FUNCS = ("SUM", "ROW_NUMBER")


def _expr_fail(message: str, **context) -> BusinessError:
    return BusinessError(E_EXPR, message, context)


@dataclass(frozen=True)
class AggregatePlan:
    query_id: str
    table: str
    group_by: tuple[str, ...]
    func: str
    arg: str | None  # column name or None for COUNT(*)
    alias: str
    canonical: str
    digest: str


@dataclass(frozen=True)
class WindowPlan:
    query_id: str
    table: str
    partition_by: tuple[str, ...]
    order_by: str
    func: str
    arg: str | None
    lower: int  # inclusive lower offset, e.g. -2
    upper: int  # inclusive upper offset, e.g. 0
    max_rows: int
    alias: str
    canonical: str
    digest: str


@dataclass
class ExecutionPlan:
    aggregates: list[AggregatePlan] = field(default_factory=list)
    windows: list[WindowPlan] = field(default_factory=list)

    def all_plans(self):
        return self.aggregates + self.windows


def _fold_identifier(identifier: str) -> str:
    folded = identifier.strip().lower()
    if not folded:
        raise _expr_fail("empty identifier")
    return folded


def _canonical_aggregate(qid: str, table: str, group_by: tuple[str, ...],
                         func: str, arg: str | None, alias: str) -> tuple[str, str]:
    arg_text = "*" if arg is None else arg
    group_text = ", ".join(group_by) if group_by else ""
    canonical = (
        f"AGG {func}({arg_text}) ON {table} "
        f"GROUP BY [{group_text}] AS {alias}"
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return canonical, digest


def _canonical_window(qid: str, table: str, partition_by: tuple[str, ...],
                      order_by: str, func: str, arg: str | None,
                      lower: int, upper: int, max_rows: int,
                      alias: str) -> tuple[str, str]:
    arg_text = "*" if arg is None else arg
    part_text = ", ".join(partition_by) if partition_by else ""
    canonical = (
        f"WINDOW {func}({arg_text}) ON {table} "
        f"PARTITION BY [{part_text}] ORDER BY {order_by} "
        f"RANGE BETWEEN {lower} AND {upper} MAX_ROWS {max_rows} AS {alias}"
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return canonical, digest


def _require_column(table: Table, column: str, raw_id: str) -> str:
    names = {col.name for col in table.columns}
    if column not in names:
        raise _expr_fail(
            "query references an unknown column",
            query=raw_id,
            table=table.name,
            field=column,
        )
    return column


def _build_aggregate(raw: dict, tables: dict[str, Table],
                     seen_ids: set[str]) -> AggregatePlan:
    qid = raw.get("id")
    if not isinstance(qid, str) or not qid.strip():
        raise _expr_fail("aggregate query requires a string id", field="id")
    if qid in seen_ids:
        raise _expr_fail("duplicate query id", query=qid, field="id")
    seen_ids.add(qid)

    table_name = raw.get("table")
    if table_name not in tables:
        raise _expr_fail("query references unknown table", query=qid, table=table_name)
    table = tables[table_name]

    raw_group = raw.get("group_by", [])
    if not isinstance(raw_group, list) or not all(isinstance(c, str) for c in raw_group):
        raise _expr_fail("group_by must be a list of column names",
                         query=qid, field="group_by")
    group_by = tuple(sorted(_fold_identifier(c) for c in raw_group))
    for column in group_by:
        _require_column(table, column, qid)

    func = str(raw.get("func", "")).strip().upper()
    if func not in AGG_FUNCS:
        raise _expr_fail("unsupported aggregate function", query=qid,
                         field="func", actual=func, supported=list(AGG_FUNCS))

    raw_arg = raw.get("arg")
    if func == "COUNT":
        if raw_arg in (None, "*"):
            arg = None
        else:
            arg = _fold_identifier(raw_arg)
            _require_column(table, arg, qid)
    else:
        if not isinstance(raw_arg, str) or not raw_arg.strip():
            raise _expr_fail(f"{func} requires a numeric argument column",
                             query=qid, field="arg")
        arg = _fold_identifier(raw_arg)
        _require_column(table, arg, qid)
        if table.column_type(arg) not in NUMERIC_TYPES:
            raise _expr_fail(
                f"{func} argument must be numeric",
                query=qid, field=arg, actual_type=table.column_type(arg),
            )

    alias = _fold_identifier(raw.get("alias", f"{func.lower()}_{arg or 'star'}"))
    canonical, digest = _canonical_aggregate(qid, table.name, group_by, func, arg, alias)
    return AggregatePlan(qid, table.name, group_by, func, arg, alias, canonical, digest)


def _build_window(raw: dict, tables: dict[str, Table],
                  seen_ids: set[str]) -> WindowPlan:
    qid = raw.get("id")
    if not isinstance(qid, str) or not qid.strip():
        raise _expr_fail("window query requires a string id", field="id")
    if qid in seen_ids:
        raise _expr_fail("duplicate query id", query=qid, field="id")
    seen_ids.add(qid)

    table_name = raw.get("table")
    if table_name not in tables:
        raise _expr_fail("query references unknown table", query=qid, table=table_name)
    table = tables[table_name]

    raw_part = raw.get("partition_by", [])
    if not isinstance(raw_part, list) or not all(isinstance(c, str) for c in raw_part):
        raise _expr_fail("partition_by must be a list of column names",
                         query=qid, field="partition_by")
    partition_by = tuple(sorted(_fold_identifier(c) for c in raw_part))
    for column in partition_by:
        _require_column(table, column, qid)

    order_by = _fold_identifier(raw.get("order_by", table.timestamp_column))
    _require_column(table, order_by, qid)
    if table.column_type(order_by) != "timestamp":
        raise _expr_fail("window order_by must reference a timestamp column",
                         query=qid, field=order_by,
                         actual_type=table.column_type(order_by))

    func = str(raw.get("func", "")).strip().upper()
    if func not in WINDOW_FUNCS:
        raise _expr_fail("unsupported window function", query=qid,
                         field="func", actual=func, supported=list(WINDOW_FUNCS))
    raw_arg = raw.get("arg")
    if func == "ROW_NUMBER":
        arg = None
        if raw_arg not in (None, "*"):
            raise _expr_fail("ROW_NUMBER takes no argument", query=qid, field="arg")
    else:
        if not isinstance(raw_arg, str) or not raw_arg.strip():
            raise _expr_fail("window SUM requires a numeric argument column",
                             query=qid, field="arg")
        arg = _fold_identifier(raw_arg)
        _require_column(table, arg, qid)
        if table.column_type(arg) not in NUMERIC_TYPES:
            raise _expr_fail("window SUM argument must be numeric", query=qid,
                             field=arg, actual_type=table.column_type(arg))

    frame = raw.get("frame", {})
    if not isinstance(frame, dict):
        raise _expr_fail("frame must be an object", query=qid, field="frame")
    lower, upper = _parse_bounds(frame, qid)
    max_rows = frame.get("max_rows", 1000)
    if not isinstance(max_rows, int) or isinstance(max_rows, bool) or max_rows <= 0:
        raise _expr_fail("frame.max_rows must be a positive integer",
                         query=qid, field="frame.max_rows", actual=max_rows)
    alias = _fold_identifier(raw.get("alias", f"{func.lower()}_{arg or 'rn'}"))
    canonical, digest = _canonical_window(
        qid, table.name, partition_by, order_by, func, arg,
        lower, upper, max_rows, alias)
    return WindowPlan(qid, table.name, partition_by, order_by, func, arg,
                      lower, upper, max_rows, alias, canonical, digest)


def _parse_bounds(frame: dict, qid: str) -> tuple[int, int]:
    lower_raw = frame.get("lower", 0)
    upper_raw = frame.get("upper", 0)
    if not isinstance(lower_raw, int) or isinstance(lower_raw, bool):
        raise _expr_fail("frame lower bound must be an integer offset",
                         query=qid, field="frame.lower", actual=lower_raw)
    if not isinstance(upper_raw, int) or isinstance(upper_raw, bool):
        raise _expr_fail("frame upper bound must be an integer offset",
                         query=qid, field="frame.upper", actual=upper_raw)
    lower, upper = int(lower_raw), int(upper_raw)
    if lower > 0 or upper < 0:
        raise _expr_fail("frame must cover the current row "
                         "(lower <= 0 <= upper)", query=qid,
                         field="frame", lower=lower, upper=upper)
    if upper < lower:
        raise _expr_fail("frame upper bound precedes lower bound",
                         query=qid, field="frame", lower=lower, upper=upper)
    return lower, upper


# --------------------------------------------------------------------------- #
# Constrained SQL dialect
# --------------------------------------------------------------------------- #

_SQL_TOKEN_RE = None


def _tokenize_sql(sql: str) -> list[tuple[str, str]]:
    import re
    global _SQL_TOKEN_RE
    if _SQL_TOKEN_RE is None:
        _SQL_TOKEN_RE = re.compile(
            r"\s*(?:(--[^\n]*)|(\b\d+\b)|([A-Za-z_][A-Za-z0-9_]*)|([(),*=.<>!+-]))",
            re.IGNORECASE,
        )
    tokens: list[tuple[str, str]] = []
    pos = 0
    while pos < len(sql):
        match = _SQL_TOKEN_RE.match(sql, pos)
        if not match or match.end() == pos:
            raise _expr_fail("illegal SQL token near position",
                             position=pos, near=sql[pos:pos + 12])
        comment, number, ident, punct = match.groups()
        if number is not None:
            tokens.append(("NUM", number))
        elif ident is not None:
            tokens.append(("IDENT", ident))
        elif punct is not None:
            tokens.append(("PUNCT", punct))
        pos = match.end()
    return tokens


class _SqlParser:
    """Single-statement SELECT parser; every nested SELECT is rejected."""

    def __init__(self, sql: str):
        self.sql = sql
        self.tokens = _tokenize_sql(sql.rstrip().rstrip(";"))
        self.pos = 0

    def _peek(self) -> tuple[str, str] | None:
        return self.tokens[self.pos] if self.pos < len(self.tokens) else None

    def _take(self) -> tuple[str, str]:
        token = self.tokens[self.pos]
        self.pos += 1
        return token

    def _expect_ident(self, expected: str | None = None) -> str:
        token = self._peek()
        if token is None or token[0] != "IDENT":
            raise _expr_fail("expected identifier in SQL",
                             near=self._remaining())
        value = self._take()[1]
        if expected is not None and value.upper() != expected.upper():
            raise _expr_fail(f"expected SQL keyword {expected}",
                             actual=value, near=self._remaining())
        return value

    def _keyword(self, word: str) -> bool:
        token = self._peek()
        if token and token[0] == "IDENT" and token[1].upper() == word:
            self._take()
            return True
        return False

    def _remaining(self) -> str:
        return " ".join(t[1] for t in self.tokens[self.pos:self.pos + 6])

    def parse(self, index: int) -> dict:
        if not self._keyword("SELECT"):
            raise _expr_fail("SQL query must start with SELECT", index=index)
        func, arg = self._parse_select_item()
        over_partition: list[str] = []
        over_order: str | None = None
        over_frame: dict | None = None
        if self._keyword("OVER"):
            over_partition, over_order, over_frame = self._parse_over_clause()
        if not self._keyword("FROM"):
            raise _expr_fail("SQL query requires FROM", index=index)
        table = _fold_identifier(self._expect_ident())
        group_by: list[str] = []
        partition_by: list[str] = []
        order_by: str | None = None
        frame: dict | None = None
        kind = "aggregate"
        while self._peek() is not None:
            keyword = self._peek()[1].upper()
            if keyword == "GROUP":
                self._take()
                self._expect_ident("BY")
                group_by = self._parse_name_list()
            elif keyword == "OVER":
                raise _expr_fail("unexpected second OVER clause", index=index)
            elif keyword in ("WHERE", "JOIN", "HAVING", "ORDER", "LIMIT",
                             "UNION", "WITH"):
                raise _expr_fail("unsupported SQL clause",
                                 clause=keyword, index=index)
            else:
                raise _expr_fail("unexpected SQL token",
                                 near=self._remaining(), index=index)
        if kind == "window" or over_frame is not None:
            return {
                "id": f"sql_window_{index}",
                "kind": "window",
                "table": table,
                "partition_by": over_partition,
                "order_by": over_order,
                "func": func,
                "arg": arg,
                "frame": over_frame or {"lower": 0, "upper": 0, "max_rows": 1000},
            }
        return {
            "id": f"sql_aggregate_{index}",
            "kind": "aggregate",
            "table": table,
            "group_by": group_by,
            "func": func,
            "arg": arg,
        }

    def _parse_select_item(self) -> tuple[str, str | None]:
        ident = self._expect_ident()
        func = ident.upper()
        if func not in AGG_FUNCS and func not in WINDOW_FUNCS:
            raise _expr_fail("unsupported function in SELECT", actual=ident)
        if self._peek() is None or self._peek()[0] != "PUNCT" or self._peek()[1] != "(":
            raise _expr_fail("function call requires parentheses")
        self._take()
        if self._peek() and self._peek()[0] == "PUNCT" and self._peek()[1] == "*":
            self._take()
            arg = "*"
        else:
            arg = self._expect_ident()
        if self._peek() is None or self._peek()[0] != "PUNCT" or self._peek()[1] != ")":
            raise _expr_fail("unterminated function call")
        self._take()
        return func, arg

    def _parse_name_list(self) -> list[str]:
        names = [_fold_identifier(self._expect_ident())]
        while self._peek() and self._peek()[0] == "PUNCT" and self._peek()[1] == ",":
            self._take()
            names.append(_fold_identifier(self._expect_ident()))
        return names

    def _parse_over_clause(self) -> tuple[list[str], str | None, dict]:
        if not self._peek() or self._peek()[0] != "PUNCT" or self._peek()[1] != "(":
            raise _expr_fail("OVER requires a parenthesized window definition")
        self._take()
        partition_by: list[str] = []
        order_by: str | None = None
        frame: dict = {"lower": 0, "upper": 0, "max_rows": 1000}
        while self._peek() and not (self._peek()[0] == "PUNCT" and self._peek()[1] == ")"):
            keyword = self._peek()[1].upper()
            if keyword == "PARTITION":
                self._take()
                self._expect_ident("BY")
                partition_by = self._parse_name_list()
            elif keyword == "ORDER":
                self._take()
                self._expect_ident("BY")
                order_by = _fold_identifier(self._expect_ident())
            elif keyword == "RANGE":
                self._take()
                frame = self._parse_range_frame()
            elif keyword == "ROWS":
                raise _expr_fail("only RANGE frames are supported; ROWS is rejected")
            else:
                raise _expr_fail("unsupported window clause", clause=keyword)
        if self._peek() is None:
            raise _expr_fail("unterminated OVER clause")
        self._take()
        return partition_by, order_by, frame

    def _parse_range_frame(self) -> dict:
        frame = {"lower": 0, "upper": 0, "max_rows": 1000}
        if self._keyword("BETWEEN"):
            frame["lower"] = self._parse_bound("lower")
            self._expect_ident("AND")
            frame["upper"] = self._parse_bound("upper")
        else:
            offset = self._parse_bound("bound")
            frame["lower"], frame["upper"] = 0, offset
        if self._keyword("MAX_ROWS"):
            token = self._peek()
            if token is None or token[0] != "NUM":
                raise _expr_fail("MAX_ROWS requires an integer")
            frame["max_rows"] = int(self._take()[1])
        return frame

    def _parse_bound(self, label: str) -> int:
        token = self._peek()
        if token and token[0] == "IDENT" and token[1].upper() == "CURRENT":
            self._take()
            self._expect_ident("ROW")
            return 0
        sign = 1
        if self._peek() and self._peek()[0] == "-":
            self._take()
            sign = -1
        token = self._peek()
        if token is None or token[0] != "NUM":
            raise _expr_fail("frame bound must be CURRENT ROW or an integer offset")
        offset = sign * int(self._take()[1])
        direction = self._expect_ident().upper()
        if direction not in ("PRECEDING", "FOLLOWING"):
            raise _expr_fail("frame bound must end with PRECEDING/FOLLOWING",
                             actual=direction)
        if direction == "PRECEDING":
            offset = -abs(offset)
        else:
            offset = abs(offset)
        return offset


def parse_sql(sql: str, index: int) -> dict:
    if sql.count(";") > 1:
        raise _expr_fail("multiple SQL statements are not supported", index=index)
    tokens = _tokenize_sql(sql)
    nesting = 0
    for kind, value in tokens:
        if kind == "(":
            nesting += 1
        elif kind == ")":
            nesting -= 1
        elif nesting > 0 and kind == "IDENT" and value.upper() == "SELECT":
            raise _expr_fail("subqueries are not supported", index=index)
    return _SqlParser(sql).parse(index)


def load_queries(path: str, tables: dict[str, Table]) -> ExecutionPlan:
    import json

    try:
        with open(path, "r", encoding="utf-8") as handle:
            document = json.load(handle)
    except FileNotFoundError:
        raise _expr_fail("query file not found", path=path)
    except json.JSONDecodeError as exc:
        raise BusinessError(
            E_EXPR, "query file is not valid JSON",
            {"path": path, "line": exc.lineno, "column": exc.colno,
             "detail": exc.msg},
        )
    if not isinstance(document, dict) or not isinstance(document.get("queries"), list):
        raise _expr_fail("query document needs a 'queries' list", path=path)

    plan = ExecutionPlan()
    seen_ids: set[str] = set()
    for index, raw in enumerate(document["queries"]):
        if isinstance(raw, str):
            raw = parse_sql(raw, index)
        if not isinstance(raw, dict):
            raise _expr_fail("each query must be an object or SQL string",
                             index=index)
        kind = raw.get("kind")
        if kind == "aggregate":
            plan.aggregates.append(_build_aggregate(raw, tables, seen_ids))
        elif kind == "window":
            plan.windows.append(_build_window(raw, tables, seen_ids))
        else:
            raise _expr_fail("query kind must be aggregate or window",
                             index=index, actual=kind)
    return plan
