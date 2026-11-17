"""Baseline declaration parsing with endpoint and interval validation."""

from __future__ import annotations

import ast
import hashlib
import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .canonical import PathNormalizationError, canonical_json_text, load_json_bytes, normalize_config_path
from .codes import Code
from .issues import Issue
from .models import MISSING, BaselineRange, BaselineRule


@dataclass
class BaselineResult:
    source_name: str
    sha256: str
    rules: list[BaselineRule] = field(default_factory=list)
    issues: list[Issue] = field(default_factory=list)

    def by_key(self) -> dict[tuple[str, str], list[BaselineRule]]:
        result: dict[tuple[str, str], list[BaselineRule]] = {}
        for rule in self.rules:
            result.setdefault((rule.host_id, rule.path), []).append(rule)
        for rules in result.values():
            rules.sort(
                key=lambda item: (
                    item.effective_from_seq if item.effective_from_seq is not None else -1,
                    item.effective_to_seq if item.effective_to_seq is not None else 2**63 - 1,
                    item.source,
                    item.declaration_index,
                )
            )
        return result


def _issue(code: str, source_name: str, index: int | None, **details: Any) -> Issue:
    return Issue(
        code,
        file_id=source_name,
        file_index=1,
        record_index=index,
        details=details,
    )


def _first(value: dict[str, Any], *names: str, default: Any = MISSING) -> Any:
    for name in names:
        if name in value:
            return value[name]
    return default


def _extract_entries(payload: Any) -> tuple[list[Any] | None, str | None]:
    if isinstance(payload, list):
        return payload, None
    if not isinstance(payload, dict):
        return None, "top-level baseline JSON must be an object or array"
    for key in ("baselines", "rules", "declarations"):
        if isinstance(payload.get(key), list):
            return payload[key], None
    if "host_id" in payload or "host" in payload:
        return [payload], None
    return None, "no baseline declaration array or record found"


def _parse_endpoint(text: str) -> Any:
    text = text.strip()
    if not text:
        return MISSING
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        try:
            value = ast.literal_eval(text)
        except (ValueError, SyntaxError) as exc:
            raise ValueError(f"invalid interval endpoint {text!r}") from exc
    if isinstance(value, (list, dict)):
        raise ValueError("interval endpoints must be JSON scalars")
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("interval endpoints must be finite")
    return value


def _parse_interval_notation(text: str) -> tuple[Any, Any, bool, bool]:
    value = text.strip()
    if len(value) < 5 or value[0] not in "[(" or value[-1] not in ")]":
        raise ValueError("interval notation must look like [min,max) or (min,max]")
    body = value[1:-1]
    if "," not in body:
        raise ValueError("interval notation requires one comma")
    lower_text, upper_text = body.split(",", 1)
    return (
        _parse_endpoint(lower_text),
        _parse_endpoint(upper_text),
        value[0] == "[",
        value[-1] == "]",
    )


def _bool_field(
    value: dict[str, Any],
    names: tuple[str, ...],
    *,
    default: bool,
) -> tuple[bool, str | None]:
    raw = _first(value, *names, default=default)
    if not isinstance(raw, bool):
        return default, f"{names[0]} must be a boolean"
    return raw, None


