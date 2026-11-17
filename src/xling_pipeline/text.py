"""文本归一化、确定性分词与语言识别。"""

import unicodedata

# 全角 ASCII（U+FF01..U+FF5E）与半角偏移
_FULLWIDTH_LOW = 0xFF01
_FULLWIDTH_HIGH = 0xFF5E
_FULLWIDTH_OFFSET = 0xFEE0
_IDEOGRAPHIC_SPACE = "\u3000"


def fold_fullwidth(text):
    """全角字母/数字/标点折叠为半角，全角空格折叠为普通空格。"""
    chars = []
    for char in text:
        code = ord(char)
        if char == _IDEOGRAPHIC_SPACE:
            chars.append(" ")
        elif _FULLWIDTH_LOW <= code <= _FULLWIDTH_HIGH:
            chars.append(chr(code - _FULLWIDTH_OFFSET))
        else:
            chars.append(char)
    return "".join(chars)


def normalize_text(text, rules):
    """按配置执行全半角折叠、大小写折叠与空白合并。"""
    if rules.get("fold_fullwidth", True):
        text = fold_fullwidth(text)
    if rules.get("case_fold", True):
        text = text.casefold()
    if rules.get("merge_whitespace", True):
        text = " ".join(text.split())
    return text.strip()


def _is_cjk(char):
    code = ord(char)
    return (
        0x4E00 <= code <= 0x9FFF
        or 0x3400 <= code <= 0x4DBF
        or 0xF900 <= code <= 0xFAFF
    )


def _is_latin(char):
    return "LATIN" in unicodedata.name(char, "")


def script_distribution(text):
    """统计 CJK 与拉丁字母字符数，返回（cjk 数, latin 数, 总字母数）。"""
    cjk = 0
    latin = 0
    for char in text:
        if _is_cjk(char):
            cjk += 1
        elif char.isalpha() and _is_latin(char):
            latin += 1
    return cjk, latin, cjk + latin


class Tokenizer:
    """前向最大匹配分词器。

    词表来自翻译表词项与停用词；英文按字母/数字串切分，中文按 FMM
    切分，未登录单字作为独立 token 保留（后续按停用词与索引过滤）。
    """

    def __init__(self, vocab_terms, stopwords):
        self.vocab = set(vocab_terms)
        self.stopwords = set(stopwords)
        self._max_len = max((len(term) for term in self.vocab), default=1)

    def tokenize(self, text):
        tokens = []
        index = 0
        length = len(text)
        while index < length:
            char = text[index]
            if not char.isalnum() and not _is_cjk(char):
                index += 1
                continue
            if _is_cjk(char):
                token = self._match_cjk(text, index)
                index += len(token)
            else:
                token, index = self._match_latin(text, index)
            if token and token not in self.stopwords:
                tokens.append(token)
        return tokens

    def _match_cjk(self, text, start):
        upper = min(len(text), start + self._max_len)
        for end in range(upper, start, -1):
            candidate = text[start:end]
            if candidate in self.vocab:
                return candidate
        return text[start]

    def _match_latin(self, text, start):
        end = start
        while end < len(text) and (text[end].isalnum()):
            end += 1
        return text[start:end], end


def detect_language(text, tokens, config, translation_vocab):
    """依据脚本分布与翻译表覆盖率判定语言，返回（语言标签, 判定依据）。"""
    li_config = config["language_identification"]
    cjk, latin, total = script_distribution(text)
    in_vocab = sum(1 for token in tokens if token in translation_vocab)
    coverage = round(in_vocab / len(tokens), 6) if tokens else 0.0
    cjk_ratio = round(cjk / total, 6) if total else 0.0
    latin_ratio = round(latin / total, 6) if total else 0.0
    threshold = li_config["cjk_script_threshold"]

    evidence = {
        "cjk_chars": cjk,
        "latin_chars": latin,
        "cjk_ratio": cjk_ratio,
        "latin_ratio": latin_ratio,
        "translation_coverage": coverage,
        "rule": "",
    }

    if total == 0:
        evidence["rule"] = "fallback_no_letters"
        return li_config["fallback_language"], evidence
    if cjk_ratio >= threshold:
        evidence["rule"] = "cjk_script_majority"
        return "zh", evidence
    if latin_ratio >= threshold:
        evidence["rule"] = "latin_script_majority"
        return "en", evidence
    # 脚本不占优时回退到翻译表覆盖率
    evidence["rule"] = "translation_coverage_fallback"
    return li_config["fallback_language"], evidence


def build_vocab(translation_pairs, config):
    terms = set()
    for pair in translation_pairs:
        terms.add(pair["source"])
        terms.add(pair["target"])
    for lang in config["languages"]["allowed"]:
        terms.update(config["tokenization"].get("stopwords", {}).get(lang, []))
        terms.update(config["tokenization"].get("fillers", {}).get(lang, []))
    return terms
