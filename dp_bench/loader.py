"""本地输入加载与确定性校验。

样本文件每行格式::

    S01 经济/NN 建设/NN 取得/VV 成就/NN

句 ID 与 token 之间、token 之间以空格分隔；每个 token 必须为“词/词性”，
词与词性数量不匹配（缺斜杠、空 token、只有句 ID 等）判 E_SAMPLE_MALFORMED。
"""

from __future__ import annotations

import json
from pathlib import Path

from .errors import ConfigInvalidError, SampleMalformedError
from .model import Token, TransitionModel

CONFIG_FIELDS = (
    "max_workers",
    "max_steps_per_sentence",
    "global_step_budget",
    "per_sentence_quota",
    "timeout_observe_ms",
    "batch_size",
)


def load_json(path: str | Path) -> dict:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def validate_config(raw: dict) -> dict:
    """校验调度配置，返回规范化后的配置字典。"""
    if not isinstance(raw, dict):
        raise ConfigInvalidError("调度配置必须为 JSON 对象", phase="config")
    missing = [name for name in CONFIG_FIELDS if name not in raw]
    if missing:
        raise ConfigInvalidError(
            f"调度配置缺少字段: {','.join(missing)}", phase="config")

    values = {}
    for name in CONFIG_FIELDS:
        value = raw[name]
        if isinstance(value, bool) or not isinstance(value, int):
            raise ConfigInvalidError(
                f"配置字段 {name} 必须为整数，实际为 {type(value).__name__}",
                phase="config")
        values[name] = value

    if values["max_workers"] <= 0:
        raise ConfigInvalidError(
            f"并发数非正: max_workers={values['max_workers']}",
            phase="config")
    if values["global_step_budget"] <= 0:
        raise ConfigInvalidError(
            "全局总步数预算必须大于零", phase="config")
    if values["per_sentence_quota"] <= 0:
        raise ConfigInvalidError(
            "每句预算配额必须大于零", phase="config")
    if values["max_steps_per_sentence"] <= 0:
        raise ConfigInvalidError(
            "每句最大解析步数必须大于零", phase="config")
    if values["batch_size"] <= 0:
        raise ConfigInvalidError("批次大小必须大于零", phase="config")
    if values["timeout_observe_ms"] < 0:
        raise ConfigInvalidError(
            "超时观测阈值不能为负", phase="config")
    return values


def load_config(path: str | Path) -> dict:
    return validate_config(load_json(path))


def load_model(path: str | Path) -> TransitionModel:
    return TransitionModel(load_json(path))


def parse_sample_line(line: str, line_no: int) -> tuple[str, list[Token]]:
    parts = line.split()
    if len(parts) < 2:
        raise SampleMalformedError(
            f"第 {line_no} 行样本畸形：只有句 ID 没有 token",
            phase="sample", step_no=line_no)
    sentence_id = parts[0]
    tokens: list[Token] = []
    for idx, piece in enumerate(parts[1:], start=1):
        if piece.count("/") != 1:
            raise SampleMalformedError(
                f"第 {line_no} 行 token '{piece}' 词与词性数量不匹配"
                "（缺少斜杠或斜杠过多）",
                phase="sample", sentence_id=sentence_id, step_no=line_no)
        word, pos = piece.split("/")
        if not word or not pos:
            raise SampleMalformedError(
                f"第 {line_no} 行 token '{piece}' 词或词性为空",
                phase="sample", sentence_id=sentence_id, step_no=line_no)
        tokens.append(Token(index=idx, word=word, pos=pos))
    return sentence_id, tokens


def load_samples(path: str | Path) -> list[tuple[str, list[Token]]]:
    """加载全部样本；任何畸形行都抛 E_SAMPLE_MALFORMED。"""
    sentences: list[tuple[str, list[Token]]] = []
    seen: set[str] = set()
    with open(path, "r", encoding="utf-8") as fh:
        for line_no, raw_line in enumerate(fh, start=1):
            line = raw_line.strip()
            if not line:
                continue
            sentence_id, tokens = parse_sample_line(line, line_no)
            if sentence_id in seen:
                raise SampleMalformedError(
                    f"第 {line_no} 行句 ID '{sentence_id}' 重复",
                    phase="sample", sentence_id=sentence_id,
                    step_no=line_no)
            seen.add(sentence_id)
            sentences.append((sentence_id, tokens))
    if not sentences:
        raise SampleMalformedError("样本文件为空", phase="sample")
    return sentences
