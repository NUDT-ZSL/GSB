from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .cache import ContentAddressedCache
from .config import PipelineConfig
from .models import AnswerPackage, Citation, Document, QueryTurn, Snippet
from .retrieval import ROUTE_ORDER, merge_recall, score_document_route
from .snippets import extract_snippets, select_snippets
from .text import normalize_query
from .util import canonical_json, q6, sha256_text


@dataclass
class PipelineResult:
    package: AnswerPackage
    normalized: dict[str, Any]
    recall: dict[str, Any]
    snippet_candidates: dict[str, list[dict[str, Any]]]
    selected: list[dict[str, Any]]
    stage_report: dict[str, Any]
    stage_sets: dict[str, Any]

    @property
    def canonical_answer(self) -> str:
        return canonical_json(self.package.semantic_dict())

    @property
    def byte_hash(self) -> str:
        return sha256_text(self.canonical_answer)

    def to_dict(self) -> dict[str, Any]:
        return {
            "package": self.package.semantic_dict(),
            "normalized": self.normalized,
            "recall": self.recall,
            "snippet_candidates": self.snippet_candidates,
            "selected": self.selected,
            "stage_report": self.stage_report,
            "stage_sets": self.stage_sets,
            "byte_hash": self.byte_hash,
        }


class Pipeline:
    def __init__(
        self,
        config: PipelineConfig | None = None,
        cache_root: Path | None = None,
    ) -> None:
        self.config = config or PipelineConfig()
        self.cache = ContentAddressedCache(cache_root or Path(".cqir") / "cache")

    def _cached(
        self,
        key: str,
        factory: Any,
        force_recompute: bool,
        stage: str,
        report: dict[str, Any],
    ) -> Any:
        if not force_recompute:
            existing = self.cache.get(key)
            if existing is not None:
                report["reused_stages"].append(stage)
                return existing
        value = factory()
        self.cache.put(key, value)
        report["recomputed_stages"].append(stage)
        return value

    def run(
        self,
        query: QueryTurn,
        queries: Iterable[QueryTurn],
        documents: dict[str, Document],
        force_recompute: bool = False,
    ) -> PipelineResult:
        report: dict[str, Any] = {
            "recomputed_stages": [],
            "reused_stages": [],
            "recomputed_docs": [],
            "reused_docs": [],
            "removed_docs": [],
        }
        query_list = sorted(
            queries,
            key=lambda item: (item.conv_id, item.turn_no, item.query_text),
        )
        normalized = normalize_query(query, query_list, self.config)
        normalize_key = self.cache.key(
            "normalize",
            query.to_dict(),
            [item.to_dict() for item in query_list],
            self.config.to_dict(),
        )
        normalized = self._cached(
            normalize_key,
            lambda: normalized,
            force_recompute,
            "normalize",
            report,
        )

        route_scores: dict[str, dict[str, float]] = {}
        doc_artifact_report: dict[str, str] = {}
        for route in ROUTE_ORDER:
            route_scores[route] = {}
            for doc_id, document in sorted(documents.items()):
                key = self.cache.key(
                    "route_score",
                    route,
                    normalized,
                    document.digest,
                    self.config.to_dict(),
                )
                stage = f"route:{route}:{doc_id}"
                score = self._cached(
                    key,
                    lambda route=route, document=document: score_document_route(
                        route,
                        document,
                        normalized,
                        self.config,
                    ),
                    force_recompute,
                    stage,
                    report,
                )
                route_scores[route][doc_id] = float(score)
                if stage in report["reused_stages"]:
                    report["reused_docs"].append(doc_id)
                    doc_artifact_report[stage] = "reused"
                else:
                    report["recomputed_docs"].append(doc_id)
                    doc_artifact_report[stage] = "recomputed"

        recall_key = self.cache.key(
            "recall_merge",
            normalized,
            route_scores,
            {doc_id: document.doc_version for doc_id, document in documents.items()},
            self.config.to_dict(),
        )
        recall_result = self._cached(
            recall_key,
            lambda: merge_recall(route_scores, documents, normalized, self.config),
            force_recompute,
            "recall_merge",
            report,
        )

        snippet_candidates: dict[str, list[dict[str, Any]]] = {}
        for doc_id in recall_result["top_doc_ids"]:
            document = documents[doc_id]
            snippet_key = self.cache.key(
                "snippet_candidates",
                normalized,
                document.digest,
                self.config.to_dict(),
            )
            snippet_candidates[doc_id] = self._cached(
                snippet_key,
                lambda document=document: extract_snippets(
                    document,
                    normalized,
                    self.config,
                ),
                force_recompute,
                f"snippets:{doc_id}",
                report,
            )
            if f"snippets:{doc_id}" in report["reused_stages"]:
                report["reused_docs"].append(doc_id)
            else:
                report["recomputed_docs"].append(doc_id)

        selection_key = self.cache.key(
            "snippet_selection",
            recall_result["top_doc_ids"],
            {doc_id: snippet_candidates[doc_id] for doc_id in recall_result["top_doc_ids"]},
            self.config.to_dict(),
        )
        selected = self._cached(
            selection_key,
            lambda: select_snippets(
                recall_result,
                snippet_candidates,
                documents,
                self.config,
            ),
            force_recompute,
            "snippet_selection",
            report,
        )

        assembly_key = self.cache.key(
            "answer_assembly",
            normalized,
            selected,
            {item["doc_id"]: documents[item["doc_id"]].doc_version for item in selected},
            self.config.to_dict(),
        )
        package_payload = self._cached(
            assembly_key,
            lambda: self._assemble_payload(normalized, selected, documents),
            force_recompute,
            "answer_assembly",
            report,
        )
        package = AnswerPackage.from_storage_dict(package_payload)
        package.validate(documents)

        report["recomputed_stages"] = sorted(set(report["recomputed_stages"]))
        report["reused_stages"] = sorted(set(report["reused_stages"]))
        report["recomputed_docs"] = sorted(set(report["recomputed_docs"]))
        report["reused_docs"] = sorted(
            set(report["reused_docs"]) - set(report["recomputed_docs"])
        )
        report["doc_artifacts"] = dict(sorted(doc_artifact_report.items()))
        report["cache_root"] = str(self.cache.root)
        stage_sets = self._stage_sets(normalized, recall_result, selected, documents)
        return PipelineResult(
            package=package,
            normalized=normalized,
            recall=recall_result,
            snippet_candidates=snippet_candidates,
            selected=selected,
            stage_report=report,
            stage_sets=stage_sets,
        )

    def _assemble_payload(
        self,
        normalized: dict[str, Any],
        selected: list[dict[str, Any]],
        documents: dict[str, Document],
    ) -> dict[str, Any]:
        snippets: list[Snippet] = []
        citations: list[Citation] = []
        for rank, item in enumerate(selected):
            document = documents[item["doc_id"]]
            score = q6(float(item["score_detail"]["final_score"]))
            snippets.append(
                Snippet(
                    doc_id=item["doc_id"],
                    start=int(item["start"]),
                    end=int(item["end"]),
                    text=str(item["text"]),
                    score_detail=item["score_detail"],
                )
            )
            citations.append(
                Citation(
                    doc_id=item["doc_id"],
                    start=int(item["start"]),
                    end=int(item["end"]),
                    score=score,
                    confidence_low=q6(max(0.0, score - self.config.confidence_half_width)),
                    confidence_high=q6(min(1.0, score + self.config.confidence_half_width)),
                    paragraph_role=document.role_for_span(int(item["start"]), int(item["end"])),
                    order_weight=q6(1.0 / (rank + 1)),
                    revision=1,
                    context_refs=tuple(str(ref) for ref in normalized["context_chain"]),
                )
            )
        answer_text = self._answer_text(selected)
        doc_versions = {
            citation.doc_id: documents[citation.doc_id].doc_version
            for citation in citations
        }
        return AnswerPackage(
            conv_id=str(normalized["conv_id"]),
            turn_no=int(normalized["turn_no"]),
            answer_text=answer_text,
            doc_versions=dict(sorted(doc_versions.items())),
            context_chain=tuple(str(ref) for ref in normalized["context_chain"]),
            snippets=tuple(snippets),
            citations=tuple(citations),
        ).to_storage_dict()

    @staticmethod
    def _answer_text(selected: list[dict[str, Any]]) -> str:
        if not selected:
            return "未找到满足条件的本地文档片段。"
        parts = [
            f"[{rank + 1}] {item['text']}"
            for rank, item in enumerate(selected)
        ]
        return "本地抽取式回答：" + " ".join(parts)

    @staticmethod
    def _stage_sets(
        normalized: dict[str, Any],
        recall_result: dict[str, Any],
        selected: list[dict[str, Any]],
        documents: dict[str, Document],
    ) -> dict[str, Any]:
        return {
            "normalized": {
                "conv_id": normalized["conv_id"],
                "turn_no": normalized["turn_no"],
                "doc_ids": [],
            },
            "recall": {
                "conv_id": normalized["conv_id"],
                "turn_no": normalized["turn_no"],
                "doc_ids": list(recall_result["top_doc_ids"]),
            },
            "selection": {
                "conv_id": normalized["conv_id"],
                "turn_no": normalized["turn_no"],
                "doc_ids": sorted({item["doc_id"] for item in selected}),
            },
            "assembly": {
                "conv_id": normalized["conv_id"],
                "turn_no": normalized["turn_no"],
                "doc_ids": sorted(
                    {
                        document_id
                        for document_id in documents
                        if any(item["doc_id"] == document_id for item in selected)
                    }
                ),
            },
        }


