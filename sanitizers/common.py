"""共享的敏感号码识别与规范化逻辑。

检测器与"修复后"脱敏实现共用同一套识别规则。两者对"什么算一个手机号 /
证件号"保持一致，报告里的残留数量才有可比性。

识别规则：
  * 手机号：11 位，1 开头、第二位 3-9；允许 +86 前缀、空格 / 连字符 / 点 /
    中点 / 下划线分隔、全角数字与全角空格、零宽字符、数字之间夹杂空白。
  * 证件号：18 位居民身份证号（6 位地址码 + 8 位出生日期 + 3 位顺序码 +
    1 位校验位），另兼容 15 位旧式证件号。地址码和出生日期必须结构合法，
    避免把普通长数字误判为证件号。
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import date

PHONE_TOKEN = "[手机号已脱敏]"
ID_TOKEN = "[证件号已脱敏]"

# 零宽字符 / 字节序标记 / 软连字符：不可见，却能把一个号码"拆开"，
# 识别前必须剔除。用显式码点书写，避免源文件里出现不可见字符。
_INVISIBLE_CODEPOINTS = (
    0x200B,  # zero width space
    0x200C,  # zero width non-joiner
    0x200D,  # zero width joiner
    0x2060,  # word joiner
    0xFEFF,  # zero width no-break space / BOM
    0x180E,  # mongolian vowel separator
    0x00AD,  # soft hyphen
)
_INVISIBLE_TABLE = {codepoint: None for codepoint in _INVISIBLE_CODEPOINTS}

# 号码内部允许出现的分隔字符：半角空格、制表符、不换行空格（U+00A0）、
# 全角空格（U+3000）、连字符与破折号（U+2010..U+2015）、中点（U+00B7）、
# 项目符号（U+2022）、点、下划线、减号。用 \u 转义书写，避免源码里出现
# 难辨认的不可见字符。不包含换行符，因此号码不会跨行匹配。
# 字符类用码点构造，源码保持纯 ASCII，避免全角空格等字符难以辨认。
_SEP_CODEPOINTS = (0x20, 0x09, 0x00A0, 0x3000, 0x2010, 0x2015, 0x00B7, 0x2022, 0x2E, 0x5F, 0x2D)
_SEP_CHARS = "".join(chr(code) for code in _SEP_CODEPOINTS)
SEP = "[" + re.escape(_SEP_CHARS) + "]"

# 手机号；+86 前缀可有可无，数字之间允许 0~4 个分隔字符。
PHONE_PATTERN = (
    rf"(?<![0-9])(?:\+?86{SEP}{{0,2}})?1[3-9](?:{SEP}{{0,4}}[0-9]){{9}}(?![0-9])"
)

# 18 位证件号：6 位地址码 + 11 位中间数字 + 1 位数字或 X 校验位。
ID18_PATTERN = (
    rf"(?<![0-9Xx])[0-9]{{6}}(?:{SEP}{{0,4}}[0-9]){{11}}"
    rf"(?:{SEP}{{0,4}}[0-9Xx])(?![0-9Xx])"
)

# 15 位旧式证件号。
ID15_PATTERN = rf"(?<![0-9])[0-9]{{6}}(?:{SEP}{{0,4}}[0-9]){{9}}(?![0-9])"

# 省级行政区代码（身份证前两位）。
PROVINCE_CODES = {
    "11", "12", "13", "14", "15",
    "21", "22", "23",
    "31", "32", "33", "34", "35", "36", "37",
    "41", "42", "43", "44", "45", "46",
    "50", "51", "52", "53", "54",
    "61", "62", "63", "64", "65",
    "71", "81", "82", "91",
}

_PHONE_RE = re.compile(PHONE_PATTERN, re.ASCII)
_ID18_RE = re.compile(ID18_PATTERN, re.ASCII)
_ID15_RE = re.compile(ID15_PATTERN, re.ASCII)


@dataclass(frozen=True)
class SensitiveMatch:
    """一次命中：类型、在（规范化后）文本中的区间、匹配到的文本。"""

    kind: str  # "phone" | "id_card"
    start: int
    end: int
    raw: str


@dataclass(frozen=True)
class ResidualHit:
    """按行统计的残留命中。"""

    kind: str
    line: int


def normalize_text(text: str) -> str:
    """NFKC 规范化并移除不可见字符。

    修复后实现直接在规范化后的文本上识别号码，因此全角数字、全角空格、
    全角连字符等变形写法都会先被折叠，不会漏检。
    """
    return unicodedata.normalize("NFKC", text).translate(_INVISIBLE_TABLE)


def normalize_with_offsets(text: str) -> tuple[str, list[int], list[int]]:
    """逐字符规范化，同时记录每个规范化字符对应的原始下标。

    返回 (normalized, starts, ends)。starts[i] / ends[i] 是规范化文本第 i 个
    字符在原文中的起止下标。修复实现既能在规范化文本上精确匹配，又能只
    替换原文中的号码区间，保留日志其余部分的原貌。
    """
    out: list[str] = []
    starts: list[int] = []
    ends: list[int] = []
    for index, char in enumerate(text):
        if ord(char) in _INVISIBLE_TABLE:
            continue
        for normalized_char in unicodedata.normalize("NFKC", char):
            out.append(normalized_char)
            starts.append(index)
            ends.append(index + 1)
    return "".join(out), starts, ends


def _compact(value: str) -> str:
    """去掉分隔符，保留数字与 X/x，用于结构校验。"""
    return re.sub(r"[^0-9Xx]", "", value)


def compact_digits(text: str) -> str:
    """把文本压成纯数字串（去掉一切非数字字符），用于"原文检索"。"""
    return re.sub(r"[^0-9]", "", text)


def contains_number(text: str, compact: str) -> bool:
    """按"号码原文"检索：数字之间允许分隔符，但不允许其他数字夹入。

    compact 是号码去分隔符后的字符串（证件号可能以 X 结尾）。修复后
    文本若仍能找到同一串数字，说明原文没有被替换干净。
    """
    if not compact:
        return False
    pieces = [re.escape(char) for char in compact]
    pattern = r"(?<![0-9])" + rf"{SEP}{{0,4}}".join(pieces) + r"(?![0-9])"
    return re.search(pattern, normalize_text(text), re.ASCII) is not None


def core_digits(match: SensitiveMatch) -> str:
    """取号码的"核心"数字串：手机号去掉 +86 前缀，证件号保留末尾 X。"""
    compact = _compact(match.raw)
    if match.kind == "phone" and len(compact) == 13 and compact.startswith("86"):
        return compact[2:]
    return compact


def _valid_date(year: int, month: int, day: int, *, min_year: int = 1900) -> bool:
    if not (min_year <= year <= 2100):
        return False
    try:
        date(year, month, day)
    except ValueError:
        return False
    return True


def is_valid_id_card(raw: str) -> bool:
    """按地址码、出生日期校验证件号，降低普通长数字的误报。"""
    compact = _compact(raw)
    if not compact:
        return False

    if len(compact) == 18:
        if not compact[:17].isdigit() or compact[-1] not in "0123456789Xx":
            return False
        if compact[:2] not in PROVINCE_CODES:
            return False
        return _valid_date(int(compact[6:10]), int(compact[10:12]), int(compact[12:14]))

    if len(compact) == 15:
        if not compact.isdigit() or compact[:2] not in PROVINCE_CODES:
            return False
        return _valid_date(
            1900 + int(compact[6:8]), int(compact[8:10]), int(compact[10:12])
        )

    return False


def is_valid_phone(raw: str) -> bool:
    digits = re.sub(r"[^0-9]", "", raw)
    if len(digits) == 13 and digits.startswith("86"):
        digits = digits[2:]
    return len(digits) == 11 and digits[0] == "1" and digits[1] in "3456789"


def find_sensitive_in_normalized(normalized: str) -> list[SensitiveMatch]:
    """在已规范化文本中找出全部手机号 / 证件号，区间互不重叠。

    先匹配更长的证件号，再匹配手机号；与之重叠的短匹配会被丢弃，避免
    一个 18 位证件号中的数字片段被重复计成手机号。
    """
    spans: list[SensitiveMatch] = []

    def overlaps(start: int, end: int) -> bool:
        return any(start < item.end and item.start < end for item in spans)

    for pattern, kind, validator in (
        (_ID18_RE, "id_card", is_valid_id_card),
        (_ID15_RE, "id_card", is_valid_id_card),
        (_PHONE_RE, "phone", is_valid_phone),
    ):
        for match in pattern.finditer(normalized):
            if overlaps(match.start(), match.end()):
                continue
            if not validator(match.group(0)):
                continue
            spans.append(SensitiveMatch(kind, match.start(), match.end(), match.group(0)))

    spans.sort(key=lambda item: item.start)
    return spans


def find_sensitive(text: str) -> list[SensitiveMatch]:
    """在任意文本中找出全部敏感号码；区间指向规范化文本，仅用于统计。"""
    return find_sensitive_in_normalized(normalize_text(text))


def detect_residuals(text: str) -> list[ResidualHit]:
    """逐行检测残留号码，行号从 1 开始。"""
    hits: list[ResidualHit] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        for match in find_sensitive(line):
            hits.append(ResidualHit(match.kind, line_number))
    return hits


def residual_entries(hits: list[ResidualHit]) -> list[dict]:
    """把命中整理成报告里的残留列表。

    同一类型、同一行合并为一条，并记录该行命中的次数。列表按行号再按类型
    排序，保证输出稳定。
    """
    counters: dict[tuple[str, int], int] = {}
    for hit in hits:
        key = (hit.kind, hit.line)
        counters[key] = counters.get(key, 0) + 1
    return [
        {"type": kind, "line": line, "count": count}
        for (kind, line), count in sorted(
            counters.items(), key=lambda item: (item[0][1], item[0][0])
        )
    ]
