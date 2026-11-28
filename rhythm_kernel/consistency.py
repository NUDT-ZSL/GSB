"""Cross-language time-alignment consistency checks.

Channel medians are compared with their language median, language medians
with the global median.  Outliers beyond the project threshold become
warnings that identify the exact channel or language, alongside any
corrected click that escapes the legal time range.
"""

from __future__ import annotations

from .model import group_key, median


def _group_corrections(results):
    grouped = {}
    for result in results:
        key = group_key(result["language"], result["channel"])
        grouped.setdefault(key, []).append(result)
    return grouped


def build_consistency_report(results, policy, legal_range):
    grouped = _group_corrections(results)
    threshold = policy["consistency_threshold_ms"]

    by_channel = {}
    language_corrections = {}
    for key in sorted(grouped):
        bucket = grouped[key]
        corrections = [
            result["correction_ms"]
            for result in bucket
            if result["correction_ms"] is not None
        ]
        language = bucket[0]["language"]
        channel = bucket[0]["channel"]
        median_correction = median(corrections) if corrections else None
        by_channel[key] = {
            "channel": channel,
            "correction_sample_count": len(corrections),
            "delta_from_language_median_ms": None,
            "event_count": len(bucket),
            "language": language,
            "median_correction_ms": median_correction,
        }
        language_corrections.setdefault(language, []).extend(corrections)

    by_language = {}
    all_corrections = []
    for language in sorted(language_corrections):
        values = language_corrections[language]
        all_corrections.extend(values)
        by_language[language] = {
            "correction_sample_count": len(values),
            "delta_from_global_median_ms": None,
            "event_count": sum(
                len(bucket)
                for key, bucket in grouped.items()
                if bucket[0]["language"] == language
            ),
            "median_correction_ms": median(values),
        }
    global_median = median(all_corrections)

    issues = []
    for language, payload in by_language.items():
        delta = payload["median_correction_ms"] - global_median
        payload["delta_from_global_median_ms"] = delta
        if abs(delta) > threshold:
            issues.append(
                {
                    "code": "consistency.language_correction_outlier",
                    "delta_ms": delta,
                    "global_median_correction_ms": global_median,
                    "language": language,
                    "median_correction_ms": payload["median_correction_ms"],
                    "message": (
                        "Language median correction differs from the global "
                        "median beyond threshold"
                    ),
                    "path": "stats.languages",
                    "severity": "warning",
                    "threshold_ms": threshold,
                }
            )

    for key, payload in by_channel.items():
        if payload["median_correction_ms"] is None:
            continue
        language_median = by_language[payload["language"]]["median_correction_ms"]
        delta = payload["median_correction_ms"] - language_median
        payload["delta_from_language_median_ms"] = delta
        if abs(delta) > threshold:
            issues.append(
                {
                    "channel": payload["channel"],
                    "code": "consistency.channel_correction_outlier",
                    "delta_ms": delta,
                    "group_key": key,
                    "language": payload["language"],
                    "language_median_correction_ms": language_median,
                    "median_correction_ms": payload["median_correction_ms"],
                    "message": (
                        "Channel median correction differs from its language "
                        "median beyond threshold"
                    ),
                    "path": "stats.channels",
                    "severity": "warning",
                    "threshold_ms": threshold,
                }
            )

    start, end = legal_range["start_ms"], legal_range["end_ms"]
    for result in results:
        corrected = result["corrected_click_ms"]
        if corrected is not None and not (start <= corrected <= end):
            issues.append(
                {
                    "channel": result["channel"],
                    "code": "consistency.corrected_time_out_of_range",
                    "event_id": result["event_id"],
                    "language": result["language"],
                    "legal_range_ms": [start, end],
                    "listener_id": result["listener_id"],
                    "message": "Corrected event time is outside the legal range",
                    "path": "corrected_click_ms",
                    "severity": "warning",
                    "value": corrected,
                }
            )

    issues.sort(key=lambda item: item["code"])
    severity_counts = {"error": 0, "info": 0, "warning": 0}
    for item in issues:
        severity_counts[item["severity"]] = (
            severity_counts.get(item["severity"], 0) + 1
        )

    return {
        "by_channel": by_channel,
        "by_language": by_language,
        "checked_event_count": len(results),
        "checks": [
            "event fields against a deterministic fresh recomputation",
            "event correction against median(RTT samples)/2",
            "correction direction raw minus one-way delay",
            "language/channel correction medians against baselines",
        ],
        "errors_ok": not any(
            item["severity"] == "error" for item in issues
        ),
        "global_median_correction_ms": global_median,
        "issue_count": len(issues),
        "issues": issues,
        "ok": not issues,
        "severity_counts": severity_counts,
        "thresholds": {
            "comparison": "absolute median-correction delta; strict greater-than",
            "consistency_threshold_ms": threshold,
            "float_tolerance_ms": policy["float_tolerance_ms"],
        },
    }
