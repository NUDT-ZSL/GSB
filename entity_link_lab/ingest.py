"""Deterministic NER JSONL ingestion and codepoint accounting."""

from __future__ import annotations

import re
from collections import defaultdict
from pathlib import Path
from typing import Any

from .canonical import read_jsonl
from .errors import E_INPUT_INVALID, LinkError


TOKEN_RE = re.compile(r"[\w']+", flags=re.UNICODE)


def tokenize(text: str) -> list[str]:
    return [token.casefold() for token in TOKEN_RE.findall(text)]


def char_bigrams(text: str) -> set[str]:
    normalized = "".join(ch.lower() for ch in text if ch.isalnum())
    if len(normalized) < 2:
        return {normalized} if normalized else set()
    return {normalized[index : index + 2] for index in range(len(normalized) - 1)}


def jaccard(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def overlap_coefficient(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / min(len(left), len(right))


def bps(value: float) -> int:
    return max(0, min(10_000, int(round(value * 10_000))))


def _invalid(message: str, **details: Any) -> LinkError:
    return LinkError(E_INPUT_INVALID, message, stage="ingest", details=details)


def load_mentions(path: str | Path) -> list[dict[str, Any]]:
    rows = read_jsonl(path)
    sentences: list[dict[str, Any]] = []
    seen_sentence_keys: set[tuple[str, int]] = set()

    for line_no, row in enumerate(rows, 1):
        doc_id = row.get("doc_id")
        sent_id = row.get("sent_id")
        mentions_raw = row.get("mentions")
        if not isinstance(doc_id, str) or not doc_id:
            raise _invalid("doc_id must be a non-empty string", line=line_no)
        if not isinstance(sent_id, int) or isinstance(sent_id, bool) or sent_id < 0:
            raise _invalid("sent_id must be a non-negative integer", line=line_no, doc_id=doc_id)
        key = (doc_id, sent_id)
        if key in seen_sentence_keys:
            raise _invalid("duplicate doc_id/sent_id row", doc_id=doc_id, sent_id=sent_id)
        seen_sentence_keys.add(key)
        if not isinstance(mentions_raw, list):
            raise _invalid("mentions must be a list", doc_id=doc_id, sent_id=sent_id)
        sentence = row.get("sentence", "")
        if not isinstance(sentence, str):
            raise _invalid("sentence must be a string", doc_id=doc_id, sent_id=sent_id)

        mentions: list[dict[str, Any]] = []
        for local_index, raw_mention in enumerate(mentions_raw):
            if not isinstance(raw_mention, dict):
                raise _invalid("mention must be an object", doc_id=doc_id, sent_id=sent_id)
            name = raw_mention.get("name")
            entity_type = raw_mention.get("entity_type")
            start = raw_mention.get("start")
            end = raw_mention.get("end")
            if not isinstance(name, str) or not name:
                raise _invalid("mention name must be non-empty string", index=local_index)
            if not isinstance(entity_type, str) or not entity_type:
                raise _invalid("entity_type must be non-empty string", mention=name)
            if not isinstance(start, int) or isinstance(start, bool) or start < 0:
                raise _invalid("start must be non-negative codepoint index", mention=name)
            if not isinstance(end, int) or isinstance(end, bool) or end <= start:
                raise _invalid("end must be greater than start", mention=name)
            if end > len(sentence):
                raise _invalid(
                    "codepoint span exceeds sentence length",
                    mention=name,
                    end=end,
                    sentence_length=len(sentence),
                )
            surface = sentence[start:end]
            expected_text = raw_mention.get("text", name)
            if surface != expected_text:
                raise _invalid(
                    "codepoint span does not match mention text",
                    mention=name,
                    expected=expected_text,
                    actual=surface,
                    start=start,
                    end=end,
                )
            mention_id = raw_mention.get("mention_id")
            if mention_id is not None and not isinstance(mention_id, str):
                raise _invalid("mention_id must be a string", mention=name)
            mentions.append(
                {
                    "mention_id": mention_id,
                    "doc_id": doc_id,
                    "sent_id": sent_id,
                    "name": name,
                    "text": expected_text,
                    "entity_type": entity_type,
                    "start": start,
                    "end": end,
                    "local_index": local_index,
                    "tokens": tokenize(expected_text),
                    "bigrams": sorted(char_bigrams(expected_text)),
                }
            )

        mentions.sort(key=lambda item: (item["start"], item["end"], item["entity_type"], item["name"]))
        for index, mention in enumerate(mentions):
            mention["local_index"] = index
            mention["sentence_text"] = sentence
            mention["sentence_tokens"] = tokenize(sentence)
        sentences.append(
            {
                "doc_id": doc_id,
                "sent_id": sent_id,
                "sentence": sentence,
                "tokens": tokenize(sentence),
                "mentions": mentions,
                "line_no": line_no,
            }
        )

    sentences.sort(key=lambda item: (item["doc_id"], item["sent_id"]))
    offsets: dict[tuple[str, int], int] = {}
    by_doc: dict[str, list[dict[str, Any]]] = defaultdict(list)
    cumulative: dict[str, int] = defaultdict(int)
    for sentence in sentences:
        doc_id = sentence["doc_id"]
        offsets[(doc_id, sentence["sent_id"])] = cumulative[doc_id]
        cumulative[doc_id] += len(sentence["sentence"]) + 1
        by_doc[doc_id].append(sentence)

    all_mentions: list[dict[str, Any]] = []
    for sentence in sentences:
        base = offsets[(sentence["doc_id"], sentence["sent_id"])]
        for mention in sentence["mentions"]:
            mention["global_start"] = base + mention["start"]
            mention["global_end"] = base + mention["end"]
            all_mentions.append(mention)

    all_mentions.sort(
        key=lambda item: (
            item["doc_id"],
            item["sent_id"],
            item["start"],
            item["end"],
            item["entity_type"],
            item["mention_id"] or "",
        )
    )
    seen_ids: set[str] = set()
    for order_index, mention in enumerate(all_mentions):
        if not mention["mention_id"]:
            mention["mention_id"] = f"m-{order_index + 1:04d}"
        if mention["mention_id"] in seen_ids:
            raise _invalid("duplicate mention_id", mention_id=mention["mention_id"])
        seen_ids.add(mention["mention_id"])
        mention["order_index"] = order_index

    return all_mentions


def index_mentions(mentions: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {mention["mention_id"]: mention for mention in mentions}


def load_seed_candidates(path: str | Path, mentions: list[dict[str, Any]]) -> list[dict[str, str]]:
    rows = read_jsonl(path)
    by_id = index_mentions(mentions)
    pairs: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for row in rows:
        left = row.get("mention_a") or row.get("a")
        right = row.get("mention_b") or row.get("b")
        if not isinstance(left, str) or not isinstance(right, str):
            raise _invalid("candidate requires mention_a and mention_b strings", row=row)
        if left not in by_id or right not in by_id:
            raise _invalid("candidate references unknown mention", a=left, b=right)
        a, b = sorted((by_id[left], by_id[right]), key=lambda item: item["order_index"])
        if a["doc_id"] != b["doc_id"]:
            raise _invalid("candidate crosses documents", a=left, b=right)
        key = (a["mention_id"], b["mention_id"])
        if key in seen:
            continue
        seen.add(key)
        pairs.append(
            {
                "pair_key": f"{key[0]}|{key[1]}",
                "mention_a": key[0],
                "mention_b": key[1],
                "origin": str(row.get("origin", "seed_list")),
            }
        )
    return sorted(pairs, key=lambda item: item["pair_key"])
