"""Explicit numeric range enforcement reported with the E_RANGE business code.

This is intentionally separate from loader schema validation (E_CONFIG).  It
runs after quantization/propagation so that *derived* items -- whose numeric
value is produced rather than authored -- are also checked against their unit
bounds, in integer grid units.
"""

from __future__ import annotations

from .errors import RangeError
from .fixedpoint import Grid, quantize
from .models import Archive, QuantizedItem


def enforce_ranges(archives: list[Archive],
                   quantized: dict[tuple[str, str], QuantizedItem],
                   grid: Grid) -> None:
    problems: list[str] = []
    out_key: str | None = None
    for archive in sorted(archives, key=lambda a: a.node_id):
        for key in sorted(archive.items):
            item = archive.items[key]
            qi = quantized[(archive.node_id, key)]
            low_q = quantize(item.bounds[0], grid, "bounds-min").quantized
            high_q = quantize(item.bounds[1], grid, "bounds-max").quantized
            if qi.qvalue < low_q or qi.qvalue > high_q:
                out_key = key
                problems.append(
                    f"{qi.location}/value: key {key!r} on node {archive.node_id!r} "
                    f"qvalue={qi.qvalue} outside grid bounds "
                    f"[{low_q}, {high_q}] (unit={item.unit})"
                )
    if problems:
        raise RangeError(
            "numeric value out of supported range",
            locations=problems,
            key=out_key,
            grid=grid.places,
        )
