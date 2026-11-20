from __future__ import annotations

from typing import Any


class KGError(Exception):
    """A stable, machine-readable pipeline error."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        details: dict[str, Any] | None = None,
        doc_id: str | None = None,
        alias: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}
        self.doc_id = doc_id
        self.alias = alias

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "code": self.code,
            "message": self.message,
            "details": self.details,
        }
        if self.doc_id is not None:
            result["doc_id"] = self.doc_id
        if self.alias is not None:
            result["alias"] = self.alias
        return result


def error_dict(
    code: str,
    message: str,
    *,
    category: str | None = None,
    doc_id: str | None = None,
    alias: str | None = None,
    **details: Any,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "code": code,
        "message": message,
        "details": details,
    }
    if category is not None:
        payload["category"] = category
    if doc_id is not None:
        payload["doc_id"] = doc_id
    if alias is not None:
        payload["alias"] = alias
    return payload


def raise_conflict_alias(alias: str, entities: list[str]) -> None:
    raise KGError(
        "E_ALIAS_CONFLICT",
        f"别名 {alias!r} 同时指向多个规范实体",
        alias=alias,
        details={"alias": alias, "candidate_entity_ids": sorted(entities)},
    )


def raise_normalize_cycle(alias: str, path: list[str]) -> None:
    raise KGError(
        "E_NORMALIZE_CYCLE",
        f"规范化映射存在自引用或环: {alias!r}",
        alias=alias,
        details={"alias": alias, "cycle_path": path},
    )

