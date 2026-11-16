"""固定种子的本地确定性语料与查询集生成。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np

from .normalization import normalize_text, tokenize


def build_vocab(size: int = 60) -> list[str]:
    """生成可控词表，base 词全部小写、纯 ASCII，保证归一化稳定。"""
    roots = [
        "alpha", "bravo", "charlie", "delta", "echo", "foxtrot", "golf",
        "hotel", "india", "juliet", "kilo", "lima", "mike", "november",
        "oscar", "papa", "quebec", "romeo", "sierra", "tango", "uniform",
        "victor", "whiskey", "xray", "yankee", "zulu",
    ]
    vocab: list[str] = []
    index = 0
    while len(vocab) < size:
        root = roots[index % len(roots)]
        suffix = index // len(roots)
        vocab.append(root if suffix == 0 else f"{root}{suffix}")
        index += 1
    return vocab[:size]


def _zipf_probs(vocab_size: int, skew: float) -> np.ndarray:
    ranks = np.arange(1, vocab_size + 1, dtype=np.float64)
    weights = 1.0 / np.power(ranks, skew)
    return weights / weights.sum()


def _surface_form(token: str, rng: np.random.Generator) -> str:
    """以确定性方式注入大小写与全角变体，归一化后仍映射回词表词项。"""
    choice = rng.integers(0, 5)
    if choice == 0:
        return token.upper()
    if choice == 1:
        return token.capitalize()
    if choice == 2:
        return "".join(chr(ord(ch) + 0xFEE0) if "a" <= ch <= "z" else ch
                       for ch in token)
    if choice == 3:
        return f" {token}  "
    return token


def generate_corpus(
    *,
    seed: int,
    doc_count: int,
    vocab: list[str],
    skew: float = 1.15,
    min_tokens: int = 18,
    max_tokens: int = 60,
    invalid_specs: list[dict[str, Any]] | None = None,
) -> list[str]:
    rng = np.random.default_rng(seed)
    probs = _zipf_probs(len(vocab), skew)
    lines: list[str] = []
    invalid_specs = invalid_specs or []
    invalid_by_line = {spec["line_no"]: spec for spec in invalid_specs}
    for line_no in range(1, doc_count + 1):
        if line_no in invalid_by_line:
            lines.append(_invalid_line(invalid_by_line[line_no], line_no))
            continue
        length = int(rng.integers(min_tokens, max_tokens + 1))
        chosen = rng.choice(len(vocab), size=length, p=probs)
        tokens = [
            _surface_form(vocab[int(index)], rng) for index in chosen
        ]
        body = " ".join(tokens)
        source = f"src-{int(rng.integers(0, 4))}"
        doc = {
            "doc_id": f"doc-{line_no:05d}",
            "title": f"标题 {line_no:05d}",
            "body": body,
            "source": source,
        }
        lines.append(json.dumps(doc, ensure_ascii=False))
    return lines


def _invalid_line(spec: dict[str, Any], line_no: int) -> str:
    kind = spec["kind"]
    if kind == "parse_invalid":
        return '{"doc_id": "doc-bad", "body": "alpha alpha", '
    if kind == "empty_doc":
        return json.dumps({
            "doc_id": f"doc-empty-{line_no:05d}",
            "title": "空正文",
            "body": spec.get("body", "   "),
            "source": "src-bad",
        }, ensure_ascii=False)
    if kind == "dup_doc_id":
        return json.dumps({
            "doc_id": spec["dup_of"],
            "title": "重复文档",
            "body": "alpha bravo charlie",
            "source": "src-bad",
        }, ensure_ascii=False)
    raise ValueError(f"未知异常注入类型: {kind}")


def generate_query_set(
    *,
    seed: int,
    vocab: list[str],
    lines: list[str],
    query_count: int = 10,
) -> dict[str, Any]:
    """从成功文档统计期望命中数，查询包含高频/低频/零命中/多词合取。"""
    rng = np.random.default_rng(seed + 991)
    doc_term_sets: list[set[str]] = []
    doc_term_positions: list[dict[str, list[int]]] = []
    valid_ids: list[str] = []
    for raw_line in lines:
        try:
            payload = json.loads(raw_line)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict) or not payload.get("body", " ").strip():
            continue
        tokens = tokenize(payload["body"])
        if not tokens:
            continue
        positions: dict[str, list[int]] = {}
        for index, token in enumerate(tokens):
            positions.setdefault(token, []).append(index)
        doc_term_sets.append(set(tokens))
        doc_term_positions.append(positions)
        valid_ids.append(str(payload["doc_id"]))

    doc_freq: dict[str, int] = {}
    for term_set in doc_term_sets:
        for term in term_set:
            doc_freq[term] = doc_freq.get(term, 0) + 1
    high_freq = sorted(
        (term for term in vocab if doc_freq.get(term, 0) >= 3),
        key=lambda term: (-doc_freq[term], term),
    )
    low_freq = sorted(
        (term for term in vocab if 0 < doc_freq.get(term, 0) < 3),
        key=lambda term: (doc_freq[term], term),
    )

    queries: list[dict[str, Any]] = []

    def add_query(terms: list[str], want_positions: bool = True) -> None:
        seq = len(queries)
        expected = [
            doc_id
            for doc_id, term_set in zip(valid_ids, doc_term_sets)
            if all(normalize_text(term).split()[0] in term_set for term in terms)
        ]
        queries.append({
            "query_id": f"q-{seq:03d}",
            "sort_key": seq,
            "terms": terms,
            "want_positions": want_positions,
            "expected_count": len(expected),
            "expected_doc_ids": expected,
        })

    if high_freq:
        add_query([high_freq[0]])
    if len(high_freq) > 1:
        add_query([high_freq[0], high_freq[1]])
    if len(high_freq) > 3:
        add_query([high_freq[1], high_freq[2], high_freq[3]])
    if low_freq:
        add_query([low_freq[0]])
    add_query(["zzz-never-appears-000"])
    add_query([high_freq[0] if high_freq else vocab[0]], want_positions=False)
    while len(queries) < query_count and high_freq:
        first = high_freq[int(rng.integers(0, len(high_freq)))]
        second = high_freq[int(rng.integers(0, len(high_freq)))]
        terms = list(dict.fromkeys([first, second]))
        add_query(terms)
    return {
        "seed": seed + 991,
        "queries": queries[:query_count],
    }


def write_samples(
    sample_dir: str | Path,
    *,
    seed: int = 20261117,
    doc_count: int = 120,
    vocab_size: int = 60,
) -> dict[str, Any]:
    sample_path = Path(sample_dir)
    sample_path.mkdir(parents=True, exist_ok=True)
    vocab = build_vocab(vocab_size)
    vocab_path = sample_path / "vocab.txt"
    vocab_path.write_text("\n".join(vocab) + "\n", encoding="utf-8")

    invalid_specs = [
        {"line_no": 7, "kind": "parse_invalid"},
        {"line_no": 13, "kind": "empty_doc"},
        {"line_no": 29, "kind": "dup_doc_id", "dup_of": "doc-00001"},
        {"line_no": 58, "kind": "empty_doc", "body": "\u3000　"},
    ]
    lines = generate_corpus(
        seed=seed,
        doc_count=doc_count,
        vocab=vocab,
        invalid_specs=invalid_specs,
    )
    corpus_path = sample_path / "corpus.jsonl"
    corpus_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    query_set = generate_query_set(
        seed=seed, vocab=vocab, lines=lines, query_count=10,
    )
    query_path = sample_path / "queries.json"
    query_path.write_text(
        json.dumps(query_set, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return {
        "vocab_path": str(vocab_path),
        "corpus_path": str(corpus_path),
        "queries_path": str(query_path),
        "seed": seed,
        "doc_count": doc_count,
        "vocab_size": vocab_size,
        "invalid_specs": invalid_specs,
    }
