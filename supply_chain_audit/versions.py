"""Canonical artifact keys and deterministic version-expression parsing."""

from __future__ import annotations

import re
from dataclasses import dataclass

from .codes import E_CANONICAL_KEY, E_CONSTRAINT_SYNTAX, E_VERSION_SYNTAX

_NAME_PART = r"[a-z0-9][a-z0-9._-]*"
NAME_RE = re.compile(rf"^{_NAME_PART}(?:/{_NAME_PART})*$")
VERSION_RE = re.compile(
    r"^v?(?P<major>0|[1-9]\d*)\.(?P<minor>0|[1-9]\d*)\.(?P<patch>0|[1-9]\d*)"
    r"(?:-(?P<prerelease>[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
    r"(?:\+(?P<build>[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$"
)
WILDCARD_RE = re.compile(
    r"^v?(?P<major>\d+)(?:\.(?P<minor>\d+|[xX*]))?(?:\.(?P<patch>\d+|[xX*]))?$"
)
CONSTRAINT_CLAUSE_RE = re.compile(
    r"^\s*(?P<op>==|!=|>=|<=|=|>|<|\^|~)?\s*(?P<version>.+?)\s*$"
)


@dataclass(frozen=True)
class Version:
    major: int
    minor: int
    patch: int
    prerelease: str | None = None
    build: str | None = None

    @property
    def core(self) -> tuple[int, int, int]:
        return self.major, self.minor, self.patch

    def __str__(self) -> str:
        text = f"{self.major}.{self.minor}.{self.patch}"
        if self.prerelease:
            text += f"-{self.prerelease}"
        if self.build:
            text += f"+{self.build}"
        return text


def normalize_name(name: object) -> str:
    if not isinstance(name, str):
        return ""
    return name.strip().lower()


def canonical_key(name: object, version: object) -> str:
    return f"{normalize_name(name)}@{str(version).strip()}"


def valid_name(name: str) -> bool:
    return bool(NAME_RE.fullmatch(name))


def split_key(key: object) -> tuple[str | None, str | None]:
    if not isinstance(key, str) or "@" not in key:
        return None, None
    name, version = key.rsplit("@", 1)
    name = normalize_name(name)
    version = version.strip()
    if not name or not version:
        return None, None
    return name, version


def validate_canonical_key(key: object) -> tuple[bool, str | None]:
    if not isinstance(key, str) or "@" not in key:
        return False, E_CANONICAL_KEY
    raw_name, raw_version = key.rsplit("@", 1)
    name = normalize_name(raw_name)
    version = raw_version.strip()
    if raw_name != name or raw_version != version:
        return False, E_CANONICAL_KEY
    if not valid_name(name):
        return False, E_CANONICAL_KEY
    if not valid_exact_version(version):
        return False, E_VERSION_SYNTAX
    return True, None


def parse_exact_version(text: str) -> Version | None:
    match = VERSION_RE.fullmatch(text.strip())
    if not match:
        return None
    return Version(
        major=int(match.group("major")),
        minor=int(match.group("minor")),
        patch=int(match.group("patch")),
        prerelease=match.group("prerelease"),
        build=match.group("build"),
    )


def valid_exact_version(text: object) -> bool:
    return isinstance(text, str) and parse_exact_version(text) is not None


def _valid_version_token(token: str) -> bool:
    token = token.strip()
    if token in {"*", "x", "X"}:
        return True
    if parse_exact_version(token) is not None:
        return True
    match = WILDCARD_RE.fullmatch(token)
    if not match:
        return False
    # A wildcard segment may only occur at the first omitted/wildcard position.
    minor = match.group("minor")
    patch = match.group("patch")
    if minor in {"x", "X", "*"} and patch is not None and patch not in {"x", "X", "*"}:
        return False
    return True


def validate_constraint_expression(expression: object) -> tuple[bool, str | None]:
    """Validate comma-AND and ``||``-OR version constraint expressions."""

    if not isinstance(expression, str) or not expression.strip():
        return False, E_CONSTRAINT_SYNTAX
    for alternative in expression.split("||"):
        clauses = [clause.strip() for clause in alternative.split(",") if clause.strip()]
        if not clauses:
            return False, E_CONSTRAINT_SYNTAX
        for clause in clauses:
            match = CONSTRAINT_CLAUSE_RE.fullmatch(clause)
            if not match:
                return False, E_CONSTRAINT_SYNTAX
            version = match.group("version").strip()
            if not _valid_version_token(version):
                return False, E_CONSTRAINT_SYNTAX
    return True, None
