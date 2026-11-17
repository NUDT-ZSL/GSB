from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Iterable

from .canonical import canonical_json, sha256_json
from .errors import InputError, RuleError, VectorError
from .models import (
    LOOPBACK_HOSTS,
    BackendDecl,
    DeclarationBundle,
    Request,
    RuleDecl,
)


MAX_CONCURRENCY = 128
MAX_BATCH_SIZE = 128
MAX_PARAMETER_LENGTH = 4096

LOOPBACK_HOST = "127.0.0.1"


def _read_json_file(path: Path, code: str, root_path: str) -> Any:
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        if code == "E_VECTOR":
            raise VectorError(f"cannot read parameter vector file: {exc}", root_path)
        raise RuleError(f"cannot read declaration file: {exc}", root_path)
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        if code == "E_VECTOR":
            raise VectorError(
                f"invalid parameter vector JSON: {exc.msg}",
                root_path,
                line=exc.lineno,
                column=exc.colno,
            )
        raise RuleError(
            f"invalid declaration JSON: {exc.msg}",
            root_path,
            line=exc.lineno,
            column=exc.colno,
        )


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _require_object(value: Any, path: str, what: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise RuleError(f"{what} must be a JSON object", path)
    return value


def _require_string(value: Any, path: str, what: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RuleError(f"{what} must be a non-empty string", path)
    return value


def _bounded_int(value: Any, path: str, what: str, minimum: int, maximum: int) -> int:
    if not _is_int(value) or value < minimum or value > maximum:
        raise RuleError(
            f"{what} must be an integer in [{minimum}, {maximum}]",
            path,
            value=value,
        )
    return value


def _load_parameter_vector(
    raw_file: Any,
    *,
    base_dir: Path,
    backend_index: int,
    expected_length: int,
) -> tuple[tuple[float, ...], str]:
    field_path = f"$.backends[{backend_index}].parameter_file"
    if not isinstance(raw_file, str) or not raw_file.strip():
        raise VectorError("parameter_file must be a non-empty relative path", field_path)
    relative = Path(raw_file)
    if relative.is_absolute():
        raise VectorError("parameter_file must be relative to the declaration file", field_path)
    candidate = (base_dir / relative).resolve()
    try:
        candidate.relative_to(base_dir)
    except ValueError as exc:
        raise VectorError(
            "parameter_file escapes the declaration directory", field_path
        ) from exc
    if not candidate.is_file():
        raise VectorError("parameter vector file does not exist", field_path)
    raw = _read_json_file(candidate, "E_VECTOR", field_path)
    if not isinstance(raw, list):
        raise VectorError("parameter vector must be a JSON array", field_path)
    if len(raw) != expected_length:
        raise VectorError(
            "parameter vector length does not match parameter_length",
            field_path,
            expected_length=expected_length,
            actual_length=len(raw),
        )
    values: list[float] = []
    for index, item in enumerate(raw):
        item_path = f"{field_path}[{index}]"
        if not _is_number(item):
            raise VectorError("parameter must be a finite JSON number", item_path)
        number = float(item)
        if not math.isfinite(number):
            raise VectorError("parameter must be finite", item_path)
        values.append(0.0 if number == 0.0 else number)
    return tuple(values), str(relative.as_posix())


def _parse_backends(path: Path) -> tuple[BackendDecl, ...]:
    document = _read_json_file(path, "E_RULE", "$")
    root = _require_object(document, "$", "backend declaration")
    if "backends" not in root or not isinstance(root["backends"], list):
        raise RuleError("$.backends must be a JSON array", "$.backends")
    if not root["backends"]:
        raise RuleError("at least one backend is required", "$.backends")

    base_dir = path.resolve().parent
    backends: list[BackendDecl] = []
    backend_ids: set[str] = set()
    model_ids: set[str] = set()
    addresses: set[tuple[str, int]] = set()

    for index, raw_backend in enumerate(root["backends"]):
        path_prefix = f"$.backends[{index}]"
        backend = _require_object(raw_backend, path_prefix, "backend entry")
        backend_id = _require_string(
            backend.get("backend_id"), f"{path_prefix}.backend_id", "backend_id"
        )
        if backend_id in backend_ids:
            raise RuleError(
                "backend_id must be unique", f"{path_prefix}.backend_id", value=backend_id
            )
        backend_ids.add(backend_id)

        model_id = _require_string(
            backend.get("model_id"), f"{path_prefix}.model_id", "model_id"
        )
        if model_id in model_ids:
            raise RuleError(
                "model_id must be unique", f"{path_prefix}.model_id", value=model_id
            )
        model_ids.add(model_id)

        host_raw = backend.get("host", LOOPBACK_HOST)
        host = _require_string(host_raw, f"{path_prefix}.host", "host")
        if host not in LOOPBACK_HOSTS:
            raise RuleError(
                "host must be a loopback address", f"{path_prefix}.host", value=host
            )

        port = _bounded_int(backend.get("port"), f"{path_prefix}.port", "port", 1, 65535)
        address = (host, port)
        if address in addresses:
            raise RuleError(
                "loopback host/port pairs must be unique",
                f"{path_prefix}.port",
                address=f"{host}:{port}",
            )
        addresses.add(address)

        max_concurrency = _bounded_int(
            backend.get("max_concurrency"),
            f"{path_prefix}.max_concurrency",
            "max_concurrency",
            1,
            MAX_CONCURRENCY,
        )
        parameter_length = _bounded_int(
            backend.get("parameter_length"),
            f"{path_prefix}.parameter_length",
            "parameter_length",
            1,
            MAX_PARAMETER_LENGTH,
        )
        parameters, parameter_file = _load_parameter_vector(
            backend.get("parameter_file"),
            base_dir=base_dir,
            backend_index=index,
            expected_length=parameter_length,
        )
        backends.append(
            BackendDecl(
                backend_id=backend_id,
                model_id=model_id,
                host=host,
                port=port,
                max_concurrency=max_concurrency,
                parameter_length=parameter_length,
                parameters=parameters,
                parameter_file=parameter_file,
            )
        )
    return tuple(sorted(backends, key=lambda item: item.backend_id))


def _scalar_condition(value: Any) -> bool:
    return isinstance(value, (str, int, bool)) and not isinstance(value, float)


def _parse_rules(path: Path, backends: tuple[BackendDecl, ...]) -> tuple[
    tuple[RuleDecl, ...], tuple[str, ...], str
]:
    document = _read_json_file(path, "E_RULE", "$")
    root = _require_object(document, "$", "rule declaration")
    raw_schema = root.get("feature_schema")
    if not isinstance(raw_schema, list) or not raw_schema:
        raise RuleError("$.feature_schema must be a non-empty array", "$.feature_schema")
    features: list[str] = []
    for index, feature in enumerate(raw_schema):
        feature_path = f"$.feature_schema[{index}]"
        if not isinstance(feature, str) or not feature:
            raise RuleError("feature names must be non-empty strings", feature_path)
        if feature in features:
            raise RuleError("feature names must be unique", feature_path, value=feature)
        features.append(feature)
    feature_set = set(features)

    raw_rules = root.get("rules")
    if not isinstance(raw_rules, list) or not raw_rules:
        raise RuleError("$.rules must be a non-empty array", "$.rules")

    backend_by_id = {backend.backend_id: backend for backend in backends}
    rules: list[RuleDecl] = []
    rule_ids: set[str] = set()
    for index, raw_rule in enumerate(raw_rules):
        path_prefix = f"$.rules[{index}]"
        rule = _require_object(raw_rule, path_prefix, "rule entry")
        rule_id = _require_string(
            rule.get("rule_id"), f"{path_prefix}.rule_id", "rule_id"
        )
        if rule_id in rule_ids:
            raise RuleError("rule_id must be unique", f"{path_prefix}.rule_id", value=rule_id)
        rule_ids.add(rule_id)
        priority = _bounded_int(
            rule.get("priority"),
            f"{path_prefix}.priority",
            "priority",
            0,
            1_000_000,
        )
        raw_match = rule.get("match")
        if not isinstance(raw_match, dict):
            raise RuleError("match must be a JSON object", f"{path_prefix}.match")
        match_items: list[tuple[str, str | int | bool]] = []
        for feature, expected in raw_match.items():
            feature_path = f"{path_prefix}.match.{feature}"
            if not isinstance(feature, str) or feature not in feature_set:
                raise RuleError(
                    "match field must exist in feature_schema",
                    feature_path,
                    field=feature,
                )
            if not _scalar_condition(expected):
                raise RuleError(
                    "match value must be a string, integer, or boolean",
                    feature_path,
                    value=expected,
                )
            match_items.append((feature, expected))
        target_backend = _require_string(
            rule.get("target_backend"),
            f"{path_prefix}.target_backend",
            "target_backend",
        )
        if target_backend not in backend_by_id:
            raise RuleError(
                "target_backend does not exist",
                f"{path_prefix}.target_backend",
                value=target_backend,
            )
        batch_size = _bounded_int(
            rule.get("batch_size"),
            f"{path_prefix}.batch_size",
            "batch_size",
            1,
            MAX_BATCH_SIZE,
        )
        concurrency = backend_by_id[target_backend].max_concurrency
        if batch_size > concurrency:
            raise RuleError(
                "batch_size must not exceed target backend max_concurrency",
                f"{path_prefix}.batch_size",
                batch_size=batch_size,
                max_concurrency=concurrency,
            )
        rules.append(
            RuleDecl(
                rule_id=rule_id,
                priority=priority,
                match=tuple(sorted(match_items)),
                target_backend=target_backend,
                batch_size=batch_size,
            )
        )
    rules.sort(key=lambda item: (item.priority, item.rule_id))
    version_payload = {
        "feature_schema": sorted(features),
        "rules": [rule.to_dict() for rule in sorted(rules, key=lambda item: item.rule_id)],
    }
    rule_version = "rv1:" + sha256_json(version_payload)
    return tuple(rules), tuple(sorted(features)), rule_version


def load_declarations(
    backends_path: str | Path, rules_path: str | Path
) -> DeclarationBundle:
    backends_file = Path(backends_path).resolve()
    rules_file = Path(rules_path).resolve()
    backends = _parse_backends(backends_file)
    rules, feature_schema, rule_version = _parse_rules(rules_file, backends)
    return DeclarationBundle(
        backends=backends,
        rules=rules,
        feature_schema=feature_schema,
        rule_version=rule_version,
        backends_path=str(backends_file),
        rules_path=str(rules_file),
    )


def _validate_feature_value(value: Any, path: str) -> str | int | bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value
    if isinstance(value, int):
        return value
    raise InputError("feature value must be a string, integer, or boolean", path)


def _parse_request_document(
    raw: Any,
    *,
    root_path: str,
    feature_schema: set[str],
    seen_ids: set[str],
) -> Request:
    if not isinstance(raw, dict):
        raise InputError("request must be a JSON object", root_path)
    request_id = raw.get("request_id")
    if not isinstance(request_id, str) or not request_id:
        raise InputError("request_id must be a non-empty string", f"{root_path}.request_id")
    if request_id in seen_ids:
        raise InputError("request_id must be unique within a sequence", f"{root_path}.request_id")
    seen_ids.add(request_id)
    raw_features = raw.get("features")
    if not isinstance(raw_features, dict):
        raise InputError("features must be a JSON object", f"{root_path}.features")
    features: list[tuple[str, str | int | bool]] = []
    for key, value in raw_features.items():
        feature_path = f"{root_path}.features.{key}"
        if key not in feature_schema:
            raise InputError("request feature is not declared", feature_path, field=key)
        features.append((key, _validate_feature_value(value, feature_path)))
    raw_input = raw.get("input")
    if not isinstance(raw_input, list) or not raw_input:
        raise InputError("input must be a non-empty numeric array", f"{root_path}.input")
    inputs: list[float] = []
    for index, value in enumerate(raw_input):
        input_path = f"{root_path}.input[{index}]"
        if not _is_number(value):
            raise InputError("input values must be finite numbers", input_path)
        number = float(value)
        if not math.isfinite(number):
            raise InputError("input values must be finite", input_path)
        inputs.append(0.0 if number == 0.0 else number)
    return Request(
        request_id=request_id,
        features=tuple(sorted(features)),
        inputs=tuple(inputs),
    )


def parse_request_documents(
    documents: list[Any], feature_schema: Iterable[str]
) -> list[Request]:
    allowed_features = set(feature_schema)
    requests: list[Request] = []
    seen_ids: set[str] = set()
    for index, raw in enumerate(documents):
        requests.append(
            _parse_request_document(
                raw,
                root_path=f"$[{index}]",
                feature_schema=allowed_features,
                seen_ids=seen_ids,
            )
        )
    if not requests:
        raise InputError("request sequence is empty", "$")
    return requests


def load_requests_ndjson(
    path: str | Path, feature_schema: Iterable[str]
) -> list[Request]:
    request_path = Path(path).resolve()
    try:
        lines = request_path.read_text(encoding="utf-8-sig").splitlines()
    except OSError as exc:
        raise InputError(f"cannot read request sequence: {exc}", "$", file=str(request_path))
    allowed_features = set(feature_schema)
    seen_ids: set[str] = set()
    requests: list[Request] = []
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        root_path = f"$[{line_number - 1}]"
        try:
            raw = json.loads(line)
        except json.JSONDecodeError as exc:
            raise InputError(
                f"invalid request JSON: {exc.msg}",
                root_path,
                line=line_number,
                column=exc.colno,
            )
        requests.append(
            _parse_request_document(
                raw,
                root_path=root_path,
                feature_schema=allowed_features,
                seen_ids=seen_ids,
            )
        )
    if not requests:
        raise InputError("request sequence is empty", "$")
    return requests


def dump_declaration_summary(bundle: DeclarationBundle) -> str:
    return canonical_json(
        {
            "backends": [backend.backend_id for backend in bundle.backends],
            "feature_schema": list(bundle.feature_schema),
            "rule_version": bundle.rule_version,
            "rules": [rule.rule_id for rule in bundle.ordered_rules()],
        }
    )
