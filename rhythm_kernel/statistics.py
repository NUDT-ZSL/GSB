"""Participation, accuracy and deviation statistics by channel/language."""

from __future__ import annotations

from .model import GRADES, group_key, median


def _empty_grade_counts():
    return {grade: 0 for grade in GRADES}


def _ratio(numerator, denominator):
    if denominator == 0:
        return None
    return numerator / denominator


def _base_group(result_iter):
    grade_counts = _empty_grade_counts()
    event_count = 0
    judged_count = 0
    invalid_count = 0
    accurate_count = 0
    sum_abs = 0.0
    listeners = set()
    corrections = []
    for result in result_iter:
        event_count += 1
        listeners.add(result["listener_id"])
        if not result["valid"]:
            invalid_count += 1
            continue
        judged_count += 1
        grade_counts[result["grade"]] += 1
        if result["grade"] in ("PERFECT", "GOOD"):
            accurate_count += 1
        sum_abs += result["judgment"]["abs_deviation_ms"]
        if result["correction_ms"] is not None:
            corrections.append(result["correction_ms"])
    return {
        "grade_counts": grade_counts,
        "event_count": event_count,
        "judged_count": judged_count,
        "invalid_count": invalid_count,
        "accurate_count": accurate_count,
        "sum_abs": sum_abs,
        "listeners": listeners,
        "corrections": corrections,
    }


def build_stats(results, policy):
    overall_event_count = len(results)
    overall_listeners = {result["listener_id"] for result in results}

    overall_raw = _base_group(results)
    overall = {
        "accuracy_ratio": _ratio(
            overall_raw["accurate_count"], overall_raw["judged_count"]
        ),
        "accurate_count": overall_raw["accurate_count"],
        "event_count": overall_event_count,
        "grade_counts": overall_raw["grade_counts"],
        "invalid_count": overall_raw["invalid_count"],
        "judged_count": overall_raw["judged_count"],
        "judged_ratio": _ratio(overall_raw["judged_count"], overall_event_count),
        "mean_abs_deviation_ms": _ratio(
            overall_raw["sum_abs"], overall_raw["judged_count"]
        ),
        "sum_abs_deviation_ms": overall_raw["sum_abs"],
        "unique_listener_count": len(overall_listeners),
    }

    channels = _channel_stats(results, overall_listeners)
    languages = _language_stats(results, overall, overall_listeners, policy)
    return {
        "channels": channels,
        "definitions": {
            "accuracy_ratio": "(PERFECT + GOOD) / judged_count",
            "listener_participation_ratio": (
                "language unique_listener_count / overall unique_listener_count"
            ),
            "mean_abs_deviation_ms": "mean(abs(deviation_ms)) over judged events",
            "participation_ratio": "language event_count / overall event_count",
        },
        "languages": languages,
        "overall": overall,
        "thresholds": {
            "accuracy_ratio": policy["deviation_thresholds"]["accuracy_ratio"],
            "comparison": (
                "absolute language value minus overall baseline; "
                "strict greater-than"
            ),
            "mean_abs_deviation_ms": policy["deviation_thresholds"][
                "mean_abs_deviation_ms"
            ],
        },
    }


def _channel_stats(results, overall_listeners):
    grouped = {}
    for result in results:
        key = group_key(result["language"], result["channel"])
        grouped.setdefault(key, []).append(result)

    rows = []
    for key in sorted(grouped):
        bucket = grouped[key]
        raw = _base_group(bucket)
        language = bucket[0]["language"]
        channel = bucket[0]["channel"]
        rows.append(
            {
                "accuracy_ratio": _ratio(
                    raw["accurate_count"], raw["judged_count"]
                ),
                "accurate_count": raw["accurate_count"],
                "channel": channel,
                "event_count": raw["event_count"],
                "grade_counts": raw["grade_counts"],
                "group_key": key,
                "invalid_count": raw["invalid_count"],
                "judged_count": raw["judged_count"],
                "judged_ratio": _ratio(raw["judged_count"], raw["event_count"]),
                "language": language,
                "listener_participation_ratio": _ratio(
                    len(raw["listeners"]), len(overall_listeners)
                ),
                "mean_abs_deviation_ms": _ratio(
                    raw["sum_abs"], raw["judged_count"]
                ),
                "median_correction_ms": _ratio(
                    median(raw["corrections"]), 1
                )
                if raw["corrections"]
                else None,
                "sum_abs_deviation_ms": raw["sum_abs"],
                "unique_listener_count": len(raw["listeners"]),
            }
        )
    return rows


def _language_stats(results, overall, overall_listeners, policy):
    grouped = {}
    for result in results:
        grouped.setdefault(result["language"], []).append(result)

    mad_threshold = policy["deviation_thresholds"]["mean_abs_deviation_ms"]
    accuracy_threshold = policy["deviation_thresholds"]["accuracy_ratio"]
    rows = []
    for language in sorted(grouped):
        bucket = grouped[language]
        raw = _base_group(bucket)
        accuracy = _ratio(raw["accurate_count"], raw["judged_count"])
        mad = _ratio(raw["sum_abs"], raw["judged_count"])
        delta_accuracy = (
            accuracy - overall["accuracy_ratio"] if accuracy is not None else None
        )
        delta_mad = (
            mad - overall["mean_abs_deviation_ms"] if mad is not None else None
        )
        flag_reasons = []
        if delta_mad is not None and delta_mad > mad_threshold:
            flag_reasons.append("mean_abs_deviation_ms")
        if delta_accuracy is not None and abs(delta_accuracy) > accuracy_threshold:
            flag_reasons.append("accuracy_ratio")
        rows.append(
            {
                "accuracy_ratio": accuracy,
                "accurate_count": raw["accurate_count"],
                "deviation_from_overall": {
                    "accuracy_ratio": delta_accuracy,
                    "mean_abs_deviation_ms": delta_mad,
                },
                "event_count": raw["event_count"],
                "flag_reasons": flag_reasons,
                "flagged": bool(flag_reasons),
                "grade_counts": raw["grade_counts"],
                "invalid_count": raw["invalid_count"],
                "judged_count": raw["judged_count"],
                "judged_ratio": _ratio(raw["judged_count"], raw["event_count"]),
                "language": language,
                "listener_participation_ratio": _ratio(
                    len(raw["listeners"]), len(overall_listeners)
                ),
                "mean_abs_deviation_ms": mad,
                "participation_ratio": _ratio(
                    raw["event_count"], overall["event_count"]
                ),
                "sum_abs_deviation_ms": raw["sum_abs"],
                "unique_listener_count": len(raw["listeners"]),
            }
        )
    return rows
