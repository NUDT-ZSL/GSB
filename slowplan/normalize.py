from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .errors import BusinessError, E_SQL_UNNORMALIZABLE
from .util import sha256_text


KEYWORDS = {
    "SELECT",
    "FROM",
    "WHERE",
    "AND",
    "OR",
    "IN",
    "IS",
    "NULL",
    "NOT",
    "ORDER",
    "BY",
    "GROUP",
    "LIMIT",
    "OFFSET",
    "ASC",
    "DESC",
    "AS",
    "DISTINCT",
    "TRUE",
    "FALSE",
    "JOIN",
    "LEFT",
    "RIGHT",
    "INNER",
    "OUTER",
    "ON",
    "UNION",
    "ALL",
    "BETWEEN",
    "LIKE",
    "HAVING",
}

OPERATORS = {"=", "<", ">", "<=", ">=", "<>", "!=", "+", "-", "*", "/", "%", "||"}
IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")


@dataclass(frozen=True)
class Token:
    kind: str
    text: str


def _business(sql: str, reason: str, position: int) -> BusinessError:
    return BusinessError(
        E_SQL_UNNORMALIZABLE,
        "SQL cannot be normalized",
        {"sql": sql, "reason": reason, "position": position},
    )


def _tokenize(sql: str) -> list[Token]:
    tokens: list[Token] = []
    i = 0
    length = len(sql)
    while i < length:
        ch = sql[i]
        if ch.isspace():
            i += 1
            continue
        if sql.startswith("--", i):
            newline = sql.find("\n", i + 2)
            i = length if newline < 0 else newline + 1
            continue
        if sql.startswith("/*", i):
            end = sql.find("*/", i + 2)
            if end < 0:
                raise _business(sql, "unterminated block comment", i)
            i = end + 2
            continue
        if ch == "'":
            start = i
            i += 1
            value = []
            while i < length:
                if sql[i] == "'":
                    if i + 1 < length and sql[i + 1] == "'":
                        value.append("'")
                        i += 2
                        continue
                    i += 1
                    break
                value.append(sql[i])
                i += 1
            else:
                raise _business(sql, "unterminated string literal", start)
            tokens.append(Token("string", "".join(value)))
            continue
        if ch in ('"', "`", "["):
            close = "]" if ch == "[" else ch
            start = i
            i += 1
            value = []
            while i < length:
                if sql[i] == close:
                    if close != "]" and i + 1 < length and sql[i + 1] == close:
                        value.append(close)
                        i += 2
                        continue
                    i += 1
                    break
                value.append(sql[i])
                i += 1
            else:
                raise _business(sql, "unterminated quoted identifier", start)
            ident = "".join(value).lower()
            if not IDENT_RE.match(ident):
                raise _business(sql, "invalid quoted identifier", start)
            tokens.append(Token("word", ident))
            continue
        if ch.isdigit():
            start = i
            i += 1
            while i < length and (sql[i].isdigit() or sql[i] in ".eE+-"):
                if sql[i] in "+-" and sql[i - 1] not in "eE":
                    break
                i += 1
            tokens.append(Token("number", sql[start:i]))
            continue
        if ch.isalpha() or ch == "_":
            start = i
            i += 1
            while i < length and (sql[i].isalnum() or sql[i] in "_$"):
                i += 1
            tokens.append(Token("word", sql[start:i]))
            continue
        if ch in "?@:$":
            start = i
            i += 1
            if ch in "@:":
                while i < length and (sql[i].isalnum() or sql[i] == "_"):
                    i += 1
            tokens.append(Token("param", sql[start:i]))
            continue
        if ch == "%" and i + 1 < length and sql[i + 1] == "s":
            tokens.append(Token("param", "%s"))
            i += 2
            continue
        two = sql[i : i + 2]
        if two in {"<=", ">=", "<>", "!=", "||", "::"}:
            tokens.append(Token("op", two))
            i += 2
            continue
        if ch in "=<>+-*/%":
            tokens.append(Token("op", ch))
            i += 1
            continue
        if ch in "(),.;":
            tokens.append(Token("punct", ch))
            i += 1
            continue
        raise _business(sql, f"unexpected character {ch!r}", i)
    return tokens


def _collapse_placeholders(tokens: list[Token]) -> list[Token]:
    result: list[Token] = []
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if token.kind == "word" and token.text.upper() == "IN" and i + 1 < len(tokens) and tokens[i + 1].text == "(":
            depth = 1
            j = i + 2
            inside: list[Token] = []
            while j < len(tokens) and depth > 0:
                current = tokens[j]
                if current.text == "(":
                    depth += 1
                elif current.text == ")":
                    depth -= 1
                    if depth == 0:
                        break
                inside.append(current)
                j += 1
            if depth != 0:
                raise BusinessError(E_SQL_UNNORMALIZABLE, "SQL cannot be normalized", {"reason": "unbalanced IN list"})
            meaningful = [item for item in inside if item.text != ","]
            if meaningful and all(item.kind in {"number", "string", "param"} for item in meaningful):
                result.extend([token, Token("punct", "("), Token("param", "?"), Token("punct", ")")])
                i = j + 1
                continue
        if token.kind in {"number", "string"}:
            result.append(Token("param", "?"))
        elif token.kind == "param":
            result.append(Token("param", "?"))
        else:
            result.append(token)
        i += 1
    return result


