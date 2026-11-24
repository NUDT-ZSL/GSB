"""Deterministic semantic-version subset and constraint evaluation."""

from __future__ import annotations

import re
from dataclasses import dataclass

from .codes import E_CONSTRAINT_SYNTAX, E_VERSION_SYNTAX
from .model import AuditError


_VERSION_RE = re.compile(
    r"^v?(?P<major>\d+)\.(?P<minor>\d+)(?:\.(?P<patch>\d+))?"
    r"(?:-(?P<pre>[0-9A-Za-z.-]+))?(?:\+(?P<build>[0-9A-Za-z.-]+))?$"
)


@dataclass(frozen=True, order=True)
class Version:
    major: int
    minor: int
    patch: int
    prerelease: tuple[str | int, ...] = ()

    def __str__(self) -> str:
        text = f"{self.major}.{self.minor}.{self.patch}"
        if self.prerelease:
            text += "-" + ".".join(str(p) for p in self.prerelease)
        return text


def _prerelease_key(part: str) -> tuple[int, object]:
    if part.isdigit():
        return (0, int(part))
    return (1, part)


def parse_version(text: object) -> Version:
    if not isinstance(text, str):
        raise AuditError(E_VERSION_SYNTAX, f"version must be a string, got {type(text).__name__}")
    match = _VERSION_RE.match(text.strip())
    if not match:
        raise AuditError(E_VERSION_SYNTAX, f"invalid version expression: {text!r}", "version")
    pre: tuple[str | int, ...] = ()
    if match.group("pre"):
        pre = tuple(int(p) if p.isdigit() else p for p in match.group("pre").split("."))
    return Version(
        int(match.group("major")),
        int(match.group("minor")),
        int(match.group("patch") or 0),
        pre,
    )


_COMPARATORS = ("<=", ">=", "==", "=", "<", ">", "^", "~")
_TOKEN_RE = re.compile(
    r"^\s*(?P<op><=|>=|==|=|<|>|\^|~)?\s*"
    r"(?P<ver>v?(?:\d+|[xX*])(?:\.(?:\d+|[xX*])){0,2}"
    r"(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?|\*)\s*$"
)


@dataclass(frozen=True)
class Constraint:
    raw: str
    clauses: tuple[tuple[str, Version | None], ...]


def parse_constraint(text: object) -> Constraint:
    if text is None or text == "":
        return Constraint(raw="*", clauses=(("*", None),))
    if not isinstance(text, str):
        raise AuditError(E_CONSTRAINT_SYNTAX, "constraint must be a string", "constraint")
    raw = text.strip()
    if raw == "*":
        return Constraint(raw=raw, clauses=(("*", None),))
    clauses: list[tuple[str, Version | None]] = []
    for piece in re.split(r",|\s+", raw):
        if not piece:
            continue
        match = _TOKEN_RE.match(piece)
        if not match:
            raise AuditError(E_CONSTRAINT_SYNTAX, f"invalid version constraint: {text!r}", "constraint")
        op = match.group("op") or "="
        ver_text = match.group("ver")
        if ver_text.strip().lower() in {"*", "x"}:
            clauses.append(("*", None))
            continue
        wildcard_parts = ver_text.lstrip("vV").split(".")
        if any(part.lower() in {"x", "*"} for part in wildcard_parts):
            numeric = [int(part) for part in wildcard_parts if part.isdigit()]
            if not numeric or len(numeric) != next(i for i, p in enumerate(wildcard_parts) if not p.isdigit()):
                raise AuditError(E_CONSTRAINT_SYNTAX, f"invalid wildcard constraint: {text!r}", "constraint")
            lower = Version(*(numeric + [0, 0][: 3 - len(numeric)]))
            if len(numeric) == 1:
                upper = Version(lower.major + 1, 0, 0)
            else:
                upper = Version(lower.major, lower.minor + 1, 0)
            clauses.append((">=", lower))
            clauses.append(("<", upper))
        else:
            version = parse_version(ver_text)
            clauses.append((op, version))
    if not clauses:
        raise AuditError(E_CONSTRAINT_SYNTAX, f"empty version constraint: {text!r}", "constraint")
    return Constraint(raw=raw, clauses=tuple(clauses))


def _compare(a: Version, b: Version) -> int:
    if a.major != b.major:
        return -1 if a.major < b.major else 1
    if a.minor != b.minor:
        return -1 if a.minor < b.minor else 1
    if a.patch != b.patch:
        return -1 if a.patch < b.patch else 1
    if a.prerelease == b.prerelease:
        return 0
    if not a.prerelease:
        return 1
    if not b.prerelease:
        return -1
    for ap, bp in zip(a.prerelease, b.prerelease):
        a_num = isinstance(ap, int)
        b_num = isinstance(bp, int)
        if a_num and b_num and ap != bp:
            return -1 if ap < bp else 1
        if a_num and not b_num:
            return -1
        if not a_num and b_num:
            return 1
        if ap != bp:
            assert isinstance(ap, str) and isinstance(bp, str)
            return -1 if ap < bp else 1
    return len(a.prerelease) - len(b.prerelease)


def _satisfies_clause(version: Version, op: str, expected: Version | None) -> bool:
    if op == "*" or expected is None:
        return True
    cmp_result = _compare(version, expected)
    if op in ("=", "=="):
        return cmp_result == 0
    if op == ">=":
        return cmp_result >= 0
    if op == "<=":
        return cmp_result <= 0
    if op == ">":
        return cmp_result > 0
    if op == "<":
        return cmp_result < 0
    if op == "~":
        lower = expected
        upper = Version(expected.major, expected.minor + 1, 0)
        return _compare(version, lower) >= 0 and _compare(version, upper) < 0
    if op == "^":
        lower = expected
        if expected.major > 0:
            upper = Version(expected.major + 1, 0, 0)
        elif expected.minor > 0:
            upper = Version(0, expected.minor + 1, 0)
        else:
            upper = Version(0, 0, expected.patch + 1)
        return _compare(version, lower) >= 0 and _compare(version, upper) < 0
    raise AuditError(E_CONSTRAINT_SYNTAX, f"unsupported operator {op}")


def satisfies(version_text: str | None, constraint_text: object) -> bool:
    if version_text is None:
        return False
    version = parse_version(version_text)
    constraint = parse_constraint(constraint_text)
    return all(_satisfies_clause(version, op, ver) for op, ver in constraint.clauses)
