"""SRT 文件解析：逐块校验，坏块跳过并记录原因，好块保留原始序号与时间文本。"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .timing import parse_timecode


@dataclass(frozen=True)
class ParsedCue:
    index: int          # SRT 原始序号
    time_text: str      # 原始时间轴整行文本（含 -->）
    start_ms: int
    end_ms: int
    text: str           # 字幕正文，显式换行以 \\n 保留


@dataclass(frozen=True)
class ParseError:
    block_no: int       # 坏块在文件中的物理块序（从 1 开始）
    reason: str
    raw: str


def parse_srt(path: str | Path) -> tuple[list[ParsedCue], list[ParseError]]:
    """解析 SRT。返回 (有效字幕列表, 坏块错误列表)，顺序与文件一致。"""
    srt_path = Path(path)
    try:
        content = srt_path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        raise FileNotFoundError(f"SRT 文件不存在: {srt_path}")

    # 兼容 \r\n / \r；按一个或多个空行切块
    normalized = content.replace("\r\n", "\n").replace("\r", "\n")
    blocks = [b for b in normalized.split("\n\n") if b.strip()]

    cues: list[ParsedCue] = []
    errors: list[ParseError] = []

    for block_no, block in enumerate(blocks, start=1):
        lines = block.split("\n")
        if len(lines) < 3:
            errors.append(
                ParseError(block_no, "缺字段：一个字幕块至少需要序号、时间轴、正文三行", block)
            )
            continue

        index_token, time_line, *body = lines
        # 校验 1：序号必须是正整数
        try:
            index = int(index_token.strip())
            if index <= 0:
                raise ValueError
        except ValueError:
            errors.append(
                ParseError(block_no, f"序号非法（应为正整数）: {index_token.strip()!r}", block)
            )
            continue

        # 校验 2：时间轴格式必须为 start --> end 且两个时间码均合法
        arrow_parts = time_line.strip().split("-->")
        if len(arrow_parts) != 2:
            errors.append(ParseError(block_no, f"时间轴缺少 '-->': {time_line.strip()!r}", block))
            continue
        start_token, end_token = (p.strip() for p in arrow_parts)
        try:
            start_ms = parse_timecode(start_token)
            end_ms = parse_timecode(end_token)
        except ValueError as exc:
            errors.append(ParseError(block_no, f"时间格式非法：{exc}", block))
            continue

        # 校验 3：正文不得为空
        text = "\n".join(body).strip()
        if not text:
            errors.append(ParseError(block_no, "字幕正文为空", block))
            continue

        cues.append(
            ParsedCue(
                index=index,
                time_text=time_line.strip(),
                start_ms=start_ms,
                end_ms=end_ms,
                text=text,
            )
        )

    return cues, errors