class IncrementalEngine:
    def __init__(self, pipeline: Pipeline) -> None:
        self.pipeline = pipeline

    def run(
        self,
        query: QueryTurn,
        queries: Iterable[QueryTurn],
        documents: dict[str, Document],
        previous_result: PipelineResult | None = None,
        changed_doc_ids: Iterable[str] = (),
        deleted_doc_ids: Iterable[str] = (),
        force_recompute: bool = False,
    ) -> dict[str, Any]:
        changed = sorted(set(changed_doc_ids))
        deleted = sorted(set(deleted_doc_ids))
        result = self.pipeline.run(
            query,
            queries,
            documents,
            force_recompute=force_recompute,
        )
        previous_citations = (
            list(previous_result.package.citations)
            if previous_result is not None
            else []
        )
        affected_citations = [
            citation.to_dict()
            for citation in previous_citations
            if citation.doc_id in set(changed) | set(deleted)
        ]
        unaffected_citations = [
            citation.to_dict()
            for citation in previous_citations
            if citation.doc_id not in set(changed) | set(deleted)
        ]
        report = dict(result.stage_report)
        report["removed_docs"] = deleted
        report["changed_docs"] = changed
        report["affected_citations"] = affected_citations
        report["unaffected_citations"] = unaffected_citations
        report["recompute_set"] = {
            "stages": report["recomputed_stages"],
            "doc_ids": report["recomputed_docs"],
            "citation_keys": [
                [item["doc_id"], item["start"], item["end"]]
                for item in affected_citations
            ],
        }
        report["reuse_set"] = {
            "stages": report["reused_stages"],
            "doc_ids": report["reused_docs"],
            "citation_keys": [
                [item["doc_id"], item["start"], item["end"]]
                for item in unaffected_citations
            ],
        }
        return {"result": result, "report": report}
