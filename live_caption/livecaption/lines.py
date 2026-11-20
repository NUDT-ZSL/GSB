"""行装配与软换行。

规则（显式声明）：
- 以词为单位换行，禁止拆断单词；
- 词宽 = 词内字符宽度之和（CJK=char_width_cjk，其余=char_width_latin）；
- 行宽 = 各词词宽之和 + 词间空格宽度（space_width，词间恰好一个）；
- 贪心装配：当前行放不下下一个词即换行；
- 置信度低于门限的词在临时视图中以 ⟨word⟩ 渲染，定稿视图按原词渲染；
  临时标记属显示层装饰，不计入行宽（保证临时/定稿行分组一致）；
- 单个词超行宽 -> E_LINE_WIDTH；
- 单块装配行数超过每屏最大行数 -> E_LINE_SCREEN。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Sequence, Tuple

from .config import DisplayPolicy
from .errors import E_LINE_WIDTH, E_LINE_SCREEN, BusinessError
from .events import WordEvent


def char_width(ch: str, policy: DisplayPolicy) -> int:
    o = ord(ch)
    if (0x4E00 <= o <= 0x9FFF) or (0x3000 <= o <= 0x303F) or (0xFF00 <= o <= 0xFFEF):
        return policy.char_width_cjk
    return policy.char_width_latin


def word_width(word: str, policy: DisplayPolicy) -> int:
    return sum(char_width(c, policy) for c in word)


def render_word(ev: WordEvent, policy: DisplayPolicy, final: bool) -> str:
    if not final and ev.confidence < policy.confidence_threshold:
        return "⟨" + ev.word + "⟩"
    return ev.word


@dataclass(frozen=True)
class Line:
    """一行字幕：组成词的时间轴事件与渲染文本。"""
    line_no: int
    events: Tuple[WordEvent, ...]
    text: str
    width: int

    @property
    def word_indices(self) -> Tuple[int, ...]:
        return tuple(e.word_index for e in self.events)


def pack_words(rendered: Sequence[str], events: Sequence[WordEvent],
               policy: DisplayPolicy, stream_id: str, chunk_seq: int,
               start_line_no: int = 0) -> List[Line]:
    """把预渲染词序列按词边界贪心装配为字幕行（禁止拆词）。

    行宽按原词（ev.word）计算，临时标记不计宽。
    """
    lines: List[Line] = []
    cur_words: List[str] = []
    cur_events: List[WordEvent] = []
    cur_width = 0
    for text, ev in zip(rendered, events):
        w = word_width(ev.word, policy)
        if w > policy.max_line_chars:
            raise BusinessError(
                E_LINE_WIDTH,
                f"word exceeds max line width: {text!r} width={w}",
                stream_id=stream_id, chunk_seq=chunk_seq,
                word=ev.word, word_index=ev.word_index,
                word_width=w, max_line_chars=policy.max_line_chars,
            )
        add = w if not cur_words else policy.space_width + w
        if cur_words and cur_width + add > policy.max_line_chars:
            lines.append(Line(start_line_no + len(lines), tuple(cur_events),
                              " ".join(cur_words), cur_width))
            cur_words, cur_events, cur_width = [], [], 0
            add = w
        cur_words.append(text)
        cur_events.append(ev)
        cur_width += add
    if cur_words:
        lines.append(Line(start_line_no + len(lines), tuple(cur_events),
                          " ".join(cur_words), cur_width))
    return lines


def assemble_lines(events: Sequence[WordEvent], policy: DisplayPolicy,
                   stream_id: str, chunk_seq: int, final: bool = False,
                   start_line_no: int = 0) -> List[Line]:
    """把已按时间轴排序的事件装配为字幕行。"""
    rendered = [render_word(ev, policy, final) for ev in events]
    return pack_words(rendered, events, policy, stream_id, chunk_seq,
                      start_line_no)


def assemble_mixed(events: Sequence[WordEvent], final_flags: Sequence[bool],
                   policy: DisplayPolicy, stream_id: str,
                   chunk_seq: int) -> List[Line]:
    """逐词指定临时/定稿渲染口径的装配（引擎增量视图使用）。"""
    rendered = [render_word(ev, policy, f) for ev, f in zip(events, final_flags)]
    return pack_words(rendered, events, policy, stream_id, chunk_seq)


def check_screen(lines: Sequence[Line], policy: DisplayPolicy,
                 stream_id: str, chunk_seq: int) -> None:
    """单块行数超过每屏最大行数 -> E_LINE_SCREEN。"""
    if len(lines) > policy.max_lines_per_screen:
        raise BusinessError(
            E_LINE_SCREEN,
            f"chunk needs {len(lines)} lines > max {policy.max_lines_per_screen}",
            stream_id=stream_id, chunk_seq=chunk_seq,
            lines=len(lines), max_lines_per_screen=policy.max_lines_per_screen,
        )
