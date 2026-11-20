from __future__ import annotations

import copy
import re
from collections import Counter
from typing import Any

from .errors import Issue
from .jsonutil import is_int
from .model import EngineConfig, Resource

NAME_RE = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")
LABEL_KEY_RE = re.compile(
    r"^([a-z0-9]([-a-z0-9]*[a-z0-9])?(\.[a-z0-9]([-a-z0-9]*[a-z0-9])?)*/)?"
    r"[A-Za-z0-9]([-A-Za-z0-9_.]*[A-Za-z0-9])?$"
)
PROTOCOLS = {"TCP", "UDP", "SCTP"}
SELECTOR_OPERATORS = {"In", "NotIn", "Exists", "DoesNotExist"}
SELECTOR_TERM_RE = re.compile(
    r"^\s*([A-Za-z0-9](?:[-A-Za-z0-9_.]*[A-Za-z0-9])?)"
    r"\s*(==|=|!=|in|notin)\s*(.*?)\s*$",
    re.IGNORECASE,
)


class _ObjectValidator:
    def __init__(self, source: str, index: int, path: str) -> None:
        self.source = source
        self.index = index
        self.path = path
        self.issues: list[Issue] = []
        self.kind: str | None = None
        self.namespace: str | None = None
        self.name: str | None = None

    def issue(self, code: str, path: str, message: str) -> None:
        self.issues.append(
            Issue(
                code=code,
                source=self.source,
                index=self.index,
                kind=self.kind,
                namespace=self.namespace,
                name=self.name,
                path=path,
                message=message,
            )
        )

    def require_string(
        self, value: Any, path: str, *, pattern: re.Pattern[str] | None = None
    ) -> str | None:
        if not isinstance(value, str) or not value:
            self.issue("E_BAD_RESOURCE", path, "must be a non-empty string")
            return None
        if pattern is not None and not pattern.fullmatch(value):
            self.issue("E_BAD_RESOURCE", path, "contains invalid characters")
            return None
        return value

    def labels(self, value: Any, path: str) -> dict[str, str] | None:
        if not isinstance(value, dict):
            self.issue("E_BAD_RESOURCE", path, "labels must be an object")
            return None
        result: dict[str, str] = {}
        for key in sorted(value):
            label_path = f"{path}.{key}"
            if not isinstance(key, str) or not LABEL_KEY_RE.fullmatch(key):
                self.issue(
                    "E_BAD_RESOURCE", label_path, "invalid label key syntax"
                )
                continue
            item = value[key]
            if not isinstance(item, str):
                self.issue(
                    "E_BAD_RESOURCE", label_path, "label value must be a string"
                )
                continue
            result[key] = item
        return result

    def selector(self, value: Any, path: str) -> dict[str, Any] | None:
        try:
            return parse_selector(value)
        except ValueError as exc:
            self.issue("E_BAD_SELECTOR", path, str(exc))
            return None

    def ports(
        self, value: Any, path: str, *, required: bool
    ) -> list[dict[str, Any]] | None:
        if value is None and not required:
            return []
        if not isinstance(value, list):
            self.issue("E_BAD_RESOURCE", path, "ports must be an array")
            return None
        if required and not value:
            self.issue("E_BAD_RESOURCE", path, "ports must not be empty")
            return None
        result: list[dict[str, Any]] = []
        seen_names: set[str] = set()
        for index, port in enumerate(value):
            port_path = f"{path}[{index}]"
            if not isinstance(port, dict):
                self.issue(
                    "E_BAD_RESOURCE", port_path, "port entry must be an object"
                )
                continue
            number = port.get("port")
            if not is_int(number) or not 1 <= number <= 65535:
                self.issue(
                    "E_BAD_RESOURCE",
                    f"{port_path}.port",
                    "port must be an integer in [1, 65535]",
                )
                continue
            target = port.get("targetPort", number)
            if not (
                (is_int(target) and 1 <= target <= 65535)
                or (isinstance(target, str) and bool(target))
            ):
                self.issue(
                    "E_BAD_RESOURCE",
                    f"{port_path}.targetPort",
                    "targetPort must be a valid integer or non-empty name",
                )
                continue
            protocol = port.get("protocol", "TCP")
            if not isinstance(protocol, str) or protocol.upper() not in PROTOCOLS:
                self.issue(
                    "E_BAD_RESOURCE",
                    f"{port_path}.protocol",
                    "protocol must be TCP, UDP, or SCTP",
                )
                continue
            normalized: dict[str, Any] = {
                "port": number,
                "targetPort": target,
                "protocol": protocol.upper(),
            }
            if "name" in port:
                name = port["name"]
                if not isinstance(name, str) or not name:
                    self.issue(
                        "E_BAD_RESOURCE",
                        f"{port_path}.name",
                        "port name must be a non-empty string",
                    )
                    continue
                if name in seen_names:
                    self.issue(
                        "E_DUP_KEY",
                        f"{port_path}.name",
                        f"duplicate port name: {name}",
                    )
                    continue
                seen_names.add(name)
                normalized["name"] = name
            result.append(normalized)
        return result

    def config_map_refs(
        self, value: Any, path: str
    ) -> list[str] | None:
        if value is None:
            return []
        if not isinstance(value, list) or not all(
            isinstance(item, str) and item for item in value
        ):
            self.issue(
                "E_BAD_RESOURCE",
                path,
                "configMapRefs must be an array of non-empty strings",
            )
            return None
        if len(value) != len(set(value)):
            self.issue(
                "E_DUP_KEY", path, "configMapRefs must not contain duplicates"
            )
            return None
        return list(value)

    def validate(self, raw: Any) -> Resource | None:
        if not isinstance(raw, dict):
            self.issue(
                "E_BAD_RESOURCE", self.path, "resource must be a JSON object"
            )
            return None

        kind = raw.get("kind")
        if kind not in ("Deployment", "Service", "ConfigMap"):
            self.issue(
                "E_BAD_RESOURCE",
                f"{self.path}.kind",
                "kind must be Deployment, Service, or ConfigMap",
            )
            return None
        self.kind = kind

        namespace = raw.get("namespace")
        if (
            not isinstance(namespace, str)
            or not 1 <= len(namespace) <= 63
            or not NAME_RE.fullmatch(namespace)
        ):
            self.issue(
                "E_BAD_RESOURCE",
                f"{self.path}.namespace",
                "namespace must be a valid DNS label",
            )
            return None
        self.namespace = namespace

        name = raw.get("name")
        if (
            not isinstance(name, str)
            or not 1 <= len(name) <= 253
            or not NAME_RE.fullmatch(name)
        ):
            self.issue(
                "E_BAD_RESOURCE",
                f"{self.path}.name",
                "name must be a valid DNS label",
            )
            return None
        self.name = name

        body = copy.deepcopy(raw)
        labels = self.labels(raw.get("labels", {}), f"{self.path}.labels")
        if labels is not None:
            body["labels"] = labels

        if kind == "Deployment":
            replicas = raw.get("replicas")
            if not is_int(replicas) or not 0 <= replicas <= 1_000_000:
                self.issue(
                    "E_BAD_RESOURCE",
                    f"{self.path}.replicas",
                    "replicas must be an integer in [0, 1000000]",
                )
            image = self.require_string(
                raw.get("image"), f"{self.path}.image"
            )
            if image is not None:
                body["image"] = image
            selector = self.selector(
                raw.get("selector"), f"{self.path}.selector"
            )
            if selector is not None:
                body["selector"] = selector
            refs = self.config_map_refs(
                raw.get("configMapRefs", []),
                f"{self.path}.configMapRefs",
            )
            if refs is not None:
                body["configMapRefs"] = refs
            ports = self.ports(
                raw.get("ports", []),
                f"{self.path}.ports",
                required=False,
            )
            if ports is not None:
                body["ports"] = ports
        elif kind == "Service":
            selector = self.selector(
                raw.get("selector"), f"{self.path}.selector"
            )
            if selector is not None:
                body["selector"] = selector
            ports = self.ports(
                raw.get("ports"), f"{self.path}.ports", required=True
            )
            if ports is not None:
                body["ports"] = ports
        else:
            data = raw.get("data")
            if not isinstance(data, dict):
                self.issue(
                    "E_BAD_RESOURCE",
                    f"{self.path}.data",
                    "data must be an object",
                )
            else:
                normalized_data: dict[str, str] = {}
                for key in sorted(data):
                    value = data[key]
                    if not isinstance(key, str) or not key:
                        self.issue(
                            "E_BAD_RESOURCE",
                            f"{self.path}.data",
                            "data keys must be non-empty strings",
                        )
                        continue
                    if not isinstance(value, str):
                        self.issue(
                            "E_BAD_RESOURCE",
                            f"{self.path}.data.{key}",
                            "ConfigMap data values must be strings",
                        )
                        continue
                    normalized_data[key] = value
                body["data"] = normalized_data

        if self.issues:
            return None
        return Resource(kind, namespace, name, body)


