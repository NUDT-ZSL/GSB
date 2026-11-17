"""Sliding-window construction and deterministic drift scoring."""

from __future__ import annotations

from collections import Counter
from typing import Any, Callable, Iterable, Mapping

from .features import VocabState, lookup_category_counts, window_sort_key
from .loading import failure, sort_failures
from .scoring import label_alerts, score_window

WindowKey = tuple[str, int]


def _covered_turn_rows(
    session_id: str,
    turn_numbers: Iterable[int],
    turns_by_session: Mapping[str, Mapping[int, dict[str, Any]]],
) -> list[dict[str, Any]]:
    output = []
    for turn_no in turn_numbers:
        feature = turns_by_session.get(session_id, {}).get(turn_no)
        if feature is not None:
            output.append({"session_id": session_id, "turn_no": turn_no})
    return output


def build_window(
    session_id: str,
    start_turn: int,
    *,
    turns_by_session: Mapping[str, Mapping[int, dict[str, Any]]],
    vocab: VocabState,
    window_size: int,
) -> dict[str, Any]:
    expected_turns = list(range(start_turn, start_turn + window_size))
    session_turns = turns_by_session.get(session_id, {})
    missing = [turn_no for turn_no in expected_turns if turn_no not in session_turns]
    covered = _covered_turn_rows(session_id, expected_turns, turns_by_session)
    partial_term_counts: Counter[str] = Counter()
    partial_referenced_terms: set[str] = set()
    for turn_no in expected_turns:
        feature = session_turns.get(turn_no)
        if feature is None:
            continue
        partial_term_counts.update(feature["term_counts"])
        partial_referenced_terms.update(feature["raw_terms"])
    base = {
        "window_id": window_sort_key(session_id, start_turn),
        "stable_key": window_sort_key(session_id, start_turn),
        "session_id": session_id,
        "start_turn": start_turn,
        "end_turn": start_turn + window_size - 1,
        "window_size": window_size,
        "covered_turns": covered,
        "covered_turn_numbers": [item["turn_no"] for item in covered],
        "missing_turn_numbers": missing,
        "referenced_terms": sorted(partial_referenced_terms),
        "partial_term_counts": {
            term: int(partial_term_counts[term]) for term in sorted(partial_term_counts)
        },
        "drift_score": None,
        "distribution_empty": None,
        "alert": False,
        "boundary_equal_threshold": False,
        "alert_rank": None,
        "baseline_status": "NOT_EVALUATED",
        "previous_start_turn": None,
        "previous_distribution": None,
        "previous_distribution_empty": None,
        "components": {},
        "raw_components": {},
        "contributions": {},
        "contribution_sum": None,
        "rounding_residual": None,
        "threshold": None,
        "exception_type": None,
    }
    if missing:
        base.update(
            {
                "status": "skipped",
                "reason": "WINDOW_INCOMPLETE",
                "distribution": None,
                "category_counts": None,
                "term_counts": None,
                "referenced_terms": sorted(partial_referenced_terms),
            }
        )
        return base

    term_counts = partial_term_counts
    referenced_terms = partial_referenced_terms
    ordered_term_counts = {term: int(term_counts[term]) for term in sorted(term_counts)}
    category_counts = lookup_category_counts(ordered_term_counts, vocab)
    base.update(
        {
            "status": "full",
            "reason": None,
            "category_counts": category_counts,
            "term_counts": ordered_term_counts,
            "referenced_terms": sorted(referenced_terms),
        }
    )
    return base


def candidate_start_turns(turns_by_session: Mapping[str, Mapping[int, dict[str, Any]]]) -> list[WindowKey]:
    return sorted(
        (session_id, turn_no)
        for session_id, turns in turns_by_session.items()
        for turn_no in turns
    )


def build_all_windows(
    turns_by_session: Mapping[str, Mapping[int, dict[str, Any]]],
    *,
    vocab: VocabState,
    window_size: int,
) -> list[dict[str, Any]]:
    windows = [
        build_window(
            session_id,
            start_turn,
            turns_by_session=turns_by_session,
            vocab=vocab,
            window_size=window_size,
        )
        for session_id, start_turn in candidate_start_turns(turns_by_session)
    ]
    return sorted(windows, key=lambda item: (item["session_id"], item["start_turn"]))


def _forced_window_injection(
    forced: Mapping[WindowKey, dict[str, Any]] | None,
    session_id: str,
    start_turn: int,
) -> dict[str, Any] | None:
    if not forced:
        return None
    return forced.get((session_id, start_turn))


