"""v1/v2/v3 三套书写形式的双向迁移器。

升级时逐项记录补全来源（completions）；降级时对目标格式无法表达的字段
按显式声明的丢弃规则处理并输出丢弃明细（discards），不做静默改写。
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field

from .errors import FormatVersionUnsupported
from .formats.common import CanonicalSlice, CanonicalSliceSet, TokenTiming

SUPPORTED = (1, 2, 3)

RULE_MS_QUANTIZE = "v1_ms_only:round_half_up"
RULE_DROP_TOKEN_TIMING = "v_target_cannot_represent:token_timing"
RULE_DROP_CONF_INTERVAL = "v_target_cannot_represent:confidence_interval"
RULE_COLLAPSE_CONF_INTERVAL = "v2_scalar_only:store_midpoint"
RULE_DROP_SPEAKER = "v1_cannot_represent:speaker"
RULE_DROP_SEGMENT_LABEL = "v_target_cannot_represent:segment_label"
RULE_DROP_REVISION = "v_target_cannot_represent:revision"
RULE_DROP_VOCAB_BINDING = "v1_cannot_represent:vocab_version"


@dataclass
class MigrationResult:
    source_version: int
    target_version: int
    slice_set: CanonicalSliceSet
    completions: list = field(default_factory=list)
    discards: list = field(default_factory=list)

    def to_dict(self):
        return {
            "source_version": self.source_version,
            "target_version": self.target_version,
            "completions": self.completions,
            "discards": self.discards,
            "slice_count": len(self.slice_set.slices),
            "digest": self.slice_set.digest(),
        }


def _complete(result, utt, field_name, source, value):
    result.completions.append({
        "utterance_id": utt, "field": field_name,
        "source": source, "value": value,
    })


def _discard(result, utt, field_name, rule, detail):
    result.discards.append({
        "utterance_id": utt, "field": field_name,
        "rule": rule, "detail": detail,
    })


def _upgrade(slice_out: CanonicalSlice, source_version: int, target_version: int,
             result: MigrationResult):
    utt = slice_out.utterance_id
    if not slice_out.flat_tokens and not slice_out.tokens:
        words = [w for w in slice_out.text.split() if w]
        slice_out.flat_tokens = words
        _complete(result, utt, "tokens", "derived:text_whitespace", words)
    if slice_out.speaker is None:
        slice_out.speaker = "spk_unknown"
        _complete(result, utt, "speaker", "default:spk_unknown", "spk_unknown")
    if slice_out.conf_lo is None:
        slice_out.conf_lo = 0.0
        slice_out.conf_hi = 1.0
        _complete(result, utt, "confidence_interval", "default:[0.0,1.0]", [0.0, 1.0])
    if target_version >= 3:
        if not slice_out.tokens:
            words = slice_out.token_strings()
            span = slice_out.end_us - slice_out.start_us
            n = max(1, len(words))
            step = span // n
            timings = []
            for i, word in enumerate(words):
                t_start = slice_out.start_us + i * step
                t_end = slice_out.start_us + (i + 1) * step if i + 1 < n else slice_out.end_us
                timings.append(TokenTiming(word, t_start, t_end,
                                           slice_out.conf_lo, slice_out.conf_hi))
            slice_out.tokens = timings
            _complete(result, utt, "token_timings", "interpolated:even_split",
                      [t.to_dict() for t in timings])
        if source_version == 2 and slice_out.conf_lo == slice_out.conf_hi:
            slice_out.provenance["confidence_interval"] = "derived:v2_scalar_confidence"
        if slice_out.segment_label is None:
            slice_out.segment_label = "speech"
            _complete(result, utt, "segment_label", "default:speech", "speech")
        if not slice_out.revision:
            _complete(result, utt, "revision", "default:0", 0)


def _downgrade(slice_out: CanonicalSlice, target_version: int, result: MigrationResult):
    utt = slice_out.utterance_id
    if target_version <= 2 and slice_out.tokens:
        _discard(result, utt, "token_timings", RULE_DROP_TOKEN_TIMING,
                 f"{len(slice_out.tokens)} 个 token 级微秒时序")
        slice_out.tokens = []
    if target_version == 2:
        if slice_out.conf_lo is not None and slice_out.conf_lo != slice_out.conf_hi:
            midpoint = round((slice_out.conf_lo + slice_out.conf_hi) / 2.0, 6)
            _discard(result, utt, "confidence_interval", RULE_COLLAPSE_CONF_INTERVAL,
                     f"[{slice_out.conf_lo},{slice_out.conf_hi}] -> {midpoint}")
            slice_out.conf_lo = slice_out.conf_hi = midpoint
        if slice_out.segment_label is not None:
            _discard(result, utt, "segment_label", RULE_DROP_SEGMENT_LABEL,
                     slice_out.segment_label)
            slice_out.segment_label = None
        if slice_out.revision:
            _discard(result, utt, "revision", RULE_DROP_REVISION, slice_out.revision)
            slice_out.revision = 0
    if target_version == 1:
        if slice_out.start_us % 1000 or slice_out.end_us % 1000:
            _discard(result, utt, "sub_ms_timing", RULE_MS_QUANTIZE,
                     f"start_us={slice_out.start_us}, end_us={slice_out.end_us}")
            slice_out.start_us = slice_out.start_ms * 1000
            slice_out.end_us = slice_out.end_ms * 1000
        if slice_out.conf_lo is not None:
            _discard(result, utt, "confidence_interval", RULE_DROP_CONF_INTERVAL,
                     f"[{slice_out.conf_lo},{slice_out.conf_hi}]")
            slice_out.conf_lo = slice_out.conf_hi = None
        if slice_out.speaker is not None:
            _discard(result, utt, "speaker", RULE_DROP_SPEAKER, slice_out.speaker)
            slice_out.speaker = None
        if slice_out.segment_label is not None:
            _discard(result, utt, "segment_label", RULE_DROP_SEGMENT_LABEL,
                     slice_out.segment_label)
            slice_out.segment_label = None
        if slice_out.revision:
            _discard(result, utt, "revision", RULE_DROP_REVISION, slice_out.revision)
            slice_out.revision = 0
        slice_out.tokens = []
        slice_out.flat_tokens = None


def migrate(slice_set: CanonicalSliceSet, target_version: int) -> MigrationResult:
    if target_version not in SUPPORTED:
        raise FormatVersionUnsupported(f"未知目标格式版本: v{target_version}")
    source_version = slice_set.source_format
    out = CanonicalSliceSet(clip_id=slice_set.clip_id, source_format=target_version,
                            vocab_version=slice_set.vocab_version)
    result = MigrationResult(source_version=source_version,
                             target_version=target_version, slice_set=out)
    for original in slice_set.slices:
        slice_out = copy.deepcopy(original)
        if target_version > source_version:
            _upgrade(slice_out, source_version, target_version, result)
        elif target_version < source_version:
            _downgrade(slice_out, target_version, result)
        if target_version == 1 and slice_set.vocab_version:
            _discard(result, slice_out.utterance_id, "vocab_version",
                     RULE_DROP_VOCAB_BINDING, slice_set.vocab_version)
        out.slices.append(slice_out)
    if target_version == 1:
        out.vocab_version = None
    return result