def _parse_range(value: Any) -> tuple[BaselineRange | None, str | None]:
    if isinstance(value, str):
        try:
            minimum, maximum, min_inclusive, max_inclusive = _parse_interval_notation(value)
        except ValueError as exc:
            return None, str(exc)
        return _validate_range(
            minimum,
            maximum,
            min_inclusive,
            max_inclusive,
            (),
        )
    if not isinstance(value, dict):
        return None, "range must be an interval string or object"

    notation = value.get("interval")
    if notation is None:
        notation = value.get("value_range")
    if isinstance(notation, str) and notation.strip().startswith(("[", "(")):
        try:
            minimum, maximum, min_inclusive, max_inclusive = _parse_interval_notation(notation)
        except ValueError as exc:
            return None, str(exc)
    else:
        minimum = _first(value, "min", "minimum", "lower", "low", "start", default=MISSING)
        maximum = _first(value, "max", "maximum", "upper", "high", "end", default=MISSING)
        min_inclusive, min_error = _bool_field(
            value,
            ("min_inclusive", "minimum_inclusive", "lower_inclusive", "include_min"),
            default=True,
        )
        if min_error:
            return None, min_error
        max_inclusive, max_error = _bool_field(
            value,
            ("max_inclusive", "maximum_inclusive", "upper_inclusive", "include_max"),
            default=True,
        )
        if max_error:
            return None, max_error

    raw_allowed = value.get("allowed_values")
    if raw_allowed is None and "allowed_value" in value:
        raw_allowed = [value["allowed_value"]]
    if raw_allowed is None:
        allowed: tuple[Any, ...] = ()
    elif isinstance(raw_allowed, list):
        allowed = tuple(raw_allowed)
    else:
        return None, "allowed_values must be an array"
    try:
        for item in allowed:
            canonical_json_text(item)
    except (TypeError, ValueError) as exc:
        return None, f"allowed value is not finite JSON: {exc}"
    return _validate_range(minimum, maximum, min_inclusive, max_inclusive, allowed)


def _validate_range(
    minimum: Any,
    maximum: Any,
    min_inclusive: bool,
    max_inclusive: bool,
    allowed: tuple[Any, ...],
) -> tuple[BaselineRange | None, str | None]:
    if not allowed and minimum is MISSING and maximum is MISSING:
        return None, "range requires min/max endpoints or allowed_values"
    for endpoint_name, endpoint in (("minimum", minimum), ("maximum", maximum)):
        if endpoint is MISSING:
            continue
        if isinstance(endpoint, bool) or not isinstance(endpoint, (int, float, str)):
            return None, f"{endpoint_name} must be a finite number or string"
        if isinstance(endpoint, float) and not math.isfinite(endpoint):
            return None, f"{endpoint_name} must be finite"
    if minimum is not MISSING and maximum is not MISSING:
        numeric = (
            isinstance(minimum, (int, float))
            and not isinstance(minimum, bool)
            and isinstance(maximum, (int, float))
            and not isinstance(maximum, bool)
        )
        textual = isinstance(minimum, str) and isinstance(maximum, str)
        if not numeric and not textual:
            return None, "minimum and maximum must have compatible scalar types"
        try:
            if minimum > maximum:
                return None, "minimum is greater than maximum"
        except TypeError as exc:
            return None, f"range endpoints cannot be ordered: {exc}"
        if minimum == maximum and (not min_inclusive or not max_inclusive):
            return None, "exclusive endpoints describe an empty range"
    return (
        BaselineRange(
            minimum=minimum,
            maximum=maximum,
            minimum_inclusive=min_inclusive,
            maximum_inclusive=max_inclusive,
            allowed_values=allowed,
        ),
        None,
    )


def _parse_effective_interval(
    value: dict[str, Any],
) -> tuple[int | None, int | None, bool, bool, str | None]:
    start = _first(
        value,
        "effective_from_seq",
        "effective_from",
        "valid_from_seq",
        "from_seq",
        default=None,
    )
    end = _first(
        value,
        "effective_to_seq",
        "effective_to",
        "valid_to_seq",
        "to_seq",
        default=None,
    )
    for name, endpoint in (("effective_from_seq", start), ("effective_to_seq", end)):
        if endpoint is not None and (
            isinstance(endpoint, bool) or not isinstance(endpoint, int) or endpoint < 0
        ):
            return None, None, True, True, f"{name} must be a non-negative integer or null"
    start_inclusive, start_error = _bool_field(
        value, ("effective_from_inclusive", "from_inclusive"), default=True
    )
    if start_error:
        return None, None, True, True, start_error
    end_inclusive, end_error = _bool_field(
        value, ("effective_to_inclusive", "to_inclusive"), default=True
    )
    if end_error:
        return None, None, True, True, end_error
    if start is not None and end is not None:
        if start > end or (start == end and (not start_inclusive or not end_inclusive)):
            return None, None, start_inclusive, end_inclusive, "effective interval is empty"
    return start, end, start_inclusive, end_inclusive, None


