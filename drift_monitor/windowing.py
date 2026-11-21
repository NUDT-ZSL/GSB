"""Reference-window sliding with exact removal of leaving batches."""

from __future__ import annotations

from dataclasses import replace as dataclass_replace
from dataclasses import dataclass, field
from typing import Any, Dict, List, Sequence

from .config_model import NUMERIC, FeatureSpec
from .errors import DriftError, ErrorCode
from .statistics import (
    BatchStat,
    CategoricalStat,
    NumericStat,
    fold_window,
    merge_categorical,
    merge_numeric,
    subtract_categorical,
    subtract_numeric,
)


def replace_numeric(stat: NumericStat) -> NumericStat:
    return dataclass_replace(stat, sorted_values=list(stat.sorted_values))


def replace_categorical(stat: CategoricalStat) -> CategoricalStat:
    return dataclass_replace(stat, frequencies=dict(stat.frequencies))


@dataclass
class WindowState:
    length: int
    step: int
    batch_ids: List[int] = field(default_factory=list)
    start_batch: int = -1
    end_batch: int = -1
    last_removed: List[int] = field(default_factory=list)
    last_removal_amount: Dict[str, Dict[str, Any]] = field(default_factory=dict)

    def range_label(self) -> str:
        if not self.batch_ids:
            return "empty"
        return f"[{self.start_batch},{self.end_batch}]"


def validate_window_params(length: int, step: int) -> None:
    if length <= 0:
        raise DriftError(
            ErrorCode.WINDOW_INVALID,
            "reference window length must be a positive integer",
            {"field": "window_length", "value": length},
        )
    if step <= 0:
        raise DriftError(
            ErrorCode.WINDOW_INVALID,
            "sliding step must be a positive integer",
            {"field": "window_step", "value": step},
        )
    if step > length:
        raise DriftError(
            ErrorCode.WINDOW_INVALID,
            "sliding step cannot exceed the reference window length",
            {"field": "window_step", "value": step, "window_length": length},
        )


class ReferenceWindow:
    """Holds the per-feature window aggregates and supports exact eviction."""

    def __init__(self, length: int, step: int = 1):
        validate_window_params(length, step)
        self.length = length
        self.step = step
        self.state = WindowState(length=length, step=step)
        self._stats: Dict[int, BatchStat] = {}
        self._active_ids: List[int] = []
        self._aggregate: Dict[str, Any] = {"batch_ids": [], "numeric": {}, "categorical": {}}

    def observed_batch_ids(self) -> List[int]:
        return sorted(self._stats)

    def append_and_slide(
        self,
        incoming: BatchStat,
        specs: Dict[str, FeatureSpec],
    ) -> WindowState:
        self._stats[incoming.batch_id] = incoming
        previous_ids = list(self._active_ids)
        present_ids = sorted([*previous_ids, incoming.batch_id])
        keep_ids = present_ids[-self.length :]
        leaving_ids = [bid for bid in previous_ids if bid not in keep_ids]

        if not previous_ids:
            # First batch: initialize both paths from the same per-batch record.
            incremental = fold_window([incoming], specs)
        else:
            # Primary path: algebraically remove leaving batches, then merge the
            # incoming batch.  No approximation is used for sufficient stats.
            working = self._clone_aggregate()
            for bid in leaving_ids:
                self._subtract_batch(working, self._stats[bid], specs)
            self._merge_batch(working, incoming, specs)
            working["batch_ids"] = list(keep_ids)
            incremental = working

        # Verification path: independently fold surviving batch details.
        recomputed = fold_window([self._stats[bid] for bid in keep_ids], specs)
        if not _aggregate_equal(incremental, recomputed, specs):
            raise DriftError(
                ErrorCode.INCONSISTENT,
                "exact removal disagrees with independent batch-detail recomputation",
                {"category": "window_recompute"},
            )
        self._aggregate = recomputed
        self._active_ids = list(keep_ids)
        self.state.batch_ids = keep_ids
        self.state.start_batch = keep_ids[0] if keep_ids else -1
        self.state.end_batch = keep_ids[-1] if keep_ids else -1
        self.state.last_removed = leaving_ids
        self.state.last_removal_amount = self._describe_removal(leaving_ids, specs)
        return self.state

    def current_aggregate(self) -> Dict[str, Any]:
        return self._aggregate

    def _clone_aggregate(self) -> Dict[str, Any]:
        return {
            "batch_ids": list(self._aggregate["batch_ids"]),
            "numeric": {
                feature: replace_numeric(self._aggregate["numeric"][feature])
                for feature in sorted(self._aggregate["numeric"])
            },
            "categorical": {
                feature: replace_categorical(self._aggregate["categorical"][feature])
                for feature in sorted(self._aggregate["categorical"])
            },
        }

    def _subtract_batch(
        self,
        working: Dict[str, Any],
        leaving: BatchStat,
        specs: Dict[str, FeatureSpec],
    ) -> None:
        for feature in sorted(specs):
            spec = specs[feature]
            if spec.column_type == NUMERIC:
                working["numeric"][feature] = subtract_numeric(
                    working["numeric"][feature], leaving.numeric[feature]
                )
            else:
                working["categorical"][feature] = subtract_categorical(
                    working["categorical"][feature], leaving.categorical[feature]
                )

    def _merge_batch(
        self,
        working: Dict[str, Any],
        incoming: BatchStat,
        specs: Dict[str, FeatureSpec],
    ) -> None:
        for feature in sorted(specs):
            spec = specs[feature]
            if spec.column_type == NUMERIC:
                working["numeric"][feature] = merge_numeric(
                    [working["numeric"][feature], incoming.numeric[feature]]
                )
            else:
                working["categorical"][feature] = merge_categorical(
                    [working["categorical"][feature], incoming.categorical[feature]]
                )

    def _describe_removal(
        self, leaving_ids: Sequence[int], specs: Dict[str, FeatureSpec]
    ) -> Dict[str, Dict[str, Any]]:
        description: Dict[str, Dict[str, Any]] = {}
        for bid in leaving_ids:
            record = self._stats[bid]
            entry: Dict[str, Any] = {}
            for feature in sorted(specs):
                spec = specs[feature]
                if spec.column_type == NUMERIC:
                    stat = record.numeric[feature]
                    entry[feature] = {
                        "caliber": "numeric",
                        "removed_count": stat.count,
                        "removed_sum": stat.sum,
                        "removed_sum_squares": stat.sum_squares,
                        "removed_missing": stat.missing,
                    }
                else:
                    stat = record.categorical[feature]
                    entry[feature] = {
                        "caliber": "categorical",
                        "removed_total": stat.total,
                        "removed_missing": stat.missing,
                        "removed_frequencies": dict(sorted(stat.frequencies.items())),
                    }
            description[str(bid)] = entry
        return description


def _aggregate_equal(
    left: Dict[str, Any], right: Dict[str, Any], specs: Dict[str, FeatureSpec]
) -> bool:
    if left["batch_ids"] != right["batch_ids"]:
        return False
    for feature in sorted(specs):
        spec = specs[feature]
        if spec.column_type == NUMERIC:
            a = left["numeric"][feature]
            b = right["numeric"][feature]
            if (
                a.count != b.count
                or a.sum != b.sum
                or a.sum_squares != b.sum_squares
                or a.sorted_values != b.sorted_values
            ):
                return False
        else:
            a = left["categorical"][feature]
            b = right["categorical"][feature]
            if a.frequencies != b.frequencies or a.missing != b.missing or a.total != b.total:
                return False
    return True
