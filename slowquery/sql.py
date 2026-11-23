"""A deterministic SQL SELECT normalizer.

The normalizer intentionally targets the read-only query shapes accepted by the
engine.  It is a real recursive-descent parser, not a regular expression
rewriter, so identical structures with different literal values, whitespace,
keyword case, or IN-list lengths produce one canonical template.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from .canonical import canonical_json, digest
from .errors import EngineError


KEYWORDS = {
    "SELECT",
    "DISTINCT",
    "ALL",
    "FROM",
    "WHERE",
    "GROUP",
    "BY",
    "HAVING",
    "ORDER",
    "LIMIT",
    "OFFSET",
    "AS",
    "AND",
    "OR",
    "NOT",
    "NULL",
    "TRUE",
    "FALSE",
    "IS",
    "IN",
    "BETWEEN",
    "LIKE",
    "GLOB",
    "REGEXP",
    "ESCAPE",
    "JOIN",
    "INNER",
    "LEFT",
    "RIGHT",
    "FULL",
    "OUTER",
    "CROSS",
    "ON",
    "USING",
    "ASC",
    "DESC",
    "NULLS",
    "FIRST",
    "LAST",
    "CASE",
    "WHEN",
    "THEN",
    "ELSE",
    "END",
    "CAST",
    "EXISTS",
    "COLLATE",
    "UNION",
    "INTERSECT",
    "EXCEPT",
    "WITH",
}

CLAUSE_KEYWORDS = {
    "FROM",
    "WHERE",
    "GROUP",
    "HAVING",
    "ORDER",
    "LIMIT",
    "OFFSET",
}

JOIN_START = {"JOIN", "INNER", "LEFT", "RIGHT", "FULL", "CROSS"}

MULTI_CHAR_OPERATORS = ("<=", ">=", "<>", "!=", "==", "||", "<<", ">>")
SINGLE_CHAR_OPERATORS = set("=<>+-*/%~&|")


@dataclass(frozen=True)
class Token:
    kind: str
    value: str
    pos: int

    @property
    def upper(self) -> str:
        return self.value.upper()


def _parse_error(message: str, sql: str, pos: int) -> EngineError:
    return EngineError(
        "ERR_SQL_PARSE",
        message,
        {"position": pos, "sql": sql},
    )


def _lex(sql: str) -> list[Token]:
    tokens: list[Token] = []
    i = 0
    length = len(sql)
    while i < length:
        char = sql[i]
        if char.isspace():
            i += 1
            continue
        if sql.startswith("--", i):
            newline = sql.find("\n", i + 2)
            i = length if newline < 0 else newline + 1
            continue
        if sql.startswith("/*", i):
            end = sql.find("*/", i + 2)
            if end < 0:
                raise _parse_error("unterminated block comment", sql, i)
            i = end + 2
            continue
        if char == "'":
            start = i
            i += 1
            chunks: list[str] = []
            while i < length:
                if sql[i] == "'":
                    if i + 1 < length and sql[i + 1] == "'":
                        chunks.append("'")
                        i += 2
                        continue
                    i += 1
                    break
                chunks.append(sql[i])
                i += 1
            else:
                raise _parse_error("unterminated string literal", sql, start)
            tokens.append(Token("STRING", "".join(chunks), start))
            continue
        if char in ('"', "`", "["):
            start = i
            closing = "]" if char == "[" else char
            i += 1
            chunks = []
            while i < length:
                if sql[i] == closing:
                    if closing != "]" and i + 1 < length and sql[i + 1] == closing:
                        chunks.append(closing)
                        i += 2
                        continue
                    i += 1
                    break
                chunks.append(sql[i])
                i += 1
            else:
                raise _parse_error("unterminated quoted identifier", sql, start)
            tokens.append(Token("IDENT", "".join(chunks), start))
            continue
        if char.isdigit() or (char == "." and i + 1 < length and sql[i + 1].isdigit()):
            start = i
            if char == ".":
                i += 1
                while i < length and sql[i].isdigit():
                    i += 1
            else:
                while i < length and sql[i].isdigit():
                    i += 1
                if i < length and sql[i] == ".":
                    i += 1
                    while i < length and sql[i].isdigit():
                        i += 1
            if i < length and sql[i] in "eE":
                exp_start = i
                i += 1
                if i < length and sql[i] in "+-":
                    i += 1
                digit_start = i
                while i < length and sql[i].isdigit():
                    i += 1
                if digit_start == i:
                    i = exp_start
            tokens.append(Token("NUMBER", sql[start:i], start))
            continue
        if char in "?@:$":
            start = i
            i += 1
            while i < length and (sql[i].isalnum() or sql[i] == "_"):
                i += 1
            tokens.append(Token("PARAM", sql[start:i], start))
            continue
        if char.isalpha() or char == "_":
            start = i
            i += 1
            while i < length and (sql[i].isalnum() or sql[i] in "_$"):
                i += 1
            tokens.append(Token("WORD", sql[start:i], start))
            continue
        if char in "(),.;":
            tokens.append(Token(char, char, i))
            i += 1
            continue
        matched = False
        for operator in MULTI_CHAR_OPERATORS:
            if sql.startswith(operator, i):
                tokens.append(Token("OP", operator, i))
                i += len(operator)
                matched = True
                break
        if matched:
            continue
        if char in SINGLE_CHAR_OPERATORS:
            tokens.append(Token("OP", char, i))
            i += 1
            continue
        raise _parse_error(f"unexpected character {char!r}", sql, i)
    tokens.append(Token("EOF", "", length))
    return tokens


class _Parser:
    def __init__(self, sql: str, tokens: list[Token]):
        self.sql = sql
        self.tokens = tokens
        self.index = 0

    @property
    def current(self) -> Token:
        return self.tokens[self.index]

    def advance(self) -> Token:
        token = self.current
        self.index += 1
        return token

    def is_word(self, *words: str) -> bool:
        return (
            self.current.kind == "WORD"
            and self.current.upper in {word.upper() for word in words}
        )

    def accept_word(self, *words: str) -> bool:
        if self.is_word(*words):
            self.advance()
            return True
        return False

    def expect_word(self, word: str) -> Token:
        if not self.is_word(word):
            raise self.error(f"expected {word}")
        return self.advance()

    def accept(self, kind: str) -> bool:
        if self.current.kind == kind:
            self.advance()
            return True
        return False

    def expect(self, kind: str) -> Token:
        if self.current.kind != kind:
            raise self.error(f"expected {kind!r}")
        return self.advance()

    def error(self, message: str) -> EngineError:
        return _parse_error(message, self.sql, self.current.pos)

    def parse(self) -> dict[str, Any]:
        query = self.parse_query()
        self.accept(";")
        if self.current.kind != "EOF":
            raise self.error("unexpected trailing SQL token")
        return query

    def parse_query(self) -> dict[str, Any]:
        if self.is_word("WITH"):
            raise self.error("WITH/CTE queries are not supported")
        self.expect_word("SELECT")
        distinct = False
        if self.accept_word("DISTINCT"):
            distinct = True
        else:
            self.accept_word("ALL")

        select_items = self.parse_select_items()
        sources: list[dict[str, Any]] = []
        if self.accept_word("FROM"):
            sources = self.parse_from()

        where = None
        if self.accept_word("WHERE"):
            where = self.parse_expression()

        group_by: list[Any] = []
        if self.accept_word("GROUP"):
            self.expect_word("BY")
            group_by = self.parse_expression_list()

        having = None
        if self.accept_word("HAVING"):
            having = self.parse_expression()

        order_by: list[dict[str, Any]] = []
        if self.accept_word("ORDER"):
            self.expect_word("BY")
            order_by = self.parse_order_items()

        limit = None
        offset = None
        if self.accept_word("LIMIT"):
            limit = self.parse_expression()
            if self.accept_word("OFFSET"):
                offset = self.parse_expression()
        elif self.accept_word("OFFSET"):
            offset = self.parse_expression()

        if self.is_word("UNION", "INTERSECT", "EXCEPT"):
            raise self.error("set operations are not supported")

        return {
            "kind": "select",
            "distinct": distinct,
            "select": select_items,
            "from": sources,
            "where": where,
            "group_by": group_by,
            "having": having,
            "order_by": order_by,
            "limit": limit,
            "offset": offset,
        }

    def parse_select_items(self) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        while True:
            expression = self.parse_expression()
            alias = None
            if self.accept_word("AS"):
                if self.current.kind not in {"WORD", "IDENT"}:
                    raise self.error("expected select alias")
                alias = self.advance().value.lower()
            elif (
                self.current.kind in {"WORD", "IDENT"}
                and not self.is_clause_boundary()
            ):
                alias = self.advance().value.lower()
            if alias is None:
                items.append(expression)
            else:
                items.append({"type": "alias", "expr": expression, "alias": alias})
            if not self.accept(","):
                break
        if not items:
            raise self.error("SELECT list cannot be empty")
        return items

    def is_clause_boundary(self) -> bool:
        if self.current.kind in {"EOF", ";", ")"}:
            return True
        return self.current.kind == "WORD" and self.current.upper in CLAUSE_KEYWORDS | {
            "HAVING",
            "ORDER",
            "LIMIT",
            "OFFSET",
            "UNION",
            "INTERSECT",
            "EXCEPT",
            "ASC",
            "DESC",
        }

    def parse_from(self) -> list[dict[str, Any]]:
        sources = [self.parse_source_item("base", None)]
        while True:
            if self.accept(","):
                sources.append(self.parse_source_item("cross", None))
                continue
            if not self.is_word(*JOIN_START):
                break
            join_type = "inner"
            if self.is_word("INNER"):
                self.advance()
            elif self.is_word("LEFT"):
                self.advance()
                join_type = "left"
                self.accept_word("OUTER")
            elif self.is_word("RIGHT"):
                self.advance()
                join_type = "right"
                self.accept_word("OUTER")
            elif self.is_word("FULL"):
                self.advance()
                join_type = "full"
                self.accept_word("OUTER")
            elif self.is_word("CROSS"):
                self.advance()
                join_type = "cross"
            self.expect_word("JOIN")
            condition = None
            if self.accept_word("ON"):
                condition = self.parse_expression()
            elif self.accept_word("USING"):
                self.expect("(")
                columns = []
                while True:
                    if self.current.kind not in {"WORD", "IDENT"}:
                        raise self.error("expected column in USING")
                    columns.append(self.advance().value.lower())
                    if not self.accept(","):
                        break
                self.expect(")")
                condition = {"type": "using", "columns": columns}
            sources.append(self.parse_source_item(join_type, condition))
        return sources

    def parse_source_item(
        self, join_type: str, condition: dict[str, Any] | None
    ) -> dict[str, Any]:
        if self.accept("("):
            raise self.error("derived tables are not supported")
        if self.current.kind not in {"WORD", "IDENT"}:
            raise self.error("expected table name")
        parts = [self.advance().value.lower()]
        while self.accept("."):
            if self.current.kind not in {"WORD", "IDENT"}:
                raise self.error("expected qualified table name")
            parts.append(self.advance().value.lower())
        table = parts[-1]
        schema = ".".join(parts[:-1]) or None
        alias = None
        if self.accept_word("AS"):
            if self.current.kind not in {"WORD", "IDENT"}:
                raise self.error("expected table alias")
            alias = self.advance().value.lower()
        elif (
            self.current.kind in {"WORD", "IDENT"}
            and self.current.upper not in CLAUSE_KEYWORDS
            and self.current.upper not in JOIN_START
            and self.current.upper != "ON"
        ):
            alias = self.advance().value.lower()
        return {
            "table": table,
            "schema": schema,
            "alias": alias,
            "join": join_type,
            "condition": condition,
        }

    def parse_expression_list(self) -> list[Any]:
        values = [self.parse_expression()]
        while self.accept(","):
            values.append(self.parse_expression())
        return values

    def parse_order_items(self) -> list[dict[str, Any]]:
        items = []
        while True:
            expression = self.parse_expression()
            direction = "asc"
            if self.accept_word("ASC"):
                direction = "asc"
            elif self.accept_word("DESC"):
                direction = "desc"
            nulls = None
            if self.accept_word("NULLS"):
                if self.accept_word("FIRST"):
                    nulls = "first"
                elif self.accept_word("LAST"):
                    nulls = "last"
                else:
                    raise self.error("expected FIRST or LAST after NULLS")
            items.append(
                {"expr": expression, "direction": direction, "nulls": nulls}
            )
            if not self.accept(","):
                break
        return items

    def parse_expression(self) -> dict[str, Any]:
        return self.parse_or()

    def parse_or(self) -> dict[str, Any]:
        left = self.parse_and()
        while self.accept_word("OR"):
            right = self.parse_and()
            left = {"type": "binary", "op": "or", "left": left, "right": right}
        return left

    def parse_and(self) -> dict[str, Any]:
        left = self.parse_not()
        while self.accept_word("AND"):
            right = self.parse_not()
            left = {"type": "binary", "op": "and", "left": left, "right": right}
        return left

    def parse_not(self) -> dict[str, Any]:
        if self.accept_word("NOT"):
            return {"type": "unary", "op": "not", "expr": self.parse_not()}
        return self.parse_comparison()

    def parse_comparison(self) -> dict[str, Any]:
        left = self.parse_additive()
        while True:
            if self.current.kind == "OP" and self.current.value in {
                "=",
                "==",
                "!=",
                "<>",
                "<",
                "<=",
                ">",
                ">=",
            }:
                operator = self.advance().value
                right = self.parse_additive()
                left = {
                    "type": "binary",
                    "op": operator,
                    "left": left,
                    "right": right,
                }
                continue
            if self.accept_word("IS"):
                negated = self.accept_word("NOT")
                if self.accept_word("NULL"):
                    left = {
                        "type": "is",
                        "value": "null",
                        "negated": negated,
                        "expr": left,
                    }
                elif self.accept_word("TRUE"):
                    left = {
                        "type": "is",
                        "value": "true",
                        "negated": negated,
                        "expr": left,
                    }
                elif self.accept_word("FALSE"):
                    left = {
                        "type": "is",
                        "value": "false",
                        "negated": negated,
                        "expr": left,
                    }
                elif self.accept_word("DISTINCT") and self.accept_word("FROM"):
                    right = self.parse_additive()
                    left = {
                        "type": "is_distinct",
                        "negated": negated,
                        "left": left,
                        "right": right,
                    }
                else:
                    raise self.error("unsupported IS expression")
                continue
            negated = False
            save = self.index
            if self.accept_word("NOT"):
                negated = True
            if self.accept_word("IN"):
                left = self.parse_in(left, negated)
                continue
            if self.accept_word("BETWEEN"):
                lower = self.parse_additive()
                self.expect_word("AND")
                upper = self.parse_additive()
                left = {
                    "type": "between",
                    "negated": negated,
                    "expr": left,
                    "lower": lower,
                    "upper": upper,
                }
                continue
            if self.is_word("LIKE", "GLOB", "REGEXP"):
                operator = self.advance().value.lower()
                pattern = self.parse_additive()
                escape = None
                if self.accept_word("ESCAPE"):
                    escape = self.parse_additive()
                left = {
                    "type": "like",
                    "op": operator,
                    "negated": negated,
                    "expr": left,
                    "pattern": pattern,
                    "escape": escape,
                }
                continue
            if negated:
                self.index = save
            break
        return left

    def parse_in(self, left: dict[str, Any], negated: bool) -> dict[str, Any]:
        self.expect("(")
        if self.is_word("SELECT"):
            query = self.parse_query()
            self.expect(")")
            return {"type": "in_subquery", "negated": negated, "expr": left, "query": query}
        items = []
        while True:
            items.append(self.parse_expression())
            if not self.accept(","):
                break
        self.expect(")")
        if not items:
            raise self.error("IN list cannot be empty")
        all_scalar_params = all(
            item.get("type") in {"literal", "param", "tuple"} for item in items
        )
        if all_scalar_params:
            # A single marker makes IN (1) and IN (1, 2, 3) one template.
            normalized_items = [{"type": "in_list_marker"}]
        else:
            normalized_items = items
        return {
            "type": "in",
            "negated": negated,
            "expr": left,
            "items": normalized_items,
        }

    def parse_additive(self) -> dict[str, Any]:
        left = self.parse_multiplicative()
        while (
            self.current.kind == "OP"
            and self.current.value in {"+", "-", "||"}
        ):
            operator = self.advance().value
            right = self.parse_multiplicative()
            left = {
                "type": "binary",
                "op": operator,
                "left": left,
                "right": right,
            }
        return left

    def parse_multiplicative(self) -> dict[str, Any]:
        left = self.parse_unary()
        while self.current.kind == "OP" and self.current.value in {"*", "/", "%"}:
            operator = self.advance().value
            right = self.parse_unary()
            left = {
                "type": "binary",
                "op": operator,
                "left": left,
                "right": right,
            }
        return left

    def parse_unary(self) -> dict[str, Any]:
        if self.current.kind == "OP" and self.current.value in {"+", "-", "~"}:
            operator = self.advance().value
            return {"type": "unary", "op": operator, "expr": self.parse_unary()}
        return self.parse_primary()

    def parse_primary(self) -> dict[str, Any]:
        token = self.current
        if token.kind in {"NUMBER", "STRING"}:
            self.advance()
            return {"type": "literal"}
        if token.kind == "PARAM":
            self.advance()
            return {"type": "param"}
        if token.kind == "OP" and token.value == "*":
            self.advance()
            return {"type": "wildcard"}
        if self.accept("("):
            if self.is_word("SELECT"):
                query = self.parse_query()
                self.expect(")")
                return {"type": "subquery", "query": query}
            values = [self.parse_expression()]
            while self.accept(","):
                values.append(self.parse_expression())
            self.expect(")")
            if len(values) == 1:
                return values[0]
            return {"type": "tuple", "items": values}
        if self.accept_word("CASE"):
            return self.parse_case()
        if self.accept_word("EXISTS"):
            self.expect("(")
            if not self.is_word("SELECT"):
                raise self.error("EXISTS requires a SELECT")
            query = self.parse_query()
            self.expect(")")
            return {"type": "exists", "query": query}
        if token.kind in {"WORD", "IDENT"}:
            name = self.advance().value.lower()
            if self.accept("("):
                return self.parse_function(name)
            parts = [name]
            while self.accept("."):
                if self.current.kind not in {"WORD", "IDENT"}:
                    if self.accept("*"):
                        parts.append("*")
                        break
                    raise self.error("expected column name after '.'")
                parts.append(self.advance().value.lower())
            return {"type": "column", "parts": parts}
        raise self.error("expected SQL expression")

    def parse_function(self, name: str) -> dict[str, Any]:
        distinct = False
        if self.accept_word("DISTINCT"):
            distinct = True
        args: list[Any] = []
        cast_type = None
        if name == "cast":
            value = self.parse_expression()
            self.expect_word("AS")
            type_parts = []
            depth = 0
            while not (self.current.kind == ")" and depth == 0):
                if self.current.kind == "EOF":
                    raise self.error("unterminated CAST")
                current = self.advance()
                if current.kind == "(":
                    depth += 1
                elif current.kind == ")":
                    depth -= 1
                type_parts.append(current.value.lower())
            self.expect(")")
            return {
                "type": "cast",
                "expr": value,
                "target": " ".join(type_parts),
            }
        if not self.accept(")"):
            while True:
                if self.current.kind == "OP" and self.current.value == "*":
                    self.advance()
                    args.append({"type": "wildcard"})
                else:
                    args.append(self.parse_expression())
                if not self.accept(","):
                    break
            self.expect(")")
        return {
            "type": "function",
            "name": name,
            "distinct": distinct,
            "args": args,
        }

    def parse_case(self) -> dict[str, Any]:
        base = None
        if not self.is_word("WHEN"):
            base = self.parse_expression()
        branches = []
        while self.accept_word("WHEN"):
            condition = self.parse_expression()
            self.expect_word("THEN")
            value = self.parse_expression()
            branches.append({"when": condition, "then": value})
        otherwise = None
        if self.accept_word("ELSE"):
            otherwise = self.parse_expression()
        self.expect_word("END")
        if not branches:
            raise self.error("CASE requires at least one WHEN")
        return {
            "type": "case",
            "base": base,
            "branches": branches,
            "else": otherwise,
        }


def parse_sql(sql: str) -> dict[str, Any]:
    if not isinstance(sql, str) or not sql.strip():
        raise EngineError(
            "ERR_SQL_PARSE", "SQL text must be a non-empty string", {"sql": sql}
        )
    tokens = _lex(sql)
    return _Parser(sql, tokens).parse()


def normalize_sql(sql: str) -> dict[str, Any]:
    """Return the canonical template for a SQL string.

    The returned mapping deliberately contains no source positions or literal
    values.  Calling this function repeatedly with equivalent SQL is stable.
    """

    structure = parse_sql(sql)
    canonical = canonical_json(structure)
    template_id = digest({"normalizer": 1, "structure": structure})
    return {
        "template_id": template_id,
        "structure": structure,
        "canonical": canonical,
    }


def iter_columns(node: Any) -> Iterable[dict[str, Any]]:
    if not isinstance(node, dict):
        return
    if node.get("type") == "column":
        yield node
    for value in node.values():
        if isinstance(value, dict):
            yield from iter_columns(value)
        elif isinstance(value, list):
            for item in value:
                yield from iter_columns(item)


def table_aliases(structure: dict[str, Any]) -> dict[str, str]:
    aliases: dict[str, str] = {}
    for source in structure.get("from", []):
        table = source["table"]
        aliases[table] = table
        if source.get("alias"):
            aliases[source["alias"]] = table
    return aliases


def query_tables(structure: dict[str, Any]) -> list[str]:
    seen = []
    for source in structure.get("from", []):
        if source["table"] not in seen:
            seen.append(source["table"])
    return seen


def predicate_columns(structure: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract deterministic predicate metadata used for index eligibility."""

    aliases = table_aliases(structure)
    predicates: list[dict[str, Any]] = []

    def add_column(node: dict[str, Any], kind: str) -> None:
        parts = node.get("parts", [])
        if not parts or parts[-1] == "*":
            return
        column = parts[-1]
        qualifier = parts[-2] if len(parts) >= 2 else None
        table = aliases.get(qualifier) if qualifier else None
        predicates.append(
            {"table": table, "qualifier": qualifier, "column": column, "kind": kind}
        )

    def visit(node: Any) -> None:
        if not isinstance(node, dict):
            return
        node_type = node.get("type")
        if node_type == "binary" and node.get("op") in {"and", "or"}:
            visit(node.get("left"))
            visit(node.get("right"))
            return
        if node_type == "binary" and node.get("op") in {
            "=",
            "==",
            "<",
            "<=",
            ">",
            ">=",
            "!=",
            "<>",
        }:
            left = node.get("left", {})
            right = node.get("right", {})
            kind = "equality" if node["op"] in {"=", "=="} else "range"
            if left.get("type") == "column":
                add_column(left, kind)
            if right.get("type") == "column":
                add_column(right, kind)
            return
        if node_type == "in":
            expr = node.get("expr", {})
            if expr.get("type") == "column":
                add_column(expr, "equality")
            return
        if node_type == "between":
            expr = node.get("expr", {})
            if expr.get("type") == "column":
                add_column(expr, "range")
            return
        if node_type in {"is", "like"}:
            expr = node.get("expr", {})
            if expr.get("type") == "column":
                add_column(expr, "equality")
            return
        for value in node.values():
            if isinstance(value, dict):
                visit(value)
            elif isinstance(value, list):
                for item in value:
                    visit(item)

    visit(structure.get("where"))
    for source in structure.get("from", []):
        visit(source.get("condition"))
    return predicates


def all_columns(node: Any) -> set[tuple[str | None, str]]:
    return {
        (
            column["parts"][-2] if len(column.get("parts", [])) >= 2 else None,
            column["parts"][-1],
        )
        for column in iter_columns(node)
        if column.get("parts") and column["parts"][-1] != "*"
    }
