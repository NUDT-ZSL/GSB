"""固定种子与参数的演示样本：基准 v3 切片集、词表版本与默认识别配置。"""
from __future__ import annotations

from .config import RecognitionConfig, Vocab
from .formats.common import CanonicalSlice, CanonicalSliceSet, TokenTiming
from .pcm import DEFAULT_CLIP_ID, speech_intervals_ms

TEXTS = ["hello world", "good morning friends", "see you tomorrow"]
SPEAKERS = ["spk1", "spk2", "spk1"]
CONF_INTERVALS = [(0.88, 0.97), (0.81, 0.93), (0.9, 0.99)]
TOKEN_SPLITS = [
    [(0, 500_000), (550_000, 1_200_000)],
    [(0, 300_000), (320_000, 610_000), (640_000, 900_000)],
    [(0, 420_000), (450_000, 700_000), (730_000, 1_500_000)],
]
VOCAB_V1 = "vocab-1"
VOCAB_V2 = "vocab-2"


def build_ground_truth(clip_id: str = DEFAULT_CLIP_ID) -> CanonicalSliceSet:
    """v3 级基准切片集：token 级微秒时序、置信度区间、分段标签、修订号。"""
    intervals = speech_intervals_ms()
    result = CanonicalSliceSet(clip_id=clip_id, source_format=3,
                               vocab_version=VOCAB_V1)
    for index, ((start_ms, end_ms), text) in enumerate(zip(intervals, TEXTS)):
        words = text.split()
        splits = TOKEN_SPLITS[index]
        lo, hi = CONF_INTERVALS[index]
        tokens = [
            TokenTiming(token=word,
                        start_us=start_ms * 1000 + span[0],
                        end_us=start_ms * 1000 + span[1],
                        conf_lo=round(lo + 0.01 * i, 4),
                        conf_hi=round(hi - 0.01 * i, 4))
            for i, (word, span) in enumerate(zip(words, splits))
        ]
        result.slices.append(CanonicalSlice(
            utterance_id=f"u{index + 1}",
            start_us=start_ms * 1000,
            end_us=end_ms * 1000,
            text=text,
            tokens=tokens,
            speaker=SPEAKERS[index],
            conf_lo=lo,
            conf_hi=hi,
            segment_label="speech",
            revision=1,
        ))
    return result


def build_vocabs():
    tokens = [w for text in TEXTS for w in text.split()]
    vocab1 = Vocab.from_tokens(VOCAB_V1, tokens)
    tokens2 = [("hullo" if t == "hello" else t) for t in tokens]
    vocab2 = Vocab.from_tokens(VOCAB_V2, tokens2)
    return vocab1, vocab2


def default_config() -> RecognitionConfig:
    return RecognitionConfig()
