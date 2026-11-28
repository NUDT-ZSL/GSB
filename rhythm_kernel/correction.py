"""Per language/channel round-trip-delay correction profiles.

The one-way correction is half the median of the channel's RTT samples.  A
raw click is aligned to the programme logical timeline by subtracting that
one-way delay.
"""

from __future__ import annotations

from .model import group_key, median


def build_channel_corrections(channels):
    """Index the declared RTT samples by ``language|channel``.

    Only channels carrying an RTT profile are indexed; an event whose group is
    absent is flagged separately by the judging pipeline.
    """
    profiles = {}
    declared_order = []
    for channel in channels:
        key = group_key(channel["language"], channel["channel"])
        samples = list(channel["rtt_samples_ms"])
        rtt_median = median(samples)
        one_way = rtt_median / 2.0
        profiles[key] = {
            "group_key": key,
            "language": channel["language"],
            "channel": channel["channel"],
            "rtt_samples_ms": samples,
            "rtt_sample_count": len(samples),
            "rtt_median_ms": rtt_median,
            "one_way_correction_ms": one_way,
            "formula": "median(rtt_samples_ms) / 2",
            "direction": (
                "corrected_click_ms = raw_click_ms - one_way_correction_ms"
            ),
        }
        declared_order.append(key)
    return profiles, declared_order


def describe_correction(profile):
    """Public, fully populated correction block for an event."""
    return {
        "applied_correction_ms": profile["one_way_correction_ms"],
        "channel": profile["channel"],
        "direction": (
            "corrected_click_ms = raw_click_ms - applied_correction_ms"
        ),
        "formula": profile["formula"],
        "group_key": profile["group_key"],
        "language": profile["language"],
        "one_way_correction_ms": profile["one_way_correction_ms"],
        "rtt_median_ms": profile["rtt_median_ms"],
        "rtt_sample_count": profile["rtt_sample_count"],
        "rtt_samples_ms": list(profile["rtt_samples_ms"]),
    }


def empty_correction(language, channel):
    """Correction block emitted when no RTT profile matches the event."""
    return {
        "applied_correction_ms": None,
        "direction": (
            "corrected_click_ms = raw_click_ms - applied_correction_ms"
        ),
        "formula": "median(rtt_samples_ms) / 2",
        "group_key": None,
        "one_way_correction_ms": None,
        "rtt_median_ms": None,
        "rtt_sample_count": 0,
        "rtt_samples_ms": [],
    }


def channel_report(profiles, declared_order):
    """The ``corrections_by_channel`` payload, in declared profile order."""
    report = []
    for key in declared_order:
        profile = profiles[key]
        report.append(
            {
                "channel": profile["channel"],
                "direction": profile["direction"],
                "formula": profile["formula"],
                "group_key": key,
                "language": profile["language"],
                "one_way_correction_ms": profile["one_way_correction_ms"],
                "rtt_median_ms": profile["rtt_median_ms"],
                "rtt_sample_count": profile["rtt_sample_count"],
                "rtt_samples_ms": list(profile["rtt_samples_ms"]),
            }
        )
    return report
