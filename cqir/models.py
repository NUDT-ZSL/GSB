from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .errors import fail
from .util import canonical_json, q6, sha256_text


@dataclass(frozen=True)
class Paragraph:
    role: str
    text: str

    def to_dict(self) -> dict[str, str]:
        return {"role": self.role, "text": self.text}

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "Paragraph":
        if not isinstance(value, dict) or set(value) != {"role", "text"}:
            fail("E_INPUT", "paragraph must contain only role and text", {"value": value})
        return cls(role=str(value["role"]), text=str(value["text"]))


@dataclass(frozen=True)
class Document:
    doc_id: str
    category: str
    popularity: float
    terms: dict[str, int]
    doc_len: int
    doc_version: int
    paragraphs: tuple[Paragraph, ...]

    @property
    def text(self) -> str:
        return "\n\n".join(paragraph.text for paragraph in self.paragraphs)

    @property
    def digest(self) -> str:
        return sha256_text(canonical_json(self.to_dict()))

    @property
    def paragraph_spans(self) -> tuple[tuple[int, int, str], ...]:
        spans: list[tuple[int, int, str]] = []
        cursor = 0
        for paragraph in self.paragraphs:
            spans.append((cursor, cursor + len(paragraph.text), paragraph.role))
            cursor += len(paragraph.text) + 2
        return tuple(spans)

    def role_for_span(self, start: int, end: int) -> str:
        for span_start, span_end, role in self.paragraph_spans:
            if start >= span_start and end <= span_end:
                return role
        return "body"

    def to_dict(self) -> dict[str, Any]:
        return {
            "doc_id": self.doc_id,
            "category": self.category,
            "popularity": q6(self.popularity),
            "terms": dict(sorted(self.terms.items())),
            "doc_len": self.doc_len,
            "doc_version": self.doc_version,
            "paragraphs": [paragraph.to_dict() for paragraph in self.paragraphs],
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "Document":
        required = {
            "doc_id",
            "category",
            "popularity",
            "terms",
            "doc_len",
            "doc_version",
            "paragraphs",
        }
        missing = required - set(value)
        if missing:
            fail("E_INPUT", "document missing required fields", {"missing": sorted(missing)})
        return cls(
            doc_id=str(value["doc_id"]),
            category=str(value["category"]),
            popularity=q6(float(value["popularity"])),
            terms={str(key): int(count) for key, count in value["terms"].items()},
            doc_len=int(value["doc_len"]),
            doc_version=int(value["doc_version"]),
            paragraphs=tuple(Paragraph.from_dict(item) for item in value["paragraphs"]),
        )


@dataclass(frozen=True)
class QueryTurn:
    conv_id: str
    turn_no: int
    query_text: str
    locale: str
    session_hint: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "conv_id": self.conv_id,
            "turn_no": self.turn_no,
            "query_text": self.query_text,
            "locale": self.locale,
            "session_hint": self.session_hint,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "QueryTurn":
        required = {"conv_id", "turn_no", "query_text", "locale", "session_hint"}
        missing = required - set(value)
        if missing:
            fail("E_INPUT", "query missing required fields", {"missing": sorted(missing)})
        return cls(
            conv_id=str(value["conv_id"]),
            turn_no=int(value["turn_no"]),
            query_text=str(value["query_text"]),
            locale=str(value["locale"]),
            session_hint=str(value["session_hint"]),
        )


@dataclass(frozen=True)
class Snippet:
    doc_id: str
    start: int
    end: int
    text: str
    score_detail: dict[str, Any]

    @property
    def key(self) -> tuple[str, int, int]:
        return (self.doc_id, self.start, self.end)

    def to_dict(self, include_detail: bool = True) -> dict[str, Any]:
        data: dict[str, Any] = {
            "doc_id": self.doc_id,
            "start": self.start,
            "end": self.end,
            "text": self.text,
        }
        if include_detail:
            data["score_detail"] = self.score_detail
        return data


@dataclass(frozen=True)
class Citation:
    doc_id: str
    start: int
    end: int
    score: float
    confidence_low: float | None = None
    confidence_high: float | None = None
    paragraph_role: str | None = None
    order_weight: float | None = None
    revision: int | None = None
    context_refs: tuple[str, ...] | None = None

    @property
    def key(self) -> tuple[str, int, int]:
        return (self.doc_id, self.start, self.end)

    def to_dict(self) -> dict[str, Any]:
        return {
            "doc_id": self.doc_id,
            "start": self.start,
            "end": self.end,
            "score": q6(self.score),
            "confidence_low": self.confidence_low,
            "confidence_high": self.confidence_high,
            "paragraph_role": self.paragraph_role,
            "order_weight": self.order_weight,
            "revision": self.revision,
            "context_refs": list(self.context_refs) if self.context_refs is not None else None,
        }


@dataclass(frozen=True)
class AnswerPackage:
    conv_id: str
    turn_no: int
    answer_text: str
    doc_versions: dict[str, int]
    context_chain: tuple[str, ...]
    snippets: tuple[Snippet, ...]
    citations: tuple[Citation, ...]
    format_version: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def answer_sha256(self) -> str:
        return sha256_text(self.answer_text)

    @property
    def canonical_hash(self) -> str:
        return sha256_text(canonical_json(self.semantic_dict()))

    def semantic_dict(self) -> dict[str, Any]:
        return {
            "conv_id": self.conv_id,
            "turn_no": self.turn_no,
            "answer_text": self.answer_text,
            "answer_sha256": self.answer_sha256,
            "doc_versions": dict(sorted(self.doc_versions.items())),
            "context_chain": list(self.context_chain),
            "snippets": [
                {
                    "doc_id": snippet.doc_id,
                    "start": snippet.start,
                    "end": snippet.end,
                    "text": snippet.text,
                    "score": q6(snippet.score_detail.get("final_score", 0.0)),
                }
                for snippet in self.snippets
            ],
            "citations": [citation.to_dict() for citation in self.citations],
        }

    def validate(self, documents: dict[str, Document] | None = None) -> None:
        if len(self.snippets) != len(self.citations):
            fail(
                "E_INCONSISTENT",
                "snippet and citation counts differ",
                {"snippets": len(self.snippets), "citations": len(self.citations)},
            )
        seen: dict[tuple[str, int, int], int] = {}
        for index, citation in enumerate(self.citations):
            if citation.start < 0 or citation.end <= citation.start:
                fail(
                    "E_SPAN_OUT_OF_RANGE",
                    "citation span is empty or has a negative offset",
                    {
                        "doc_id": citation.doc_id,
                        "start": citation.start,
                        "end": citation.end,
                        "citation_index": index,
                    },
                )
            if documents is not None:
                document = documents.get(citation.doc_id)
                if document is None:
                    fail(
                        "E_SPAN_OUT_OF_RANGE",
                        "citation document is unavailable",
                        {"doc_id": citation.doc_id, "citation_index": index},
                    )
                if citation.end > len(document.text):
                    fail(
                        "E_SPAN_OUT_OF_RANGE",
                        "citation exceeds document codepoint length",
                        {
                            "doc_id": citation.doc_id,
                            "start": citation.start,
                            "end": citation.end,
                            "doc_codepoint_len": len(document.text),
                            "citation_index": index,
                        },
                    )
            if citation.key in seen:
                fail(
                    "E_CITE_DUP",
                    "duplicate citation for the same document span",
                    {
                        "doc_id": citation.doc_id,
                        "start": citation.start,
                        "end": citation.end,
                        "first_index": seen[citation.key],
                        "duplicate_index": index,
                    },
                )
            seen[citation.key] = index

    def to_storage_dict(self) -> dict[str, Any]:
        return {
            "conv_id": self.conv_id,
            "turn_no": self.turn_no,
            "answer_text": self.answer_text,
            "doc_versions": dict(sorted(self.doc_versions.items())),
            "context_chain": list(self.context_chain),
            "snippets": [snippet.to_dict(include_detail=True) for snippet in self.snippets],
            "citations": [citation.to_dict() for citation in self.citations],
            "format_version": self.format_version,
            "metadata": self.metadata,
        }

    @classmethod
    def from_storage_dict(cls, value: dict[str, Any]) -> "AnswerPackage":
        snippets = tuple(
            Snippet(
                doc_id=str(item["doc_id"]),
                start=int(item["start"]),
                end=int(item["end"]),
                text=str(item["text"]),
                score_detail=item.get(
                    "score_detail",
                    {
                        "doc_id": str(item["doc_id"]),
                        "start": int(item["start"]),
                        "end": int(item["end"]),
                        "weights": {"confidence": 0.0, "coverage": 0.0},
                        "confidence": 0.0,
                        "coverage": 0.0,
                        "final_score": 0.0,
                    },
                ),
            )
            for item in value["snippets"]
        )
        citations = tuple(
            Citation(
                doc_id=str(item["doc_id"]),
                start=int(item["start"]),
                end=int(item["end"]),
                score=q6(float(item["score"])),
                confidence_low=(
                    q6(float(item["confidence_low"]))
                    if item.get("confidence_low") is not None
                    else None
                ),
                confidence_high=(
                    q6(float(item["confidence_high"]))
                    if item.get("confidence_high") is not None
                    else None
                ),
                paragraph_role=item.get("paragraph_role"),
                order_weight=(
                    q6(float(item["order_weight"]))
                    if item.get("order_weight") is not None
                    else None
                ),
                revision=(
                    int(item["revision"])
                    if item.get("revision") is not None
                    else None
                ),
                context_refs=(
                    tuple(str(ref) for ref in item["context_refs"])
                    if item.get("context_refs") is not None
                    else None
                ),
            )
            for item in value["citations"]
        )
        return cls(
            conv_id=str(value["conv_id"]),
            turn_no=int(value["turn_no"]),
            answer_text=str(value["answer_text"]),
            doc_versions={
                str(doc_id): int(version)
                for doc_id, version in value["doc_versions"].items()
            },
            context_chain=tuple(str(ref) for ref in value["context_chain"]),
            snippets=snippets,
            citations=citations,
            format_version=value.get("format_version"),
            metadata=dict(value.get("metadata", {})),
        )

