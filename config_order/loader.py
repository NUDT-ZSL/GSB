"""Strict, deterministic loading of local scenario JSON files."""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Any

from .errors import E_BIND, E_ENV, Issue, LoadRejected


SCHEMA_VERSION = 1
SOURCE_KINDS = frozenset({"baseline", "file", "runtime"})
SOURCE_ID_RE = re.compile(r"^[a-z][a-z0-9_]*$")
INJECTIONS = frozenset(
    {"none", "env-corrupt", "binding-missing", "chain-digest"}
)
DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_nonempty_str(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _issue(
    code: str,
    path: str,
    message: str,
    **optional: Any,
) -> Issue:
    return Issue(code=code, field_path=path, message=message, **optional)


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-finite number {value!r} is not allowed")


def read_json(path: str | Path) -> Any:
    input_path = Path(path)
    try:
        text = input_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise LoadRejected(
            E_ENV,
            [_issue(E_ENV, "$", f"cannot read input file: {exc}")],
        ) from exc
    try:
        return json.loads(text, parse_constant=_reject_constant)
    except (json.JSONDecodeError, ValueError) as exc:
        line = getattr(exc, "lineno", None)
        column = getattr(exc, "colno", None)
        detail = f" ({line}:{column})" if line is not None else ""
        raise LoadRejected(
            E_ENV,
            [_issue(E_ENV, "$", f"invalid JSON{detail}: {exc}")],
        ) from exc


def apply_injection(raw: Any, injection: str) -> Any:
    """Apply a CLI-selected fault to the loaded input object only."""
    if injection not in INJECTIONS:
        raise ValueError(f"unsupported injection: {injection}")
    if injection == "none":
        return raw

    mutated = copy.deepcopy(raw)
    if not isinstance(mutated, dict):
        return mutated
    environments = mutated.get("environments")
    if not isinstance(environments, list) or not environments:
        return mutated
    first_env = environments[0]
    if not isinstance(first_env, dict):
        return mutated

    if injection == "env-corrupt":
        bias = first_env.get("priority_bias")
        if isinstance(bias, dict) and bias:
            first_key = sorted(bias)[0]
            bias[first_key] = "not-an-integer"
        else:
            first_env["priority_bias"] = "not-an-object"
        return mutated

    if injection == "binding-missing":
        bindings = first_env.get("bindings")
        if not isinstance(bindings, dict):
            bindings = {}
            first_env["bindings"] = bindings
        bindings["__injection__.missing_source"] = {
            "require": ["source_that_does_not_exist"],
            "forbid": [],
        }
        return mutated

    if injection == "chain-digest":
        values = first_env.get("values")
        expected: dict[str, str] = {}
        if isinstance(values, dict):
            for key in sorted(values):
                expected[str(key)] = "0" * 64
        if not expected:
            expected["__injection__.missing_chain"] = "0" * 64
        first_env["expected_chain_digests"] = expected
        return mutated

    return mutated


def load_scenario(path: str | Path, injection: str = "none") -> dict[str, Any]:
    raw = read_json(path)
    return validate_scenario(apply_injection(raw, injection))


def _validate_source_registry(
    raw: dict[str, Any], env_issues: list[Issue]
) -> dict[str, dict[str, str]]:
    sources = raw.get("sources")
    if not isinstance(sources, list) or not sources:
        env_issues.append(
            _issue(E_ENV, "sources", "must be a non-empty array")
        )
        return {}

    registry: dict[str, dict[str, str]] = {}
    for index, source in enumerate(sources):
        path = f"sources[{index}]"
        if not isinstance(source, dict):
            env_issues.append(_issue(E_ENV, path, "source must be an object"))
            continue
        source_id = source.get("id")
        kind = source.get("kind")
        description = source.get("description", "")
        valid = True
        if not _is_nonempty_str(source_id) or not SOURCE_ID_RE.fullmatch(
            str(source_id)
        ):
            env_issues.append(
                _issue(
                    E_ENV,
                    f"{path}.id",
                    "must match ^[a-z][a-z0-9_]*$",
                )
            )
            valid = False
        elif source_id in registry:
            env_issues.append(
                _issue(E_ENV, f"{path}.id", "duplicate source identifier")
            )
            valid = False
        if kind not in SOURCE_KINDS:
            env_issues.append(
                _issue(
                    E_ENV,
                    f"{path}.kind",
                    f"must be one of {sorted(SOURCE_KINDS)}",
                )
            )
            valid = False
        if not isinstance(description, str):
            env_issues.append(
                _issue(E_ENV, f"{path}.description", "must be a string")
            )
            valid = False
        if valid:
            registry[str(source_id)] = {
                "id": str(source_id),
                "kind": str(kind),
                "description": description,
            }
    return registry


def _validate_rule_list(
    rules: Any,
    path: str,
    available: set[str],
    env_id: str | None,
    env_issues: list[Issue],
    seen_rule_ids: set[str],
    scope: str,
    config_key: str | None = None,
) -> list[dict[str, str]]:
    if rules is None:
        return []
    if not isinstance(rules, list):
        env_issues.append(_issue(E_ENV, path, "must be an array"))
        return []

    normalized: list[dict[str, str]] = []
    for index, rule in enumerate(rules):
        rule_path = f"{path}[{index}]"
        if not isinstance(rule, dict):
            env_issues.append(_issue(E_ENV, rule_path, "rule must be an object"))
            continue
        valid = True
        rule_id = rule.get("id")
        higher = rule.get("higher")
        lower = rule.get("lower")
        basis = rule.get("basis")
        if not _is_nonempty_str(rule_id):
            env_issues.append(
                _issue(E_ENV, f"{rule_path}.id", "must be a non-empty string")
            )
            valid = False
        elif rule_id in seen_rule_ids:
            env_issues.append(
                _issue(E_ENV, f"{rule_path}.id", "duplicate order rule id")
            )
            valid = False
        for field_name, field_value in (("higher", higher), ("lower", lower)):
            if not _is_nonempty_str(field_value):
                env_issues.append(
                    _issue(
                        E_ENV,
                        f"{rule_path}.{field_name}",
                        "must be a non-empty source id",
                    )
                )
                valid = False
            elif field_value not in available:
                env_issues.append(
                    _issue(
                        E_ENV,
                        f"{rule_path}.{field_name}",
                        "source is not available in this environment",
                    )
                )
                valid = False
        if (
            _is_nonempty_str(higher)
            and _is_nonempty_str(lower)
            and higher == lower
        ):
            env_issues.append(
                _issue(
                    E_ENV,
                    rule_path,
                    "higher and lower must identify different sources",
                )
            )
            valid = False
        if not _is_nonempty_str(basis):
            env_issues.append(
                _issue(E_ENV, f"{rule_path}.basis", "must be a non-empty string")
            )
            valid = False
        if valid:
            seen_rule_ids.add(str(rule_id))
            normalized.append(
                {
                    "id": str(rule_id),
                    "higher": str(higher),
                    "lower": str(lower),
                    "basis": str(basis),
                    "scope": scope,
                    "config_key": config_key or "",
                }
            )
    return normalized


def _validate_binding(
    key: str,
    binding: Any,
    path: str,
    available: set[str],
    registry: dict[str, dict[str, str]],
    env_id: str,
    env_issues: list[Issue],
    bind_issues: list[Issue],
) -> dict[str, list[str]] | None:
    if not isinstance(binding, dict):
        env_issues.append(
            _issue(
                E_ENV,
                path,
                "binding must be an object",
                environment_id=env_id,
                config_key=key,
            )
        )
        return None

    allowed_fields = {"require", "forbid"}
    unknown = sorted(set(binding) - allowed_fields)
    for field_name in unknown:
        env_issues.append(
            _issue(
                E_ENV,
                f"{path}.{field_name}",
                "unknown binding field",
                environment_id=env_id,
                config_key=key,
            )
        )

    require = binding.get("require", [])
    forbid = binding.get("forbid", [])
    valid = not unknown
    if not isinstance(require, list):
        env_issues.append(
            _issue(
                E_ENV,
                f"{path}.require",
                "must be an array of source ids",
                environment_id=env_id,
                config_key=key,
            )
        )
        require = []
        valid = False
    if not isinstance(forbid, list):
        env_issues.append(
            _issue(
                E_ENV,
                f"{path}.forbid",
                "must be an array of source ids",
                environment_id=env_id,
                config_key=key,
            )
        )
        forbid = []
        valid = False

    checked_require: list[str] = []
    checked_forbid: list[str] = []
    for field_name, values, output in (
        ("require", require, checked_require),
        ("forbid", forbid, checked_forbid),
    ):
        for index, source_id in enumerate(values):
            field_path = f"{path}.{field_name}[{index}]"
            if not _is_nonempty_str(source_id):
                env_issues.append(
                    _issue(
                        E_ENV,
                        field_path,
                        "must be a non-empty source id",
                        environment_id=env_id,
                        config_key=key,
                    )
                )
                valid = False
                continue
            if source_id in output:
                env_issues.append(
                    _issue(
                        E_ENV,
                        field_path,
                        "duplicate source id in binding list",
                        environment_id=env_id,
                        config_key=key,
                        source_id=source_id,
                    )
                )
                valid = False
                continue
            output.append(source_id)
            if source_id not in registry or source_id not in available:
                bind_issues.append(
                    _issue(
                        E_BIND,
                        field_path,
                        "binding references a source that is not available",
                        environment_id=env_id,
                        config_key=key,
                        source_id=source_id,
                    )
                )
                valid = False

    if len(checked_require) > 1:
        env_issues.append(
            _issue(
                E_ENV,
                f"{path}.require",
                "at most one source can be required for a single key",
                environment_id=env_id,
                config_key=key,
            )
        )
        valid = False
    overlap = sorted(set(checked_require) & set(checked_forbid))
    for source_id in overlap:
        env_issues.append(
            _issue(
                E_ENV,
                path,
                "the same source cannot be both required and forbidden",
                environment_id=env_id,
                config_key=key,
                source_id=source_id,
            )
        )
        valid = False
    if not valid:
        return None
    return {"require": checked_require, "forbid": checked_forbid}


def _validate_values(
    values: Any,
    path: str,
    available: set[str],
    registry: dict[str, dict[str, str]],
    env_id: str,
    env_issues: list[Issue],
) -> dict[str, dict[str, Any]]:
    if not isinstance(values, dict):
        env_issues.append(_issue(E_ENV, path, "must be an object"))
        return {}
    normalized: dict[str, dict[str, Any]] = {}
    for key in sorted(values):
        key_path = f"{path}.{key}"
        if not _is_nonempty_str(key):
            env_issues.append(
                _issue(E_ENV, key_path, "configuration key must be non-empty")
            )
            continue
        source_values = values[key]
        if not isinstance(source_values, dict):
            env_issues.append(
                _issue(
                    E_ENV,
                    key_path,
                    "key values must map source ids to values",
                    environment_id=env_id,
                    config_key=key,
                )
            )
            continue
        normalized_key: dict[str, Any] = {}
        for source_id in sorted(source_values):
            source_path = f"{key_path}.{source_id}"
            if source_id not in registry:
                env_issues.append(
                    _issue(
                        E_ENV,
                        source_path,
                        "value references an unknown source",
                        environment_id=env_id,
                        config_key=key,
                        source_id=source_id,
                    )
                )
                continue
            if source_id not in available:
                env_issues.append(
                    _issue(
                        E_ENV,
                        source_path,
                        "value references a source not available in the environment",
                        environment_id=env_id,
                        config_key=key,
                        source_id=source_id,
                    )
                )
                continue
            normalized_key[source_id] = source_values[source_id]
        normalized[key] = normalized_key
    return normalized


def _validate_environment(
    raw: dict[str, Any],
    index: int,
    registry: dict[str, dict[str, str]],
    seen_env_ids: set[str],
    env_issues: list[Issue],
    bind_issues: list[Issue],
) -> dict[str, Any] | None:
    path = f"environments[{index}]"
    env_id = raw.get("id")
    valid = True
    if not _is_nonempty_str(env_id):
        env_issues.append(_issue(E_ENV, f"{path}.id", "must be a non-empty string"))
        env_id = None
        valid = False
    elif env_id in seen_env_ids:
        env_issues.append(
            _issue(E_ENV, f"{path}.id", "duplicate environment identifier")
        )
        valid = False
    else:
        seen_env_ids.add(str(env_id))
    env_id_str = str(env_id) if env_id is not None else None

    level = raw.get("level")
    if not _is_int(level) or level < 0:
        env_issues.append(
            _issue(
                E_ENV,
                f"{path}.level",
                "must be a non-negative integer",
                environment_id=env_id_str,
            )
        )
        valid = False

    parent = raw.get("parent")
    if parent is not None and not _is_nonempty_str(parent):
        env_issues.append(
            _issue(
                E_ENV,
                f"{path}.parent",
                "must be null or a non-empty environment id",
                environment_id=env_id_str,
            )
        )
        valid = False

    available_raw = raw.get("available_sources")
    available: set[str] = set()
    if not isinstance(available_raw, list) or not available_raw:
        env_issues.append(
            _issue(
                E_ENV,
                f"{path}.available_sources",
                "must be a non-empty array",
                environment_id=env_id_str,
            )
        )
        valid = False
    else:
        for source_index, source_id in enumerate(available_raw):
            source_path = f"{path}.available_sources[{source_index}]"
            if not _is_nonempty_str(source_id):
                env_issues.append(
                    _issue(
                        E_ENV,
                        source_path,
                        "must be a non-empty source id",
                        environment_id=env_id_str,
                    )
                )
                valid = False
                continue
            if source_id not in registry:
                env_issues.append(
                    _issue(
                        E_ENV,
                        source_path,
                        "source is not in the supported source registry",
                        environment_id=env_id_str,
                        source_id=source_id,
                    )
                )
                valid = False
                continue
            if source_id in available:
                env_issues.append(
                    _issue(
                        E_ENV,
                        source_path,
                        "duplicate available source id",
                        environment_id=env_id_str,
                        source_id=source_id,
                    )
                )
                valid = False
                continue
            available.add(source_id)

    bias = raw.get("priority_bias")
    normalized_bias: dict[str, int] = {}
    if not isinstance(bias, dict):
        env_issues.append(
            _issue(
                E_ENV,
                f"{path}.priority_bias",
                "must be an object mapping source ids to integers",
                environment_id=env_id_str,
            )
        )
        valid = False
    else:
        for source_id in sorted(bias):
            bias_path = f"{path}.priority_bias.{source_id}"
            if source_id not in available:
                env_issues.append(
                    _issue(
                        E_ENV,
                        bias_path,
                        "bias source is not available in this environment",
                        environment_id=env_id_str,
                        source_id=source_id,
                    )
                )
                valid = False
                continue
            if not _is_int(bias[source_id]):
                env_issues.append(
                    _issue(
                        E_ENV,
                        bias_path,
                        "priority bias must be an integer",
                        environment_id=env_id_str,
                        source_id=source_id,
                    )
                )
                valid = False
                continue
            normalized_bias[source_id] = int(bias[source_id])
        for source_id in sorted(available - set(bias)):
            env_issues.append(
                _issue(
                    E_ENV,
                    f"{path}.priority_bias.{source_id}",
                    "priority bias is required for every available source",
                    environment_id=env_id_str,
                    source_id=source_id,
                )
            )
            valid = False

    seen_rule_ids: set[str] = set()
    global_rules = _validate_rule_list(
        raw.get("order_rules", []),
        f"{path}.order_rules",
        available,
        env_id_str,
        env_issues,
        seen_rule_ids,
        "global",
    )
    key_rules_raw = raw.get("key_order_rules", {})
    key_rules: dict[str, list[dict[str, str]]] = {}
    if not isinstance(key_rules_raw, dict):
        env_issues.append(
            _issue(
                E_ENV,
                f"{path}.key_order_rules",
                "must be an object mapping keys to rule arrays",
                environment_id=env_id_str,
            )
        )
        valid = False
    else:
        for config_key in sorted(key_rules_raw):
            key_path = f"{path}.key_order_rules.{config_key}"
            if not _is_nonempty_str(config_key):
                env_issues.append(
                    _issue(
                        E_ENV,
                        key_path,
                        "configuration key must be non-empty",
                        environment_id=env_id_str,
                    )
                )
                valid = False
                continue
            key_rules[config_key] = _validate_rule_list(
                key_rules_raw[config_key],
                key_path,
                available,
                env_id_str,
                env_issues,
                seen_rule_ids,
                "key",
                config_key=config_key,
            )

    values = _validate_values(
        raw.get("values"),
        f"{path}.values",
        available,
        registry,
        env_id_str or "",
        env_issues,
    )

    bindings_raw = raw.get("bindings", {})
    bindings: dict[str, dict[str, list[str]]] = {}
    if not isinstance(bindings_raw, dict):
        env_issues.append(
            _issue(
                E_ENV,
                f"{path}.bindings",
                "must be an object mapping keys to bindings",
                environment_id=env_id_str,
            )
        )
        valid = False
    else:
        for config_key in sorted(bindings_raw):
            binding_path = f"{path}.bindings.{config_key}"
            if not _is_nonempty_str(config_key):
                env_issues.append(
                    _issue(
                        E_ENV,
                        binding_path,
                        "configuration key must be non-empty",
                        environment_id=env_id_str,
                    )
                )
                valid = False
                continue
            normalized_binding = _validate_binding(
                config_key,
                bindings_raw[config_key],
                binding_path,
                available,
                registry,
                env_id_str or "",
                env_issues,
                bind_issues,
            )
            if normalized_binding is not None:
                bindings[config_key] = normalized_binding

    expected_raw = raw.get("expected_chain_digests", {})
    expected: dict[str, str] = {}
    if not isinstance(expected_raw, dict):
        env_issues.append(
            _issue(
                E_ENV,
                f"{path}.expected_chain_digests",
                "must be an object mapping keys to digests",
                environment_id=env_id_str,
            )
        )
        valid = False
    else:
        for config_key in sorted(expected_raw):
            digest_path = f"{path}.expected_chain_digests.{config_key}"
            digest = expected_raw[config_key]
            if not isinstance(digest, str) or not DIGEST_RE.fullmatch(digest):
                env_issues.append(
                    _issue(
                        E_ENV,
                        digest_path,
                        "must be a lowercase 64-character SHA-256 digest",
                        environment_id=env_id_str,
                        config_key=config_key,
                    )
                )
                valid = False
                continue
            expected[config_key] = digest

    if not valid or env_id_str is None:
        return None
    return {
        "id": env_id_str,
        "level": int(level),
        "parent": parent,
        "available_sources": sorted(available),
        "priority_bias": normalized_bias,
        "order_rules": sorted(
            global_rules,
            key=lambda rule: (rule["higher"], rule["lower"], rule["id"]),
        ),
        "key_order_rules": {
            key: sorted(
                key_rules[key],
                key=lambda rule: (rule["higher"], rule["lower"], rule["id"]),
            )
            for key in sorted(key_rules)
        },
        "bindings": {key: bindings[key] for key in sorted(bindings)},
        "values": {key: values[key] for key in sorted(values)},
        "expected_chain_digests": {
            key: expected[key] for key in sorted(expected)
        },
    }


def validate_scenario(raw: Any) -> dict[str, Any]:
    env_issues: list[Issue] = []
    bind_issues: list[Issue] = []
    if not isinstance(raw, dict):
        raise LoadRejected(
            E_ENV,
            [_issue(E_ENV, "$", "scenario root must be an object")],
        )

    if raw.get("schema_version") != SCHEMA_VERSION or not _is_int(
        raw.get("schema_version")
    ):
        env_issues.append(
            _issue(
                E_ENV,
                "schema_version",
                f"must be the integer {SCHEMA_VERSION}",
            )
        )

    environment_set_version = raw.get("environment_set_version")
    if not _is_nonempty_str(environment_set_version):
        env_issues.append(
            _issue(
                E_ENV,
                "environment_set_version",
                "must be a non-empty string",
            )
        )

    registry = _validate_source_registry(raw, env_issues)
    environments_raw = raw.get("environments")
    normalized_environments: list[dict[str, Any]] = []
    seen_env_ids: set[str] = set()
    if not isinstance(environments_raw, list) or not environments_raw:
        env_issues.append(
            _issue(E_ENV, "environments", "must be a non-empty array")
        )
    else:
        for index, environment in enumerate(environments_raw):
            if not isinstance(environment, dict):
                env_issues.append(
                    _issue(
                        E_ENV,
                        f"environments[{index}]",
                        "environment must be an object",
                    )
                )
                continue
            normalized = _validate_environment(
                environment,
                index,
                registry,
                seen_env_ids,
                env_issues,
                bind_issues,
            )
            if normalized is not None:
                normalized_environments.append(normalized)

    env_by_id = {environment["id"]: environment for environment in normalized_environments}
    for environment in normalized_environments:
        parent = environment["parent"]
        if parent is None:
            continue
        if parent == environment["id"]:
            env_issues.append(
                _issue(
                    E_ENV,
                    f"environments.{environment['id']}.parent",
                    "an environment cannot be its own parent",
                    environment_id=environment["id"],
                )
            )
        elif parent not in seen_env_ids:
            env_issues.append(
                _issue(
                    E_ENV,
                    f"environments.{environment['id']}.parent",
                    "parent environment does not exist",
                    environment_id=environment["id"],
                )
            )

    for environment in normalized_environments:
        if environment["parent"] not in env_by_id:
            continue
        seen: list[str] = []
        current: str | None = environment["id"]
        while current is not None and current in env_by_id:
            if current in seen:
                env_issues.append(
                    _issue(
                        E_ENV,
                        f"environments.{environment['id']}.parent",
                        "environment parent hierarchy contains a cycle",
                        environment_id=environment["id"],
                        detail={"cycle": seen[seen.index(current) :] + [current]},
                    )
                )
                break
            seen.append(current)
            current = env_by_id[current]["parent"]

    if env_issues:
        raise LoadRejected(E_ENV, sorted(
            env_issues,
            key=lambda issue: (
                issue.field_path,
                issue.code,
                issue.message,
                issue.environment_id or "",
                issue.config_key or "",
            ),
        ))
    if bind_issues:
        raise LoadRejected(E_BIND, sorted(
            bind_issues,
            key=lambda issue: (
                issue.field_path,
                issue.code,
                issue.message,
                issue.environment_id or "",
                issue.config_key or "",
                issue.source_id or "",
            ),
        ))

    return {
        "schema_version": SCHEMA_VERSION,
        "environment_set_version": str(environment_set_version),
        "sources": [registry[source_id] for source_id in sorted(registry)],
        "environments": sorted(
            normalized_environments,
            key=lambda environment: environment["id"],
        ),
    }
