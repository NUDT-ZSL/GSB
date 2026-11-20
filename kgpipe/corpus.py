from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .errors import KGError
from .models import Corpus, Document, Sentence
from .util import sha256_bytes, sha256_text


REQUIRED_FIELDS = ("shard_id", "doc_id", "sent_no", "text", "char_len")


def _fail(message: str, **details: Any) -> None:
    raise KGError("E_INGEST", message, details=details)


def shard_directory(data_dir: str | Path) -> Path:
    root = Path(data_dir)
    nested = root / "shards"
    return nested if nested.is_dir() else root


def load_corpus(data_dir: str | Path) -> Corpus:
    directory = shard_directory(data_dir)
    if not directory.exists():
        _fail("语料分片目录不存在", path=str(directory))
    files = sorted(directory.glob("*.jsonl"), key=lambda path: path.name)
    if not files:
        _fail("没有找到 JSONL 分片文件", path=str(directory))

    sentence_map: dict[tuple[str, int], Sentence] = {}
    doc_sentences: dict[str, list[Sentence]] = {}
    shard_ids: set[str] = set()
    shard_checksums: dict[str, str] = {}
    combined: list[str] = []

    for path in files:
        raw_bytes = path.read_bytes()
        file_digest = sha256_bytes(raw_bytes)
        combined.append(f"{path.name}:{file_digest}")
        file_shard_ids: set[str] = set()
        try:
            content = raw_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            _fail(
                "分片不是合法 UTF-8",
                path=str(path),
                error=str(exc),
            )
        for line_number, line in enumerate(content.splitlines(), start=1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                _fail(
                    "分片包含非法 JSON",
                    path=str(path),
                    line_number=line_number,
                    error=str(exc),
                )
            if not isinstance(row, dict):
                _fail(
                    "分片行必须是 JSON 对象",
                    path=str(path),
                    line_number=line_number,
                )
            missing = [field for field in REQUIRED_FIELDS if field not in row]
            if missing:
                _fail(
                    "分片行缺少必需字段",
                    path=str(path),
                    line_number=line_number,
                    missing_fields=missing,
                )
            shard_id = row["shard_id"]
            doc_id = row["doc_id"]
            sent_no = row["sent_no"]
            text = row["text"]
            char_len = row["char_len"]
            if not isinstance(shard_id, str) or not shard_id:
                _fail(
                    "shard_id 必须是非空字符串",
                    path=str(path),
                    line_number=line_number,
                )
            if not isinstance(doc_id, str) or not doc_id:
                _fail(
                    "doc_id 必须是非空字符串",
                    path=str(path),
                    line_number=line_number,
                )
            if not isinstance(sent_no, int) or isinstance(sent_no, bool) or sent_no < 0:
                _fail(
                    "sent_no 必须是非负整数",
                    path=str(path),
                    line_number=line_number,
                    doc_id=doc_id,
                )
            if not isinstance(text, str):
                _fail(
                    "text 必须是字符串",
                    path=str(path),
                    line_number=line_number,
                    doc_id=doc_id,
                )
            if not isinstance(char_len, int) or isinstance(char_len, bool):
                _fail(
                    "char_len 必须是整数",
                    path=str(path),
                    line_number=line_number,
                    doc_id=doc_id,
                )
            if char_len != len(text):
                _fail(
                    "char_len 与 text 的码点长度不一致",
                    path=str(path),
                    line_number=line_number,
                    doc_id=doc_id,
                    expected=len(text),
                    actual=char_len,
                )
            key = (doc_id, sent_no)
            if key in sentence_map:
                previous = sentence_map[key]
                _fail(
                    "同一 doc_id 的 sent_no 重复",
                    path=str(path),
                    line_number=line_number,
                    doc_id=doc_id,
                    sent_no=sent_no,
                    previous_shard_id=previous.shard_id,
                )
            sentence = Sentence(shard_id, doc_id, sent_no, text, char_len)
            sentence_map[key] = sentence
            doc_sentences.setdefault(doc_id, []).append(sentence)
            file_shard_ids.add(shard_id)
            shard_ids.add(shard_id)

        if len(file_shard_ids) != 1:
            _fail(
                "一个分片文件必须只包含一个 shard_id",
                path=str(path),
                shard_ids=sorted(file_shard_ids),
            )
        shard_id = next(iter(file_shard_ids))
        if shard_id in shard_checksums:
            _fail(
                "不同分片文件重复使用同一 shard_id",
                path=str(path),
                shard_id=shard_id,
            )
        shard_checksums[shard_id] = file_digest

    documents: list[Document] = []
    for doc_id in sorted(doc_sentences):
        sentences = sorted(doc_sentences[doc_id], key=lambda item: item.sent_no)
        shards = sorted({item.shard_id for item in sentences})
        documents.append(Document(doc_id=doc_id, shard_ids=shards, sentences=sentences))
    if not documents:
        _fail("语料为空", path=str(directory))

    return Corpus(
        documents=tuple(documents),
        shard_count=len(shard_ids),
        sentence_count=sum(len(doc.sentences) for doc in documents),
        char_count=sum(doc.text_char_count for doc in documents),
        input_checksum="sha256:" + sha256_text("\n".join(sorted(combined))),
        shard_checksums=dict(sorted(shard_checksums.items())),
    )