def _split_selector_terms(value: str) -> list[str]:
    terms: list[str] = []
    current: list[str] = []
    depth = 0
    for character in value:
        if character == "(":
            depth += 1
        elif character == ")":
            depth -= 1
            if depth < 0:
                raise ValueError("selector has unbalanced parentheses")
        if character == "," and depth == 0:
            term = "".join(current).strip()
            if term:
                terms.append(term)
            current = []
        else:
            current.append(character)
    if depth != 0:
        raise ValueError("selector has unbalanced parentheses")
    term = "".join(current).strip()
    if term:
        terms.append(term)
    return terms


def _parse_selector_string(value: str) -> dict[str, Any]:
    match_labels: dict[str, str] = {}
    expressions: list[dict[str, Any]] = []
    for raw_term in _split_selector_terms(value):
        term = raw_term.strip()
        match = SELECTOR_TERM_RE.fullmatch(term)
        if match:
            key = match.group(1)
            operator = match.group(2).lower()
            raw_values = match.group(3).strip()
            if not LABEL_KEY_RE.fullmatch(key):
                raise ValueError(f"invalid selector key: {key}")
            if operator in ("=", "=="):
                if raw_values.startswith("(") or not raw_values:
                    raise ValueError(f"invalid equality selector: {term}")
                match_labels[key] = raw_values
            elif operator == "!=":
                if raw_values.startswith("(") or not raw_values:
                    raise ValueError(f"invalid inequality selector: {term}")
                expressions.append(
                    {"key": key, "operator": "NotIn", "values": [raw_values]}
                )
            else:
                if not (
                    raw_values.startswith("(") and raw_values.endswith(")")
                ):
                    raise ValueError(f"invalid set selector: {term}")
                values = [
                    item.strip()
                    for item in raw_values[1:-1].split(",")
                    if item.strip()
                ]
                if not values:
                    raise ValueError(f"empty set selector: {term}")
                expressions.append(
                    {
                        "key": key,
                        "operator": (
                            "In" if operator == "in" else "NotIn"
                        ),
                        "values": values,
                    }
                )
            continue
        if term.startswith("!"):
            key = term[1:].strip()
            if not key or not LABEL_KEY_RE.fullmatch(key):
                raise ValueError(f"invalid existence selector: {term}")
            expressions.append(
                {"key": key, "operator": "DoesNotExist", "values": []}
            )
            continue
        if LABEL_KEY_RE.fullmatch(term):
            expressions.append(
                {"key": term, "operator": "Exists", "values": []}
            )
            continue
        raise ValueError(f"cannot parse selector term: {term}")
    return {
        "matchLabels": dict(sorted(match_labels.items())),
        "matchExpressions": expressions,
    }


