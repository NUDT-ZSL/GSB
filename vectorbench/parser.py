"""Recursive-descent expression parser producing a canonical expression tree."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Optional

from .errors import BenchError, ErrorCode

ARITHMETIC = ("+", "-", "*", "/", "%")
COMPARISON = ("<", "<=", ">", ">=", "==", "!=")
LOGICAL = ("&&", "||")


@dataclass(frozen=True)
class Node:
    kind: str  # literal | column | unary | binary | ternary
    type_name: Optional[str] = None  # literal static type only
    value: object = None
    name: Optional[str] = None
    op: Optional[str] = None
    children: tuple["Node", ...] = ()

    def column_refs(self) -> set[str]:
        refs: set[str] = set()
        if self.kind == "column":
            refs.add(self.name)  # type: ignore[arg-type]
        for child in self.children:
            refs.update(child.column_refs())
        return refs

    def depth(self) -> int:
        if not self.children:
            return 1
        return 1 + max(child.depth() for child in self.children)

    def node_count(self) -> int:
        return 1 + sum(child.node_count() for child in self.children)

    def canonical_text(self) -> str:
        if self.kind == "literal":
            if self.type_name == "bool":
                return "true" if self.value else "false"
            if self.type_name == "int":
                return str(int(self.value))  # type: ignore[arg-type]
            return _canonical_float(float(self.value))  # type: ignore[arg-type]
        if self.kind == "column":
            return f"col:{self.name}"
        if self.kind == "unary":
            return f"({self.op} {self.children[0].canonical_text()})"
        if self.kind == "ternary":
            cond, yes, no = self.children
            return f"(?: {cond.canonical_text()} {yes.canonical_text()} {no.canonical_text()})"
        return f"({self.op} " + " ".join(child.canonical_text() for child in self.children) + ")"

    def structural_digest(self) -> str:
        return hashlib.sha256(self.canonical_text().encode("utf-8")).hexdigest()

    def walk(self):
        yield self
        for child in self.children:
            yield from child.walk()

    def walk_postorder(self):
        for child in self.children:
            yield from child.walk_postorder()
        yield self


def _canonical_float(value: float) -> str:
    if value != value:  # NaN is rejected at parse time but kept deterministic here.
        return "nan"
    return repr(value)


class _Tokenizer:
    _MULTI = ("<=", ">=", "==", "!=", "&&", "||")
    _SINGLE = set("+-*/%<>()?:!")

    def __init__(self, text: str, query_index: int):
        self.text = text
        self.query_index = query_index
        self.position = 0
        self.tokens: list[tuple[str, str]] = []
        self._scan()
        self.index = 0

    def _fail(self, message: str, position: Optional[int] = None) -> BenchError:
        return BenchError(
            ErrorCode.E_EXPR,
            message,
            {"query_index": self.query_index, "position": self.position if position is None else position},
        )

    def _scan(self) -> None:
        while self.position < len(self.text):
            char = self.text[self.position]
            if char.isspace():
                self.position += 1
                continue
            if char.isalpha() or char == "_":
                start = self.position
                while self.position < len(self.text) and (
                    self.text[self.position].isalnum() or self.text[self.position] == "_"
                ):
                    self.position += 1
                word = self.text[start:self.position]
                if word in ("true", "false"):
                    self.tokens.append(("BOOL", word))
                elif word in ("and", "or", "not"):
                    alias = {"and": "&&", "or": "||", "not": "!"}[word]
                    self.tokens.append(("OP", alias))
                else:
                    self.tokens.append(("NAME", word))
                continue
            if char.isdigit() or (char == "." and self.position + 1 < len(self.text) and self.text[self.position + 1].isdigit()):
                start = self.position
                dot_seen = False
                while self.position < len(self.text):
                    current = self.text[self.position]
                    if current.isdigit():
                        self.position += 1
                    elif current == "." and not dot_seen:
                        dot_seen = True
                        self.position += 1
                    else:
                        break
                lexeme = self.text[start:self.position]
                self.tokens.append(("FLOAT" if dot_seen else "INT", lexeme))
                continue
            two = self.text[self.position:self.position + 2]
            if two in self._MULTI:
                self.tokens.append(("OP", two))
                self.position += 2
                continue
            if char in self._SINGLE:
                self.tokens.append(("OP", char))
                self.position += 1
                continue
            raise self._fail(f"illegal character {char!r}")

    def peek(self) -> Optional[tuple[str, str]]:
        return self.tokens[self.index] if self.index < len(self.tokens) else None

    def take(self) -> tuple[str, str]:
        token = self.peek()
        if token is None:
            raise self._fail("unexpected end of expression")
        self.index += 1
        return token

    def expect_op(self, op: str) -> None:
        token = self.peek()
        if token is None or token != ("OP", op):
            raise self._fail(f"expected operator {op!r}")
        self.index += 1


class _Parser:
    def __init__(self, text: str, query_index: int):
        self.tokenizer = _Tokenizer(text, query_index)

    @property
    def query_index(self) -> int:
        return self.tokenizer.query_index

    def parse(self) -> Node:
        if not self.tokenizer.tokens:
            raise BenchError(ErrorCode.E_EXPR, "empty expression", {"query_index": self.query_index})
        node = self._ternary()
        if self.tokenizer.peek() is not None:
            raise BenchError(
                ErrorCode.E_EXPR,
                "unexpected trailing tokens",
                {"query_index": self.query_index, "token": self.tokenizer.peek()[1]},  # type: ignore[index]
            )
        return node

    def _ternary(self) -> Node:
        condition = self._logical_or()
        token = self.tokenizer.peek()
        if token == ("OP", "?"):
            self.tokenizer.take()
            when_true = self._ternary()
            self.tokenizer.expect_op(":")
            when_false = self._ternary()
            return Node("ternary", op="?:", children=(condition, when_true, when_false))
        return condition

    def _binary_chain(self, sub_parser, operators: tuple[str, ...]) -> Node:
        left = sub_parser()
        while self.tokenizer.peek() is not None and self.tokenizer.peek()[1] in operators:  # type: ignore[index]
            _, op = self.tokenizer.take()
            right = sub_parser()
            left = Node("binary", op=op, children=(left, right))
        return left

    def _logical_or(self) -> Node:
        return self._binary_chain(self._logical_and, ("||",))

    def _logical_and(self) -> Node:
        return self._binary_chain(self._equality, ("&&",))

    def _equality(self) -> Node:
        return self._binary_chain(self._comparison, ("==", "!="))

    def _comparison(self) -> Node:
        return self._binary_chain(self._additive, ("<", "<=", ">", ">="))

    def _additive(self) -> Node:
        return self._binary_chain(self._multiplicative, ("+", "-"))

    def _multiplicative(self) -> Node:
        return self._binary_chain(self._unary, ("*", "/", "%"))

    def _unary(self) -> Node:
        token = self.tokenizer.peek()
        if token == ("OP", "-"):
            self.tokenizer.take()
            return Node("unary", op="-", children=(self._unary(),))
        if token == ("OP", "!"):
            self.tokenizer.take()
            return Node("unary", op="!", children=(self._unary(),))
        if token and token[0] == "OP" and token[1] == "+":
            self.tokenizer.take()
            return self._unary()
        return self._primary()

    def _primary(self) -> Node:
        token = self.tokenizer.peek()
        if token is None:
            raise BenchError(ErrorCode.E_EXPR, "expected an operand", {"query_index": self.query_index})
        kind, lexeme = token
        if kind == "INT":
            self.tokenizer.take()
            try:
                return Node("literal", type_name="int", value=int(lexeme))
            except ValueError as exc:
                raise BenchError(ErrorCode.E_EXPR, "invalid integer literal",
                                  {"query_index": self.query_index, "literal": lexeme}) from exc
        if kind == "FLOAT":
            self.tokenizer.take()
            try:
                return Node("literal", type_name="float", value=float(lexeme))
            except ValueError as exc:
                raise BenchError(ErrorCode.E_EXPR, "invalid float literal",
                                  {"query_index": self.query_index, "literal": lexeme}) from exc
        if kind == "BOOL":
            self.tokenizer.take()
            return Node("literal", type_name="bool", value=(lexeme == "true"))
        if kind == "NAME":
            self.tokenizer.take()
            return Node("column", name=lexeme)
        if lexeme == "(":
            self.tokenizer.take()
            node = self._ternary()
            self.tokenizer.expect_op(")")
            return node
        raise BenchError(
            ErrorCode.E_EXPR,
            "unsupported operator or malformed expression",
            {"query_index": self.query_index, "token": lexeme},
        )


def parse_expression(text: str, query_index: int = 0) -> Node:
    return _Parser(text, query_index).parse()
