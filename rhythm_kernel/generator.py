"""Deterministic demo project generator (standard library only).

The fixed seed makes every run reproducible offline.  The generator emits the
same project schema used by real broadcast projects, including out-of-range
clicks, an unknown song and a channel without an RTT profile, so every
validation branch of the kernel is exercised by the bundled sample.
"""

from __future__ import annotations

import random

from .model import beat_interval_ms

SEED = 20261128

SONG_SPECS = [
    ("song-alpha", "晨光脉冲", 120.0, 0.0),
    ("song-beta", "海风回环", 96.0, 250.0),
    ("song-gamma", "夜行列车", 150.0, 125.0),
]

CHANNEL_SPECS = [
    ("en", "radio", (120.0, 170.0)),
    ("en", "web", (80.0, 115.0)),
    ("es", "app", (95.0, 135.0)),
    ("es", "web", (85.0, 120.0)),
    ("ja", "voice", (320.0, 470.0)),
    ("ja", "web", (90.0, 125.0)),
    ("zh", "app", (88.0, 125.0)),
    ("zh", "web", (72.0, 105.0)),
]

LANGUAGES = ("en", "es", "ja", "zh")
EVENTS_PER_VALID_GROUP = 6
PROGRAM_END_MS = 240000.0


def _build_songs(rng):
    songs = []
    for song_id, title, bpm, offset in SONG_SPECS:
        interval = beat_interval_ms(bpm)
        beats = []
        index = 0
        while True:
            position = offset + interval * index
            if position > PROGRAM_END_MS:
                break
            beats.append(round(position, 6))
            index += 1
        songs.append(
            {
                "beats_ms": beats,
                "bpm": bpm,
                "duration_ms": PROGRAM_END_MS,
                "id": song_id,
                "title": title,
            }
        )
    return songs


def _build_channels(rng):
    channels = []
    for language, channel, (low, high) in CHANNEL_SPECS:
        samples = [round(rng.uniform(low, high), 3) for _ in range(5)]
        channels.append(
            {
                "channel": channel,
                "language": language,
                "rtt_samples_ms": samples,
            }
        )
    return channels


def _build_timeline():
    return [
        {
            "end_ms": 60000.0,
            "id": "seg-alpha-1",
            "name": "晨光开场",
            "song_id": "song-alpha",
            "start_ms": 0.0,
        },
        {
            "end_ms": 120000.0,
            "id": "seg-beta",
            "name": "海风中段",
            "song_id": "song-beta",
            "start_ms": 60000.0,
        },
        {
            "end_ms": 180000.0,
            "id": "seg-gamma",
            "name": "夜行高潮",
            "song_id": "song-gamma",
            "start_ms": 120000.0,
        },
        {
            "end_ms": 240000.0,
            "id": "seg-alpha-2",
            "name": "晨光返场",
            "song_id": "song-alpha",
            "start_ms": 180000.0,
        },
    ]


def _segment_for_time(timeline, time_ms):
    for segment in timeline:
        if segment["start_ms"] <= time_ms <= segment["end_ms"]:
            return segment
    return timeline[-1]


def _build_events(rng, timeline, channels):
    events = []
    serial = 1
    channel_index = {
        (item["language"], item["channel"]): item for item in channels
    }
    for language, channel, _ in CHANNEL_SPECS:
        profile = channel_index[(language, channel)]
        one_way = _median(profile["rtt_samples_ms"]) / 2.0
        listener_pool = [
            f"listener-{language}-{channel}-{tag}" for tag in ("a", "b")
        ]
        for _ in range(EVENTS_PER_VALID_GROUP):
            segment = rng.choice(timeline)
            logical_click = rng.uniform(
                segment["start_ms"] + 1500.0, segment["end_ms"] - 1500.0
            )
            deviation = rng.choice(
                [
                    rng.uniform(5.0, 28.0),
                    rng.uniform(35.0, 78.0),
                    rng.uniform(95.0, 165.0),
                    rng.uniform(185.0, 235.0),
                ]
            )
            sign = rng.choice((-1.0, 1.0))
            corrected = logical_click + sign * deviation
            corrected = max(100.0, min(PROGRAM_END_MS - 100.0, corrected))
            raw_click = corrected + one_way
            submitted = max(0.0, raw_click - rng.uniform(120.0, 950.0))
            attributed = _segment_for_time(timeline, corrected)
            events.append(
                {
                    "channel": channel,
                    "click_ms": round(raw_click, 3),
                    "id": f"evt-{serial:04d}",
                    "language": language,
                    "listener_id": rng.choice(listener_pool),
                    "requested_song_id": attributed["song_id"],
                    "submitted_at_ms": round(submitted, 3),
                }
            )
            serial += 1

    events.extend(_invalid_events(serial))
    return events


def _invalid_events(start_serial):
    return [
        {
            "channel": "web",
            "click_ms": 240900.0,
            "id": "evt-invalid-late-click",
            "language": "zh",
            "listener_id": "listener-zh-web-outlier",
            "requested_song_id": "song-alpha",
            "submitted_at_ms": 240500.0,
        },
        {
            "channel": "radio",
            "click_ms": 91300.0,
            "id": "evt-invalid-unknown-song",
            "language": "en",
            "listener_id": "listener-en-radio-outlier",
            "requested_song_id": "song-not-in-project",
            "submitted_at_ms": 91000.0,
        },
        {
            "channel": "kiosk",
            "click_ms": 151200.0,
            "id": "evt-invalid-missing-channel",
            "language": "es",
            "listener_id": "listener-es-kiosk-outlier",
            "requested_song_id": "song-gamma",
            "submitted_at_ms": 151000.0,
        },
    ]


def _median(values):
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2.0


def generate_project(seed=SEED):
    """Return a fully self-contained deterministic demo project."""
    rng = random.Random(seed)
    timeline = _build_timeline()
    channels = _build_channels(rng)
    songs = _build_songs(rng)
    events = _build_events(rng, timeline, channels)
    return {
        "channels": channels,
        "events": events,
        "legal_time_range_ms": {"end_ms": PROGRAM_END_MS, "start_ms": 0.0},
        "metadata": {
            "deterministic": True,
            "generator": "rhythm_kernel.generator",
            "seed": seed,
        },
        "policy": {
            "consistency_threshold_ms": 45.0,
            "deviation_thresholds": {
                "accuracy_ratio": 0.2,
                "mean_abs_deviation_ms": 30.0,
            },
            "float_tolerance_ms": 0.001,
            "window_ratios": {
                "good": 0.18,
                "miss": 0.5,
                "offset": 0.45,
                "perfect": 0.08,
            },
        },
        "project_id": f"multilingual-rhythm-demo-{seed}",
        "schema_version": 1,
        "songs": songs,
        "timeline": timeline,
    }
