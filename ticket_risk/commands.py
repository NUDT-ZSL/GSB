"""Interactive command parser with a small, documented syntax."""

from __future__ import annotations

import shlex
from dataclasses import dataclass, field
from typing import Any

from .errors import E_COMMAND_INVALID, E_COMMAND_UNKNOWN
from .loader import RISK_LEVELS


WEIGHT_ALIASES = {
    "sentiment_scale": "lexicon_weight_scale",
    "lexicon_weight_scale": "lexicon_weight_scale",
    "risk_scale": "risk_weight_scale",
    "risk_weight_scale": "risk_weight_scale",
    "position_decay": "position_decay",
    "fusion_temperature": "fusion_temperature",
}

EDIT_COMMANDS = {
    "set_weight",
    "set_threshold",
    "set_lexicon",
    "set_risk_keyword",
    "set_degree",
    "set_risk_level_weight",
    "set_negation_window",
}

CONTROL_COMMANDS = {
    "help",
    "status",
    "wait",
    "verify",
    "snapshot",
    "report",
    "quit",
}


@dataclass(frozen=True)
class ParsedCommand:
    raw: str
    kind: str
    args: tuple[Any, ...] = field(default_factory=tuple)


def _parse_float(value: str, label: str) -> float:
    try:
        return float(value)
    except ValueError as exc:
        raise ValueError(f"{label} must be a number: {value!r}") from exc


def parse_command(line: str) -> ParsedCommand | None:
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return None
    try:
        tokens = shlex.split(stripped, comments=True, posix=True)
    except ValueError as exc:
        raise ValueError(f"{E_COMMAND_INVALID}: {exc}") from exc
    if not tokens:
        return None
    kind = tokens[0].lower()
    args = tokens[1:]
    if kind in {"quit", "exit", "help", "status", "wait", "verify", "snapshot", "report"}:
        if kind == "exit":
            kind = "quit"
        if args:
            raise ValueError(f"E_COMMAND_INVALID: {kind} takes no arguments")
        return ParsedCommand(raw=stripped, kind=kind)
    if kind == "set_weight":
        if len(args) != 2:
            raise ValueError("E_COMMAND_INVALID: set_weight <name> <value>")
        name = args[0]
        if name not in WEIGHT_ALIASES:
            raise ValueError(f"E_COMMAND_INVALID: unknown weight name: {name}")
        return ParsedCommand(stripped, kind, (WEIGHT_ALIASES[name], _parse_float(args[1], "value")))
    if kind == "set_threshold":
        if len(args) != 2:
            raise ValueError("E_COMMAND_INVALID: set_threshold <level> <value>")
        level = args[0]
        if level not in RISK_LEVELS:
            raise ValueError(f"E_COMMAND_INVALID: unknown risk level: {level}")
        return ParsedCommand(stripped, kind, (level, _parse_float(args[1], "threshold")))
    if kind == "set_lexicon":
        if len(args) != 2:
            raise ValueError("E_COMMAND_INVALID: set_lexicon <word> <weight>")
        return ParsedCommand(stripped, kind, (args[0], _parse_float(args[1], "weight")))
    if kind == "set_risk_keyword":
        if len(args) != 2:
            raise ValueError("E_COMMAND_INVALID: set_risk_keyword <keyword> <weight>")
        return ParsedCommand(stripped, kind, (args[0], _parse_float(args[1], "weight")))
    if kind == "set_degree":
        if len(args) != 2:
            raise ValueError("E_COMMAND_INVALID: set_degree <word> <factor>")
        return ParsedCommand(stripped, kind, (args[0], _parse_float(args[1], "factor")))
    if kind == "set_risk_level_weight":
        if len(args) != 2:
            raise ValueError("E_COMMAND_INVALID: set_risk_level_weight <level> <weight>")
        if args[0] not in RISK_LEVELS:
            raise ValueError(f"E_COMMAND_INVALID: unknown risk level: {args[0]}")
        return ParsedCommand(stripped, kind, (args[0], _parse_float(args[1], "weight")))
    if kind == "set_negation_window":
        if len(args) != 1:
            raise ValueError("E_COMMAND_INVALID: set_negation_window <integer>")
        try:
            value = int(args[0])
        except ValueError as exc:
            raise ValueError("E_COMMAND_INVALID: negation window must be an integer") from exc
        return ParsedCommand(stripped, kind, (value,))
    raise ValueError(f"{E_COMMAND_UNKNOWN}: unknown command: {kind}")


HELP_TEXT = """命令:
  set_weight <sentiment_scale|risk_scale|position_decay|fusion_temperature> <value>
  set_threshold <一般|催办|投诉|风险> <value>
  set_lexicon <词> <weight>
  set_risk_keyword <关键词> <weight>
  set_degree <程度词> <factor>
  set_risk_level_weight <等级> <weight>
  set_negation_window <整数>
  status | wait | verify | snapshot | report | help | quit
空行与 # 开头行被忽略。"""