def _rule_dict_for_signature(
    host_id: str,
    path: str,
    source: str,
    start: int | None,
    end: int | None,
    start_inclusive: bool,
    end_inclusive: bool,
    ranges: tuple[BaselineRange, ...],
) -> str:
    return canonical_json_text(
        {
            "host_id": host_id,
            "path": path,
            "source": source,
            "effective_from_seq": start,
            "effective_to_seq": end,
            "effective_from_inclusive": start_inclusive,
            "effective_to_inclusive": end_inclusive,
            "ranges": [item.to_dict() for item in ranges],
        }
    )


def _parse_declaration(
    entry: Any,
    index: int,
    source_name: str,
) -> tuple[list[BaselineRule], list[Issue]]:
    issues: list[Issue] = []
    if not isinstance(entry, dict):
        return [], [_issue(Code.SCHEMA_INVALID, source_name, index, reason="entry must be object")]

    host_id = _first(entry, "host_id", "host")
    if not isinstance(host_id, str) or not host_id or host_id != host_id.strip():
        return [], [_issue(Code.HOST_INVALID, source_name, index)]

    raw_path = _first(entry, "path", "config_path", "config_key")
    try:
        path = normalize_config_path(raw_path)
    except PathNormalizationError as exc:
        return [], [
            _issue(
                Code.PATH_INVALID,
                source_name,
                index,
                host_id=host_id,
                reason=str(exc),
                input=raw_path,
            )
        ]

    source = entry.get("source", f"baseline#{index}")
    if not isinstance(source, str) or not source:
        issues.append(
            _issue(
                Code.SCHEMA_INVALID,
                source_name,
                index,
                host_id=host_id,
                path=path,
                reason="source must be a non-empty string",
            )
        )
        source = f"baseline#{index}"

    start, end, start_inclusive, end_inclusive, interval_error = _parse_effective_interval(entry)
    if interval_error:
        return [], [
            _issue(
                Code.BASELINE_INVALID_INTERVAL,
                source_name,
                index,
                host_id=host_id,
                path=path,
                reason=interval_error,
            )
        ]

    range_items = entry.get("ranges")
    if range_items is None and "value_domain" in entry:
        domain = entry["value_domain"]
        if isinstance(domain, dict) and isinstance(domain.get("intervals"), list):
            range_items = domain["intervals"]
        elif isinstance(domain, list):
            range_items = domain
        else:
            range_items = [domain]
    if range_items is None:
        range_items = [entry]
    if not isinstance(range_items, list) or not range_items:
        return [], [
            _issue(
                Code.BASELINE_INVALID_RANGE,
                source_name,
                index,
                host_id=host_id,
                path=path,
                reason="ranges must be a non-empty array",
            )
        ]

    parsed_rules: list[BaselineRule] = []
    for range_index, range_item in enumerate(range_items):
        item_source = source
        item_start, item_end = start, end
        item_start_inclusive, item_end_inclusive = start_inclusive, end_inclusive
        if isinstance(range_item, dict):
            item_source = range_item.get("source", source)
            if not isinstance(item_source, str) or not item_source:
                issues.append(
                    _issue(
                        Code.SCHEMA_INVALID,
                        source_name,
                        index,
                        host_id=host_id,
                        path=path,
                        reason=f"ranges[{range_index}].source must be a non-empty string",
                    )
                )
                item_source = source
            (
                override_start,
                override_end,
                override_start_inclusive,
                override_end_inclusive,
                item_interval_error,
            ) = _parse_effective_interval(range_item)
            if item_interval_error:
                issues.append(
                    _issue(
                        Code.BASELINE_INVALID_INTERVAL,
                        source_name,
                        index,
                        host_id=host_id,
                        path=path,
                        reason=f"ranges[{range_index}]: {item_interval_error}",
                    )
                )
                continue
            if override_start is not None:
                item_start = override_start
            if override_end is not None:
                item_end = override_end
            if any(
                key in range_item
                for key in ("effective_from_inclusive", "from_inclusive")
            ):
                item_start_inclusive = override_start_inclusive
            if any(key in range_item for key in ("effective_to_inclusive", "to_inclusive")):
                item_end_inclusive = override_end_inclusive

        parsed_range, range_error = _parse_range(range_item)
        if range_error:
            issues.append(
                _issue(
                    Code.BASELINE_INVALID_RANGE,
                    source_name,
                    index,
                    host_id=host_id,
                    path=path,
                    reason=f"ranges[{range_index}]: {range_error}",
                )
            )
            continue
        assert parsed_range is not None
        parsed_rules.append(
            BaselineRule(
                host_id=host_id,
                path=path,
                source=item_source,
                effective_from_seq=item_start,
                effective_to_seq=item_end,
                effective_from_inclusive=item_start_inclusive,
                effective_to_inclusive=item_end_inclusive,
                ranges=(parsed_range,),
                declaration_index=index,
            )
        )
    return parsed_rules, issues


