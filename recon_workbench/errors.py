"""Business failures and deterministic error records."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

from .canonical import canonical_json


E_VIEW_INVALID = "E_VIEW_INVALID"
E_MATCH_INSUFFICIENT = "E_MATCH_INSUFFICIENT"
E_NONFINITE_FEATURE = "E_NONFINITE_FEATURE"
E_CONFIG_INVALID = "E_CONFIG_INVALID"
E_CONFIG_MISMATCH = "E_CONFIG_MISMATCH"
E_STAGE_EXCEPTION = "E_STAGE_EXCEPTION"

STAGE_ORDER = {
    "config": 0,
    "snapshot": 1,
    "features": 2,
    "matching": 3,
    "depth": 4,
    "merge": 5,
}


@dataclass(frozen=True)
class Failure:
    code: str
    stage: str
    message: str
    view_index: int | None = None
    pair: tuple[int, int] | None = None
    injection_ids: tuple[str, ...] = ()
    detail: dict[str, Any] = field(default_factory=dict)

    def sort_key(self) -> tuple[Any, ...]:
        pair_key = (self.pair[0], self.pair[1]) if self.pair else (-1, -1)
        view_key = self.view_index if self.view_index is not None else -1
        return (
            STAGE_ORDER.get(self.stage, 99),
            pair_key,
            view_key,
            self.code,
            self.injection_ids,
            self.message,
        )

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "code": self.code,
            "stage": self.stage,
            "message": self.message,
        }
        if self.view_index is not None:
            result["view_index"] = self.view_index
        if self.pair is not None:
            result["pair"] = [self.pair[0], self.pair[1]]
        if self.injection_ids:
            result["injection_ids"] = list(self.injection_ids)
        if self.detail:
            result["detail"] = self.detail
        return result


def failure_from_dict(record: dict[str, Any]) -> Failure:
    pair_value = record.get("pair")
    pair = None
    if pair_value is not None:
        pair = (int(pair_value[0]), int(pair_value[1]))
    return Failure(
        code=str(record["code"]),
        stage=str(record["stage"]),
        message=str(record.get("message", "")),
        view_index=(int(record["view_index"]) if record.get("view_index") is not None else None),
        pair=pair,
        injection_ids=tuple(str(value) for value in record.get("injection_ids", [])),
        detail=dict(record.get("detail", {})),
    )


def sort_failures(failures: Iterable[Failure]) -> list[Failure]:
    # Deduplicate by all meaningful fields while retaining the first occurrence.
    unique: dict[tuple[Any, ...], Failure] = {}
    for failure in failures:
        payload = failure.to_dict()
        key = (
            payload.get("code"),
            payload.get("stage"),
            payload.get("view_index"),
            tuple(payload.get("pair", [])),
            tuple(payload.get("injection_ids", [])),
            payload.get("message"),
            canonical_json(payload.get("detail", {})),
        )
        unique.setdefault(key, failure)
    return sorted(unique.values(), key=lambda item: item.sort_key())


class BusinessRuleError(RuntimeError):
    """A task-level, reportable failure that must not abort sibling tasks."""

    def __init__(self, failure: Failure):
        super().__init__(failure.message)
        self.failure = failure
