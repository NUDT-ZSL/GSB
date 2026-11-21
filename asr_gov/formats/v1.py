"""v1 扁平行式切片表：utterance_id|start_ms|end_ms|text，无字段名，无置信度。"""
from __future__ import annotations

from ..errors import FormatCorrupt, TimeInvalid, DupUtterance
from .common import CanonicalSlice, CanonicalSliceSet

HEADER = "#format: asr-slices-v1"
FIELD_COUNT = 4


def parse(text: str, clip_id: str) -> CanonicalSliceSet:
    result = CanonicalSliceSet(clip_id=clip_id, source_format=1)
    seen = set()
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("|")
        if len(parts) != FIELD_COUNT:
            raise FormatCorrupt(
                f"v1 行字段数应为 {FIELD_COUNT}，实际 {len(parts)}",
                clip_id=clip_id, location=f"line {lineno}: {line!r}",
            )
        utt, start_s, end_s, utt_text = (p.strip() for p in parts)
        try:
            start_ms = int(start_s)
            end_ms = int(end_s)
        except ValueError:
            raise TimeInvalid(
                "v1 时间字段非整数",
                clip_id=clip_id, location=f"line {lineno}: {line!r}",
            ) from None
        if start_ms >= end_ms:
            raise TimeInvalid(
                f"v1 start_ms({start_ms}) >= end_ms({end_ms})",
                clip_id=clip_id, location=f"line {lineno}: {line!r}",
            )
        if utt in seen:
            raise DupUtterance(
                f"utterance_id 重复: {utt}",
                clip_id=clip_id, location=f"line {lineno}: {line!r}",
            )
        seen.add(utt)
        result.slices.append(CanonicalSlice(
            utterance_id=utt,
            start_us=start_ms * 1000,
            end_us=end_ms * 1000,
            text=utt_text,
            provenance={"start_us": "v1:ms_to_us", "end_us": "v1:ms_to_us"},
        ))
    return result


def dump(slice_set: CanonicalSliceSet) -> str:
    lines = [HEADER]
    for s in slice_set.slices:
        lines.append("|".join([s.utterance_id, str(s.start_ms), str(s.end_ms), s.text]))
    return "\n".join(lines) + "\n"
