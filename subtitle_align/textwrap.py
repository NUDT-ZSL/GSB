"""基于 Pillow 的文本渲染与行数测量。

换行规则（确定性，无环境随机因素）：
- 先按显式换行符 ``\\n`` 分段，每段独立换行后拼接；
- 中文等全角字符逐字可断行；英文按单词断行，单词不被从中间拆开
  （单个单词本身超宽时才允许独占一行并由后续逻辑自然溢出）；
- 贪心填充：能放入当前行就放入，否则另起一行。
"""

from __future__ import annotations

from pathlib import Path

from PIL import ImageFont

# Windows 常见中英文字体优先级；均支持中文与拉丁字符
_FONT_CANDIDATES = (
    r"C:\Windows\Fonts\msyh.ttc",
    r"C:\Windows\Fonts\simhei.ttf",
    r"C:\Windows\Fonts\simsun.ttc",
    r"C:\Windows\Fonts\arial.ttf",
)


def resolve_font(font_size: int) -> tuple[ImageFont.FreeTypeFont, str]:
    """返回 (字体对象, 字体来源说明)。

    优先使用系统 TrueType 字体；全部不可用时退回 Pillow 内置位图字体，
    保证工具在任何机器上都能运行（内置字体不支持缩放，仅作兜底）。
    """
    for candidate in _FONT_CANDIDATES:
        if Path(candidate).exists():
            try:
                return ImageFont.truetype(candidate, font_size), candidate
            except OSError:
                continue
    return ImageFont.load_default(), "Pillow内置默认字体"


def _is_cjk(ch: str) -> bool:
    """是否为不可从中间断词的全角/CJK 字符（逐字断行）。"""
    code = ord(ch)
    return (
        0x3000 <= code <= 0x303F        # CJK 标点
        or 0x3040 <= code <= 0x30FF     # 平假名/片假名
        or 0x3400 <= code <= 0x4DBF     # 扩展A
        or 0x4E00 <= code <= 0x9FFF     # CJK 统一表意文字
        or 0xFF00 <= code <= 0xFFEF     # 全角拉丁/标点
        or 0x20000 <= code <= 0x2A6DF   # 扩展B
    )


# 避头尾：这些标点不出现在行首（句号、逗号、顿号、闭括号等）
_NO_LINE_START = set("。，、；：！？）】》」』”’…·.,!?;:)]}%")


def _is_no_line_start(ch: str) -> bool:
    return ch in _NO_LINE_START


def _tokenize(paragraph: str) -> list[str]:
    """把一段文本切成 token：CJK 单字、英文单词、空白或标点串。"""
    tokens: list[str] = []
    buf: list[str] = []

    def flush() -> None:
        if buf:
            tokens.append("".join(buf))
            buf.clear()

    for ch in paragraph:
        if ch.isspace():
            flush()
            tokens.append(ch)
        elif _is_cjk(ch):
            flush()
            tokens.append(ch)
        else:
            buf.append(ch)
    flush()
    return tokens


def wrap_text(text: str, font: ImageFont.FreeTypeFont, max_width: int) -> list[str]:
    """按 max_width（像素）对文本贪心换行，返回渲染行列表。

    空串返回空列表；纯空白文本同样返回空列表（调用方按文本缺失处理）。
    显式 ``\\n`` 强制分段。
    """
    if not text or not text.strip():
        return []

    lines: list[str] = []
    for paragraph in text.split("\n"):
        if not paragraph:
            lines.append("")
            continue
        current = ""
        for token in _tokenize(paragraph):
            if token.isspace():
                # 行首空白直接丢弃，避免无意义缩进
                candidate = current + token if current else current
            else:
                candidate = current + token
            if not current:
                current = token if not token.isspace() else ""
            elif font.getlength(candidate) <= max_width:
                current = candidate
            else:
                # 避头尾：闭合标点不能出现在行首，把它收进上一行（即便略宽）
                if len(token) == 1 and _is_no_line_start(token):
                    current = candidate
                    lines.append(current.rstrip())
                    current = ""
                else:
                    lines.append(current.rstrip())
                    # 单词本身超宽时也只能独占一行
                    current = "" if token.isspace() else token
        lines.append(current.rstrip())
    # 去掉末尾由显式换行产生的空行（保留中间空行）
    while lines and lines[-1] == "":
        lines.pop()
    return lines


def count_lines(text: str, font: ImageFont.FreeTypeFont, max_width: int) -> int:
    """返回文本渲染后的行数；空/纯空白文本返回 0。"""
    return len(wrap_text(text, font, max_width))