def parse_selector(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        return _parse_selector_string(value)
    if not isinstance(value, dict):
        raise ValueError("selector must be an object or selector string")
    unknown = set(value) - {"matchLabels", "matchExpressions"}
    if unknown:
        raise ValueError(
            "selector contains unknown keys: " + ", ".join(sorted(unknown))
        )
    raw_labels = value.get("matchLabels", {})
    if not isinstance(raw_labels, dict):
        raise ValueError("selector.matchLabels must be an object")
    match_labels: dict[str, str] = {}
    for key, item in raw_labels.items():
        if not isinstance(key, str) or not LABEL_KEY_RE.fullmatch(key):
            raise ValueError(f"invalid selector label key: {key}")
        if not isinstance(item, str):
            raise ValueError(
                f"selector label value for {key} must be a string"
            )
        match_labels[key] = item

    raw_expressions = value.get("matchExpressions", [])
    if not isinstance(raw_expressions, list):
        raise ValueError("selector.matchExpressions must be an array")
    expressions: list[dict[str, Any]] = []
    for index, expression in enumerate(raw_expressions):
        if not isinstance(expression, dict):
            raise ValueError(
                f"selector.matchExpressions[{index}] must be an object"
            )
        if set(expression) - {"key", "operator", "values"}:
            raise ValueError(
                f"selector.matchExpressions[{index}] has unknown keys"
            )
        key = expression.get("key")
        operator = expression.get("operator")
        values = expression.get("values", [])
        if not isinstance(key, str) or not LABEL_KEY_RE.fullmatch(key):
            raise ValueError(
                f"selector.matchExpressions[{index}].key is invalid"
            )
        if operator not in SELECTOR_OPERATORS:
            raise ValueError(
                f"selector.matchExpressions[{index}].operator is invalid"
            )
        if not isinstance(values, list) or not all(
            isinstance(item, str) for item in values
        ):
            raise ValueError(
                f"selector.matchExpressions[{index}].values must be strings"
            )
        if operator in ("In", "NotIn") and not values:
            raise ValueError(
                f"selector.matchExpressions[{index}] requires values"
            )
        if operator in ("Exists", "DoesNotExist") and values:
            raise ValueError(
                f"selector.matchExpressions[{index}] must not have values"
            )
        expressions.append(
            {
                "key": key,
                "operator": operator,
                "values": sorted(values),
            }
        )
    return {
        "matchLabels": dict(sorted(match_labels.items())),
        "matchExpressions": sorted(
            expressions,
            key=lambda item: (
                item["key"],
                item["operator"],
                tuple(item["values"]),
            ),
        ),
    }


def selector_matches(selector: dict[str, Any], labels: dict[str, str]) -> bool:
    for key, value in selector.get("matchLabels", {}).items():
        if labels.get(key) != value:
            return False
    for expression in selector.get("matchExpressions", []):
        key = expression["key"]
        operator = expression["operator"]
        values = expression["values"]
        present = key in labels
        if operator == "In" and (not present or labels[key] not in values):
            return False
        if operator == "NotIn" and present and labels[key] in values:
            return False
        if operator == "Exists" and not present:
            return False
        if operator == "DoesNotExist" and present:
            return False
    return True


def _parse_source(
    document: Any, source: str, issue_prefix: str
) -> tuple[dict[str, Resource], list[Issue]]:
    issues: list[Issue] = []
    if not isinstance(document, dict):
        issues.append(
            Issue(
                code="E_BAD_RESOURCE",
                source=source,
                path="$",
                message="resource document root must be an object",
                quarantined=True,
            )
        )
        return {}, issues
    raw_objects = document.get("objects")
    if not isinstance(raw_objects, list):
        issues.append(
            Issue(
                code="E_BAD_RESOURCE",
                source=source,
                path="$.objects",
                message="objects must be an array",
                quarantined=True,
            )
        )
        return {}, issues

    parsed: list[tuple[int, Resource]] = []
    for index, raw in enumerate(raw_objects):
        validator = _ObjectValidator(
            source, index, f"$.objects[{index}]"
        )
        resource = validator.validate(raw)
        issues.extend(validator.issues)
        if resource is not None:
            parsed.append((index, resource))

    counts = Counter(
        (resource.namespace, resource.name)
        for _, resource in parsed
    )
    duplicated = {key for key, count in counts.items() if count > 1}
    if duplicated:
        retained: list[tuple[int, Resource]] = []
        for index, resource in parsed:
            if (resource.namespace, resource.name) in duplicated:
                issues.append(
                    Issue(
                        code="E_DUP_KEY",
                        source=source,
                        index=index,
                        kind=resource.kind,
                        namespace=resource.namespace,
                        name=resource.name,
                        path=f"$.objects[{index}]",
                        message=(
                            "duplicate namespace/name identity across resources: "
                            f"{resource.namespace}/{resource.name}"
                        ),
                    )
                )
            else:
                retained.append((index, resource))
        parsed = retained

    resources = {resource.key: resource for _, resource in parsed}
    issues.sort(key=Issue.sort_key)
    return resources, issues


def _validate_actual_references(
    actual: dict[str, Resource], issues: list[Issue]
) -> dict[str, Resource]:
    config_maps = {
        (resource.namespace, resource.name)
        for resource in actual.values()
        if resource.kind == "ConfigMap"
    }
    quarantined: set[str] = set()
    for resource in sorted(actual.values(), key=lambda item: item.sort_key):
        if resource.kind != "Deployment":
            continue
        for ref in resource.body.get("configMapRefs", []):
            if (resource.namespace, ref) not in config_maps:
                quarantined.add(resource.key)
                issues.append(
                    Issue(
                        code="E_DANGLING_REF",
                        source="actual",
                        kind=resource.kind,
                        namespace=resource.namespace,
                        name=resource.name,
                        path="$.configMapRefs",
                        message=(
                            "Deployment references a ConfigMap that is absent "
                            f"from actual state: {resource.namespace}/{ref}"
                        ),
                        extra={"missingConfigMap": ref},
                    )
                )
    if quarantined:
        for key in quarantined:
            del actual[key]
        issues.sort(key=Issue.sort_key)
    return actual


def parse_bundle(
    expected_document: Any,
    actual_document: Any,
    config_document: Any,
) -> tuple[
    dict[str, Resource],
    dict[str, Resource],
    EngineConfig,
    list[Issue],
]:
    desired, desired_issues = _parse_source(
        expected_document, "expected", "expected"
    )
    actual, actual_issues = _parse_source(
        actual_document, "actual", "actual"
    )
    actual = _validate_actual_references(actual, actual_issues)
    config = EngineConfig.from_dict(config_document)
    issues = sorted(desired_issues + actual_issues, key=Issue.sort_key)
    return desired, actual, config, issues


def load_bundle(
    expected_document: Any,
    actual_document: Any,
    config_document: Any,
) -> tuple[dict[str, Resource], dict[str, Resource], EngineConfig, list[Issue]]:
    desired, actual, config, issues = parse_bundle(
        expected_document, actual_document, config_document
    )
    return desired, actual, config, issues

