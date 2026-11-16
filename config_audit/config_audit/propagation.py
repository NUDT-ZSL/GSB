"""Fixed-point error propagation along linear derivation chains.

The derivation graph is processed in deterministic topological order (DFS over
sorted keys).  Every value is represented on the integer grid by a closed
integer interval ``[low, high]`` expressed in grid steps.

Scaling bookkeeping
-------------------
Let ``S = grid.factor``.  A real value ``x`` is stored as the integer
``X = round(x * S)`` grid steps.  For a derived real value
``y = sum(c_i * x_i) + k`` the correctly scaled integer expression is::

    Y = sum( C_i * X_i ) + K,   where C_i = round(c_i * S), K = round(k * S)

so every product carries one factor of ``S`` (the coefficient's).  The integer
interval for ``Y`` is obtained by sign-aware interval multiplication; its
extremes include upstream value error, coefficient quantization error and the
constant quantization error.  The representative is the half-even quantization
of the exact real expression, and ``down_error`` / ``up_error`` are the
nonnegative integer gaps from that representative to the interval extremes.
All arithmetic is integer / Decimal -- never float.
"""

from __future__ import annotations

from decimal import Decimal

from .errors import CycleError, PropagateError
from .fixedpoint import Grid, quantize, quantize_coefficient
from .models import Archive, ConfigItem, Formula, QuantizedItem
from .schema import SOURCE_MANUAL


class _Node:
    __slots__ = ("low", "high", "q", "down", "up", "trace", "contributions")

    def __init__(self) -> None:
        self.low: int = 0
        self.high: int = 0
        self.q: int = 0
        self.down: int = 0
        self.up: int = 0
        self.trace: dict[str, object] = {}
        self.contributions: list[dict] = []


def build_quantized(archives: list[Archive], grid: Grid
                    ) -> dict[tuple[str, str], QuantizedItem]:
    """Quantize every item and propagate error bounds over derivation chains."""
    result: dict[tuple[str, str], QuantizedItem] = {}
    for archive in sorted(archives, key=lambda a: a.node_id):
        nodes = _propagate_archive(archive, grid)
        for key in sorted(nodes):
            node = nodes[key]
            item = archive.items[key]
            result[(archive.node_id, key)] = QuantizedItem(
                node_id=archive.node_id,
                key=key,
                unit=item.unit,
                source=item.source,
                qvalue=node.q,
                down_error=node.down,
                up_error=node.up,
                location=item.location,
                trace=dict(node.trace),
            )
    return result


def _propagate_archive(archive: Archive, grid: Grid) -> dict[str, _Node]:
    items = archive.items
    state: dict[str, int] = {}      # 0=unvisited, 1=visiting, 2=done
    nodes: dict[str, _Node] = {}

    def visit(key: str, stack: tuple[str, ...]) -> _Node:
        color = state.get(key, 0)
        if color == 1:
            cycle = list(stack[stack.index(key):]) + [key]
            raise CycleError(
                "derivation dependency cycle detected",
                locations=[f"{items[k].location}/formula" for k in cycle],
                key=key,
                grid=grid.places,
            )
        if color == 2:
            return nodes[key]
        state[key] = 1
        item = items[key]
        if item.source == SOURCE_MANUAL:
            nodes[key] = _manual_node(item, grid)
        else:
            nodes[key] = _derived_node(
                item, grid, items,
                lambda up: visit(up, stack + (key,)))
        state[key] = 2
        return nodes[key]

    for key in sorted(items):
        visit(key, ())
    return nodes


def _manual_node(item: ConfigItem, grid: Grid) -> _Node:
    assert item.value is not None
    rec = quantize(item.value, grid, label="value")
    node = _Node()
    node.q = rec.quantized
    node.low = rec.quantized - rec.down_error
    node.high = rec.quantized + rec.up_error
    node.down = rec.down_error
    node.up = rec.up_error
    node.trace["value"] = rec
    return node


def _derived_node(item: ConfigItem, grid: Grid,
                  items, resolve) -> _Node:
    formula: Formula = item.formula  # type: ignore[assignment]
    node = _Node()
    exact = Decimal(0)
    interval_low = 0
    interval_high = 0
    contributions: list[dict] = []

    # Constant scaled onto the grid: K = round(k * S).
    const_rec = quantize(formula.constant, grid, label="constant")
    node.trace["constant"] = const_rec
    exact += formula.constant
    interval_low += const_rec.quantized
    interval_high += const_rec.quantized

    for up_key in formula.upstream:
        if up_key not in items:
            # Defensive break-point: the loader normally rejects missing
            # references with E_CONFIG, but propagation must fail loudly with
            # E_PROPAGATE if it ever reaches an undeterminable upstream.
            raise PropagateError(
                "upstream item missing during propagation",
                locations=[f"{item.location}/formula"],
                key=up_key,
                grid=grid.places,
            )
        try:
            upstream = resolve(up_key)
        except KeyError as exc:
            raise PropagateError(
                "upstream item missing during propagation",
                locations=[f"{item.location}/formula"],
                key=up_key,
                grid=grid.places,
            ) from exc
        if upstream.down is None or upstream.up is None:
            raise PropagateError(
                "upstream error bound cannot be determined",
                locations=[f"{item.location}/formula"],
                key=up_key,
                grid=grid.places,
            )

        coef = formula.coefficients[up_key]
        coef_rec = quantize_coefficient(coef)
        node.trace[f"coef:{up_key}"] = coef_rec
        cq = coef_rec.quantized                      # C_i = round(c_i * S)
        # Exact real expression uses the upstream representative real value.
        exact += coef * (Decimal(upstream.q) / Decimal(grid.factor))

        up_low = upstream.low
        up_high = upstream.high
        if cq >= 0:
            term_low = cq * up_low
            term_high = cq * up_high
        else:
            term_low = cq * up_high
            term_high = cq * up_low
        interval_low += term_low
        interval_high += term_high

        # |coef|-weighted contribution of this upstream (integer grid steps).
        weight = abs(cq)
        contributions.append({
            "upstream_key": up_key,
            "coefficient_q": cq,
            "weight_abs_coef": weight,
            "upstream_down_error": upstream.down,
            "upstream_up_error": upstream.up,
            "weighted_down": weight * upstream.down,
            "weighted_up": weight * upstream.up,
            "term_interval": [term_low, term_high],
        })

    node.contributions = contributions
    rep = quantize(exact, grid, label="derived")
    node.q = rep.quantized
    node.low = interval_low
    node.high = interval_high
    node.down = max(0, node.q - interval_low)
    node.up = max(0, interval_high - node.q)
    node.trace["derived"] = rep
    node.trace["propagation"] = {
        "path": list(formula.upstream),
        "interval": [interval_low, interval_high],
        "contributions": contributions,
        "constant_q": const_rec.quantized,
        "factor": grid.factor,
    }
    return node
