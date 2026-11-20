"""Input validation helpers for dialogue timelines and gains."""

from __future__ import annotations

import math
from numbers import Real
from typing import Any

from .catalog import EVENT_TYPE_IDS
from .codes import BusinessCode
from .config import ms_to_samples
from .errors import AedError


def _timeline_error(
    field: str,
    message: str,
    *,
    dialog_id: str,
    details: dict[str, Any] | None = None,
) -> AedError:
    return AedError(
        BusinessCode.E_TIMELINE_INVALID,
        message,
        dialog_id=dialog_id,
        field=field,
        details=details,
    )


def validate_dialog_timeline(
    timeline: dict[str, Any],
    *,
    dialog_id: str,
    duration_ms: float,
    sample_rate: int = 16_000,
    gain_min: float = 0.0,
    gain_max: float = 4.0,
) -> None:
    """Validate turn/event bounds and gains.

    Entries may use ``start_ms``/``end_ms`` or ``start_sample``/``end_sample``.
    Event types, when present, must come from the controlled vocabulary.
    """

    if not isinstance(timeline, dict):
        raise _timeline_error(
            "timeline",
            "timeline must be an object",
            dialog_id=dialog_id,
        )
    if (
        isinstance(duration_ms, bool)
        or not isinstance(duration_ms, Real)
        or not math.isfinite(float(duration_ms))
        or duration_ms <= 0
    ):
        raise _timeline_error(
            "duration_ms",
            "duration_ms must be positive",
            dialog_id=dialog_id,
        )
    duration_samples = ms_to_samples(
        float(duration_ms),
        sample_rate,
        field="duration_ms",
        dialog_id=dialog_id,
    )

    for collection_name in ("turns", "events"):
        entries = timeline.get(collection_name, [])
        if not isinstance(entries, list):
            raise _timeline_error(
                collection_name,
                f"{collection_name} must be a list",
                dialog_id=dialog_id,
            )
        for index, entry in enumerate(entries):
            if not isinstance(entry, dict):
                raise _timeline_error(
                    f"{collection_name}[{index}]",
                    "timeline entry must be an object",
                    dialog_id=dialog_id,
                )
            if "start_sample" in entry or "end_sample" in entry:
                start = entry.get("start_sample")
                end = entry.get("end_sample")
                if (
                    isinstance(start, bool)
                    or isinstance(end, bool)
                    or not isinstance(start, int)
                    or not isinstance(end, int)
                ):
                    raise _timeline_error(
                        f"{collection_name}[{index}].sample_range",
                        "start_sample and end_sample must be integers",
                        dialog_id=dialog_id,
                        details={"entry_index": index},
                    )
            else:
                start_ms = entry.get("start_ms")
                end_ms = entry.get("end_ms")
                if (
                    isinstance(start_ms, bool)
                    or not isinstance(start_ms, Real)
                    or isinstance(end_ms, bool)
                    or not isinstance(end_ms, Real)
                    or not math.isfinite(float(start_ms))
                    or not math.isfinite(float(end_ms))
                ):
                    raise _timeline_error(
                        f"{collection_name}[{index}].time_range",
                        "start_ms and end_ms must be finite numbers",
                        dialog_id=dialog_id,
                        details={"entry_index": index},
                    )
                start = ms_to_samples(
                    float(start_ms),
                    sample_rate,
                    field=f"{collection_name}[{index}].start_ms",
                    dialog_id=dialog_id,
                )
                end = ms_to_samples(
                    float(end_ms),
                    sample_rate,
                    field=f"{collection_name}[{index}].end_ms",
                    dialog_id=dialog_id,
                )
            if start < 0 or end > duration_samples or end <= start:
                raise _timeline_error(
                    f"{collection_name}[{index}].time_range",
                    "timeline entry is outside the dialog or has an empty range",
                    dialog_id=dialog_id,
                    details={
                        "entry_index": index,
                        "start_sample": start,
                        "end_sample": end,
                        "duration_samples": duration_samples,
                    },
                )
            if "gain" in entry:
                gain = entry["gain"]
                if (
                    isinstance(gain, bool)
                    or not isinstance(gain, Real)
                    or not math.isfinite(float(gain))
                    or not gain_min <= float(gain) <= gain_max
                ):
                    raise _timeline_error(
                        f"{collection_name}[{index}].gain",
                        f"gain must be in [{gain_min}, {gain_max}]",
                        dialog_id=dialog_id,
                        details={"entry_index": index, "gain": gain},
                    )
            if collection_name == "events" and "event_type" in entry:
                event_type = entry["event_type"]
                if event_type not in EVENT_TYPE_IDS or event_type == "background":
                    raise AedError(
                        BusinessCode.E_EVENT_TYPE,
                        f"uncontrolled event type {event_type!r}",
                        dialog_id=dialog_id,
                        field=f"events[{index}].event_type",
                        details={"entry_index": index, "event_type": event_type},
                    )
