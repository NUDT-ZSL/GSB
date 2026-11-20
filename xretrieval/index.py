"""Partitioned multilingual inverted index construction."""

from .canon import checksum
from .errors import E_RESOURCE_LIMIT, XError


def build_partition(lang, docs, partition_version, bridge_resource_version,
                    max_postings):
    """Build the canonical partition snapshot content for one language.

    ``docs`` must contain exactly the documents of this language. Postings
    are sorted by doc_id ascending; terms are stored under canonical
    (sorted-key) serialization, so the resulting snapshot is byte
    reproducible for identical input.
    """
    docs = sorted(docs, key=lambda d: d["doc_id"])
    inverted = {}
    doc_lens = {}
    total_postings = 0
    for doc in docs:
        doc_id = doc["doc_id"]
        doc_lens[doc_id] = doc["doc_len"]
        for item in sorted(doc["terms"], key=lambda t: t["term"]):
            posting = inverted.setdefault(item["term"], [])
            posting.append([doc_id, item.get("tf", 1)])
            total_postings += 1
    if total_postings > max_postings:
        raise XError(E_RESOURCE_LIMIT,
                     f"partition {lang!r} has {total_postings} postings, "
                     f"limit is {max_postings}",
                     partition=lang, limit="max_postings_per_partition",
                     value=total_postings)
    for term in inverted:
        inverted[term].sort(key=lambda p: p[0])
    doc_count = len(docs)
    avg_doc_len = (sum(doc_lens[d] for d in sorted(doc_lens)) / doc_count
                   if doc_count else 0.0)
    content = {
        "snapshot": "partition",
        "lang": lang,
        "partition_version": partition_version,
        "bridge_resource_version": bridge_resource_version,
        "doc_count": doc_count,
        "total_postings": total_postings,
        "avg_doc_len": avg_doc_len,
        "doc_lens": doc_lens,
        "postings": inverted,
    }
    content["checksum"] = checksum(content)
    return content


def verify_partition_content(content):
    """Recompute the checksum of a partition snapshot content."""
    body = {k: v for k, v in content.items() if k != "checksum"}
    return checksum(body) == content.get("checksum")
