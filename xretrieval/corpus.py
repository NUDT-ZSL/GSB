"""Document and query JSONL loading with business validation."""

import json

from .errors import E_CONFIG_INVALID, E_DUP_DOC, E_TERM_INVALID, XError


def _read_jsonl(path, kind):
    rows = []
    with open(path, "r", encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise XError(E_CONFIG_INVALID,
                             f"malformed JSON at {path}:{lineno}: {exc}",
                             field=f"{kind}[{lineno}]")
    return rows


def _validate_terms(terms, owner, owner_key):
    if not isinstance(terms, list):
        raise XError(E_TERM_INVALID, f"terms must be a list for {owner}",
                     **{owner_key: owner})
    for item in terms:
        term = item.get("term") if isinstance(item, dict) else None
        if not term:
            raise XError(E_TERM_INVALID, f"empty term in {owner}",
                         **{owner_key: owner})
        tf = item.get("tf", 1)
        if not isinstance(tf, int) or tf <= 0:
            raise XError(E_TERM_INVALID,
                         f"term frequency must be a positive integer in {owner}",
                         **{owner_key: owner})


def load_documents(path):
    """Load and validate documents. Returns list sorted by (doc_lang, doc_id)."""
    docs = _read_jsonl(path, "documents")
    seen = {}
    for doc in docs:
        doc_id = doc.get("doc_id")
        if not doc_id:
            raise XError(E_TERM_INVALID, "document missing doc_id", field="doc_id")
        if doc_id in seen:
            raise XError(E_DUP_DOC, f"duplicate doc_id {doc_id!r}", doc_id=doc_id)
        seen[doc_id] = doc.get("doc_lang")
        if not doc.get("doc_lang"):
            raise XError(E_CONFIG_INVALID, f"document {doc_id} missing doc_lang",
                         doc_id=doc_id, field="doc_lang")
        _validate_terms(doc.get("terms"), doc_id, "doc_id")
        computed = sum(t.get("tf", 1) for t in doc["terms"])
        if "doc_len" in doc and doc["doc_len"] != computed:
            raise XError(E_CONFIG_INVALID,
                         f"doc_len {doc['doc_len']} != sum(tf) {computed}",
                         doc_id=doc_id, field="doc_len")
        doc["doc_len"] = computed
    docs.sort(key=lambda d: (d["doc_lang"], d["doc_id"]))
    return docs


def load_queries(path):
    queries = _read_jsonl(path, "queries")
    for query in queries:
        qid = query.get("query_id")
        if not qid:
            raise XError(E_CONFIG_INVALID, "query missing query_id",
                         field="query_id")
        if not query.get("query_lang"):
            raise XError(E_CONFIG_INVALID, f"query {qid} missing query_lang",
                         query_id=qid, field="query_lang")
        top_k = query.get("top_k")
        if not isinstance(top_k, int) or top_k <= 0:
            raise XError(E_CONFIG_INVALID,
                         f"query {qid} has non-positive top_k {top_k!r}",
                         query_id=qid, field="top_k")
        _validate_terms(query.get("terms"), qid, "query_id")
    return queries
