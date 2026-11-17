"""Exact version expressions, intervals, and reviewable error bounds.

A version is a vector of non-negative integer components.  All intervals in
one manifest share one resolution depth: the maximum component count observed
in that manifest.  The quantum below is therefore exactly one least
significant component step at that depth.  Errors are stored as ``Fraction``
values in those quanta and serialized as a numerator/denominator pair plus an
exact decimal string.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal, localcontext
from fractions import Fraction
from functools import total_ordering
from typing import Any, Iterable, Sequence

from .errors import InputError


VERSION_RE = re.compile(r"^[vV]?(\d+(?:\.\d+){0,15})$")
INTERVAL_RE = re.compile(
    r"^\s*([\[(])\s*([^,]*?)\s*,\s*([^,]*?)\s*([\])])\s*$"
)
COMPARATOR_RE = re.compile(r"(>=|<=|==|=|>|<)\s*[vV]?(\d+(?:\.\d+){0,15})")
PLAIN_COMPONENT_RE = re.compile(r"^\d+(?:\.\d+){0,15}$")
HYPHEN_RANGE_RE = re.compile(
    r"^\s*[vV]?(\d+(?:\.\d+){0,15})\s+-\s+[vV]?(\d+(?:\.\d+){0,15})\s*$"
)


def _fraction_dict(value: Fraction | None) -> dict | None:
    if value is None:
        return None
    numerator = value.numerator
    denominator = value.denominator
    remainder = denominator
    factors = {2: 0, 5: 0}
    for factor in (2, 5):
        while remainder % factor == 0:
            factors[factor] += 1
            remainder //= factor
    terminating = remainder == 1
    with localcontext() as ctx:
        if terminating:
            ctx.prec = max(
                50,
                len(str(abs(numerator)))
                + max(factors[2], factors[5])
                + 8,
            )
        else:
            ctx.prec = 80
        decimal = Decimal(numerator) / Decimal(denominator)
    return {
        "numerator": numerator,
        "denominator": denominator,
        "decimal": format(decimal, "f"),
        "decimal_exact": terminating,
    }


fraction_to_dict = _fraction_dict


@total_ordering
@dataclass(frozen=True, eq=False)
class Version:
    """A comparable numeric version vector."""

    components: tuple[int, ...]

    @classmethod
    def parse(cls, value: str) -> "Version":
        if not isinstance(value, str):
            raise InputError("E_VERSION_EXPR", "version must be a string")
        match = VERSION_RE.fullmatch(value.strip())
        if not match:
            raise InputError("E_VERSION_EXPR", f"unsupported version: {value!r}")
        parts = tuple(int(part) for part in match.group(1).split("."))
        if any(part > 10**12 for part in parts):
            raise InputError("E_VERSION_EXPR", "version component exceeds 10^12")
        return cls(parts)

    @property
    def depth(self) -> int:
        return len(self.components)

    def _comparison_key(self, other: "Version") -> tuple[tuple[int, ...], tuple[int, ...]]:
        depth = max(self.depth, other.depth)
        return self.padded(depth), other.padded(depth)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Version):
            return NotImplemented
        left, right = self._comparison_key(other)
        return left == right

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, Version):
            return NotImplemented
        left, right = self._comparison_key(other)
        return left < right

    def __hash__(self) -> int:
        components = self.components
        while len(components) > 1 and components[-1] == 0:
            components = components[:-1]
        if components == (0,):
            components = ()
        return hash(components)

    def padded(self, depth: int) -> tuple[int, ...]:
        if depth < self.depth:
            raise ValueError("cannot truncate a version")
        return self.components + (0,) * (depth - self.depth)

    def normalized_components(self, depth: int) -> list[int]:
        return list(self.padded(depth))

    def text(self, depth: int | None = None) -> str:
        components = self.components if depth is None else self.padded(depth)
        return ".".join(str(part) for part in components)

    def to_dict(self, depth: int | None = None) -> dict:
        normalized = self.components if depth is None else self.padded(depth)
        return {"components": list(normalized), "text": self.text(depth)}


@dataclass(frozen=True)
class VersionContext:
    """Shared resolution metadata for one manifest."""

    resolution_depth: int
    base: int

    @classmethod
    def from_versions(cls, versions: Iterable[Version | None]) -> "VersionContext":
        present = [version for version in versions if version is not None]
        depth = max((version.depth for version in present), default=1)
        max_component = max(
            (component for version in present for component in version.components),
            default=0,
        )
        # A base larger than every component preserves lexicographic order when
        # the vector is mapped to a single integer.
        base = max(10, max_component + 1)
        return cls(resolution_depth=depth, base=base)

    def scalar(self, version: Version) -> int:
        value = 0
        for component in version.padded(self.resolution_depth):
            value = value * self.base + component
        return value

    def distance_quanta(self, left: Version, right: Version) -> Fraction:
        return Fraction(self.scalar(left) - self.scalar(right), 1)

    def to_dict(self) -> dict:
        return {
            "resolution_depth": self.resolution_depth,
            "base": self.base,
            "quantum": "1 least-significant component step",
        }


@dataclass(frozen=True)
class EndpointError:
    quantization: Fraction
    inclusion: Fraction

    @property
    def total(self) -> Fraction:
        return self.quantization + self.inclusion

    def to_dict(self) -> dict:
        return {
            "quantization": _fraction_dict(self.quantization),
            "inclusion": _fraction_dict(self.inclusion),
            "total": _fraction_dict(self.total),
            "unit": "quantum",
        }


def endpoint_error(inclusive: bool) -> EndpointError:
    # A half-quantum rounding allowance plus one quantum when the endpoint is
    # open, because an excluded boundary can be approached arbitrarily closely.
    return EndpointError(Fraction(1, 2), Fraction(0) if inclusive else Fraction(1))


@dataclass(frozen=True)
class Interval:
    """A normalized closed/open interval in the version vector domain."""

    lower: Version | None
    upper: Version | None
    lower_inclusive: bool
    upper_inclusive: bool
    source: str | None = None
    allow_inverted: bool = field(default=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self.lower is None and self.lower_inclusive:
            object.__setattr__(self, "lower_inclusive", False)
        if self.upper is None and self.upper_inclusive:
            object.__setattr__(self, "upper_inclusive", False)
        if self.lower is not None and self.upper is not None:
            if self.lower > self.upper and not self.allow_inverted:
                raise InputError("E_VERSION_EXPR", "lower endpoint exceeds upper endpoint")
            if self.lower == self.upper and not (
                self.lower_inclusive and self.upper_inclusive
            ):
                # This is a well-formed empty interval.  It is normalized as
                # such and will be rejected by constraint intersection.
                pass

    @classmethod
    def exact(cls, version: Version) -> "Interval":
        return cls(version, version, True, True)

    @classmethod
    def wildcard(cls) -> "Interval":
        return cls(None, None, False, False)

    @property
    def is_point(self) -> bool:
        return (
            self.lower is not None
            and self.upper is not None
            and self.lower == self.upper
            and self.lower_inclusive
            and self.upper_inclusive
        )

    @property
    def is_formally_empty(self) -> bool:
        if self.lower is None or self.upper is None:
            return False
        return self.lower > self.upper or (
            self.lower == self.upper
            and not (self.lower_inclusive and self.upper_inclusive)
        )

    def contains(self, version: Version) -> bool:
        if self.is_formally_empty:
            return False
        if self.lower is not None:
            if version < self.lower:
                return False
            if version == self.lower and not self.lower_inclusive:
                return False
        if self.upper is not None:
            if version > self.upper:
                return False
            if version == self.upper and not self.upper_inclusive:
                return False
        return True

    def canonical_text(self) -> str:
        lower = "*" if self.lower is None else self.lower.text()
        upper = "*" if self.upper is None else self.upper.text()
        left = "[" if self.lower is not None and self.lower_inclusive else "("
        right = "]" if self.upper is not None and self.upper_inclusive else ")"
        return f"{left}{lower},{upper}{right}"

    def to_dict(self, context: VersionContext | None = None) -> dict:
        return {
            "expression": self.canonical_text(),
            "lower": None
            if self.lower is None
            else self.lower.to_dict(context.resolution_depth if context else None),
            "upper": None
            if self.upper is None
            else self.upper.to_dict(context.resolution_depth if context else None),
            "lower_inclusive": self.lower_inclusive,
            "upper_inclusive": self.upper_inclusive,
            "is_point": self.is_point,
            "formally_empty": self.is_formally_empty,
        }


@dataclass(frozen=True)
class IntersectionResult:
    interval: Interval
    core_empty: bool
    expanded_empty: bool
    uncertainty: bool
    lower_error: Fraction | None
    upper_error: Fraction | None
    width_error: Fraction | None
    reason: str
    contributor_count: int

    @property
    def status(self) -> str:
        if self.uncertainty:
            return "uncertain"
        if self.core_empty:
            return "unsatisfiable"
        return "satisfiable"

    def to_dict(self, context: VersionContext | None = None) -> dict:
        width_is_unbounded = (
            self.interval.lower is None or self.interval.upper is None
        ) and not self.core_empty
        return {
            "status": self.status,
            "reason": self.reason,
            "interval": self.interval.to_dict(context),
            "core_empty": self.core_empty,
            "expanded_empty": self.expanded_empty,
            "error_bound": {
                "lower": _fraction_dict(self.lower_error),
                "upper": _fraction_dict(self.upper_error),
                "width": _fraction_dict(self.width_error),
                "width_unbounded": width_is_unbounded,
                "unit": "quantum",
                "depth": context.resolution_depth if context else None,
                "base": context.base if context else None,
            },
            "contributor_count": self.contributor_count,
        }


def _parse_endpoint(text: str, *, lower: bool) -> Version | None:
    stripped = text.strip()
    if not stripped or stripped == "*":
        return None
    if not PLAIN_COMPONENT_RE.fullmatch(stripped.lstrip("vV")):
        raise InputError("E_VERSION_EXPR", f"invalid interval endpoint: {text!r}")
    return Version.parse(stripped)


def _parse_comparator_expression(expression: str) -> Interval:
    compact = expression.replace(",", " ").strip()
    matches = list(COMPARATOR_RE.finditer(compact))
    if not matches:
        raise InputError("E_VERSION_EXPR", f"unsupported version expression: {expression!r}")
    remainder = COMPARATOR_RE.sub("", compact).strip()
    if remainder:
        raise InputError("E_VERSION_EXPR", f"unparsed version expression: {remainder!r}")

    lower: Version | None = None
    upper: Version | None = None
    lower_inclusive = True
    upper_inclusive = True
    for match in matches:
        operator, raw_version = match.groups()
        version = Version.parse(raw_version)
        if operator in (">", ">="):
            inclusive = operator == ">="
            if lower is None or version > lower or (
                version == lower and not inclusive and lower_inclusive
            ):
                lower = version
                lower_inclusive = inclusive
        elif operator in ("<", "<="):
            inclusive = operator == "<="
            if upper is None or version < upper or (
                version == upper and not inclusive and upper_inclusive
            ):
                upper = version
                upper_inclusive = inclusive
        elif operator in ("=", "=="):
            if lower is not None or upper is not None:
                raise InputError(
                    "E_VERSION_EXPR",
                    "exact version cannot be combined with a bound",
                )
            lower = upper = version
            lower_inclusive = upper_inclusive = True
        else:
            raise InputError("E_VERSION_EXPR", f"unknown comparator: {operator}")

    return Interval(lower, upper, lower_inclusive, upper_inclusive, source=expression)


def _interval_from_object(value: dict) -> Interval:
    if "interval" in value:
        parsed = parse_version_expression(value["interval"])
        return parsed
    if "exact" in value or (
        "version" in value and "op" not in value and "operator" not in value
    ):
        raw = value.get("exact", value.get("version"))
        if isinstance(raw, Interval):
            return raw
        if isinstance(raw, str) and not PLAIN_COMPONENT_RE.fullmatch(
            raw.strip().lstrip("vV")
        ):
            return parse_version_expression(raw)
        return Interval.exact(Version.parse(str(raw)))
    if "op" in value or "operator" in value:
        operator = str(value.get("op", value.get("operator")))
        if "version" not in value:
            raise InputError("E_VERSION_EXPR", "operator constraint is missing version")
        return _parse_comparator_expression(f"{operator}{value['version']}")

    lower_raw = value.get("lower", value.get("min"))
    upper_raw = value.get("upper", value.get("max"))
    if lower_raw is None and upper_raw is None:
        if not value:
            return Interval.wildcard()
        raise InputError("E_VERSION_EXPR", "interval object has no bounds")
    lower = None if lower_raw is None else Version.parse(str(lower_raw))
    upper = None if upper_raw is None else Version.parse(str(upper_raw))
    lower_inclusive = bool(
        value.get(
            "lower_inclusive",
            value.get("include_min", value.get("min_inclusive", True)),
        )
    )
    upper_inclusive = bool(
        value.get(
            "upper_inclusive",
            value.get("include_max", value.get("max_inclusive", True)),
        )
    )
    return Interval(lower, upper, lower_inclusive, upper_inclusive)


def normalize_interval(interval: Interval) -> Interval:
    """Return an endpoint-order-independent canonical interval."""

    lower = interval.lower
    upper = interval.upper
    lower_inclusive = interval.lower_inclusive if lower is not None else False
    upper_inclusive = interval.upper_inclusive if upper is not None else False
    if interval.allow_inverted and lower is not None and upper is not None:
        if lower > upper or (lower == upper and not (lower_inclusive and upper_inclusive)):
            return interval
    if lower is not None and upper is not None and lower > upper:
        lower, upper = upper, lower
        lower_inclusive, upper_inclusive = upper_inclusive, lower_inclusive
    return Interval(
        lower,
        upper,
        lower_inclusive,
        upper_inclusive,
        source=interval.source,
    )


def parse_version_expression(value: Any) -> Interval:
    """Parse exact versions, comparator lists, and closed/open intervals."""

    if isinstance(value, Interval):
        return normalize_interval(value)
    if value is None:
        return Interval.wildcard()
    if isinstance(value, dict):
        return normalize_interval(_interval_from_object(value))
    if isinstance(value, (list, tuple)):
        if not value:
            return Interval.wildcard()
        intervals = [parse_version_expression(item) for item in value]
        return intersect_intervals(intervals).interval
    if not isinstance(value, str):
        raise InputError("E_VERSION_EXPR", "version expression must be a string")
    expression = value.strip()
    if not expression or expression in {"*", "any", "ANY"}:
        return Interval.wildcard()

    hyphen_match = HYPHEN_RANGE_RE.fullmatch(expression)
    if hyphen_match:
        return Interval(
            Version.parse(hyphen_match.group(1)),
            Version.parse(hyphen_match.group(2)),
            True,
            True,
            source=value,
        )

    interval_match = INTERVAL_RE.fullmatch(expression)
    if interval_match:
        left, lower_raw, upper_raw, right = interval_match.groups()
        lower = _parse_endpoint(lower_raw, lower=True)
        upper = _parse_endpoint(upper_raw, lower=False)
        return Interval(
            lower,
            upper,
            left == "[" and lower is not None,
            right == "]" and upper is not None,
            source=value,
        )

    if PLAIN_COMPONENT_RE.fullmatch(expression.lstrip("vV")):
        return Interval.exact(Version.parse(expression))

    return _parse_comparator_expression(expression)


def _selected_lower(
    intervals: Sequence[Interval], context: VersionContext
) -> tuple[Version | None, bool, Fraction | None]:
    bounded = [interval for interval in intervals if interval.lower is not None]
    if not bounded:
        return None, False, None
    max_scalar = max(context.scalar(interval.lower) for interval in bounded)  # type: ignore[arg-type]
    at_max = [
        interval
        for interval in bounded
        if context.scalar(interval.lower) == max_scalar  # type: ignore[arg-type]
    ]
    inclusive = all(interval.lower_inclusive for interval in at_max)
    error = max(endpoint_error(interval.lower_inclusive).total for interval in at_max)
    endpoint = min(
        (interval.lower for interval in at_max if interval.lower is not None),
        key=lambda version: (version.components, version.depth),
    )
    return endpoint, inclusive, error


def _selected_upper(
    intervals: Sequence[Interval], context: VersionContext
) -> tuple[Version | None, bool, Fraction | None]:
    bounded = [interval for interval in intervals if interval.upper is not None]
    if not bounded:
        return None, False, None
    min_scalar = min(context.scalar(interval.upper) for interval in bounded)  # type: ignore[arg-type]
    at_min = [
        interval
        for interval in bounded
        if context.scalar(interval.upper) == min_scalar  # type: ignore[arg-type]
    ]
    inclusive = all(interval.upper_inclusive for interval in at_min)
    error = max(endpoint_error(interval.upper_inclusive).total for interval in at_min)
    endpoint = min(
        (interval.upper for interval in at_min if interval.upper is not None),
        key=lambda version: (version.components, version.depth),
    )
    return endpoint, inclusive, error


def intersect_intervals(
    intervals: Sequence[Interval],
    context: VersionContext | None = None,
) -> IntersectionResult:
    """Intersect normalized intervals and compose reviewable error bounds.

    The selected endpoint is the tightest one.  If several constraints select
    the same numeric endpoint, an open flag on any of them wins and the largest
    endpoint error among the tied constraints is used.  The width error is the
    sum of the selected lower and upper endpoint errors.
    """

    normalized = [normalize_interval(interval) for interval in intervals]
    if not normalized:
        normalized = [Interval.wildcard()]
    versions: list[Version | None] = []
    for interval in normalized:
        versions.extend([interval.lower, interval.upper])
    ctx = context or VersionContext.from_versions(versions)

    lower, lower_inclusive, lower_error = _selected_lower(normalized, ctx)
    upper, upper_inclusive, upper_error = _selected_upper(normalized, ctx)

    core_empty = any(interval.is_formally_empty for interval in normalized)
    if lower is not None and upper is not None:
        lower_scalar = ctx.scalar(lower)
        upper_scalar = ctx.scalar(upper)
        if lower_scalar > upper_scalar:
            core_empty = True
        elif lower_scalar == upper_scalar and not (
            lower_inclusive and upper_inclusive
        ):
            core_empty = True

    expanded_lower: Fraction | None = None
    expanded_upper: Fraction | None = None
    if lower is not None and lower_error is not None:
        expanded_lower = Fraction(ctx.scalar(lower), 1) - lower_error
    if upper is not None and upper_error is not None:
        expanded_upper = Fraction(ctx.scalar(upper), 1) + upper_error
    formal_empty_input = any(interval.is_formally_empty for interval in normalized)
    expanded_empty = core_empty
    if expanded_lower is not None and expanded_upper is not None:
        expanded_empty = expanded_lower >= expanded_upper
    elif formal_empty_input:
        expanded_empty = True
    if formal_empty_input:
        expanded_empty = True

    uncertainty = bool(core_empty and not expanded_empty)
    if uncertainty:
        reason = "ERROR_BOUND_OVERLAP"
    elif core_empty:
        reason = "EMPTY_INTERSECTION"
    else:
        reason = "INTERSECTION_NONEMPTY"

    width_error: Fraction | None
    if lower_error is None and upper_error is None:
        width_error = Fraction(0)
    elif lower_error is None or upper_error is None:
        width_error = None
    else:
        width_error = lower_error + upper_error

    intersection = Interval(
        lower,
        upper,
        lower_inclusive,
        upper_inclusive,
        source="intersection",
        allow_inverted=True,
    )
    return IntersectionResult(
        interval=intersection,
        core_empty=core_empty,
        expanded_empty=expanded_empty,
        uncertainty=uncertainty,
        lower_error=lower_error,
        upper_error=upper_error,
        width_error=width_error,
        reason=reason,
        contributor_count=len(normalized),
    )


def fraction_add(left: Fraction | None, right: Fraction | None) -> Fraction | None:
    if left is None or right is None:
        return None
    return left + right