def _strip_unary_numeric_signs(tokens: list[Token]) -> list[Token]:
    result: list[Token] = []
    unary_prefixes = {"IN", "LIMIT", "OFFSET", "SELECT", "BETWEEN", "AND", "WHERE", "VALUES"}
    for index, token in enumerate(tokens):
        if token.kind == "op" and token.text in {"+", "-"} and index + 1 < len(tokens):
            next_token = tokens[index + 1]
            previous = result[-1] if result else None
            unary = (
                previous is None
                or previous.text in {"(", ",", "=", "<", ">", "<=", ">=", "<>", "!=", "+", "-", "*", "/", "%"}
                or (previous.kind == "word" and previous.text.upper() in unary_prefixes)
            )
            if unary and next_token.kind == "number":
                continue
        result.append(token)
    return result


def _render(tokens: list[Token]) -> str:
    pieces: list[str] = []
    previous: Token | None = None
    for token in tokens:
        if token.kind == "word":
            upper = token.text.upper()
            text = upper if upper in KEYWORDS else token.text.lower()
        else:
            text = token.text
        if previous is None:
            pieces.append(text)
        elif text in {",", ")", ";", "."} or previous.text in {"(", "."}:
            pieces.append(text)
        elif text == "(":
            previous_is_keyword = previous.kind == "word" and previous.text.upper() in KEYWORDS
            pieces.append(("" if previous.kind == "word" and not previous_is_keyword else " ") + text)
        elif text in OPERATORS or previous.text in OPERATORS:
            pieces.append(" " + text)
        else:
            pieces.append(" " + text)
        previous = token
    return "".join(pieces).strip()


def _split_top_level(tokens: list[Token], delimiter: str) -> list[list[Token]]:
    parts: list[list[Token]] = []
    current: list[Token] = []
    depth = 0
    for token in tokens:
        if token.text == "(":
            depth += 1
        elif token.text == ")":
            depth -= 1
        if depth == 0 and token.kind == "word" and token.text.upper() == delimiter:
            parts.append(current)
            current = []
            continue
        current.append(token)
    parts.append(current)
    return parts


