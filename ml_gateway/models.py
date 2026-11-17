from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .canonical import canonical_float_vector, canonical_json


LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1"})


@dataclass(frozen=True)
class BackendDecl:
    backend_id: str
    model_id: str
    host: str
    port: int
    max_concurrency: int
    parameter_length: int
    parameters: tuple[float, ...]
    parameter_file: str

    def config_summary(self) -> dict[str, Any]:
        return {
            "backend_id": self.backend_id,
            "model_id": self.model_id,
            "host": self.host,
            "port": self.port,
            "max_concurrency": self.max_concurrency,
            "parameter_length": self.parameter_length,
        }

    def canonical_vector(self) -> str:
        return canonical_float_vector(self.parameters)


@dataclass(frozen=True)
class RuleDecl:
    rule_id: str
    priority: int
    match: tuple[tuple[str, str | int | bool], ...]
    target_backend: str
    batch_size: int

    def match_dict(self) -> dict[str, str | int | bool]:
        return dict(self.match)

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "priority": self.priority,
            "match": self.match_dict(),
            "target_backend": self.target_backend,
            "batch_size": self.batch_size,
        }


@dataclass(frozen=True)
class DeclarationBundle:
    backends: tuple[BackendDecl, ...]
    rules: tuple[RuleDecl, ...]
    feature_schema: tuple[str, ...]
    rule_version: str
    backends_path: str
    rules_path: str

    @property
    def backend_by_id(self) -> dict[str, BackendDecl]:
        return {backend.backend_id: backend for backend in self.backends}

    @property
    def rule_by_id(self) -> dict[str, RuleDecl]:
        return {rule.rule_id: rule for rule in self.rules}

    def ordered_rules(self) -> list[RuleDecl]:
        return sorted(self.rules, key=lambda rule: (rule.priority, rule.rule_id))

    def rules_document(self) -> list[dict[str, Any]]:
        return [rule.to_dict() for rule in sorted(self.rules, key=lambda item: item.rule_id)]


@dataclass(frozen=True)
class Request:
    request_id: str
    features: tuple[tuple[str, str | int | bool], ...]
    inputs: tuple[float, ...]

    @property
    def feature_dict(self) -> dict[str, str | int | bool]:
        return dict(self.features)

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "features": self.feature_dict,
            "input": list(self.inputs),
        }


@dataclass(frozen=True)
class BackendState:
    backend_id: str
    model_id: str
    host: str
    port: int
    max_concurrency: int
    parameter_length: int
    load_sequence: int
    content_signature: str
    config_digest: str
    fingerprint: str
    canonical_vector: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "backend_id": self.backend_id,
            "model_id": self.model_id,
            "host": self.host,
            "port": self.port,
            "max_concurrency": self.max_concurrency,
            "parameter_length": self.parameter_length,
            "load_sequence": self.load_sequence,
            "content_signature": self.content_signature,
            "config_digest": self.config_digest,
            "fingerprint": self.fingerprint,
            "canonical_vector": self.canonical_vector,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "BackendState":
        return cls(
            backend_id=str(value["backend_id"]),
            model_id=str(value["model_id"]),
            host=str(value["host"]),
            port=int(value["port"]),
            max_concurrency=int(value["max_concurrency"]),
            parameter_length=int(value["parameter_length"]),
            load_sequence=int(value["load_sequence"]),
            content_signature=str(value["content_signature"]),
            config_digest=str(value["config_digest"]),
            fingerprint=str(value["fingerprint"]),
            canonical_vector=str(value["canonical_vector"]),
        )


def rule_payload(bundle: DeclarationBundle) -> str:
    return canonical_json(
        {
            "feature_schema": list(bundle.feature_schema),
            "rules": bundle.rules_document(),
        }
    )
