"""Declarative, query-scoped fault injection."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import BizError, digest
from .rules import INJECTION_STAGES
from .sample import read_json_file


STAGE_CODES = {
    "route": "E_RULE_VERSION_CONFLICT",
    "rewrite": "E_SHARD_KEY_OUT_OF_RANGE",
    "execute": "E_PHYSICAL_TABLE_MISSING",
    "merge": "E_RESULT_DIGEST_MISMATCH",
}


@dataclass(frozen=True)
class Scenario:
    id: str
    stage: str
    code: str
    template_id: str
    params: dict[str, Any]
    shard_id: int | None
    details: dict[str, Any]

    def matches(
        self,
        stage: str,
        template_id: str,
        params: dict[str, Any],
        shard_id: int | None = None,
    ) -> bool:
        if self.stage != stage or self.template_id != template_id:
            return False
        if digest(self.params) != digest(params):
            return False
        if self.shard_id is not None and shard_id is not None and self.shard_id != shard_id:
            return False
        return True


class InjectionSet:
    def __init__(self, scenarios: list[Scenario] | None = None) -> None:
        self.scenarios = tuple(sorted(scenarios or [], key=lambda item: item.id))

    @classmethod
    def empty(cls) -> "InjectionSet":
        return cls([])

    @classmethod
    def load(cls, path: str | Path) -> "InjectionSet":
        raw = read_json_file(Path(path), code="E_BAD_INJECTION")
        if not isinstance(raw, dict) or raw.get("format_version") != 1:
            raise BizError("E_BAD_INJECTION", "injection manifest must use format_version 1")
        rows = raw.get("scenarios")
        if not isinstance(rows, list):
            raise BizError("E_BAD_INJECTION", "injection scenarios must be an array")
        scenarios: list[Scenario] = []
        seen: set[str] = set()
        for row in rows:
            if not isinstance(row, dict):
                raise BizError("E_BAD_INJECTION", "injection scenario must be an object")
            scenario_id = row.get("id")
            if not isinstance(scenario_id, str) or not scenario_id:
                raise BizError("E_BAD_INJECTION", "injection id must be a non-empty string")
            if scenario_id in seen:
                raise BizError(
                    "E_BAD_INJECTION",
                    "injection ids must be unique",
                    details={"id": scenario_id},
                )
            seen.add(scenario_id)
            stage = row.get("stage")
            if stage not in INJECTION_STAGES:
                raise BizError(
                    "E_BAD_INJECTION",
                    "injection stage must be route, rewrite, execute, or merge",
                    details={"id": scenario_id, "stage": stage},
                )
            code = row.get("code")
            if code != STAGE_CODES[stage]:
                raise BizError(
                    "E_BAD_INJECTION",
                    "injection code does not match the canonical stage error code",
                    details={
                        "expected_code": STAGE_CODES[stage],
                        "id": scenario_id,
                        "stage": stage,
                    },
                )
            match = row.get("match", {})
            if not isinstance(match, dict):
                raise BizError("E_BAD_INJECTION", "injection match must be an object")
            template_id = match.get("template_id")
            if not isinstance(template_id, str) or not template_id:
                raise BizError(
                    "E_BAD_INJECTION",
                    "injection match.template_id is required",
                    details={"id": scenario_id},
                )
            params = match.get("params", {})
            if not isinstance(params, dict):
                raise BizError(
                    "E_BAD_INJECTION",
                    "injection match.params must be an object",
                    details={"id": scenario_id},
                )
            shard_id = row.get("shard_id")
            if shard_id is not None and (type(shard_id) is not int or shard_id < 0):
                raise BizError(
                    "E_BAD_INJECTION",
                    "injection shard_id must be a non-negative integer or null",
                    details={"id": scenario_id},
                )
            if stage in {"execute", "merge"} and shard_id is None:
                raise BizError(
                    "E_BAD_INJECTION",
                    "execute and merge injections require shard_id",
                    details={"id": scenario_id},
                )
            details = row.get("details", {})
            if not isinstance(details, dict):
                raise BizError(
                    "E_BAD_INJECTION",
                    "injection details must be an object",
                    details={"id": scenario_id},
                )
            scenarios.append(
                Scenario(
                    id=scenario_id,
                    stage=stage,
                    code=code,
                    template_id=template_id,
                    params=dict(params),
                    shard_id=shard_id,
                    details=dict(details),
                )
            )
        return cls(scenarios)

    def find(
        self,
        stage: str,
        template_id: str,
        params: dict[str, Any],
        shard_id: int | None = None,
    ) -> Scenario | None:
        for scenario in self.scenarios:
            if scenario.matches(stage, template_id, params, shard_id):
                return scenario
        return None

    def summary(self) -> list[dict[str, Any]]:
        return [
            {
                "code": scenario.code,
                "id": scenario.id,
                "shard_id": scenario.shard_id,
                "stage": scenario.stage,
                "template_id": scenario.template_id,
            }
            for scenario in self.scenarios
        ]

