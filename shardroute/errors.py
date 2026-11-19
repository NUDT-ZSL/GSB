"""Business errors and canonical serialization helpers."""

from __future__ import annotations

import hashlib
import json
from typing import Any


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


class BizError(Exception):
    """A deterministic, machine-readable business failure."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        stage: str | None = None,
        query_template: str | None = None,
        target_shards: list[int] | None = None,
        rule_version: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.stage = stage
        self.query_template = query_template
        self.target_shards = sorted(target_shards or [])
        self.rule_version = rule_version
        self.details = details or {}

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "details": self.details,
            "message": self.message,
            "query_template": self.query_template,
            "rule_version": self.rule_version,
            "stage": self.stage,
            "target_shards": self.target_shards,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "BizError":
        return cls(
            str(value.get("code", "E_UNKNOWN")),
            str(value.get("message", "unknown error")),
            stage=value.get("stage"),
            query_template=value.get("query_template"),
            target_shards=value.get("target_shards"),
            rule_version=value.get("rule_version"),
            details=value.get("details"),
        )

    def with_context(
        self,
        *,
        query_template: str | None = None,
        target_shards: list[int] | None = None,
        rule_version: str | None = None,
        stage: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> "BizError":
        self.query_template = self.query_template or query_template
        if target_shards is not None:
            self.target_shards = sorted(target_shards)
        self.rule_version = self.rule_version or rule_version
        self.stage = self.stage or stage
        if details:
            merged = dict(details)
            merged.update(self.details)
            self.details = merged
        return self


def error_envelope(exc: BizError) -> dict[str, Any]:
    return {"error": exc.as_dict(), "ok": False}

