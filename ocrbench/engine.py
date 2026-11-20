"""Cache-aware orchestration of the deterministic OCR graph.

The engine wires the pure pipeline nodes to the content cache and template
store, builds fully-specified composite keys, records reuse vs recompute
status, and provides both whole-page and chunked execution paths that share
identical normalization, dedup and confidence conventions.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence

from . import pipeline
from .cache import ContentCache
from .config import PipelineConfig
from .deterministic import content_hash
from .errors import ErrorCode, make_error
from .graph import ExecutionPlan
from .resource import check_line_count, evaluate_residency
from .snapshot import TemplateSnapshotStore
from .samplegen import LineTruth, PageSample
from .templates import GLYPH_H, GLYPH_W, build_library


@dataclass
class NodeExecution:
    node: str
    key: str
    content_hash: str
    reused: bool
    key_details: Dict[str, object]


@dataclass
class PageRunResult:
    page_id: str
    executions: List[NodeExecution]
    payloads: Dict[str, Dict[str, Any]]
    degraded: bool
    degradation_category: str

    def reused_nodes(self) -> List[str]:
        return [execution.node for execution in self.executions if execution.reused]

    def recomputed_nodes(self) -> List[str]:
        return [execution.node for execution in self.executions if not execution.reused]


class OcrEngine:
    """Runs the DAG for pages with full cache and snapshot integration."""

    def __init__(
        self,
        cache: ContentCache,
        snapshot_store: TemplateSnapshotStore,
        config: PipelineConfig,
    ) -> None:
        self.cache = cache
        self.snapshot_store = snapshot_store
        self.config = config
        self.plan = ExecutionPlan(config)

    # ------------------------------------------------------------- library
    def ensure_library(self, charset_version: int):
        """Return the template library, replacing the snapshot on version bump."""

        library = build_library(charset_version)
        return self.snapshot_store.replace(library)

    # -------------------------------------------------------------- runner
    def run_page(
        self,
        page: PageSample,
        truth_lines: Optional[Sequence[LineTruth]] = None,
        *,
        allowed_nodes: Optional[Sequence[str]] = None,
    ) -> PageRunResult:
        """Execute the DAG for one page, reusing cache wherever keys match."""

        truth_lines = list(page.lines if truth_lines is None else truth_lines)
        check_line_count(len(truth_lines), self.config.resource_limits, page.page_id)
        library = self.ensure_library(self.config.charset_version)
        pixel_hash = page.pixel_hash()
        # Pixel-side config identity deliberately excludes template version so
        # a library bump does not invalidate preprocess/detect/features.
        config_hash = self.config.pipeline_hash()

        executions: List[NodeExecution] = []
        payloads: Dict[str, Dict[str, Any]] = {}
        hashes: Dict[str, str] = {}

        def upstream_hash(node: str) -> str:
            parents = self.plan.upstream_of(node)
            return content_hash([hashes[parent] for parent in parents])

        def commit(node: str, details: Dict[str, object], payload: Dict[str, Any]):
            key = self.cache.build_key(node, details)
            existing = self.cache.get(key)
            if existing is not None:
                hashes[node] = existing.content_hash
                payloads[node] = existing.payload
                executions.append(
                    NodeExecution(
                        node, key, existing.content_hash, True, dict(details)
                    )
                )
                return
            entry = self.cache.commit(node, details, payload)
            hashes[node] = entry.content_hash
            payloads[node] = payload
            executions.append(
                NodeExecution(node, key, entry.content_hash, False, dict(details))
            )

        # Resource residency decision (whole-page vs streaming degradation).
        max_cells = max((box[2] - box[0]) // (GLYPH_W + 1) for box in [
            line.bbox for line in truth_lines
        ])
        decision = evaluate_residency(
            len(truth_lines),
            max_cells,
            GLYPH_H * GLYPH_W,
            self.config.resource_limits,
        )

        # 1) preprocess
        pre_payload = pipeline.preprocess(page.image, self.config)
        pre_details = {
            "node": "preprocess",
            "page_id": page.page_id,
            "pixel_hash": pixel_hash,
            "config_hash": config_hash,
            "upstream_hash": content_hash([pixel_hash]),
            "detect_threshold": self.config.detect_threshold,
        }
        commit("preprocess", pre_details, pre_payload)

        # 2) detect
        detect_payload = pipeline.detect_lines(payloads["preprocess"], page.page_id)
        detect_details = {
            "node": "detect",
            "page_id": page.page_id,
            "pixel_hash": pixel_hash,
            "config_hash": config_hash,
            "upstream_hash": hashes["preprocess"],
            "detect_threshold": self.config.detect_threshold,
            "line_box_summary": detect_payload["line_box_summary"],
        }
        commit("detect", detect_details, detect_payload)

        # 3) features
        feature_payload = pipeline.extract_features(
            payloads["preprocess"], payloads["detect"], self.config
        )
        feature_details = {
            "node": "features",
            "page_id": page.page_id,
            "pixel_hash": pixel_hash,
            "config_hash": config_hash,
            "upstream_hash": hashes["detect"],
            "detect_threshold": self.config.detect_threshold,
            "line_box_summary": feature_payload["line_box_summary"],
        }
        commit("features", feature_details, feature_payload)

        # 4) match
        match_payload = pipeline.match_lines(
            payloads["features"], library, self.config, page.page_id
        )
        match_details = {
            "node": "match",
            "page_id": page.page_id,
            "pixel_hash": pixel_hash,
            "config_hash": config_hash,
            "upstream_hash": hashes["features"],
            "charset_version": library.version,
            "line_box_summary": match_payload["line_box_summary"],
        }
        commit("match", match_details, match_payload)

        # 5) recognize
        recognize_payload = pipeline.recognize_lines(
            payloads["match"], self.config
        )
        recognize_details = {
            "node": "recognize",
            "page_id": page.page_id,
            "pixel_hash": pixel_hash,
            "config_hash": config_hash,
            "upstream_hash": hashes["match"],
            "charset_version": library.version,
            "dedup_threshold": pipeline.quantize(
                self.config.dedup_threshold, self.config.confidence_scale
            ),
            "line_box_summary": recognize_payload["line_box_summary"],
        }
        commit("recognize", recognize_details, recognize_payload)

        # 6) aggregate (depends on recognize, detect and the ground-truth hash)
        truth_hash = content_hash(
            [[line.line_index, line.text, list(line.bbox)] for line in truth_lines]
        )
        aggregate_payload = pipeline.aggregate_page(
            payloads["recognize"], truth_lines, self.config, page.page_id
        )
        aggregate_details = {
            "node": "aggregate",
            "page_id": page.page_id,
            "pixel_hash": pixel_hash,
            "config_hash": config_hash,
            "upstream_hash": content_hash(
                [hashes["recognize"], hashes["detect"], truth_hash]
            ),
            "charset_version": library.version,
            "truth_hash": truth_hash,
            "line_box_summary": aggregate_payload["line_box_summary"],
        }
        commit("aggregate", aggregate_details, aggregate_payload)

        if allowed_nodes is not None:
            executions = [
                execution
                for execution in executions
                if execution.node in set(allowed_nodes)
            ]

        return PageRunResult(
            page_id=page.page_id,
            executions=executions,
            payloads=payloads,
            degraded=decision.degraded,
            degradation_category=decision.degradation_category,
        )

    # ------------------------------------------------------------- chunking
    def run_page_chunked(
        self,
        page: PageSample,
        chunk_size: int,
        truth_lines: Optional[Sequence[LineTruth]] = None,
    ) -> Dict[str, Any]:
        """Recognize line ranges in chunks using identical page-level stats.

        Feature normalization statistics are computed once for the whole page
        and reused for every chunk, so chunk boundaries cannot alter values.
        The per-line recognition outputs are concatenated and compared
        byte-for-byte against the whole-page result.

        Raises ``E_CHUNK_MISMATCH`` with the first differing line/char.
        """

        truth_lines = list(page.lines if truth_lines is None else truth_lines)
        library = self.ensure_library(self.config.charset_version)
        whole = self.run_page(page, truth_lines)
        whole_recognized = whole.payloads["recognize"]

        # Page-level preprocessing/features are shared (identical statistics).
        pre_payload = pipeline.preprocess(page.image, self.config)
        detect_payload = pipeline.detect_lines(pre_payload, page.page_id)
        feature_payload = pipeline.extract_features(
            pre_payload, detect_payload, self.config
        )
        total_lines = len(feature_payload["lines"])
        if chunk_size <= 0:
            raise make_error(
                ErrorCode.RESOURCE_LIMIT,
                "chunk size must be positive",
                stage="chunk",
                chunk_size=chunk_size,
            )

        chunk_records: List[Dict[str, Any]] = []
        for start in range(0, total_lines, chunk_size):
            end = min(start + chunk_size, total_lines)
            chunk_features = {
                "lines": feature_payload["lines"][start:end],
                "norm_mean": feature_payload["norm_mean"],
                "norm_std": feature_payload["norm_std"],
                "line_box_summary": feature_payload["line_box_summary"],
            }
            matched = pipeline.match_lines(
                chunk_features, library, self.config, page.page_id
            )
            recognized = pipeline.recognize_lines(matched, self.config)
            # Re-index back to absolute line numbers while keeping order.
            for offset, record in enumerate(recognized["lines"]):
                record = dict(record)
                record["line_index"] = start + offset
                chunk_records.append(record)

        # Re-run the page-global dedup across the concatenated lines so the
        # dedup threshold and grouping semantics match the whole-page path.
        pipeline._apply_dedup(chunk_records, self.config)

        whole_records = whole_recognized["lines"]
        mismatch = _first_text_mismatch(whole_records, chunk_records)
        if mismatch is not None:
            line_index, char_index = mismatch
            raise make_error(
                ErrorCode.CHUNK_MISMATCH,
                "chunked and whole-page recognition differ",
                page_id=page.page_id,
                stage="chunk",
                first_bad_line=line_index,
                first_bad_char=char_index,
                whole_text=_safe_text(whole_records, line_index),
                chunk_text=_safe_text(chunk_records, line_index),
            )
        return {
            "page_id": page.page_id,
            "chunk_size": chunk_size,
            "line_count": total_lines,
            "records": chunk_records,
            "whole_records": whole_records,
            "identical": True,
        }


def _safe_text(records: Sequence[Dict[str, Any]], line_index: int) -> str:
    if 0 <= line_index < len(records):
        return str(records[line_index]["text"])
    return ""


def _first_text_mismatch(
    whole: Sequence[Dict[str, Any]], chunked: Sequence[Dict[str, Any]]
) -> Optional[tuple]:
    if len(whole) != len(chunked):
        return (min(len(whole), len(chunked)), 0)
    for line_index, (left, right) in enumerate(zip(whole, chunked)):
        if left["text"] != right["text"]:
            char_index = next(
                (
                    index
                    for index, (a, b) in enumerate(zip(left["text"], right["text"]))
                    if a != b
                ),
                min(len(left["text"]), len(right["text"])),
            )
            return (line_index, char_index)
    return None
