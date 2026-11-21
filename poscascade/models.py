from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .canonical import object_checksum, read_json
from .errors import BusinessError, ErrorCode


@dataclass(frozen=True)
class LabelSet:
    version: int
    labels: tuple[str, ...]
    priors: dict[str, float]
    checksum: str

    def index(self, label: str) -> int:
        try:
            return self.labels.index(label)
        except ValueError as exc:
            raise BusinessError(
                ErrorCode.E_LABEL_INVALID,
                f"标签 {label!r} 不在受控集合中",
                details={"label": label},
            ) from exc

    def to_ref(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "checksum": self.checksum,
        }

    def as_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "labels": list(self.labels),
            "priors": dict(self.priors),
            "checksum": self.checksum,
        }


@dataclass(frozen=True)
class LexiconEntry:
    word: str
    freq: int
    pos: tuple[str, ...]
    index: int


@dataclass(frozen=True)
class Lexicon:
    version: int
    entries: tuple[LexiconEntry, ...]
    checksum: str
    total_freq: int
    by_word: dict[str, LexiconEntry]

    def to_ref(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "checksum": self.checksum,
        }

    def as_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "entries": [
                {"word": item.word, "freq": item.freq, "pos": list(item.pos)}
                for item in self.entries
            ],
            "checksum": self.checksum,
        }


def _read_mapping(path: str | Path, kind: str) -> dict[str, Any]:
    try:
        raw = read_json(path)
    except (OSError, ValueError) as exc:
        code = ErrorCode.E_LABEL_INVALID if kind == "labels" else ErrorCode.E_DICT_FORMAT
        raise BusinessError(code, f"{kind} 读取失败: {exc}", details={"path": str(path)}) from exc
    if not isinstance(raw, dict):
        code = ErrorCode.E_LABEL_INVALID if kind == "labels" else ErrorCode.E_DICT_FORMAT
        raise BusinessError(code, f"{kind} 根节点必须是 JSON 对象")
    return raw


def load_labels(path: str | Path) -> LabelSet:
    raw = _read_mapping(path, "labels")
    version = raw.get("version")
    labels = raw.get("labels")
    priors_raw = raw.get("priors", {})
    if not isinstance(version, int) or isinstance(version, bool) or version <= 0:
        raise BusinessError(ErrorCode.E_LABEL_INVALID, "labels.version 必须是正整数")
    if not isinstance(labels, list) or not labels:
        raise BusinessError(ErrorCode.E_LABEL_INVALID, "labels.labels 必须是非空数组")
    if any(not isinstance(label, str) or not label for label in labels):
        raise BusinessError(ErrorCode.E_LABEL_INVALID, "每个标签必须是非空字符串")
    if len(set(labels)) != len(labels):
        raise BusinessError(ErrorCode.E_LABEL_INVALID, "受控标签集合不得包含重复项")
    if not isinstance(priors_raw, dict):
        raise BusinessError(ErrorCode.E_LABEL_INVALID, "labels.priors 必须是 JSON 对象")

    priors: dict[str, float] = {}
    for index, label in enumerate(labels):
        default_prior = 1.0 / float(index + 1)
        value = priors_raw.get(label, default_prior)
        if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
            raise BusinessError(
                ErrorCode.E_LABEL_INVALID,
                f"标签 {label} 的先验权重必须是正数",
                details={"label": label, "value": value},
            )
        priors[label] = float(value)
    unknown_prior_labels = sorted(set(priors_raw) - set(labels))
    if unknown_prior_labels:
        raise BusinessError(
            ErrorCode.E_LABEL_INVALID,
            "labels.priors 包含不在受控集合中的标签",
            details={"labels": unknown_prior_labels},
        )

    content = {"version": version, "labels": labels, "priors": priors}
    checksum = object_checksum(content)
    declared = raw.get("checksum")
    if declared is not None and declared != checksum:
        raise BusinessError(
            ErrorCode.E_LABEL_INVALID,
            "标签集声明的 checksum 与内容不一致",
            details={"declared": declared, "computed": checksum},
        )
    return LabelSet(
        version=version,
        labels=tuple(labels),
        priors=priors,
        checksum=checksum,
    )


def load_lexicon(path: str | Path, labels: LabelSet) -> Lexicon:
    raw = _read_mapping(path, "dict")
    version = raw.get("version")
    entries_raw = raw.get("entries")
    if not isinstance(version, int) or isinstance(version, bool) or version <= 0:
        raise BusinessError(ErrorCode.E_DICT_FORMAT, "词典 version 必须是正整数")
    if not isinstance(entries_raw, list) or not entries_raw:
        raise BusinessError(ErrorCode.E_DICT_FORMAT, "词典 entries 必须是非空数组")

    entries: list[LexiconEntry] = []
    seen: set[str] = set()
    for index, item in enumerate(entries_raw):
        if not isinstance(item, dict):
            raise BusinessError(
                ErrorCode.E_DICT_FORMAT,
                "词典条目必须是 JSON 对象",
                details={"dict_index": index},
            )
        word = item.get("word")
        freq = item.get("freq")
        pos = item.get("pos")
        if not isinstance(word, str) or not word:
            raise BusinessError(
                ErrorCode.E_DICT_FORMAT,
                "词典条目 word 必须是非空字符串",
                details={"dict_index": index, "word": word},
            )
        if word in seen:
            raise BusinessError(
                ErrorCode.E_DICT_FORMAT,
                f"词典条目重复: {word}",
                details={"dict_index": index, "word": word},
            )
        if not isinstance(freq, int) or isinstance(freq, bool) or freq <= 0:
            raise BusinessError(
                ErrorCode.E_DICT_FORMAT,
                f"词典条目 {word} 的 freq 必须是正整数",
                details={"dict_index": index, "word": word, "freq": freq},
            )
        if not isinstance(pos, list) or not pos:
            raise BusinessError(
                ErrorCode.E_DICT_FORMAT,
                f"词典条目 {word} 的 pos 必须是非空数组",
                details={"dict_index": index, "word": word},
            )
        if any(not isinstance(label, str) or not label for label in pos):
            raise BusinessError(
                ErrorCode.E_DICT_FORMAT,
                f"词典条目 {word} 的 pos 包含非字符串标签",
                details={"dict_index": index, "word": word},
            )
        invalid_labels = sorted(set(pos) - set(labels.labels))
        if invalid_labels:
            raise BusinessError(
                ErrorCode.E_LABEL_INVALID,
                f"词典条目 {word} 引用了越界标签",
                details={
                    "dict_index": index,
                    "word": word,
                    "invalid_labels": invalid_labels,
                },
            )
        canonical_pos = tuple(sorted(set(pos), key=labels.index))
        entries.append(
            LexiconEntry(
                word=word,
                freq=freq,
                pos=canonical_pos,
                index=index,
            )
        )
        seen.add(word)

    entries.sort(key=lambda entry: (entry.word, entry.index))
    content = {
        "version": version,
        "entries": [
            {"word": entry.word, "freq": entry.freq, "pos": sorted(entry.pos)}
            for entry in entries
        ],
    }
    checksum = object_checksum(content)
    declared = raw.get("checksum")
    if declared is not None and declared != checksum:
        raise BusinessError(
            ErrorCode.E_DICT_FORMAT,
            "词典声明的 checksum 与内容不一致",
            details={"declared": declared, "computed": checksum},
        )
    by_word = {entry.word: entry for entry in entries}
    total_freq = sum(entry.freq for entry in entries)
    return Lexicon(
        version=version,
        entries=tuple(entries),
        checksum=checksum,
        total_freq=total_freq,
        by_word=by_word,
    )
