"""Versioned experiment session shared by CLI, tests, and the Tk GUI."""

from __future__ import annotations

import math
import threading
from dataclasses import dataclass
from typing import Any, Iterable

from .catalog import Catalog, TableData, load_catalog
from .cost import CostParams, PlanSet, enumerate_plans
from .errors import SchemaError, StaleVersionError
from .executor import ExecutionBundle, execute_and_verify
from .query import Query, parse_query


@dataclass(frozen=True)
class SessionVersions:
    schema_version: int
    data_version: int
    param_version: int

    def to_dict(self) -> dict[str, int]:
        return {
            "schema_version": self.schema_version,
            "data_version": self.data_version,
            "param_version": self.param_version,
        }


@dataclass(frozen=True)
class Evaluation:
    name: str | None
    query: Query
    plans: PlanSet
    execution: ExecutionBundle
    versions: SessionVersions
    sample_scale: float
    sequence: int

    @property
    def selected_plan(self):
        assert self.plans.selected is not None
        return self.plans.selected

    @property
    def summary(self) -> dict[str, Any]:
        selected = self.execution.selected
        fallback = self.execution.fallback
        return {
            "query_id": self.query.query_id,
            "canonical_sql": self.query.canonical_sql,
            "selected_plan_id": selected.plan_id,
            "selected_plan_signature": self.selected_plan.plan_signature,
            "fallback_plan_id": fallback.plan_id,
            "result_digest": selected.digest,
            "row_count": selected.row_count,
            "stats_fingerprint": selected.stats_fingerprint,
            "consistency": self.execution.consistency.to_dict(),
            "versions": self.versions.to_dict(),
            "sample_scale": self.sample_scale,
        }

    def is_current(self, session: "ExperimentSession") -> bool:
        return self.versions == session.versions

    def to_dict(
        self,
        *,
        include_rows: bool = False,
        include_candidates: bool = True,
        current_versions: SessionVersions | None = None,
    ) -> dict[str, Any]:
        stale = current_versions is not None and self.versions != current_versions
        result: dict[str, Any] = {
            "name": self.name,
            "sequence": self.sequence,
            "versions": self.versions.to_dict(),
            "sample_scale": self.sample_scale,
            "query": {
                "source_sql": self.query.source_text,
                "canonical_sql": self.query.canonical_sql,
                "query_id": self.query.query_id,
            },
            "selected_plan_id": self.selected_plan.plan_id,
            "selected_plan": self.selected_plan.to_dict(self.plans.params, selected=True),
            "execution": self.execution.selected.to_dict(include_rows=include_rows),
            "fallback_execution": self.execution.fallback.to_dict(include_rows=False),
            "consistency": self.execution.consistency.to_dict(),
            "attribution_chain": list(self.execution.selected.attribution_chain),
            "stale": stale,
        }
        if include_candidates:
            result["candidates"] = self.plans.to_dict()["candidates"]
            result["decision_rule"] = self.plans.decision_rule
        return result


