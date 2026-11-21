"""A deliberately small, side-effect-free filter expression language."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable

from .errors import fail

_TOKEN_RE = re.compile(
    r"""
    \s*(?:
      (?P<number>-?(?:\d+\.\d*|\d*\.\d+|\d+)(?:[eE][+-]?\d+)?)
      |(?P<string>"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*')
      |(?P<op>==|!=|>=|<=|>|<)
      |(?P<lparen>\()
      |(?P<rparen>\))
      |(?P<ident>[A-Za-z_][A-Za-z0-9_]*)
      |(?P<invalid>\S)
    )
    """,
    re.VERBOSE,
)


@dataclass(frozen=True)
class Token:
    kind: str
    value: Any
    position: int


class FilterExpression:
    """Parsed filter AST with deterministic evaluation."""

    def __init__(self, text: str, *, error_code: str = "E_V1_BAD_FILTER") -> None:
        self.text = text
        self.error_code = error_code
        self.tokens = self._tokenize(text)
        self.position = 0
        self.ast = self._parse_or()
        if self.position != len(self.tokens):
            token = self.tokens[self.position]
            raise fail(
                self.error_code,
                "unexpected token in filter expression",
                expression=text,
                token=token.value,
                position=token.position,
            )
        self.columns = sorted(self._collect_identifiers(self.ast))

    @staticmethod
    def _tokenize(text: str) -> list[Token]:
        tokens: list[Token] = []
        index = 0
        while index < len(text):
            match = _TOKEN_RE.match(text, index)
            if not match:
                break
            kind = match.lastgroup
            assert kind is not None
            value: Any = match.group(kind)
            start = match.start(kind)
            if kind == "invalid":
                raise fail(
                    self.error_code,
                    "invalid character in filter expression",
                    expression=text,
                    token=value,
                    position=start,
                )
            if kind == "number":
                if any(char in value for char in ".eE"):
                    value = float(value)
                else:
                    value = int(value)
            elif kind == "string":
                import json

                if value.startswith('"'):
                    value = json.loads(value)
                else:
                    body = value[1:-1]
                    value = bytes(body, "utf-8").decode("unicode_escape")
            elif kind == "ident":
                lowered = value.lower()
                if lowered in {"and", "or", "not", "true", "false", "null"}:
                    kind = lowered
                    if lowered == "true":
                        value = True
                    elif lowered == "false":
                        value = False
                    elif lowered == "null":
                        value = None
            tokens.append(Token(kind=kind, value=value, position=start))
            index = match.end()
        if text[index:].strip():
            raise fail(
                self.error_code,
                "invalid trailing filter content",
                expression=text,
                position=index,
            )
        return tokens

    def _peek(self) -> Token | None:
        if self.position >= len(self.tokens):
            return None
        return self.tokens[self.position]

    def _take(self) -> Token:
        token = self._peek()
        if token is None:
            raise fail(self.error_code, "unexpected end of filter", expression=self.text)
        self.position += 1
        return token

    def _parse_or(self) -> Any:
        node = self._parse_and()
        while (token := self._peek()) and token.kind == "or":
            self._take()
            node = ("or", node, self._parse_and())
        return node

    def _parse_and(self) -> Any:
        node = self._parse_not()
        while (token := self._peek()) and token.kind == "and":
            self._take()
            node = ("and", node, self._parse_not())
        return node

    def _parse_not(self) -> Any:
        token = self._peek()
        if token and token.kind == "not":
            self._take()
            return ("not", self._parse_not())
        return self._parse_compare()

    def _parse_compare(self) -> Any:
        left = self._parse_primary()
        token = self._peek()
        if token and token.kind == "op":
            self._take()
            right = self._parse_primary()
            return ("compare", token.value, left, right)
        return left

    def _parse_primary(self) -> Any:
        token = self._take()
        if token.kind in {"number", "string", "true", "false", "null"}:
            return ("literal", token.value)
        if token.kind == "ident":
            return ("column", token.value)
        if token.kind == "lparen":
            node = self._parse_or()
            closing = self._take()
            if closing.kind != "rparen":
                raise fail(
                    self.error_code,
                    "missing closing parenthesis",
                    expression=self.text,
                    position=closing.position,
                )
            return node
        raise fail(
            self.error_code,
            "expected a column, literal, or parenthesized expression",
            expression=self.text,
            token=token.value,
            position=token.position,
        )

    @classmethod
    def _collect_identifiers(cls, node: Any) -> set[str]:
        tag = node[0]
        if tag == "column":
            return {node[1]}
        if tag in {"literal"}:
            return set()
        if tag in {"not"}:
            return cls._collect_identifiers(node[1])
        if tag in {"and", "or"}:
            return cls._collect_identifiers(node[1]) | cls._collect_identifiers(node[2])
        if tag == "compare":
            return cls._collect_identifiers(node[2]) | cls._collect_identifiers(node[3])
        return set()

    def evaluate(self, row: dict[str, Any]) -> bool:
        value = self._eval(self.ast, row)
        if not isinstance(value, bool):
            raise fail(
                "E_VIEW_FILTER",
                "filter expression did not produce a boolean",
                expression=self.text,
                value=value,
            )
        return value

    def _eval(self, node: Any, row: dict[str, Any]) -> Any:
        tag = node[0]
        if tag == "literal":
            return node[1]
        if tag == "column":
            name = node[1]
            if name not in row:
                raise fail(
                    "E_LOG_ROW_SCHEMA",
                    "row is missing a column required by a view filter",
                    column=name,
                )
            return row[name]
        if tag == "not":
            return not bool(self._eval(node[1], row))
        if tag == "and":
            return bool(self._eval(node[1], row)) and bool(self._eval(node[2], row))
        if tag == "or":
            return bool(self._eval(node[1], row)) or bool(self._eval(node[2], row))
        if tag == "compare":
            operator = node[1]
            left = self._eval(node[2], row)
            right = self._eval(node[3], row)
            if left is None or right is None:
                return False
            try:
                if operator == "==":
                    return left == right
                if operator == "!=":
                    return left != right
                if operator == ">":
                    return left > right
                if operator == "<":
                    return left < right
                if operator == ">=":
                    return left >= right
                if operator == "<=":
                    return left <= right
            except TypeError:
                raise fail(
                    "E_VIEW_FILTER",
                    "filter compared incompatible value types",
                    expression=self.text,
                    left=left,
                    right=right,
                )
        raise AssertionError(f"unhandled AST node: {node!r}")


def validate_columns(
    columns: Iterable[str],
    schema_columns: set[str],
    *,
    field: str,
    code: str = "E_V1_BAD_FILTER",
) -> None:
    missing = sorted(set(columns) - schema_columns)
    if missing:
        raise fail(
            code,
            f"{field} references unknown columns",
            field=field,
            missing=missing,
        )