def parse_baseline_bytes(data: bytes, *, source_name: str) -> BaselineResult:
    sha256 = hashlib.sha256(data).hexdigest()
    issues: list[Issue] = []
    try:
        payload = load_json_bytes(data)
    except (UnicodeDecodeError, ValueError) as exc:
        return BaselineResult(
            source_name=source_name,
            sha256=sha256,
            issues=[
                Issue(
                    Code.JSON_INVALID,
                    file_id=source_name,
                    file_index=1,
                    details={"reason": str(exc)},
                )
            ],
        )

    if isinstance(payload, dict) and "schema_version" in payload:
        version = payload["schema_version"]
        if isinstance(version, bool) or not isinstance(version, int) or version != 1:
            issues.append(
                _issue(
                    Code.SCHEMA_INVALID,
                    source_name,
                    None,
                    schema_version=version,
                )
            )

    entries, extraction_error = _extract_entries(payload)
    if entries is None:
        issues.append(
            _issue(Code.SCHEMA_INVALID, source_name, None, reason=extraction_error)
        )
        return BaselineResult(source_name=source_name, sha256=sha256, issues=issues)

    rules: list[BaselineRule] = []
    seen: set[str] = set()
    for index, entry in enumerate(entries):
        parsed, declaration_issues = _parse_declaration(entry, index, source_name)
        issues.extend(declaration_issues)
        for rule in parsed:
            signature = _rule_dict_for_signature(
                rule.host_id,
                rule.path,
                rule.source,
                rule.effective_from_seq,
                rule.effective_to_seq,
                rule.effective_from_inclusive,
                rule.effective_to_inclusive,
                rule.ranges,
            )
            if signature in seen:
                issues.append(
                    _issue(
                        Code.BASELINE_DUPLICATE,
                        source_name,
                        index,
                        host_id=rule.host_id,
                        path=rule.path,
                        source=rule.source,
                    )
                )
                continue
            seen.add(signature)
            rules.append(rule)

    rules.sort(
        key=lambda item: (
            item.host_id,
            item.path,
            item.effective_from_seq if item.effective_from_seq is not None else -1,
            item.effective_to_seq if item.effective_to_seq is not None else 2**63 - 1,
            item.source,
            item.declaration_index,
        )
    )
    return BaselineResult(
        source_name=source_name,
        sha256=sha256,
        rules=rules,
        issues=issues,
    )


def parse_baseline_file(path: Path) -> BaselineResult:
    try:
        data = path.read_bytes()
    except OSError as exc:
        return BaselineResult(
            source_name=str(path),
            sha256="",
            issues=[
                Issue(
                    Code.FILE_READ,
                    file_id=str(path),
                    file_index=1,
                    details={"reason": str(exc)},
                )
            ],
        )
    return parse_baseline_bytes(data, source_name=str(path))