def score_all_windows(
    windows: list[dict[str, Any]],
    *,
    vocab: VocabState,
    weights: dict[str, float],
    threshold: float,
    precision: int,
    forced_window_exceptions: Mapping[WindowKey, dict[str, Any]] | None = None,
    batch_id: str | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    window_map = {(item["session_id"], item["start_turn"]): item for item in windows}
    failures: list[dict[str, Any]] = []
    for window in windows:
        if window["status"] != "full":
            window.setdefault("drift_score", None)
            window.setdefault("alert", False)
            window.setdefault("boundary_equal_threshold", False)
            window.setdefault("alert_rank", None)
            window.setdefault("baseline_status", "NOT_EVALUATED")
            continue
        injection = _forced_window_injection(
            forced_window_exceptions, window["session_id"], window["start_turn"]
        )
        if injection is not None:
            window.update(_error_window_payload(window, "InjectedWindowError"))
            failures.append(
                failure(
                    code="E_WINDOW_INTERNAL",
                    stage="score",
                    message="injected window exception",
                    session_id=window["session_id"],
                    window_start=window["start_turn"],
                    batch_id=batch_id,
                    injection_id=injection.get("id"),
                    exception_type="InjectedWindowError",
                )
            )
            continue
        previous_start = window["start_turn"] - 1
        previous = window_map.get((window["session_id"], previous_start))
        if previous is None:
            baseline_status = "NONE"
        elif previous.get("status") == "full":
            baseline_status = "OK"
        else:
            baseline_status = "UNAVAILABLE"
        usable_previous = previous if baseline_status == "OK" else None
        try:
            score_payload = score_window(
                window,
                usable_previous,
                vocab=vocab,
                weights=weights,
                threshold=threshold,
                precision=precision,
                baseline_status=baseline_status,
            )
        except Exception as exc:  # per-window isolation is a business requirement
            window.update(_error_window_payload(window, type(exc).__name__))
            failures.append(
                failure(
                    code="E_WINDOW_INTERNAL",
                    stage="score",
                    message=str(exc),
                    session_id=window["session_id"],
                    window_start=window["start_turn"],
                    batch_id=batch_id,
                    exception_type=type(exc).__name__,
                )
            )
            continue
        window.update(score_payload)
        window["previous_start_turn"] = previous_start if baseline_status == "OK" else None
    boundary = label_alerts(windows, threshold, precision)
    score_state = {"boundary_alerts": boundary}
    return sorted(windows, key=lambda item: (item["session_id"], item["start_turn"])), sort_failures(failures), score_state


def _error_window_payload(window: dict[str, Any], exception_type: str) -> dict[str, Any]:
    return {
        "status": "error",
        "reason": "WINDOW_EXCEPTION",
        "distribution": None,
        "drift_score": None,
        "alert": False,
        "boundary_equal_threshold": False,
        "baseline_status": "ERROR",
        "previous_start_turn": None,
        "components": {},
        "raw_components": {},
        "contributions": {},
        "contribution_sum": None,
        "rounding_residual": None,
        "alert_rank": None,
        "exception_type": exception_type,
    }


def recompute_score_starts(
    windows: list[dict[str, Any]],
    start_keys: set[WindowKey],
    *,
    vocab: VocabState,
    weights: dict[str, float],
    threshold: float,
    precision: int,
    forced_window_exceptions: Mapping[WindowKey, dict[str, Any]] | None = None,
    batch_id: str | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Re-score selected starts without rebuilding their distributions.

    This is used for the one downstream scoring step after a distribution
    changes. Full-weight updates use ``score_all_windows`` instead.
    """
    window_map = {(item["session_id"], item["start_turn"]): item for item in windows}
    failures: list[dict[str, Any]] = []
    for key in sorted(start_keys):
        window = window_map.get(key)
        if window is None or window.get("status") != "full":
            continue
        previous = window_map.get((key[0], key[1] - 1))
        if previous is None:
            baseline_status = "NONE"
        elif previous.get("status") == "full":
            baseline_status = "OK"
        else:
            baseline_status = "UNAVAILABLE"
        injection = _forced_window_injection(forced_window_exceptions, key[0], key[1])
        if injection is not None:
            window.update(_error_window_payload(window, "InjectedWindowError"))
            failures.append(
                failure(
                    code="E_WINDOW_INTERNAL",
                    stage="score",
                    message="injected window exception",
                    session_id=key[0],
                    window_start=key[1],
                    batch_id=batch_id,
                    injection_id=injection.get("id"),
                    exception_type="InjectedWindowError",
                )
            )
            continue
        try:
            payload = score_window(
                window,
                previous if baseline_status == "OK" else None,
                vocab=vocab,
                weights=weights,
                threshold=threshold,
                precision=precision,
                baseline_status=baseline_status,
            )
        except Exception as exc:
            window.update(_error_window_payload(window, type(exc).__name__))
            failures.append(
                failure(
                    code="E_WINDOW_INTERNAL",
                    stage="score",
                    message=str(exc),
                    session_id=key[0],
                    window_start=key[1],
                    batch_id=batch_id,
                    exception_type=type(exc).__name__,
                )
            )
            continue
        window.update(payload)
        window["previous_start_turn"] = key[1] - 1 if baseline_status == "OK" else None
    boundary = label_alerts(windows, threshold, precision)
    return windows, sort_failures(failures), {"boundary_alerts": boundary}


def windows_to_scoring_artifact(windows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    fields = (
        "window_id",
        "status",
        "distribution_empty",
        "previous_distribution",
        "previous_distribution_empty",
        "drift_score",
        "alert",
        "boundary_equal_threshold",
        "alert_rank",
        "baseline_status",
        "previous_start_turn",
        "components",
        "raw_components",
        "contributions",
        "contribution_sum",
        "rounding_residual",
        "threshold",
        "exception_type",
    )
    return [{field: window.get(field) for field in fields} for window in windows]


def windows_to_stage_artifact(windows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    fields = (
        "window_id",
        "stable_key",
        "session_id",
        "start_turn",
        "end_turn",
        "window_size",
        "covered_turns",
        "covered_turn_numbers",
        "missing_turn_numbers",
        "status",
        "reason",
        "category_counts",
        "term_counts",
        "partial_term_counts",
        "referenced_terms",
        "distribution",
    )
    return [{field: window.get(field) for field in fields} for window in windows]


def merge_scoring_artifact(
    windows: list[dict[str, Any]], scoring: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    by_id = {item["window_id"]: item for item in scoring}
    for window in windows:
        payload = by_id.get(window["window_id"], {})
        window.update(payload)
    return windows
