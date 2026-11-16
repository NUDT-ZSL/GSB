"""Archive loading and strong validation.

Loading is all-or-nothing: every schema / reference problem found across every
input file is collected, and if any exists a single :class:`ConfigError`
(``E_CONFIG``) is raised carrying *all* error positions.  No partially loaded
archive is ever returned.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

from .errors import ConfigError
from .fixedpoint import parse_decimal
from .models import Archive, ConfigItem, Formula
from .schema import (
    ACCEPTED_NUMBER_TYPES,
    SOURCE_DERIVED,
    SUPPORTED_SOURCES,
    SUPPORTED_UNITS,
    bounds_for,
    is_valid_key,
)


class _Collector:
    def __init__(self) -> None:
        self.errors: list[str] = []

    def fail(self, location: str, message: str) -> None:
        self.errors.append(f"{location}: {message}")

    def check(self, condition: bool, location: str, message: str) -> bool:
        if not condition:
            self.fail(location, message)
        return condition

    @property
    def failed(self) -> bool:
        return bool(self.errors)


def load_archives(paths: list[str | Path]) -> list[Archive]:
    """Load and strongly validate a deterministic, sorted list of files."""
    collector = _Collector()
    archives: list[Archive] = []
    seen_nodes: set[str] = set()

    for path in sorted(Path(p) for p in paths):
        root = str(path)
        raw = _read_json(path, root, collector)
        if raw is None:
            continue
        archive = _build_archive(raw, root, collector, seen_nodes)
        if archive is not None:
            archives.append(archive)

    if not collector.failed:
        _cross_archive_checks(archives, collector)

    if collector.failed:
        raise ConfigError(
            "archive validation failed; whole batch rejected",
            locations=collector.errors,
        )
    return archives


def _read_json(path: Path, root: str, col: _Collector):
    if not path.exists():
        col.fail(root, "file not found")
        return None
    try:
        with path.open("r", encoding="utf-8") as fh:
            return json.load(fh, parse_float=Decimal, parse_int=Decimal)
    except json.JSONDecodeError as exc:
        col.fail(root, f"invalid JSON at line {exc.lineno} col {exc.colno}: {exc.msg}")
        return None
    except OSError as exc:  # pragma: no cover - filesystem edge case
        col.fail(root, f"unreadable file: {exc}")
        return None


def _build_archive(raw, root: str, col: _Collector, seen_nodes: set[str]):
    if not isinstance(raw, dict):
        col.fail(root, "top-level JSON must be an object")
        return None

    node_id = raw.get("node_id")
    version = raw.get("version")
    col.check(isinstance(node_id, str) and bool(node_id),
              f"{root}#/node_id", "missing or non-string node_id")
    col.check(isinstance(version, str) and bool(version),
              f"{root}#/version", "missing or non-string version")
    if isinstance(node_id, str):
        if node_id in seen_nodes:
            col.fail(f"{root}#/node_id",
                     f"duplicate node_id {node_id!r}")
        seen_nodes.add(node_id)

    raw_items = raw.get("items")
    if not col.check(isinstance(raw_items, list), f"{root}#/items",
                     "items must be a list"):
        return None

    items: dict[str, ConfigItem] = {}
    for idx, entry in enumerate(raw_items):
        loc = f"{root}#/items/{idx}"
        item = _build_item(entry, loc, col)
        if item is None:
            continue
        if item.key in items:
            col.fail(f"{loc}/key",
                     f"duplicate key {item.key!r} within archive")
            continue
        items[item.key] = item

    # Derived upstream keys must exist within the same archive.
    for key, item in items.items():
        if item.source == SOURCE_DERIVED and item.formula is not None:
            for up in item.formula.upstream:
                if up not in items:
                    col.fail(
                        f"{item.location}/formula",
                        f"derived key {key!r} references missing upstream "
                        f"key {up!r}",
                    )
    if col.failed:
        return None
    return Archive(node_id=node_id, version=version, items=items)


def _build_item(entry, loc: str, col: _Collector) -> ConfigItem | None:
    if not isinstance(entry, dict):
        col.fail(loc, "item must be an object")
        return None
    start_errors = len(col.errors)

    key = entry.get("key")
    unit = entry.get("unit")
    source = entry.get("source")
    key_ok = col.check(isinstance(key, str) and is_valid_key(key),
                       f"{loc}/key", f"illegal key {key!r}")
    col.check(unit in SUPPORTED_UNITS, f"{loc}/unit",
              f"unsupported unit {unit!r}; expected one of "
              f"{sorted(SUPPORTED_UNITS)}")
    col.check(source in SUPPORTED_SOURCES, f"{loc}/source",
              f"unsupported source {source!r}")

    bounds = _parse_bounds(entry.get("bounds"), unit, loc, col)
    formula = _parse_formula(entry.get("formula"), loc, col)
    value = entry.get("value", None)

    if source == SOURCE_DERIVED:
        if formula is None:
            col.fail(f"{loc}/formula", "derived item requires a formula")
        if "value" in entry:
            col.fail(f"{loc}/value",
                     "derived item must not carry an explicit value")
    elif source == "manual":
        # Only numeric type here; numeric range is enforced post-quantization
        # via E_RANGE so derived items are covered too.
        col.check(not isinstance(value, bool)
                  and isinstance(value, ACCEPTED_NUMBER_TYPES),
                  f"{loc}/value",
                  f"value must be a JSON number, got {type(value).__name__}")
        if formula is not None:
            col.fail(f"{loc}/formula", "manual item must not carry a formula")
    else:
        # Unsupported source already flagged; avoid further type assumptions.
        pass

    if len(col.errors) > start_errors:
        return None

    safe_key = key if key_ok else str(key)
    return ConfigItem(
        key=safe_key,
        value=parse_decimal(value) if source == "manual" else None,
        unit=unit,
        source=source,
        formula=formula,
        bounds=bounds if bounds is not None else bounds_for(unit),
        location=loc,
    )


def _parse_bounds(raw, unit, loc: str, col: _Collector):
    if raw is None:
        return bounds_for(unit) if unit in SUPPORTED_UNITS else None
    if not isinstance(raw, dict):
        col.fail(f"{loc}/bounds", "bounds must be an object with min/max")
        return None
    lo, hi = raw.get("min"), raw.get("max")
    if not (col.check(not isinstance(lo, bool)
                      and isinstance(lo, ACCEPTED_NUMBER_TYPES),
                      f"{loc}/bounds/min", "bounds.min must be a number")
            and col.check(not isinstance(hi, bool)
                          and isinstance(hi, ACCEPTED_NUMBER_TYPES),
                          f"{loc}/bounds/max",
                          "bounds.max must be a number")):
        return None
    lo_d, hi_d = parse_decimal(lo), parse_decimal(hi)
    if lo_d > hi_d:
        col.fail(f"{loc}/bounds", f"inverted bounds {lo_d} > {hi_d}")
        return None
    return lo_d, hi_d


def _parse_formula(raw, loc: str, col: _Collector) -> Formula | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        col.fail(f"{loc}/formula", "formula must be an object")
        return None
    coefficients = raw.get("coefficients")
    constant = raw.get("constant", 0)
    if not col.check(isinstance(coefficients, dict) and bool(coefficients),
                     f"{loc}/formula/coefficients",
                     "coefficients must be a non-empty object"):
        return None
    if not col.check(not isinstance(constant, bool)
                     and isinstance(constant, ACCEPTED_NUMBER_TYPES),
                     f"{loc}/formula/constant",
                     "constant must be a number"):
        return None

    coefs: dict[str, Decimal] = {}
    upstream: list[str] = []
    for up_key, coef in coefficients.items():
        if not col.check(isinstance(up_key, str) and is_valid_key(up_key),
                         f"{loc}/formula/coefficients",
                         f"illegal upstream key {up_key!r}"):
            continue
        if not col.check(not isinstance(coef, bool)
                         and isinstance(coef, ACCEPTED_NUMBER_TYPES),
                         f"{loc}/formula/coefficients/{up_key}",
                         "coefficient must be a number"):
            continue
        coefs[up_key] = parse_decimal(coef)
        upstream.append(up_key)
    if col.failed:
        return None
    return Formula(
        coefficients=coefs,
        constant=parse_decimal(constant),
        upstream=tuple(sorted(upstream)),
    )


def _cross_archive_checks(archives: list[Archive], col: _Collector) -> None:
    key_units: dict[str, str] = {}
    for archive in archives:
        for key, item in archive.items.items():
            prior = key_units.get(key)
            if prior is None:
                key_units[key] = item.unit
            elif prior != item.unit:
                col.fail(f"{item.location}/unit",
                         f"key {key!r} has inconsistent units {prior!r} vs "
                         f"{item.unit!r} across nodes")
