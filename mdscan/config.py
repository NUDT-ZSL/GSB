"""Loading and validation of the JSON rule configuration."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Python's re module behaves differently for patterns that can match the empty
# string in some constructs; also guard against catastrophic backtracking by
# limiting the compiled pattern size.  These are sanity limits, not a sandbox.
MAX_RULES = 1000
MAX_PATTERN_LENGTH = 4096


class ConfigError(Exception):
    """Raised when the config file is missing, unreadable or invalid."""


@dataclass(frozen=True)
class Rule:
    """A single detection rule.

    ``keywords`` are stored already case-folded, so a document keyword matches
    case-insensitively.  ``patterns`` are compiled regular expressions matched
    against every text view of the document.
    """

    name: str
    keywords: tuple[str, ...] = ()
    patterns: tuple[re.Pattern[str], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "keywords": list(self.keywords),
            "patterns": [p.pattern for p in self.patterns],
        }


# --------------------------------------------------------------------------- #
# JSON schemas accepted
# --------------------------------------------------------------------------- #
#
# Canonical form: a JSON object with a "rules" array::
#
#     {"rules": [{"name": "...", "keywords": ["eval"], "patterns": ["(?i)ignore "]}]}
#
# A bare JSON array of rules is also accepted for convenience.


def _expect_list(value: Any, label: str) -> list[Any]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ConfigError(f"{label} must be a JSON array, got {type(value).__name__}")
    return value


def _expect_str_list(value: Any, label: str) -> list[str]:
    items = _expect_list(value, label)
    out: list[str] = []
    for index, item in enumerate(items, start=1):
        if not isinstance(item, str):
            raise ConfigError(f"{label}[{index}] must be a string, got {type(item).__name__}")
        if not item:
            raise ConfigError(f"{label}[{index}] must not be empty")
        out.append(item)
    return out


def _compile_rule(raw: Any, index: int) -> Rule:
    label = f"rules[{index}]"
    if not isinstance(raw, dict):
        raise ConfigError(f"{label} must be a JSON object, got {type(raw).__name__}")

    name = raw.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ConfigError(f"{label}.name is required and must be a non-empty string")
    name = name.strip()

    keywords = _expect_str_list(raw.get("keywords"), f"{label}.keywords")
    pattern_sources = _expect_str_list(raw.get("patterns"), f"{label}.patterns")

    if not keywords and not pattern_sources:
        raise ConfigError(f"{label} ('{name}') has neither keywords nor patterns")

    compiled: list[re.Pattern[str]] = []
    for pattern_index, source in enumerate(pattern_sources, start=1):
        if len(source) > MAX_PATTERN_LENGTH:
            raise ConfigError(
                f"{label}.patterns[{pattern_index}] exceeds {MAX_PATTERN_LENGTH} characters"
            )
        try:
            compiled.append(re.compile(source))
        except re.error as exc:
            raise ConfigError(
                f"{label}.patterns[{pattern_index}] is not a valid regular expression: {exc}"
            ) from exc

    # Case-folding keeps matching case-insensitive; duplicates are collapsed
    # so a report never lists the same rule twice.
    folded = tuple(dict.fromkeys(k.casefold() for k in keywords))
    return Rule(name=name, keywords=folded, patterns=tuple(compiled))


def parse_config(document: Any) -> list[Rule]:
    """Validate an already-parsed JSON document and return the rules."""
    if isinstance(document, dict):
        raw_rules = document.get("rules")
        if raw_rules is None:
            raise ConfigError('config object must contain a "rules" array')
        raw_rules = _expect_list(raw_rules, '"rules"')
    elif isinstance(document, list):
        raw_rules = document
    else:
        raise ConfigError(
            "config root must be a JSON object with a \"rules\" array or a JSON array"
        )

    if len(raw_rules) > MAX_RULES:
        raise ConfigError(f"too many rules ({len(raw_rules)} > {MAX_RULES})")

    rules = [_compile_rule(raw, index) for index, raw in enumerate(raw_rules)]
    if not rules:
        raise ConfigError("config contains no rules")

    names = [rule.name for rule in rules]
    duplicates = sorted({n for n in names if names.count(n) > 1})
    if duplicates:
        raise ConfigError("duplicate rule name(s): " + ", ".join(duplicates))

    return rules


def load_config(path: os.PathLike[str] | str) -> list[Rule]:
    """Read and validate the rule configuration file.

    Raises :class:`ConfigError` for a missing file, undecodable bytes, invalid
    JSON or a structurally invalid rule set.
    """
    config_path = Path(path)
    try:
        raw = config_path.read_bytes()
    except FileNotFoundError as exc:
        raise ConfigError(f"config file not found: {config_path}") from exc
    except IsADirectoryError as exc:
        raise ConfigError(f"config path is a directory: {config_path}") from exc
    except PermissionError as exc:
        raise ConfigError(f"config file is not readable: {config_path} ({exc})") from exc
    except OSError as exc:
        raise ConfigError(f"cannot read config file: {config_path} ({exc})") from exc

    # Deliberately strict: a UTF-8 BOM is tolerated, anything else that is not
    # valid UTF-8 is reported instead of silently guessed.
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ConfigError(f"config file is not valid UTF-8: {config_path} ({exc})") from exc

    try:
        document = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ConfigError(
            f"config file is not valid JSON: {config_path} "
            f"(line {exc.lineno}, column {exc.colno}: {exc.msg})"
        ) from exc

    return parse_config(document)
