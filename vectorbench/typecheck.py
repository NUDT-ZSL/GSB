"""Static result typing and nullability annotation for expression trees."""
from __future__ import annotations

from dataclasses import dataclass

from .errors import BenchError, ErrorCode
from .model import BOOL, FLOAT, INT, DataBlock
from .parser import ARITHMETIC, COMPARISON, LOGICAL, Node

ARITH_RESULT = {
    ("+", INT, INT): INT,
    ("-", INT, INT): INT,
    ("*", INT, INT): INT,
    ("/", INT, INT): FLOAT,
    ("%", INT, INT): INT,
    ("+", FLOAT, FLOAT): FLOAT,
    ("-", FLOAT, FLOAT): FLOAT,
    ("*", FLOAT, FLOAT): FLOAT,
    ("/", FLOAT, FLOAT): FLOAT,
    ("%", FLOAT, FLOAT): FLOAT,
}


@dataclass(frozen=True)
class TypedNode:
    node: Node
    result_type: str
    nullable: bool


class TypeAnalyzer:
    def __init__(self, block: DataBlock, query_index: int):
        self.block = block
        self.query_index = query_index
        self.typed: dict[int, TypedNode] = {}

    def analyze(self, root: Node) -> TypedNode:
        typed = self._visit(root)
        self.typed[id(root)] = typed
        return typed

    def _record(self, node: Node, result_type: str, nullable: bool) -> TypedNode:
        typed = TypedNode(node, result_type, nullable)
        self.typed[id(node)] = typed
        return typed

    def _visit(self, node: Node) -> TypedNode:
        if node.kind == "literal":
            return self._record(node, node.type_name, False)
        if node.kind == "column":
            column = self.block.column(node.name)  # type: ignore[arg-type]
            return self._record(node, column.type_name, bool(column.nullmap.any()))
        if node.kind == "unary":
            operand = self._visit(node.children[0])
            if node.op == "-":
                if operand.result_type == BOOL:
                    raise BenchError(
                        ErrorCode.E_TYPE,
                        "unary minus cannot operate on boolean",
                        {"query_index": self.query_index, "expression": node.canonical_text()},
                    )
                return self._record(node, operand.result_type, operand.nullable)
            if node.op == "!":
                if operand.result_type != BOOL:
                    raise BenchError(
                        ErrorCode.E_TYPE,
                        "logical not requires boolean operand",
                        {"query_index": self.query_index, "expression": node.canonical_text()},
                    )
                return self._record(node, BOOL, operand.nullable)
        if node.kind == "binary":
            left = self._visit(node.children[0])
            right = self._visit(node.children[1])
            return self._visit_binary(node, left, right)
        if node.kind == "ternary":
            condition = self._visit(node.children[0])
            if condition.result_type != BOOL:
                raise BenchError(
                    ErrorCode.E_TYPE,
                    "ternary condition must be boolean",
                    {"query_index": self.query_index, "expression": node.canonical_text()},
                )
            yes = self._visit(node.children[1])
            no = self._visit(node.children[2])
            result_type = _promote(yes.result_type, no.result_type, node, self.query_index)
            nullable = condition.nullable or yes.nullable or no.nullable
            return self._record(node, result_type, nullable)
        raise BenchError(ErrorCode.E_EXPR, "unknown expression node", {"kind": node.kind})

    def _visit_binary(self, node: Node, left: TypedNode, right: TypedNode) -> TypedNode:
        op = node.op
        if op in ARITHMETIC:
            if BOOL in (left.result_type, right.result_type):
                raise BenchError(
                    ErrorCode.E_TYPE,
                    "arithmetic operators require numeric operands",
                    {"query_index": self.query_index, "operator": op,
                     "left": left.result_type, "right": right.result_type},
                )
            normalized_left = FLOAT if FLOAT in (left.result_type, right.result_type) else INT
            result_type = ARITH_RESULT[(op, normalized_left, normalized_left)]  # type: ignore[index]
            return self._record(node, result_type, left.nullable or right.nullable)
        if op in COMPARISON:
            if BOOL in (left.result_type, right.result_type) and (
                left.result_type != BOOL or right.result_type != BOOL or op not in ("==", "!=")
            ):
                raise BenchError(
                    ErrorCode.E_TYPE,
                    "ordering comparison requires numeric operands",
                    {"query_index": self.query_index, "operator": op,
                     "left": left.result_type, "right": right.result_type},
                )
            return self._record(node, BOOL, left.nullable or right.nullable)
        if op in LOGICAL:
            if left.result_type != BOOL or right.result_type != BOOL:
                raise BenchError(
                    ErrorCode.E_TYPE,
                    "logical operators require boolean operands",
                    {"query_index": self.query_index, "operator": op},
                )
            return self._record(node, BOOL, left.nullable or right.nullable)
        raise BenchError(
            ErrorCode.E_EXPR,
            "unsupported operator",
            {"query_index": self.query_index, "operator": op},
        )


def _promote(left: str, right: str, node: Node, query_index: int) -> str:
    if left == right:
        return left
    pair = {left, right}
    if pair == {INT, FLOAT}:
        return FLOAT
    raise BenchError(
        ErrorCode.E_TYPE,
        "ternary branches have incompatible types",
        {"query_index": query_index, "left": left, "right": right,
         "expression": node.canonical_text()},
    )


def annotate(root: Node, block: DataBlock, query_index: int) -> TypeAnalyzer:
    analyzer = TypeAnalyzer(block, query_index)
    analyzer.analyze(root)
    return analyzer
