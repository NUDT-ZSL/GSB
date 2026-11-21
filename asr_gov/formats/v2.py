"""v2 带字段名行式：start_us/end_us/tokens/speaker/confidence 与词表版本绑定。"""
from __future__ import annotations

from ..errors import FormatCorrupt, TimeInvalid, DupUtterance
from .common import CanonicalSlice, CanonicalSliceSet

HEADER = "#format: asr-slices-v2"
REQUIRED_KEYS = ("utterance_id", "start_us", "end_us", "text",
                 "tokens", "speaker", "confidence", "vocab_version")


def parse(text: str, clip_id: str) -> CanonicalSliceSet:
    result = CanonicalSliceSet(clip_id=clip_id, source_format=2)
    seen = set()
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        fields = {}
        for chunk in line.split("|"):
            if "=" not in chunk:
                raise FormatCorrupt(
                    "v2 字段须为 key=value 形式",
                    clip_id=clip_id, location=f"line {lineno}: {line!r}",
                )
            key, _, value = chunk.partition("=")
            fields[key.strip()] = value.strip()
        missing = [k for k in REQUIRED_KEYS if k not in fields]
        if missing:
            raise FormatCorrupt(
                f"v2 缺少键: {missing}",
                clip_id=clip_id, location=f"line {lineno}: {line!r}",
            )
        try:
            start_us = int(fields["start_us"])
            end_us = int(fields["end_us"])
        except ValueError:
            raise TimeInvalid(
                "v2 时间字段非整数",
                clip_id=clip_id, location=f"line {lineno}: {line!r}",
            ) from None
        if start_us >= end_us:
            raise TimeInvalid(
                f"v2 start_us({start_us}) >= end_us({end_us})",
                clip_id=clip_id, location=f"line {lineno}: {line!r}",
            )
        utt = fields["utterance_id"]
        if utt in seen:
            raise DupUtterance(
                f"utterance_id 重复: {utt}",
                clip_id=clip_id, location=f"line {lineno}: {line!r}",
            )
        seen.add(utt)
        try:
            confidence = float(fields["confidence"])
        except ValueError:
            raise FormatCorrupt(
                "v2 confidence 非数值",
                clip_id=clip_id, location=f"line {lineno}: {line!r}",
            ) from None
        tokens = [t for t in fields["tokens"].split(",") if t]
        result.slices.append(CanonicalSlice(
            utterance_id=utt,
            start_us=start_us,
            end_us=end_us,
            text=fields["text"],
            flat_tokens=tokens,
            speaker=fields["speaker"],
            conf_lo=confidence,
            conf_hi=confidence,
            provenance={
                "tokens": "v2:flat_token_list",
                "confidence": "v2:scalar_to_interval",
            },
        ))
        if result.vocab_version is None:
            result.vocab_version = fields["vocab_version"]
    return result


def dump(slice_set: CanonicalSliceSet) -> str:
    lines = [HEADER]
    vocab = slice_set.vocab_version or "vocab-unknown"
    for s in slice_set.slices:
        tokens = ",".join(s.token_strings())
        conf = s.conf_lo if s.conf_lo is not None else 0.0
        fields = [
            f"utterance_id={s.utterance_id}",
            f"start_us={s.start_us}",
            f"end_us={s.end_us}",
            f"text={s.text}",
            f"tokens={tokens}",
            f"speaker={s.speaker or 'spk_unknown'}",
            f"confidence={conf}",
            f"vocab_version={vocab}",
        ]
        lines.append("|".join(fields))
    return "\n".join(lines) + "\n"
