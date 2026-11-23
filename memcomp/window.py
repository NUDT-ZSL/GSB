"""确定性轮次窗口切分与窗口摘要生成。"""

import json
from collections import Counter
from dataclasses import dataclass

from .errors import E_CONFIG_INVALID, MemError

WIN_PARAM_VERSION = "win-1.0"


@dataclass(frozen=True)
class WindowSpec:
    window_token_limit: int = 24
    overlap_tokens: int = 6
    summary_k: int = 6
    entry_window_span: int = 2
    chain_threshold: int = 5
    budget_tokens: int = 48

    def param_version(self):
        return (
            f"{WIN_PARAM_VERSION}:limit={self.window_token_limit}:"
            f"overlap={self.overlap_tokens}:k={self.summary_k}:"
            f"span={self.entry_window_span}"
        )

    def validate(self):
        if self.window_token_limit <= 0:
            raise MemError(E_CONFIG_INVALID, "window_token_limit 必须为正")
        if not 0 <= self.overlap_tokens < self.window_token_limit:
            raise MemError(E_CONFIG_INVALID, "overlap_tokens 必须满足 0 <= overlap < limit")
        if self.summary_k <= 0 or self.entry_window_span <= 0:
            raise MemError(E_CONFIG_INVALID, "summary_k 与 entry_window_span 必须为正")


@dataclass(frozen=True)
class Window:
    window_id: str
    session_id: str
    index: int
    token_start: int
    token_end: int
    turn_nos: tuple
    tokens: tuple

    def to_dict(self):
        return {
            "window_id": self.window_id,
            "session_id": self.session_id,
            "index": self.index,
            "token_start": self.token_start,
            "token_end": self.token_end,
            "turn_nos": list(self.turn_nos),
            "tokens": list(self.tokens),
        }


def split_windows(session, spec):
    """按 token 上限与重叠切分；窗口边界以 token 索引精确确定。"""
    spec.validate()
    stream = []
    turn_marks = []
    for turn in session["turns"]:
        stream.extend(turn["text_tokens"])
        turn_marks.extend([turn["turn_no"]] * turn["token_len"])
    windows = []
    step = spec.window_token_limit - spec.overlap_tokens
    start = 0
    index = 0
    total = len(stream)
    while start < total:
        end = min(start + spec.window_token_limit, total)
        turn_nos = tuple(sorted(set(turn_marks[start:end])))
        windows.append(
            Window(
                window_id=f"{session['session_id']}#w{index:03d}",
                session_id=session["session_id"],
                index=index,
                token_start=start,
                token_end=end,
                turn_nos=turn_nos,
                tokens=tuple(stream[start:end]),
            )
        )
        if end == total:
            break
        start += step
        index += 1
    return windows


def summarize_tokens(tokens, spec, vocab):
    """按词频权重确定性选取 top-k；同分按 token 字典序升序裁决。"""
    vocab_set = set(vocab)
    counts = Counter(t for t in tokens if t in vocab_set)
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return tuple(tok for tok, _ in ranked[: spec.summary_k])


def summarize_window(window, spec, vocab):
    return summarize_tokens(window.tokens, spec, vocab)


def windows_bytes(windows):
    return json.dumps(
        [w.to_dict() for w in windows], sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
