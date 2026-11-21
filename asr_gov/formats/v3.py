"""v3 结构化 JSON：token 级微秒时序、分段标签、置信度区间与增量修订号。"""
from __future__ import annotations

import json

from ..errors import FormatCorrupt, TimeInvalid, DupUtterance
from .common import CanonicalSlice, CanonicalSliceSet, TokenTiming

FORMAT_VERSION = 3
SLICE_KEYS = ("utterance_id", "start_us", "end_us", "text", "tokens",
              "speaker", "confidence", "segment_label", "revision")
TOKEN_KEYS = ("token", "start_us", "end_us", "conf_lo", "conf_hi")


def _require_int(value, clip_id, location):
    if isinstance(value, bool) or not isinstance(value, int):
        raise TimeInvalid("v3 时间字段非整数", clip_id=clip_id, location=location)
    return value


def parse(text: str, clip_id: str) -> CanonicalSliceSet:
    try:
        doc = json.loads(text)
    except json.JSONDecodeError as exc:
        raise FormatCorrupt(f"v3 JSON 解析失败: {exc}", clip_id=clip_id,
                            location=f"line {exc.lineno}") from None
    if not isinstance(doc, dict) or "slices" not in doc:
        raise FormatCorrupt("v3 缺少 slices 键", clip_id=clip_id, location="$")
    result = CanonicalSliceSet(clip_id=clip_id, source_format=3,
                               vocab_version=doc.get("vocab_version"))
    seen = set()
    for index, item in enumerate(doc["slices"]):
        location = f"slices[{index}]"
        missing = [k for k in SLICE_KEYS if k not in item]
        if missing:
            raise FormatCorrupt(f"v3 缺少键: {missing}", clip_id=clip_id,
                                location=location)
        start_us = _require_int(item["start_us"], clip_id, location + ".start_us")
        end_us = _require_int(item["end_us"], clip_id, location + ".end_us")
        if start_us >= end_us:
            raise TimeInvalid(
                f"v3 start_us({start_us}) >= end_us({end_us})",
                clip_id=clip_id, location=location,
            )
        utt = item["utterance_id"]
        if utt in seen:
            raise DupUtterance(f"utterance_id 重复: {utt}", clip_id=clip_id,
                               location=location)
        seen.add(utt)
        tokens = []
        for t_index, t in enumerate(item["tokens"]):
            t_loc = f"{location}.tokens[{t_index}]"
            t_missing = [k for k in TOKEN_KEYS if k not in t]
            if t_missing:
                raise FormatCorrupt(f"v3 token 缺少键: {t_missing}",
                                    clip_id=clip_id, location=t_loc)
            tokens.append(TokenTiming(
                token=t["token"],
                start_us=_require_int(t["start_us"], clip_id, t_loc + ".start_us"),
                end_us=_require_int(t["end_us"], clip_id, t_loc + ".end_us"),
                conf_lo=float(t["conf_lo"]),
                conf_hi=float(t["conf_hi"]),
            ))
        conf = item["confidence"]
        if not isinstance(conf, dict) or "lo" not in conf or "hi" not in conf:
            raise FormatCorrupt("v3 confidence 须含 lo/hi", clip_id=clip_id,
                                location=location + ".confidence")
        result.slices.append(CanonicalSlice(
            utterance_id=utt,
            start_us=start_us,
            end_us=end_us,
            text=item["text"],
            tokens=tokens,
            speaker=item["speaker"],
            conf_lo=float(conf["lo"]),
            conf_hi=float(conf["hi"]),
            segment_label=item["segment_label"],
            revision=int(item["revision"]),
        ))
    return result


def dump(slice_set: CanonicalSliceSet) -> str:
    doc = {
        "format_version": FORMAT_VERSION,
        "clip_id": slice_set.clip_id,
        "vocab_version": slice_set.vocab_version,
        "slices": [],
    }
    for s in slice_set.slices:
        doc["slices"].append({
            "utterance_id": s.utterance_id,
            "start_us": s.start_us,
            "end_us": s.end_us,
            "text": s.text,
            "tokens": [t.to_dict() for t in s.tokens],
            "speaker": s.speaker,
            "confidence": {"lo": s.conf_lo, "hi": s.conf_hi},
            "segment_label": s.segment_label,
            "revision": s.revision,
        })
    return json.dumps(doc, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
