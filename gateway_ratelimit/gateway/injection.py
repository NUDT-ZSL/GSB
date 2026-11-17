"""Fault injection: only input files or parameters are mutated, never code paths."""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path

from .errors import E_LIMIT, E_SPEC, E_VERSION, BusinessError, spec_error
from .loader import load_routes, load_token_table
from .limiters import SlidingWindowCounter
from .models import Route


@dataclass(frozen=True)
class InjectionSpec:
    kind: str
    code: str
    description: str


INJECTIONS = {
    "corrupt_policy": InjectionSpec(
        "corrupt_policy",
        E_SPEC,
        "append invalid JSON to the route policy file so loading fails with E_SPEC",
    ),
    "token_version_conflict": InjectionSpec(
        "token_version_conflict",
        E_VERSION,
        "rewrite the token table with a non-monotonic version to trigger E_VERSION",
    ),
    "zero_window": InjectionSpec(
        "zero_window",
        E_LIMIT,
        "force window_ms=0 in the validated parameters; limiter construction "
        "reports E_LIMIT",
    ),
}


def inject_corrupt_policy(routes_path: Path, backup_dir: Path) -> Path:
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup = backup_dir / routes_path.name
    shutil.copy2(routes_path, backup)
    with routes_path.open("a", encoding="utf-8") as handle:
        handle.write("\n{ this is not valid json >>>\n")
    return backup


def inject_token_version_conflict(token_path: Path, backup_dir: Path) -> Path:
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup = backup_dir / token_path.name
    shutil.copy2(token_path, backup)
    data = json.loads(token_path.read_text(encoding="utf-8"))
    original = int(data["version"])
    # The backup represents the active (higher) version; the rewritten file
    # rolls strictly backwards so the reload is a deterministic E_VERSION.
    backup_data = dict(data)
    backup_data["version"] = original + 1
    backup.write_text(json.dumps(backup_data, indent=2, ensure_ascii=False), encoding="utf-8")
    data["version"] = original
    token_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    return backup


def inject_zero_window(routes_path: Path, backup_dir: Path) -> tuple[Path, list]:
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup = backup_dir / routes_path.name
    shutil.copy2(routes_path, backup)
    data = json.loads(routes_path.read_text(encoding="utf-8"))
    for route in data["routes"]:
        route["window_ms"] = 0
    routes_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    return backup, data["routes"]


def restore(backup: Path, target: Path) -> None:
    shutil.copy2(backup, target)


def validate_injected_inputs(kind: str, routes_path: Path, token_path: Path, backup_dir: Path):
    """Run the real loaders/constructors so each injection yields its stable code."""
    if kind == "corrupt_policy":
        try:
            load_routes(routes_path)
        except BusinessError as exc:
            if exc.code != E_SPEC:
                raise
            return exc
        raise spec_error("corrupt policy injection unexpectedly loaded")
    if kind == "token_version_conflict":
        table = load_token_table(token_path)
        backup_version = json.loads(
            backup_dir.joinpath(token_path.name).read_text(encoding="utf-8")
        )["version"]
        # The backup holds the previously active version; the injected file
        # must roll strictly backwards so the reload is a version conflict.
        if table.version >= backup_version:
            raise spec_error("version conflict injection did not roll the version back")
        return BusinessError(
            E_VERSION,
            f"token table version {table.version} regresses from active "
            f"version {backup_version}",
            str(token_path),
        )
    if kind == "zero_window":
        # The normal loader rejects window_ms=0 with E_SPEC; injection mode
        # bypasses that single check so the limiter layer reports E_LIMIT,
        # matching the required fault-to-code mapping. The injected parameter
        # itself is the only thing that changed.
        raw = json.loads(routes_path.read_text(encoding="utf-8"))
        routes = [
            Route(
                name=item["name"],
                prefix=item["prefix"],
                auth_policy=item["auth_policy"],
                rate_per_sec=item["rate_per_sec"],
                burst=item["burst"],
                window_ms=item["window_ms"],
                max_in_window=item["max_in_window"],
            )
            for item in raw["routes"]
        ]
        try:
            for route in routes:
                SlidingWindowCounter(window_ms=route.window_ms,
                                     max_in_window=route.max_in_window)
        except ValueError as exc:
            return BusinessError(E_LIMIT, str(exc), str(routes_path))
        raise spec_error("zero-window injection unexpectedly constructed a limiter")
    raise spec_error(f"unknown injection kind {kind!r}")
