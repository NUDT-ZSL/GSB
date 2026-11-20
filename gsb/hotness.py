from __future__ import annotations

from typing import Any

from .models import ColumnBatch
from .util import group_key_sort_key, sort_key_token


class HotTracker:
    def __init__(
        self,
        hot_key_min_rows: int,
        skew_ratio: float,
        state: dict[str, Any] | None = None,
    ) -> None:
        self.hot_key_min_rows = hot_key_min_rows
        self.skew_ratio = skew_ratio
        if state is None:
            self.total_rows = 0
            self.counts: dict[str, int] = {}
            self.values: dict[str, Any] = {}
            self.observations: dict[str, list[dict[str, int]]] = {}
            self.current_hot: list[str] = []
            self.version = 0
            self.ever_hot: list[str] = []
            self.changes: list[dict[str, Any]] = []
        else:
            self.total_rows = int(state["total_rows"])
            self.counts = {key: int(value) for key, value in state["counts"].items()}
            self.values = dict(state["values"])
            self.observations = {
                key: [dict(item) for item in value]
                for key, value in state["observations"].items()
            }
            self.current_hot = list(state["current_hot"])
            self.version = int(state["version"])
            self.ever_hot = list(state["ever_hot"])
            self.changes = [
                {
                    **item,
                    "added": list(item.get("added", [])),
                    "removed": list(item.get("removed", [])),
                    "hot_keys": list(item.get("hot_keys", [])),
                    "observations": [
                        {
                            **observation,
                            "observed_batches": [
                                dict(batch)
                                for batch in observation.get("observed_batches", [])
                            ],
                        }
                        for observation in item.get("observations", [])
                    ],
                }
                for item in state.get("changes", [])
            ]

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_rows": self.total_rows,
            "counts": dict(sorted(self.counts.items())),
            "values": {token: self.values[token] for token in sorted(self.values)},
            "observations": {
                token: self.observations[token] for token in sorted(self.observations)
            },
            "current_hot": list(self.current_hot),
            "version": self.version,
            "ever_hot": list(self.ever_hot),
            "changes": list(self.changes),
        }

    def _candidate_hot(self) -> list[str]:
        if self.total_rows <= 0:
            return []
        candidates: list[str] = []
        for token, count in self.counts.items():
            ratio = count / self.total_rows
            if count >= self.hot_key_min_rows and ratio >= self.skew_ratio:
                candidates.append(token)
        return sorted(candidates, key=lambda token: group_key_sort_key(self.values[token]))

    def _snapshot(self, trigger_batch: int | None) -> dict[str, Any]:
        observations: list[dict[str, Any]] = []
        for token in self.current_hot:
            count = self.counts[token]
            observations.append(
                {
                    "group_key": self.values[token],
                    "rows": count,
                    "total_rows": self.total_rows,
                    "row_ratio": count / self.total_rows if self.total_rows else 0.0,
                    "observed_batches": [
                        dict(item) for item in self.observations.get(token, [])
                    ],
                }
            )
        return {
            "version": self.version,
            "trigger_batch_index": trigger_batch,
            "hot_keys": [self.values[token] for token in self.current_hot],
            "observations": observations,
        }

    def step(
        self,
        batch: ColumnBatch,
        partial: dict[str, dict[str, Any]],
    ) -> dict[str, Any] | None:
        for token, state in partial.items():
            rows = int(state["rows"])
            if rows == 0:
                continue
            self.counts[token] = self.counts.get(token, 0) + rows
            self.values[token] = state["key"]
            self.observations.setdefault(token, []).append(
                {"batch_index": batch.batch_index, "rows": rows}
            )
            self.total_rows += rows
        previous = list(self.current_hot)
        current = self._candidate_hot()
        self.current_hot = current
        ever = set(self.ever_hot)
        ever.update(current)
        self.ever_hot = sorted(ever, key=lambda token: group_key_sort_key(self.values[token]))
        if current != previous:
            self.version += 1
            added = [self.values[token] for token in current if token not in previous]
            removed = [self.values[token] for token in previous if token not in current]
            event = {
                "version": self.version,
                "trigger_batch_index": batch.batch_index,
                "added": added,
                "removed": removed,
                "hot_keys": [self.values[token] for token in current],
                "observations": self._snapshot(batch.batch_index)["observations"],
            }
            self.changes.append(event)
            return event
        return None

    def report(self) -> dict[str, Any]:
        current = self._snapshot(
            self.changes[-1]["trigger_batch_index"] if self.changes else None
        )
        return {
            "current_version": self.version,
            "hot_keys": current["hot_keys"],
            "observations": current["observations"],
            "changes": self.changes,
            "total_rows_observed": self.total_rows,
            "criteria": {
                "min_rows": self.hot_key_min_rows,
                "skew_ratio": self.skew_ratio,
                "conjunction": "rows >= min_rows AND row_ratio >= skew_ratio",
                "pre_scan_used": False,
            },
            "ever_hot_keys": [self.values[token] for token in self.ever_hot],
        }


def hot_token_set(tracker: HotTracker) -> set[str]:
    return set(tracker.ever_hot)


def hot_tokens_current(tracker: HotTracker) -> set[str]:
    return set(tracker.current_hot)


def sort_tokens(tracker: HotTracker, tokens: set[str]) -> list[str]:
    return sorted(tokens, key=lambda token: sort_key_token(tracker.values[token]))

