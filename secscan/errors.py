"""Error types and exit codes used by the CLI."""

from __future__ import annotations


class SecscanError(Exception):
    """Base class for expected, user-facing failures."""

    exit_code = 1


class ConfigError(SecscanError):
    """The configuration file is missing, unreadable or not valid JSON."""

    exit_code = 3


class RulesError(SecscanError):
    """The optional rules file exists but cannot be used."""

    exit_code = 4


class OutputError(SecscanError):
    """The report or fixed configuration cannot be written safely."""

    exit_code = 5


class VerificationError(SecscanError):
    """Post-fix re-scan still reports fixable issues."""

    exit_code = 6