class ExperimentSession:
    def __init__(
        self,
        catalog: Catalog,
        params: CostParams | None = None,
        sample_scale: float = 1.0,
        *,
        param_version: int = 1,
        history: Iterable[Evaluation] | None = None,
    ) -> None:
        self.catalog = catalog
        self._params = params if params is not None else CostParams()
        self._sample_scale = self._validate_scale(sample_scale)
        self._schema_version = catalog.schema_version
        self._data_version = catalog.data_version
        self._param_version = int(param_version)
        if self._param_version < 1:
            raise SchemaError("param_version must be a positive integer", "session.param_version")
        self._history: list[Evaluation] = list(history or ())
        self._sequence = max((entry.sequence for entry in self._history), default=0)
        self._lock = threading.RLock()
        self._view_scale: float | None = None
        self._view_data: dict[str, TableData] | None = None

    @classmethod
    def from_catalog_path(
        cls,
        path: str,
        *,
        params: CostParams | None = None,
        sample_scale: float = 1.0,
    ) -> "ExperimentSession":
        return cls(load_catalog(path), params=params, sample_scale=sample_scale)

    @staticmethod
    def _validate_scale(scale: float) -> float:
        if isinstance(scale, bool) or not isinstance(scale, (int, float)) or not math.isfinite(float(scale)):
            raise SchemaError("sample scale must be a finite number", "sample_scale")
        if not (0 < float(scale) <= 1):
            raise SchemaError("sample scale must be in (0, 1]", "sample_scale")
        return float(scale)

    @property
    def versions(self) -> SessionVersions:
        with self._lock:
            return SessionVersions(self._schema_version, self._data_version, self._param_version)

    @property
    def params(self) -> CostParams:
        with self._lock:
            return self._params

    @property
    def sample_scale(self) -> float:
        with self._lock:
            return self._sample_scale

    @property
    def history(self) -> tuple[Evaluation, ...]:
        with self._lock:
            return tuple(self._history)

    def latest_evaluations(self) -> tuple[Evaluation, ...]:
        with self._lock:
            latest: dict[str, Evaluation] = {}
            for entry in self._history:
                latest[entry.query.query_id] = entry
            return tuple(sorted(latest.values(), key=lambda entry: entry.sequence))

    def is_current(self, versions: SessionVersions) -> bool:
        return self.versions == versions

    def publishable(self, evaluation: Evaluation) -> bool:
        """A stale artifact is never allowed into selection or presentation."""

        return evaluation.versions == self.versions

    def update_cost_params(self, patch: dict[str, Any]) -> SessionVersions:
        with self._lock:
            if not isinstance(patch, dict):
                raise SchemaError("cost parameter patch must be an object", "cost_params")
            self._params = self._params.with_updates(patch)
            self._param_version += 1
            return SessionVersions(self._schema_version, self._data_version, self._param_version)

    def replace_cost_params(self, params: CostParams) -> SessionVersions:
        if not isinstance(params, CostParams):
            raise SchemaError("params must be CostParams", "cost_params")
        with self._lock:
            self._params = params
            self._param_version += 1
            return SessionVersions(self._schema_version, self._data_version, self._param_version)

    def update_sample_scale(self, scale: float) -> SessionVersions:
        new_scale = self._validate_scale(scale)
        with self._lock:
            if new_scale != self._sample_scale:
                self._sample_scale = new_scale
                self._data_version += 1
                self._param_version += 1
                self._view_scale = None
                self._view_data = None
            return SessionVersions(self._schema_version, self._data_version, self._param_version)

    def replace_sample_scale(self, scale: float, *, data_version: int) -> SessionVersions:
        """Used by snapshot restore to install an exact recorded scale/version."""

        new_scale = self._validate_scale(scale)
        if isinstance(data_version, bool) or not isinstance(data_version, int) or data_version < 1:
            raise SchemaError("data_version must be a positive integer", "snapshot.data_version")
        with self._lock:
            self._sample_scale = new_scale
            self._data_version = data_version
            self._view_scale = None
            self._view_data = None
        return self.versions

    def _data_view(self) -> dict[str, TableData]:
        with self._lock:
            if self._view_data is not None and self._view_scale == self._sample_scale:
                return self._view_data
            scale = self._sample_scale
        view_items, _ = self.catalog.scoped_data(scale)
        view = {item.table_key: item for item in view_items}
        with self._lock:
            if scale == self._sample_scale:
                self._view_scale = scale
                self._view_data = view
            return view

    def parse(self, sql: str) -> Query:
        return parse_query(sql, self.catalog)

    def evaluate(
        self,
        query: Query | str,
        *,
        name: str | None = None,
        expected_versions: SessionVersions | None = None,
    ) -> Evaluation:
        """Estimate and execute against one immutable version snapshot.

        If parameters or data change while evaluation is running, the result is
        rejected with E_STALE and is never appended to history or used for
        selection.
        """

        if isinstance(query, str):
            query = self.parse(query)
        with self._lock:
            captured = SessionVersions(self._schema_version, self._data_version, self._param_version)
            params = self._params
            sample_scale = self._sample_scale
        if expected_versions is not None and captured != expected_versions:
            raise StaleVersionError(
                "evaluation request was superseded before it started",
                "session.versions",
                expected=expected_versions.to_dict(),
                actual=captured.to_dict(),
            )
        data_view = self._data_view()
        plans = enumerate_plans(query, self.catalog, params, sample_scale)
        bundle = execute_and_verify(
            query,
            self.catalog,
            plans.selected,  # type: ignore[arg-type]
            plans.fallback_plan(),
            data_view,
        )
        with self._lock:
            current = SessionVersions(self._schema_version, self._data_version, self._param_version)
            if current != captured:
                raise StaleVersionError(
                    "evaluation result became stale while executing",
                    "session.versions",
                    expected=captured.to_dict(),
                    actual=current.to_dict(),
                )
            self._sequence += 1
            evaluation = Evaluation(
                name=name,
                query=query,
                plans=plans,
                execution=bundle,
                versions=captured,
                sample_scale=sample_scale,
                sequence=self._sequence,
            )
            self._history.append(evaluation)
            return evaluation

    def advance_param_version(self) -> SessionVersions:
        """Test/GUI helper for simulating a burst of parameter edits."""

        with self._lock:
            self._param_version += 1
            return SessionVersions(self._schema_version, self._data_version, self._param_version)

    def clear_history(self) -> None:
        with self._lock:
            self._history.clear()
            self._sequence = 0
