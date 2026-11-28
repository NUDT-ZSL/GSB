from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class PhonemeToken:
    label: str
    kind: str
    syllable_index: int
    order_in_syllable: int


@dataclass
class SyllableToken:
    index: int
    text: str
    pinyin: str
    initial: str
    final: str
    phrase_index: int
    phonemes: list[PhonemeToken] = field(default_factory=list)


@dataclass
class LyricPhrase:
    index: int
    text: str
    syllable_indices: list[int]


@dataclass
class Lyrics:
    syllables: list[SyllableToken]
    phrases: list[LyricPhrase]
    source: str


# A compact offline pronunciation table covers the deterministic examples and
# common singing-practice words. It intentionally has no network dependency.
_PINYIN: dict[str, str] = {
    "春": "chun",
    "风": "feng",
    "吹": "chui",
    "过": "guo",
    "小": "xiao",
    "河": "he",
    "旁": "pang",
    "白": "bai",
    "云": "yun",
    "落": "luo",
    "在": "zai",
    "青": "qing",
    "山": "shan",
    "上": "shang",
    "月": "yue",
    "光": "guang",
    "轻": "qing",
    "照": "zhao",
    "岗": "gang",
    "晚": "wan",
    "慢": "man",
    "送": "song",
    "花": "hua",
    "香": "xiang",
    "歌": "ge",
    "声": "sheng",
    "飞": "fei",
    "天": "tian",
    "海": "hai",
    "梦": "meng",
    "想": "xiang",
    "心": "xin",
    "中": "zhong",
    "的": "de",
    "你": "ni",
    "我": "wo",
    "他": "ta",
    "她": "ta",
    "爱": "ai",
    "情": "qing",
    "故": "gu",
    "乡": "xiang",
    "远": "yuan",
    "方": "fang",
    "路": "lu",
    "长": "chang",
    "星": "xing",
    "辰": "chen",
    "大": "da",
    "地": "di",
    "人": "ren",
    "间": "jian",
    "岁": "sui",
    "时": "shi",
    "雨": "yu",
    "水": "shui",
    "火": "huo",
    "夏": "xia",
    "秋": "qiu",
    "冬": "dong",
    "东": "dong",
    "南": "nan",
    "西": "xi",
    "北": "bei",
    "前": "qian",
    "后": "hou",
    "左": "zuo",
    "右": "you",
    "高": "gao",
    "低": "di",
    "起": "qi",
    "来": "lai",
    "去": "qu",
    "一": "yi",
    "二": "er",
    "三": "san",
    "四": "si",
    "五": "wu",
    "六": "liu",
    "七": "qi",
    "八": "ba",
    "九": "jiu",
    "十": "shi",
    "百": "bai",
    "千": "qian",
    "万": "wan",
}

_INITIALS = (
    "zh",
    "ch",
    "sh",
    "b",
    "p",
    "m",
    "f",
    "d",
    "t",
    "n",
    "l",
    "g",
    "k",
    "h",
    "j",
    "q",
    "x",
    "r",
    "z",
    "c",
    "s",
    "y",
    "w",
)


def _is_cjk(character: str) -> bool:
    code = ord(character)
    return (
        0x3400 <= code <= 0x4DBF
        or 0x4E00 <= code <= 0x9FFF
        or 0xF900 <= code <= 0xFAFF
    )


def split_pinyin(syllable: str) -> tuple[str, str]:
    text = syllable.strip().lower()
    text = re.sub(r"[0-9]", "", text)
    for initial in _INITIALS:
        if text.startswith(initial):
            final = text[len(initial) :]
            return initial, final or text
    return "", text


def _english_syllables(word: str) -> list[str]:
    chunks = re.findall(r"[A-Za-z]+(?:'[A-Za-z]+)?", word)
    result: list[str] = []
    for chunk in chunks:
        lowercase = chunk.lower()
        vowel_groups = list(re.finditer(r"[aeiouy]+", lowercase))
        if not vowel_groups:
            result.append(chunk)
            continue
        start = 0
        for position, match in enumerate(vowel_groups):
            if position == len(vowel_groups) - 1:
                end = len(chunk)
            else:
                next_vowel = vowel_groups[position + 1].start()
                split_at = max(match.end(), next_vowel - 1)
                end = min(len(chunk), split_at)
            part = chunk[start:end]
            if part:
                result.append(part)
            start = end
    return result


def _phonemes_for(
    syllable_index: int, text: str, pinyin: str, initial: str, final: str
) -> list[PhonemeToken]:
    phonemes: list[PhonemeToken] = []
    order = 0
    if initial:
        phonemes.append(
            PhonemeToken(
                label=initial,
                kind="consonant",
                syllable_index=syllable_index,
                order_in_syllable=order,
            )
        )
        order += 1
    vowel_label = final or pinyin or text
    phonemes.append(
        PhonemeToken(
            label=vowel_label,
            kind="vowel",
            syllable_index=syllable_index,
            order_in_syllable=order,
        )
    )
    return phonemes


def parse_lyrics_text(text: str, source: str = "<text>") -> Lyrics:
    syllables: list[SyllableToken] = []
    phrases: list[LyricPhrase] = []
    phrase_index = 0
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        phrase_syllables: list[int] = []
        cursor = 0
        while cursor < len(line):
            character = line[cursor]
            if _is_cjk(character):
                pinyin = _PINYIN.get(character)
                if pinyin is None:
                    pinyin = character
                    initial, final = "", character
                else:
                    initial, final = split_pinyin(pinyin)
                syllable_index = len(syllables)
                syllable = SyllableToken(
                    index=syllable_index,
                    text=character,
                    pinyin=pinyin,
                    initial=initial,
                    final=final,
                    phrase_index=phrase_index,
                )
                syllable.phonemes = _phonemes_for(
                    syllable_index, character, pinyin, initial, final
                )
                syllables.append(syllable)
                phrase_syllables.append(syllable_index)
                cursor += 1
                continue
            if character.isascii() and character.isalpha():
                match = re.match(r"[A-Za-z]+(?:'[A-Za-z]+)?", line[cursor:])
                assert match is not None
                word = match.group(0)
                cursor += len(word)
                for part in _english_syllables(word):
                    lower = part.lower()
                    vowel_position = next(
                        (index for index, char in enumerate(lower) if char in "aeiouy"),
                        len(lower),
                    )
                    initial = part[:vowel_position].lower()
                    final = part[vowel_position:].lower() or part.lower()
                    syllable_index = len(syllables)
                    syllable = SyllableToken(
                        index=syllable_index,
                        text=part,
                        pinyin=lower,
                        initial=initial,
                        final=final,
                        phrase_index=phrase_index,
                    )
                    syllable.phonemes = _phonemes_for(
                        syllable_index, part, lower, initial, final
                    )
                    syllables.append(syllable)
                    phrase_syllables.append(syllable_index)
                continue
            cursor += 1
        if phrase_syllables:
            phrases.append(
                LyricPhrase(
                    index=phrase_index,
                    text="".join(syllables[index].text for index in phrase_syllables),
                    syllable_indices=phrase_syllables,
                )
            )
            phrase_index += 1
    return Lyrics(syllables=syllables, phrases=phrases, source=source)


def load_lyrics(path: str | Path) -> Lyrics:
    path = Path(path)
    text = path.read_text(encoding="utf-8")
    return parse_lyrics_text(text, str(path))