def _parse_structure(tokens: list[Token], template_sql: str) -> dict[str, Any]:
    if not tokens or tokens[0].kind != "word" or tokens[0].text.upper() != "SELECT":
        raise BusinessError(E_SQL_UNNORMALIZABLE, "SQL cannot be normalized", {"reason": "only SELECT is supported"})

    depth = 0
    from_index = -1
    for index, token in enumerate(tokens):
        if token.text == "(":
            depth += 1
        elif token.text == ")":
            depth -= 1
        elif depth == 0 and token.kind == "word" and token.text.upper() == "FROM":
            from_index = index
            break
    if from_index < 0 or from_index + 1 >= len(tokens):
        raise BusinessError(E_SQL_UNNORMALIZABLE, "SQL cannot be normalized", {"reason": "missing FROM table"})
    if tokens[from_index + 1].kind != "word":
        raise BusinessError(E_SQL_UNNORMALIZABLE, "SQL cannot be normalized", {"reason": "invalid table name"})
    table = tokens[from_index + 1].text.lower()
    if table.upper() in KEYWORDS:
        raise BusinessError(
            E_SQL_UNNORMALIZABLE,
            "SQL cannot be normalized",
            {"reason": "missing table name after FROM", "token": tokens[from_index + 1].text},
        )
    after_table = from_index + 2
    if after_table < len(tokens):
        next_text = tokens[after_table].text.upper()
        if tokens[after_table].kind == "word" and next_text in {"JOIN", "LEFT", "RIGHT", "INNER", "OUTER", "UNION"}:
            raise BusinessError(
                E_SQL_UNNORMALIZABLE,
                "SQL cannot be normalized",
                {"reason": "joins and unions are not supported", "table": table},
            )

    where_index = -1
    end_index = len(tokens)
    depth = 0
    for index in range(after_table, len(tokens)):
        token = tokens[index]
        if token.text == "(":
            depth += 1
        elif token.text == ")":
            depth -= 1
        elif depth == 0 and token.kind == "word":
            upper = token.text.upper()
            if upper == "WHERE" and where_index < 0:
                where_index = index
            elif upper in {"GROUP", "ORDER", "LIMIT", "HAVING"}:
                end_index = index
                break
    predicates: list[dict[str, Any]] = []
    if where_index >= 0:
        where_tokens: list[Token] = []
        depth = 0
        for token in tokens[where_index + 1 : end_index]:
            if token.text == "(":
                depth += 1
            elif token.text == ")":
                depth -= 1
            if depth == 0 and token.kind == "word" and token.text.upper() in {"OR", "UNION"}:
                raise BusinessError(
                    E_SQL_UNNORMALIZABLE,
                    "SQL cannot be normalized",
                    {"reason": "OR and UNION predicates are not supported", "table": table},
                )
            where_tokens.append(token)
        for part in _split_top_level(where_tokens, "AND"):
            if not part:
                raise BusinessError(E_SQL_UNNORMALIZABLE, "SQL cannot be normalized", {"reason": "empty predicate"})
            if len(part) < 3 or part[0].kind != "word":
                raise BusinessError(
                    E_SQL_UNNORMALIZABLE,
                    "SQL cannot be normalized",
                    {"reason": "unsupported predicate", "table": table},
                )
            column = part[0].text.lower()
            if column.upper() in KEYWORDS:
                raise BusinessError(
                    E_SQL_UNNORMALIZABLE,
                    "SQL cannot be normalized",
                    {"reason": "invalid predicate column", "token": part[0].text},
                )
            op_index = 1
            if part[1].text == "." and len(part) >= 4 and part[2].kind == "word":
                column = part[2].text.lower()
                op_index = 3
            if op_index >= len(part):
                raise BusinessError(E_SQL_UNNORMALIZABLE, "SQL cannot be normalized", {"reason": "missing operator"})
            operator = part[op_index].text.upper()
            if operator == "IN":
                if op_index + 2 >= len(part) or part[op_index + 1].text != "(" or part[-1].text != ")":
                    raise BusinessError(
                        E_SQL_UNNORMALIZABLE,
                        "SQL cannot be normalized",
                        {"reason": "invalid IN predicate", "column": column},
                    )
            elif operator == "IS":
                if op_index + 1 >= len(part):
                    raise BusinessError(E_SQL_UNNORMALIZABLE, "SQL cannot be normalized", {"reason": "invalid IS predicate"})
                operator = "IS"
            elif operator not in {"=", "<", ">", "<=", ">=", "<>", "!=", "LIKE"}:
                raise BusinessError(
                    E_SQL_UNNORMALIZABLE,
                    "SQL cannot be normalized",
                    {"reason": "unsupported predicate operator", "operator": operator, "column": column},
                )
            predicates.append(
                {
                    "column": column,
                    "op": operator,
                    "kind": "in" if operator == "IN" else "value",
                }
            )

    order_by: list[dict[str, Any]] = []
    order_start = -1
    depth = 0
    for index, token in enumerate(tokens):
        if token.text == "(":
            depth += 1
        elif token.text == ")":
            depth -= 1
        elif depth == 0 and token.kind == "word" and token.text.upper() == "ORDER":
            order_start = index
            break
    if order_start >= 0 and order_start + 2 < len(tokens) and tokens[order_start + 1].text.upper() == "BY":
        order_tokens = tokens[order_start + 2 :]
        current: list[Token] = []
        for token in order_tokens:
            if token.text == ",":
                if current and current[0].kind == "word":
                    direction = "desc" if any(t.text.upper() == "DESC" for t in current) else "asc"
                    order_by.append({"column": current[0].text.lower(), "direction": direction})
                current = []
            else:
                current.append(token)
        if current and current[0].kind == "word":
            direction = "desc" if any(t.text.upper() == "DESC" for t in current) else "asc"
            order_by.append({"column": current[0].text.lower(), "direction": direction})
    return {
        "statement": "select",
        "table": table,
        "predicates": predicates,
        "order_by": order_by,
        "has_limit": any(token.kind == "word" and token.text.upper() == "LIMIT" for token in tokens),
    }


@dataclass(frozen=True)
class NormalizedQuery:
    template_sql: str
    template_id: str
    structure: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "template_sql": self.template_sql,
            "template_id": self.template_id,
            "structure": self.structure,
        }


def normalize_sql(sql: Any) -> NormalizedQuery:
    if not isinstance(sql, str) or not sql.strip():
        raise BusinessError(E_SQL_UNNORMALIZABLE, "SQL cannot be normalized", {"reason": "empty SQL"})
    sql = sql.strip()
    tokens = _tokenize(sql)
    if not tokens:
        raise BusinessError(E_SQL_UNNORMALIZABLE, "SQL cannot be normalized", {"reason": "empty SQL"})
    if tokens[-1].text == ";":
        tokens = tokens[:-1]
    if any(token.text == ";" for token in tokens):
        raise BusinessError(E_SQL_UNNORMALIZABLE, "SQL cannot be normalized", {"reason": "multiple statements"})
    depth = 0
    for token in tokens:
        if token.text == "(":
            depth += 1
        elif token.text == ")":
            depth -= 1
            if depth < 0:
                raise BusinessError(E_SQL_UNNORMALIZABLE, "SQL cannot be normalized", {"reason": "unbalanced parentheses"})
    if depth != 0:
        raise BusinessError(E_SQL_UNNORMALIZABLE, "SQL cannot be normalized", {"reason": "unbalanced parentheses"})
    tokens = _strip_unary_numeric_signs(tokens)
    tokens = _collapse_placeholders(tokens)
    template_sql = _render(tokens)
    structure = _parse_structure(tokens, template_sql)
    template_id = "tpl_" + sha256_text(template_sql)[:20]
    return NormalizedQuery(template_sql=template_sql, template_id=template_id, structure=structure)
